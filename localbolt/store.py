"""Project storage: content-addressed files, one immutable manifest per version.

Layout under the data root (mode 0700, default ~/.local/share/localbolt):

    projects/<id>/meta.json            name, slug, created, updated
    projects/<id>/versions/v0001.json  manifest: path -> {sha, size}, label, prompt...
    projects/<id>/blobs/<sha256>       file bytes
    projects/<id>/chat.jsonl           prompt / reply log
    archive/<id>-<stamp>/              archived projects (never deleted)

The head of a project is its highest version. Every change (generation, editor save,
rename, delete, restore) writes a new version; nothing is rewritten in place.
File paths are never used to touch the real filesystem: they are only dict keys in a
manifest, so path traversal cannot reach disk. They are still validated strictly.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 200
MAX_PROJECT_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 8
MAX_PATH_LEN = 160
MAX_NAME_LEN = 80

ALLOWED_EXT = frozenset(
    {".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg", ".txt", ".md", ".csv", ".xml", ".webmanifest"}
)
_SEG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_ID_RE = re.compile(r"^p-[0-9a-f]{8}$")

TEMPLATES: dict[str, dict[str, str]] = {
    "starter": {
        "index.html": (
            "<!doctype html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "  <title>New app</title>\n  <link rel=\"stylesheet\" href=\"style.css\">\n</head>\n<body>\n"
            "  <main>\n    <h1>New app</h1>\n    <p id=\"out\">Edit the files, then describe a change in the prompt box.</p>\n"
            "    <button id=\"hit\">Click me</button>\n  </main>\n  <script src=\"app.js\"></script>\n</body>\n</html>\n"
        ),
        "style.css": (
            "body { margin: 0; font: 16px/1.5 system-ui, sans-serif; background: #111418; color: #e8e6e1; }\n"
            "main { max-width: 640px; margin: 12vh auto; padding: 0 24px; }\n"
            "button { font: inherit; padding: 8px 16px; border-radius: 8px; border: 1px solid #3a4048;\n"
            "  background: #1b2026; color: inherit; cursor: pointer; }\n"
        ),
        "app.js": (
            "let clicks = 0;\n"
            "document.getElementById('hit').addEventListener('click', () => {\n"
            "  clicks += 1;\n  document.getElementById('out').textContent = 'Clicked ' + clicks + ' times.';\n});\n"
        ),
    },
    "empty": {"index.html": "<!doctype html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n  <title>Untitled</title>\n</head>\n<body>\n</body>\n</html>\n"},
}


class StoreError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_root() -> Path:
    env = os.environ.get("LOCALBOLT_HOME")
    return Path(env) if env else Path.home() / ".local" / "share" / "localbolt"


def normalize_path(path: object) -> str:
    """Return the canonical relative path or raise StoreError(bad_path)."""
    if not isinstance(path, str) or not path:
        raise StoreError("bad_path", "path is required")
    if len(path) > MAX_PATH_LEN:
        raise StoreError("bad_path", "path is too long")
    if "\\" in path or "\x00" in path or path.startswith("/") or path.endswith("/"):
        raise StoreError("bad_path", "path must be relative, with forward slashes, and name a file")
    parts = path.split("/")
    if len(parts) > MAX_DEPTH:
        raise StoreError("bad_path", f"path is deeper than {MAX_DEPTH} folders")
    for seg in parts:
        if seg in ("", ".", "..") or not _SEG_RE.match(seg):
            raise StoreError("bad_path", f"invalid path segment {seg!r}: use letters, digits, '_', '-', '.'")
    ext = os.path.splitext(parts[-1])[1].lower()
    if ext not in ALLOWED_EXT:
        raise StoreError("bad_type", f"file type {ext or '(none)'} is not supported; use one of {', '.join(sorted(ALLOWED_EXT))}")
    return path


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s or "app")[:40].strip("-") or "app"


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _json_bytes(obj: object) -> bytes:
    return (json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


class Store:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else default_root()
        for d in (self.root, self.root / "projects", self.root / "archive"):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self._locks: dict[str, threading.RLock] = {}
        self._mutex = threading.Lock()

    # ---- ids and paths -------------------------------------------------
    def _check_id(self, pid: object) -> str:
        if not isinstance(pid, str) or not _ID_RE.match(pid):
            raise StoreError("not_found", "no such project", 404)
        return pid

    def _dir(self, pid: str) -> Path:
        d = self.root / "projects" / self._check_id(pid)
        if not d.is_dir():
            raise StoreError("not_found", "no such project", 404)
        return d

    @contextlib.contextmanager
    def _lock(self, pid: str) -> Iterator[None]:
        d = self._dir(pid)
        with self._mutex:
            tl = self._locks.setdefault(pid, threading.RLock())
        with tl:
            with open(d / ".lock", "a+") as fh:
                os.chmod(d / ".lock", 0o600)
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    # ---- projects ------------------------------------------------------
    def create_project(self, name: str, template: str | None = "starter") -> dict:
        name = (name or "").strip()
        if not name:
            name = "Untitled app"
        if len(name) > MAX_NAME_LEN:
            name = name[:MAX_NAME_LEN].rstrip()
        if template is not None and template not in TEMPLATES:
            raise StoreError("bad_template", f"unknown template {template!r}")
        pid = "p-" + secrets.token_hex(4)
        d = self.root / "projects" / pid
        d.mkdir(mode=0o700)
        (d / "versions").mkdir(mode=0o700)
        (d / "blobs").mkdir(mode=0o700)
        meta = {"id": pid, "name": name, "slug": slugify(name), "created": now_iso(), "updated": now_iso()}
        _atomic_write(d / "meta.json", _json_bytes(meta))
        if template:
            self.write_files(pid, TEMPLATES[template], label=f"Template: {template}", kind="template")
        return self.get_project(pid)

    def get_project(self, pid: str) -> dict:
        d = self._dir(pid)
        meta = json.loads((d / "meta.json").read_text("utf-8"))
        meta["head"] = self._head(pid)
        return meta

    def list_projects(self) -> list[dict]:
        out = []
        for d in sorted((self.root / "projects").iterdir()):
            if d.is_dir() and _ID_RE.match(d.name):
                try:
                    out.append(self.get_project(d.name))
                except (OSError, ValueError, StoreError):
                    continue
        out.sort(key=lambda m: (m.get("updated", ""), m["id"]), reverse=True)
        return out

    def rename_project(self, pid: str, name: str) -> dict:
        name = (name or "").strip()
        if not name or len(name) > MAX_NAME_LEN:
            raise StoreError("bad_name", f"name must be 1-{MAX_NAME_LEN} characters")
        with self._lock(pid):
            d = self._dir(pid)
            meta = json.loads((d / "meta.json").read_text("utf-8"))
            meta.update(name=name, slug=slugify(name), updated=now_iso())
            _atomic_write(d / "meta.json", _json_bytes(meta))
        return self.get_project(pid)

    def archive_project(self, pid: str) -> str:
        """Move the project under archive/. Nothing is deleted."""
        with self._lock(pid):
            d = self._dir(pid)
            dest = self.root / "archive" / f"{pid}-{time.strftime('%Y%m%dT%H%M%S')}"
            os.rename(d, dest)
        return dest.name

    def _touch(self, pid: str) -> None:
        d = self._dir(pid)
        meta = json.loads((d / "meta.json").read_text("utf-8"))
        meta["updated"] = now_iso()
        _atomic_write(d / "meta.json", _json_bytes(meta))

    # ---- versions ------------------------------------------------------
    def _head(self, pid: str) -> int:
        vdir = self._dir(pid) / "versions"
        best = 0
        for p in vdir.glob("v*.json"):
            m = re.fullmatch(r"v(\d{4,})\.json", p.name)
            if m:
                best = max(best, int(m.group(1)))
        return best

    def _manifest(self, pid: str, v: int) -> dict:
        if not isinstance(v, int) or v < 1:
            raise StoreError("not_found", "no such version", 404)
        p = self._dir(pid) / "versions" / f"v{v:04d}.json"
        try:
            return json.loads(p.read_text("utf-8"))
        except FileNotFoundError:
            raise StoreError("not_found", f"no such version v{v}", 404) from None

    def _resolve(self, pid: str, v: int | None) -> int:
        head = self._head(pid)
        if v is None:
            return head
        if v < 1 or v > head:
            raise StoreError("not_found", f"no such version v{v}", 404)
        return v

    def versions(self, pid: str) -> list[dict]:
        out = []
        for n in range(1, self._head(pid) + 1):
            try:
                m = self._manifest(pid, n)
            except StoreError:
                continue
            out.append({
                "n": n,
                "created": m.get("created"),
                "label": m.get("label"),
                "kind": m.get("kind"),
                "prompt": m.get("prompt"),
                "summary": m.get("summary"),
                "changed": m.get("changed", []),
                "files": len(m.get("files", {})),
                "gen": m.get("gen"),
            })
        return out

    def files(self, pid: str, v: int | None = None) -> dict[str, dict]:
        n = self._resolve(pid, v)
        if n == 0:
            return {}
        return self._manifest(pid, n)["files"]

    def file_list(self, pid: str, v: int | None = None) -> list[dict]:
        return [{"path": p, "size": f["size"]} for p, f in sorted(self.files(pid, v).items())]

    def read_file(self, pid: str, path: str, v: int | None = None) -> str:
        return self.read_bytes(pid, path, v).decode("utf-8")

    def read_bytes(self, pid: str, path: str, v: int | None = None) -> bytes:
        files = self.files(pid, v)
        ent = files.get(path)
        if ent is None:
            raise StoreError("not_found", f"no such file {path}", 404)
        return (self._dir(pid) / "blobs" / ent["sha"]).read_bytes()

    def _commit(
        self,
        pid: str,
        build: Callable[[dict[str, dict], Callable[[str, str | bytes], dict]], list[str]],
        *,
        label: str,
        kind: str,
        prompt: str | None = None,
        summary: str | None = None,
        gen: dict | None = None,
    ) -> dict:
        """Apply `build` to a copy of the head file map under the project lock.

        `build(files, put)` mutates `files`, uses `put(path, content)` to store a blob and
        returns the list of changed paths. No changes means no new version.
        """
        with self._lock(pid):
            d = self._dir(pid)
            head = self._head(pid)
            files = dict(self._manifest(pid, head)["files"]) if head else {}

            def put(path: str, content: str | bytes) -> dict:
                if isinstance(content, str):
                    if "\x00" in content:
                        raise StoreError("bad_content", f"{path}: NUL bytes are not allowed")
                    data = content.encode("utf-8")
                else:
                    data = content
                    try:
                        data.decode("utf-8")
                    except UnicodeDecodeError:
                        raise StoreError("bad_content", f"{path}: only UTF-8 text files are supported") from None
                if len(data) > MAX_FILE_BYTES:
                    raise StoreError("too_large", f"{path}: larger than {MAX_FILE_BYTES // 1024} KiB", 413)
                sha = hashlib.sha256(data).hexdigest()
                blob = d / "blobs" / sha
                if not blob.exists():
                    _atomic_write(blob, data)
                return {"sha": sha, "size": len(data)}

            changed = build(files, put)
            if not changed:
                return {"unchanged": True, "version": head, "changed": []}
            if len(files) > MAX_FILES:
                raise StoreError("too_many_files", f"a project holds at most {MAX_FILES} files", 413)
            if sum(f["size"] for f in files.values()) > MAX_PROJECT_BYTES:
                raise StoreError("too_large", f"a project holds at most {MAX_PROJECT_BYTES // (1024 * 1024)} MiB", 413)
            n = head + 1
            manifest = {
                "n": n,
                "created": now_iso(),
                "label": label,
                "kind": kind,
                "prompt": prompt,
                "summary": summary,
                "changed": sorted(set(changed)),
                "files": files,
                "gen": gen,
            }
            _atomic_write(d / "versions" / f"v{n:04d}.json", _json_bytes(manifest))
            self._touch(pid)
            return {"unchanged": False, "version": n, "changed": manifest["changed"], "files": len(files)}

    def write_files(
        self,
        pid: str,
        writes: dict[str, str | bytes],
        *,
        deletes: Iterable[str] = (),
        label: str,
        kind: str = "edit",
        prompt: str | None = None,
        summary: str | None = None,
        base: int | None = None,
        gen: dict | None = None,
    ) -> dict:
        """Write and/or delete files as one new version.

        `base` is the version the caller last saw. If any written path changed since then
        the call fails with a 409 conflict rather than overwriting newer content.
        """
        norm_writes = {normalize_path(p): c for p, c in writes.items()}
        norm_deletes = [normalize_path(p) for p in deletes]

        def build(files: dict[str, dict], put: Callable) -> list[str]:
            if base is not None:
                old = self._manifest(pid, base)["files"] if base else {}
                for p in norm_writes:
                    if files.get(p, {}).get("sha") != old.get(p, {}).get("sha"):
                        raise StoreError("conflict", f"{p} changed since v{base}", 409)
            changed = []
            for p in norm_deletes:
                if p in files:
                    del files[p]
                    changed.append(p)
            for p, c in norm_writes.items():
                ent = put(p, c)
                if files.get(p, {}).get("sha") != ent["sha"]:
                    files[p] = ent
                    changed.append(p)
            return changed

        return self._commit(pid, build, label=label, kind=kind, prompt=prompt, summary=summary, gen=gen)

    def delete_file(self, pid: str, path: str, base: int | None = None) -> dict:
        path = normalize_path(path)

        def build(files: dict[str, dict], put: Callable) -> list[str]:
            if path not in files:
                raise StoreError("not_found", f"no such file {path}", 404)
            if base is not None:
                old = self._manifest(pid, base)["files"] if base else {}
                if files[path]["sha"] != old.get(path, {}).get("sha"):
                    raise StoreError("conflict", f"{path} changed since v{base}", 409)
            del files[path]
            return [path]

        return self._commit(pid, build, label=f"Delete {path}", kind="delete")

    def rename_file(self, pid: str, src: str, dst: str) -> dict:
        src, dst = normalize_path(src), normalize_path(dst)

        def build(files: dict[str, dict], put: Callable) -> list[str]:
            if src not in files:
                raise StoreError("not_found", f"no such file {src}", 404)
            if src == dst:
                return []
            if dst in files:
                raise StoreError("exists", f"{dst} already exists", 409)
            files[dst] = files.pop(src)
            return [src, dst]

        return self._commit(pid, build, label=f"Rename {src} to {dst}", kind="rename")

    def restore(self, pid: str, v: int) -> dict:
        """Make version v the new head, as a new version (history is never rewritten)."""
        old = dict(self._manifest(pid, self._resolve(pid, v))["files"])

        def build(files: dict[str, dict], put: Callable) -> list[str]:
            changed = [p for p in set(files) | set(old) if files.get(p, {}).get("sha") != old.get(p, {}).get("sha")]
            files.clear()
            files.update(old)
            return changed

        return self._commit(pid, build, label=f"Restore v{v}", kind="restore")

    # ---- chat log ------------------------------------------------------
    def append_chat(self, pid: str, role: str, text: str, **extra: object) -> None:
        with self._lock(pid):
            rec = {"t": now_iso(), "role": role, "text": text[:20000], **extra}
            with open(self._dir(pid) / "chat.jsonl", "a", encoding="utf-8") as fh:
                os.chmod(self._dir(pid) / "chat.jsonl", 0o600)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def chat(self, pid: str, limit: int = 60) -> list[dict]:
        p = self._dir(pid) / "chat.jsonl"
        if not p.exists():
            return []
        out = []
        for line in p.read_text("utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out[-limit:]

    # ---- zip -----------------------------------------------------------
    def zip_bytes(self, pid: str, v: int | None = None) -> tuple[str, bytes]:
        """Deterministic zip of a version: files under <slug>/, sorted, fixed timestamps."""
        n = self._resolve(pid, v)
        if n == 0:
            raise StoreError("not_found", "project has no files", 404)
        man = self._manifest(pid, n)
        meta = self.get_project(pid)
        try:
            ts = time.strptime(man["created"], "%Y-%m-%dT%H:%M:%SZ")[:6]
        except (KeyError, ValueError):
            ts = (1980, 1, 1, 0, 0, 0)
        if ts[0] < 1980:
            ts = (1980, 1, 1, 0, 0, 0)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(man["files"]):
                info = zipfile.ZipInfo(f"{meta['slug']}/{path}", date_time=ts)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, self.read_bytes(pid, path, n))
        suffix = "" if v is None else f"-v{n}"
        return f"{meta['slug']}{suffix}.zip", buf.getvalue()
