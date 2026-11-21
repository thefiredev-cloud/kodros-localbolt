"""Two local HTTP servers.

App server (default 127.0.0.1:8877): the UI and the JSON API.
Preview server (default 127.0.0.1:8878): serves generated projects only, from a different
origin than the UI, with a locked-down CSP. The UI embeds it in a sandboxed iframe.

Both bind 127.0.0.1, reject any Host header that is not 127.0.0.1:<port> or localhost:<port>
(DNS-rebinding guard), and load nothing from a CDN.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import llm
from . import store as st

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_BODY = 2 * 1024 * 1024

PREVIEW_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
    ".csv": "text/plain; charset=utf-8", ".xml": "application/xml; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}

# Opaque-origin sandboxes throw on localStorage/sessionStorage. Generated apps (todo lists,
# notes) use them constantly, so the preview gives them in-memory stand-ins. Injected into
# served HTML only; the stored files are untouched.
STORAGE_SHIM = (
    "<script>(function(){try{window.localStorage.getItem('_')}catch(e){var m=function(){var d={};"
    "return{getItem:function(k){return Object.prototype.hasOwnProperty.call(d,k)?d[k]:null},"
    "setItem:function(k,v){d[k]=String(v)},removeItem:function(k){delete d[k]},clear:function(){d={}},"
    "key:function(i){return Object.keys(d)[i]||null},get length(){return Object.keys(d).length}}};"
    "['localStorage','sessionStorage'].forEach(function(n){try{Object.defineProperty(window,n,"
    "{value:m(),configurable:true})}catch(_){}})}})();</script>"
)
_HEAD_RE = re.compile(rb"<head[^>]*>", re.I)
_HTML_RE = re.compile(rb"<html[^>]*>", re.I)
_DOCTYPE_RE = re.compile(rb"\A\s*<!doctype[^>]*>", re.I)


def inject_storage_shim(data: bytes) -> bytes:
    """Insert STORAGE_SHIM so it runs before app code without breaking the document.

    A script placed before <!doctype html> makes browsers ignore the doctype and render the
    page in quirks mode, so the shim goes after <head>, else after <html>, else after the
    doctype, and only a document with none of those gets it at the front.
    """
    shim = STORAGE_SHIM.encode()
    for rx in (_HEAD_RE, _HTML_RE, _DOCTYPE_RE):
        m = rx.search(data)
        if m:
            return data[: m.end()] + shim + data[m.end():]
    return shim + data


@dataclass
class Config:
    app_port: int = 8877
    preview_port: int = 8878
    # Port on which a reverse proxy or gateway publishes the preview server, for a UI reached through it
    # (a browser on another device cannot use 127.0.0.1:8878). 0 = no gateway.
    gateway_preview_port: int = field(default_factory=lambda: int(os.environ.get("LOCALBOLT_GATEWAY_PREVIEW_PORT", "0") or 0))
    host: str = "127.0.0.1"
    llm: llm.ModelConfig = field(default_factory=llm.ModelConfig)


def preview_csp(app_origins: list[str]) -> str:
    # connect-src 'self' and img-src without remote hosts mean a generated app cannot phone home.
    return "; ".join([
        "default-src 'none'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "media-src 'self' data: blob:",
        "connect-src 'self'",
        "form-action 'none'",
        "base-uri 'self'",
        "frame-src 'none'",
        "object-src 'none'",
        "worker-src 'none'",
        "sandbox allow-scripts allow-forms allow-modals",
        "frame-ancestors " + " ".join(app_origins),
    ])


def app_csp(preview_origin: str, gateway_preview_port: int = 0) -> str:
    frame_src = preview_origin if not gateway_preview_port else f"{preview_origin} http://*:{gateway_preview_port}"
    return "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        f"frame-src {frame_src}",
        "form-action 'none'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
    ])


class _Base(BaseHTTPRequestHandler):
    server_version = "LocalBolt"
    sys_version = ""
    allowed_hosts: frozenset[str] = frozenset()

    def log_message(self, fmt, *args):  # quiet
        pass

    def handle(self):  # a client that hangs up mid-response is not an error worth a traceback
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.allowed_hosts

    def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None, head_only: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _text(self, status: int, text: str) -> None:
        self._send(status, text.encode(), "text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# Preview origin
# ---------------------------------------------------------------------------
def make_preview_handler(store: st.Store, hosts: frozenset[str], app_origins: list[str]):
    csp = preview_csp(app_origins)
    ref_re = re.compile(r"^(head|v[0-9]{1,6})$")

    class Preview(_Base):
        allowed_hosts = hosts

        def _serve(self, head_only: bool) -> None:
            if not self._host_ok():
                return self._text(403, "forbidden host")
            raw = urlsplit(self.path).path
            parts = unquote(raw).split("/")[1:]
            if len(parts) < 3 or not ref_re.match(parts[1]):
                return self._text(404, "LocalBolt preview origin. Use /<project>/<head|vN>/<file>.")
            pid, ref = parts[0], parts[1]
            rel = "/".join(parts[2:])
            if rel == "" or rel.endswith("/"):
                rel += "index.html"
            try:
                path = st.normalize_path(rel)
                ver = None if ref == "head" else int(ref[1:])
                data = store.read_bytes(pid, path, ver)
            except st.StoreError:
                return self._text(404, "not found")
            ext = path[path.rfind("."):].lower()
            ctype = PREVIEW_TYPES.get(ext, "application/octet-stream")
            if ext in (".html", ".htm"):
                data = inject_storage_shim(data)
            self._send(200, data, ctype, {
                "Content-Security-Policy": csp,
                # The sandboxed iframe has an opaque origin, so it sends Origin: null.
                "Access-Control-Allow-Origin": "null",
                "Vary": "Origin",
                "Cross-Origin-Resource-Policy": "cross-origin",
            }, head_only)

        def do_GET(self):
            self._serve(False)

        def do_HEAD(self):
            self._serve(True)

        def _no(self):
            if not self._host_ok():
                return self._text(403, "forbidden host")
            self._send(405, b"read-only origin", "text/plain; charset=utf-8", {"Allow": "GET, HEAD"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no

    return Preview


# ---------------------------------------------------------------------------
# App origin
# ---------------------------------------------------------------------------
class App:
    def __init__(self, store: st.Store, cfg: Config):
        self.store = store
        self.cfg = cfg
        self._gen_locks: dict[str, threading.Lock] = {}
        self._mutex = threading.Lock()
        self._status: tuple[float, dict] | None = None

    def gen_lock(self, pid: str) -> threading.Lock:
        with self._mutex:
            return self._gen_locks.setdefault(pid, threading.Lock())

    def model_status(self, max_age: float = 5.0) -> dict:
        now = time.monotonic()
        if self._status and now - self._status[0] < max_age:
            return self._status[1]
        s = llm.model_status(self.cfg.llm)
        self._status = (now, s)
        return s


def make_app_handler(app: App, hosts: frozenset[str], origins: frozenset[str], preview_origin: str):
    csp = app_csp(preview_origin, app.cfg.gateway_preview_port)
    store = app.store
    r_project = re.compile(r"^/api/projects/(p-[0-9a-f]{8})(/[a-z/.]*)?$")

    class Handler(_Base):
        allowed_hosts = hosts

        # ---- plumbing --------------------------------------------------
        def _guard(self, mutating: bool) -> bool:
            if not self._host_ok():
                self._text(403, "forbidden host")
                return False
            if mutating:
                origin = self.headers.get("Origin")
                if origin is not None and origin not in origins:
                    self._text(403, "forbidden origin")
                    return False
                site = self.headers.get("Sec-Fetch-Site")
                if site is not None and site not in ("same-origin", "none"):
                    self._text(403, "forbidden cross-site request")
                    return False
            return True

        def _json(self, status: int, obj: object, headers: dict[str, str] | None = None) -> None:
            self._send(status, json.dumps(obj).encode(), "application/json; charset=utf-8", headers)

        def _err(self, e: st.StoreError) -> None:
            self._json(e.status, {"error": e.code, "message": e.message})

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise st.StoreError("bad_request", "bad Content-Length") from None
            if n > MAX_BODY:
                raise st.StoreError("too_large", "request body too large", 413)
            if n == 0:
                return {}
            try:
                obj = json.loads(self.rfile.read(n))
            except ValueError:
                raise st.StoreError("bad_request", "body is not valid JSON") from None
            if not isinstance(obj, dict):
                raise st.StoreError("bad_request", "body must be a JSON object")
            return obj

        @staticmethod
        def _ver(q: dict) -> int | None:
            v = q.get("v", [None])[0]
            if v in (None, "", "head"):
                return None
            try:
                return int(v)
            except ValueError:
                raise st.StoreError("bad_request", "v must be an integer") from None

        @staticmethod
        def _int_or_none(v: object) -> int | None:
            if v is None:
                return None
            if isinstance(v, bool) or not isinstance(v, int):
                raise st.StoreError("bad_request", "version numbers must be integers")
            return v

        # ---- verbs -----------------------------------------------------
        def do_GET(self):
            self._route("GET", False)

        def do_HEAD(self):
            self._route("GET", True)

        def do_POST(self):
            self._route("POST", False)

        def do_PUT(self):
            self._route("PUT", False)

        def do_DELETE(self):
            self._route("DELETE", False)

        def do_OPTIONS(self):
            if self._host_ok():
                self._send(405, b"", "text/plain; charset=utf-8")
            else:
                self._text(403, "forbidden host")

        def do_PATCH(self):
            self.do_OPTIONS()

        def _route(self, method: str, head_only: bool) -> None:
            if not self._guard(method != "GET"):
                return
            url = urlsplit(self.path)
            path, q = url.path, parse_qs(url.query)
            try:
                if method == "GET" and path in STATIC_FILES:
                    name, ctype = STATIC_FILES[path]
                    return self._send(200, (STATIC_DIR / name).read_bytes(), ctype, {"Content-Security-Policy": csp}, head_only)
                if method == "GET" and path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                if path == "/api/status" and method == "GET":
                    return self._json(200, {
                        "model": app.model_status(),
                        "preview_origin": preview_origin,
                        "app_port": app.cfg.app_port,
                        "gateway_preview_port": app.cfg.gateway_preview_port,
                        "limits": {"file_bytes": st.MAX_FILE_BYTES, "files": st.MAX_FILES},
                        "templates": sorted(st.TEMPLATES),
                    })
                if path == "/api/projects":
                    if method == "GET":
                        return self._json(200, {"projects": store.list_projects()})
                    if method == "POST":
                        b = self._body()
                        tpl = b.get("template", "starter")
                        return self._json(201, store.create_project(str(b.get("name") or ""), tpl))
                m = r_project.match(path)
                if m:
                    return self._project(method, m.group(1), m.group(2) or "", q, head_only)
                self._json(404, {"error": "not_found", "message": "no such route"})
            except st.StoreError as e:
                self._err(e)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _project(self, method: str, pid: str, sub: str, q: dict, head_only: bool) -> None:
            if sub == "" and method == "GET":
                p = store.get_project(pid)
                return self._json(200, {
                    "project": p,
                    "files": store.file_list(pid),
                    "versions": store.versions(pid),
                    "chat": store.chat(pid),
                    "preview": f"{preview_origin}/{pid}",
                })
            if sub == "/versions" and method == "GET":
                return self._json(200, {"versions": store.versions(pid)})
            if sub == "/files" and method == "GET":
                return self._json(200, {"files": store.file_list(pid, self._ver(q))})
            if sub == "/file":
                if method == "GET":
                    path = (q.get("path") or [""])[0]
                    v = self._ver(q)
                    norm = st.normalize_path(path)
                    return self._json(200, {"path": norm, "content": store.read_file(pid, norm, v), "version": store.get_project(pid)["head"] if v is None else v})
                if method == "PUT":
                    b = self._body()
                    if not isinstance(b.get("content"), str):
                        raise st.StoreError("bad_request", "content must be a string")
                    path = st.normalize_path(b.get("path"))
                    verb = "Edit" if path in store.files(pid) else "Create"
                    res = store.write_files(pid, {path: b["content"]}, label=f"{verb} {path}", kind="edit", base=self._int_or_none(b.get("base")))
                    return self._json(200, res)
                if method == "DELETE":
                    path = (q.get("path") or [""])[0]
                    base = q.get("base", [None])[0]
                    return self._json(200, store.delete_file(pid, path, int(base) if base and base.isdigit() else None))
            if sub == "/file/rename" and method == "POST":
                b = self._body()
                return self._json(200, store.rename_file(pid, b.get("from"), b.get("to")))
            if sub == "/restore" and method == "POST":
                v = self._int_or_none(self._body().get("version"))
                if v is None:
                    raise st.StoreError("bad_request", "version is required")
                return self._json(200, store.restore(pid, v))
            if sub == "/rename" and method == "POST":
                return self._json(200, store.rename_project(pid, str(self._body().get("name") or "")))
            if sub == "/archive" and method == "POST":
                return self._json(200, {"archived": store.archive_project(pid)})
            if sub == "/download.zip" and method == "GET":
                name, data = store.zip_bytes(pid, self._ver(q))
                return self._send(200, data, "application/zip", {"Content-Disposition": f'attachment; filename="{name}"'}, head_only)
            if sub == "/generate" and method == "POST":
                return self._generate(pid)
            raise st.StoreError("not_found", "no such route", 404)

        def _generate(self, pid: str) -> None:
            store.get_project(pid)  # 404 early
            body = self._body()
            prompt = body.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise st.StoreError("bad_prompt", "prompt is required")
            lock = app.gen_lock(pid)
            if not lock.acquire(blocking=False):
                raise st.StoreError("busy", "a generation is already running for this project", 409)
            try:
                status = app.model_status(max_age=0)
                if not status["available"]:
                    # Clear 'model unavailable' state. Nothing is invented; the editor keeps working.
                    return self._json(503, {"error": "model_unavailable", "message": "The local model is not answering. You can still edit files by hand.", "model": status})
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Connection", "close")
                self.end_headers()

                def emit(ev: dict) -> None:
                    self.wfile.write(json.dumps(ev).encode() + b"\n")
                    self.wfile.flush()

                try:
                    think = body.get("think")
                    llm.generate(store, pid, prompt, app.cfg.llm, emit, think=think if isinstance(think, bool) else None)
                except llm.ModelUnavailable as e:
                    app._status = None
                    emit({"type": "error", "error": "model_unavailable", "message": f"The local model stopped answering ({e}). Nothing was saved."})
                except llm.ModelError as e:
                    emit({"type": "error", "error": "model_error", "message": f"{e}"})
                except st.StoreError as e:
                    emit({"type": "error", "error": e.code, "message": e.message})
                except (BrokenPipeError, ConnectionResetError):
                    store.append_chat(pid, "error", "generation cancelled by the browser; nothing was saved")
                except Exception as e:  # keep the stream well-formed for the UI
                    emit({"type": "error", "error": "internal", "message": f"{type(e).__name__}: {e}"})
            finally:
                lock.release()

    return Handler


# ---------------------------------------------------------------------------
class Servers:
    """Both servers bound and ready; run() blocks, start() runs them on threads."""

    def __init__(self, store: st.Store, cfg: Config | None = None):
        self.cfg = cfg or Config()
        self.store = store
        self.app = App(store, self.cfg)
        self._preview = ThreadingHTTPServer((self.cfg.host, self.cfg.preview_port), BaseHTTPRequestHandler)
        self._app = ThreadingHTTPServer((self.cfg.host, self.cfg.app_port), BaseHTTPRequestHandler)
        self.preview_port = self._preview.server_address[1]
        self.app_port = self._app.server_address[1]
        self.cfg.app_port = self.app_port  # the real port when 0 was asked for; /api/status reports it
        for s in (self._preview, self._app):
            s.daemon_threads = True
        h = self.cfg.host
        app_hosts = frozenset({f"{h}:{self.app_port}", f"localhost:{self.app_port}"})
        prev_hosts = frozenset({f"{h}:{self.preview_port}", f"localhost:{self.preview_port}"})
        app_origins = sorted(f"http://{x}" for x in app_hosts)
        self.preview_origin = f"http://{h}:{self.preview_port}"
        self._preview.RequestHandlerClass = make_preview_handler(store, prev_hosts, app_origins)
        self._app.RequestHandlerClass = make_app_handler(self.app, app_hosts, frozenset(app_origins), self.preview_origin)
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for s in (self._preview, self._app):
            t = threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            t.start()
            self._threads.append(t)

    def run(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        for s in (self._app, self._preview):
            s.shutdown()
            s.server_close()
