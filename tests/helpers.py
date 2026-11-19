"""Shared test fixtures: temp store, a fake OpenAI-compatible streaming model, an HTTP client."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from localbolt import llm
from localbolt import store as st
from localbolt.server import Config, Servers


def tmp_store() -> tuple[st.Store, tempfile.TemporaryDirectory]:
    td = tempfile.TemporaryDirectory()
    return st.Store(td.name + "/data"), td


class FakeModel:
    """Serves /models and streaming /chat/completions. `reply` is the content to stream."""

    def __init__(self, reply: str = "", model: str = "fake-model", chunk: int = 7, fail_after: int | None = None, status: int = 200):
        self.reply, self.model, self.chunk, self.fail_after, self.status = reply, model, chunk, fail_after, status
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"data": [{"id": outer.model}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                outer.requests.append(json.loads(self.rfile.read(n)))
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    self.wfile.write(b'{"error":"down"}')
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def send(delta, finish=None):
                    ev = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                    self.wfile.flush()

                send({"role": "assistant"})
                send({"reasoning_content": "thinking about it"})
                text = outer.reply
                pieces = [text[i:i + outer.chunk] for i in range(0, len(text), outer.chunk)]
                for i, p in enumerate(pieces):
                    if outer.fail_after is not None and i >= outer.fail_after:
                        self.connection.close()
                        return
                    send({"content": p})
                send({}, "stop")
                self.wfile.write(b"data: [DONE]\n\n")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def model_cfg(fake: FakeModel | None, model: str = "fake-model") -> llm.ModelConfig:
    url = fake.url if fake else "http://127.0.0.1:9/v1"  # port 9 (discard): nothing listens
    return llm.ModelConfig(url=url, model=model, read_timeout=10, max_tokens=1000)


class Running:
    """Both servers on ephemeral ports plus a tiny client."""

    def __init__(self, store: st.Store, fake: FakeModel | None):
        self.store = store
        self.servers = Servers(store, Config(app_port=0, preview_port=0, llm=model_cfg(fake)))
        self.servers.start()
        self.app_port = self.servers.app_port
        self.preview_port = self.servers.preview_port

    def close(self):
        self.servers.stop()

    def req(self, port: int, method: str, path: str, body=None, host: str | None = None, headers: dict | None = None, raw: bytes | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        h = {"Host": host if host is not None else f"127.0.0.1:{port}"}
        h.update(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        payload = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        conn.close()
        return r.status, hdrs, payload

    def app(self, method, path, body=None, **kw):
        s, h, p = self.req(self.app_port, method, path, body, **kw)
        try:
            return s, json.loads(p)
        except ValueError:
            return s, p

    def preview(self, method, path, **kw):
        return self.req(self.preview_port, method, path, **kw)
