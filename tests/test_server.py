import json
import re
import unittest

from localbolt import llm
from localbolt import server as srv

from .helpers import FakeModel, Running, tmp_store

APP_REPLY = """=== FILE: index.html ===
<!doctype html>
<html><head><title>Clock</title><link rel="stylesheet" href="style.css"></head>
<body><h1 id="t">00:00</h1><script src="app.js"></script></body></html>
=== END FILE ===
=== FILE: style.css ===
body { background: #111; color: #eee; }
=== END FILE ===
=== FILE: app.js ===
document.getElementById('t').textContent = 'tick';
=== END FILE ===
SUMMARY: Built a clock page.
"""


class Base(unittest.TestCase):
    fake_kwargs: dict = {}
    with_model = True

    def setUp(self):
        self.store, self._td = tmp_store()
        self.addCleanup(self._td.cleanup)
        self.fake = FakeModel(APP_REPLY, **self.fake_kwargs) if self.with_model else None
        self.run_ = Running(self.store, self.fake)
        self.addCleanup(self.run_.close)
        if self.fake:
            self.addCleanup(self.fake.close)
        self.r = self.run_

    def make_project(self, files=None, name="T"):
        p = self.store.create_project(name, template=None)
        if files:
            self.store.write_files(p["id"], files, label="seed")
        return p["id"]


class HostAndOriginTests(Base):
    def test_foreign_host_rejected_on_both_servers(self):
        pid = self.make_project({"index.html": "<p>x</p>"})
        for host in ("evil.example", "evil.example:8877", "127.0.0.1.evil.example", "127.0.0.1", "localhost", "127.0.0.1:1", "[::1]:8877", ""):
            for port, path in ((self.r.app_port, "/"), (self.r.app_port, "/api/projects"), (self.r.app_port, f"/api/projects/{pid}"),
                               (self.r.preview_port, f"/{pid}/head/index.html"), (self.r.preview_port, "/")):
                s, _, body = self.r.req(port, "GET", path, host=host)
                self.assertEqual(s, 403, f"{host!r} {path} -> {s}")
                self.assertNotIn(b"<p>x</p>", body)

    def test_foreign_host_rejected_on_mutations(self):
        s, _ = self.r.app("POST", "/api/projects", {"name": "x"}, host="evil.example")
        self.assertEqual(s, 403)
        self.assertEqual(self.store.list_projects(), [])

    def test_own_host_names_accepted(self):
        for host in (f"127.0.0.1:{self.r.app_port}", f"localhost:{self.r.app_port}"):
            self.assertEqual(self.r.req(self.r.app_port, "GET", "/", host=host)[0], 200)
        for host in (f"127.0.0.1:{self.r.preview_port}", f"localhost:{self.r.preview_port}"):
            pid = self.make_project({"index.html": "<p>x</p>"})
            self.assertEqual(self.r.req(self.r.preview_port, "GET", f"/{pid}/head/index.html", host=host)[0], 200)

    def test_app_host_is_not_valid_on_preview_and_vice_versa(self):
        s, _, _ = self.r.req(self.r.preview_port, "GET", "/", host=f"127.0.0.1:{self.r.app_port}")
        self.assertEqual(s, 403)
        s, _, _ = self.r.req(self.r.app_port, "GET", "/", host=f"127.0.0.1:{self.r.preview_port}")
        self.assertEqual(s, 403)

    def test_cross_origin_writes_rejected(self):
        for hdr in ({"Origin": "http://evil.example"}, {"Origin": f"http://127.0.0.1:{self.r.preview_port}"}, {"Origin": "null"},
                    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            s, _ = self.r.app("POST", "/api/projects", {"name": "x"}, headers=hdr)
            self.assertEqual(s, 403, hdr)
        self.assertEqual(self.store.list_projects(), [])
        s, _ = self.r.app("POST", "/api/projects", {"name": "ok"}, headers={"Origin": f"http://127.0.0.1:{self.r.app_port}", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(s, 201)

    def test_binds_loopback_only(self):
        for s in (self.r.servers._app, self.r.servers._preview):
            self.assertEqual(s.server_address[0], "127.0.0.1")


class AppSurfaceTests(Base):
    def test_ui_served_with_csp_and_no_external_urls(self):
        s, h, body = self.r.req(self.r.app_port, "GET", "/")
        self.assertEqual(s, 200)
        csp = h["content-security-policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn(f"frame-src http://127.0.0.1:{self.r.preview_port}", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("'unsafe-inline'", csp)
        self.assertNotIn("'unsafe-eval'", csp)
        for path in ("/", "/static/app.js", "/static/style.css"):
            text = self.r.req(self.r.app_port, "GET", path)[2].decode()
            self.assertFalse(re.search(r"(src|href)=[\"']https?://", text), path)
            self.assertNotRegex(text, r"https?://(?!127\.0\.0\.1|localhost)[a-z]", path)

    def test_iframe_is_sandboxed_without_same_origin(self):
        html = self.r.req(self.r.app_port, "GET", "/")[2].decode()
        m = re.search(r"<iframe[^>]*>", html)
        self.assertIsNotNone(m)
        sb = re.search(r'sandbox="([^"]*)"', m.group(0)).group(1).split()
        self.assertIn("allow-scripts", sb)
        self.assertNotIn("allow-same-origin", sb)
        self.assertNotIn("allow-top-navigation", sb)
        self.assertNotIn("allow-popups-to-escape-sandbox", sb)

    def test_static_traversal_blocked(self):
        for p in ("/static/../server.py", "/static/%2e%2e/server.py", "/static/app.js/../../store.py", "/static/", "/static/secret"):
            s, _, body = self.r.req(self.r.app_port, "GET", p)
            self.assertEqual(s, 404, p)

    def test_status_reports_model_up(self):
        s, d = self.r.app("GET", "/api/status")
        self.assertEqual(s, 200)
        self.assertTrue(d["model"]["available"])
        self.assertEqual(d["preview_origin"], f"http://127.0.0.1:{self.r.preview_port}")
        self.assertEqual(d["app_port"], self.r.app_port)
        self.assertEqual(d["gateway_preview_port"], 0)

    def test_gateway_preview_port_widens_only_frame_src(self):
        from localbolt import server as srv
        csp = srv.app_csp("http://127.0.0.1:8878", 4810)
        self.assertIn("frame-src http://127.0.0.1:8878 http://*:4810;", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("script-src 'self';", csp)
        self.assertNotIn("http://*:4810", srv.app_csp("http://127.0.0.1:8878"))

    def test_project_crud_over_http(self):
        s, p = self.r.app("POST", "/api/projects", {"name": "Notes", "template": "starter"})
        self.assertEqual(s, 201)
        pid = p["id"]
        s, d = self.r.app("GET", f"/api/projects/{pid}")
        self.assertEqual([f["path"] for f in d["files"]], ["app.js", "index.html", "style.css"])
        self.assertEqual(d["preview"], f"http://127.0.0.1:{self.r.preview_port}/{pid}")
        s, f = self.r.app("GET", f"/api/projects/{pid}/file?path=index.html")
        self.assertIn("<h1>New app</h1>", f["content"])
        s, r = self.r.app("PUT", f"/api/projects/{pid}/file", {"path": "index.html", "content": "<h1>mine</h1>", "base": 1})
        self.assertEqual((s, r["version"]), (200, 2))
        s, r = self.r.app("PUT", f"/api/projects/{pid}/file", {"path": "index.html", "content": "<h1>stale</h1>", "base": 1})
        self.assertEqual(s, 409)
        s, r = self.r.app("PUT", f"/api/projects/{pid}/file", {"path": "../x.html", "content": "x"})
        self.assertEqual(s, 400)
        s, r = self.r.app("POST", f"/api/projects/{pid}/file/rename", {"from": "app.js", "to": "main.js"})
        self.assertEqual(s, 200)
        s, r = self.r.app("DELETE", f"/api/projects/{pid}/file?path=style.css")
        self.assertEqual(s, 200)
        s, r = self.r.app("POST", f"/api/projects/{pid}/restore", {"version": 1})
        self.assertEqual(s, 200)
        s, files = self.r.app("GET", f"/api/projects/{pid}/files?v=2")
        self.assertEqual([f["path"] for f in files["files"]], ["app.js", "index.html", "style.css"])
        s, r = self.r.app("POST", f"/api/projects/{pid}/rename", {"name": "Renamed"})
        self.assertEqual(r["name"], "Renamed")
        s, r = self.r.app("POST", f"/api/projects/{pid}/archive", {})
        self.assertEqual(s, 200)
        self.assertEqual(self.r.app("GET", f"/api/projects/{pid}")[0], 404)

    def test_bad_bodies(self):
        s, _ = self.r.app("POST", "/api/projects", raw=b"{not json")
        self.assertEqual(s, 400)
        s, _ = self.r.app("POST", "/api/projects", raw=b"[1]")
        self.assertEqual(s, 400)
        s, _ = self.r.app("GET", "/api/projects/p-zzzzzzzz")
        self.assertEqual(s, 404)
        s, _ = self.r.app("GET", "/api/nope")
        self.assertEqual(s, 404)

    def test_download_zip_headers_and_body(self):
        pid = self.make_project({"index.html": "<p>z</p>"}, name="Zip Me")
        s, h, body = self.r.req(self.r.app_port, "GET", f"/api/projects/{pid}/download.zip")
        self.assertEqual(s, 200)
        self.assertEqual(h["content-type"], "application/zip")
        self.assertIn('filename="zip-me.zip"', h["content-disposition"])
        self.assertTrue(body.startswith(b"PK"))

    def test_project_files_are_never_served_html_from_app_origin(self):
        pid = self.make_project({"index.html": "<script>alert(1)</script>"})
        for p in (f"/{pid}/head/index.html", f"/api/projects/{pid}/index.html", f"/p/{pid}/index.html"):
            self.assertEqual(self.r.req(self.r.app_port, "GET", p)[0], 404, p)
        s, h, body = self.r.req(self.r.app_port, "GET", f"/api/projects/{pid}/file?path=index.html")
        self.assertEqual(h["content-type"], "application/json; charset=utf-8")
        self.assertEqual(h["x-content-type-options"], "nosniff")


class PreviewIsolationTests(Base):
    def setUp(self):
        super().setUp()
        self.pid = self.make_project({
            "index.html": "<!doctype html><html><head><title>t</title></head><body><script src=\"app.js\"></script></body></html>",
            "app.js": "1;", "data/list.json": '{"a":1}', "sub/index.html": "<h1>sub</h1>", "notes.md": "# hi",
        })

    def get(self, path, **kw):
        return self.r.preview("GET", path, **kw)

    def test_separate_origin_from_ui(self):
        self.assertNotEqual(self.r.app_port, self.r.preview_port)
        s, h, _ = self.get(f"/{self.pid}/head/index.html")
        self.assertEqual(s, 200)
        self.assertEqual(h["access-control-allow-origin"], "null")  # only the sandboxed opaque origin may fetch

    def test_csp_locks_the_page_down(self):
        _, h, _ = self.get(f"/{self.pid}/head/index.html")
        csp = h["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertRegex(csp, r"sandbox allow-scripts[^;]*")
        self.assertNotIn("allow-same-origin", csp)
        self.assertNotIn("allow-top-navigation", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertNotRegex(csp, r"(script|style|img|connect|font|media)-src[^;]*https?:")
        self.assertNotIn("'unsafe-eval'", csp)
        self.assertIn("form-action 'none'", csp)
        self.assertIn("frame-src 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn(f"frame-ancestors http://127.0.0.1:{self.r.app_port} http://localhost:{self.r.app_port}", csp)
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(h["cache-control"], "no-store")
        self.assertEqual(h["referrer-policy"], "no-referrer")

    def test_csp_on_every_asset_type(self):
        for p in ("app.js", "data/list.json", "notes.md", "sub/index.html"):
            h = self.get(f"/{self.pid}/head/{p}")[1]
            self.assertIn("default-src 'none'", h["content-security-policy"], p)

    def test_content_types(self):
        self.assertEqual(self.get(f"/{self.pid}/head/app.js")[1]["content-type"], "text/javascript; charset=utf-8")
        self.assertEqual(self.get(f"/{self.pid}/head/data/list.json")[1]["content-type"], "application/json; charset=utf-8")
        self.assertEqual(self.get(f"/{self.pid}/head/notes.md")[1]["content-type"], "text/plain; charset=utf-8")

    def test_directory_index(self):
        self.assertEqual(self.get(f"/{self.pid}/head/")[0], 200)
        s, _, body = self.get(f"/{self.pid}/head/sub/")
        self.assertEqual(s, 200)
        self.assertTrue(body.endswith(b"<h1>sub</h1>"))

    def test_versions_addressable_and_head_follows_new_versions(self):
        self.store.write_files(self.pid, {"app.js": "2;"}, label="v2")
        self.assertEqual(self.get(f"/{self.pid}/head/app.js")[2], b"2;")
        self.assertEqual(self.get(f"/{self.pid}/v1/app.js")[2], b"1;")
        self.assertEqual(self.get(f"/{self.pid}/v2/app.js")[2], b"2;")
        self.assertEqual(self.get(f"/{self.pid}/v3/app.js")[0], 404)
        self.assertEqual(self.get(f"/{self.pid}/v0/app.js")[0], 404)

    def test_path_traversal_blocked(self):
        secret = self.store.root / "projects" / self.pid / "meta.json"
        self.assertTrue(secret.exists())
        attempts = [
            f"/{self.pid}/head/../meta.json", f"/{self.pid}/head/%2e%2e/meta.json", f"/{self.pid}/head/..%2fmeta.json",
            f"/{self.pid}/head/%2e%2e%2f%2e%2e%2fmeta.json", f"/{self.pid}/head/....//meta.json", f"/{self.pid}/head//meta.json",
            f"/{self.pid}/head/..\\meta.json", f"/{self.pid}/head/%5c..%5cmeta.json", f"/{self.pid}/head/app.js%00.html",
            f"/{self.pid}/head/./app.js", f"/{self.pid}/head/data/../app.js", "/../etc/passwd", "//etc/passwd",
            f"/{self.pid}/head/blobs/x.html", f"/{self.pid}/head/versions/v0001.json", f"/{self.pid}/head/.lock",
        ]
        for a in attempts:
            s, _, body = self.get(a)
            self.assertIn(s, (400, 404), a)
            self.assertNotIn(b'"slug"', body, a)

    def test_cannot_reach_other_projects_via_this_project_url(self):
        other = self.make_project({"index.html": "<p>other</p>"}, name="Other")
        for a in (f"/{self.pid}/head/../{other}/head/index.html", f"/{self.pid}/head/%2e%2e/{other}/head/index.html", f"/{self.pid}/head/{other}/index.html"):
            s, _, body = self.get(a)
            self.assertNotEqual(s, 200, a)
            self.assertNotIn(b"other", body)

    def test_bad_project_ids_and_refs(self):
        for a in ("/p-00000000/head/index.html", "/nope/head/index.html", f"/{self.pid}/main/index.html", f"/{self.pid}/head", f"/{self.pid}", "/"):
            self.assertEqual(self.get(a)[0], 404, a)

    def test_preview_is_read_only_and_has_no_api(self):
        for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            s, _, _ = self.r.preview(m, f"/{self.pid}/head/index.html", raw=b"x")
            self.assertEqual(s, 405, m)
        self.assertEqual(self.get("/api/projects")[0], 404)
        self.assertEqual(self.get("/api/status")[0], 404)
        self.assertEqual(self.get("/static/app.js")[0], 404)

    def test_storage_shim_injected_into_html_only_and_files_untouched(self):
        body = self.get(f"/{self.pid}/head/index.html")[2]
        self.assertIn(b"localStorage", body)
        self.assertLess(body.index(b"localStorage"), body.index(b'<script src="app.js">'))  # runs before app code
        self.assertEqual(self.get(f"/{self.pid}/head/app.js")[2], b"1;")
        self.assertNotIn(b"localStorage", self.store.read_bytes(self.pid, "index.html"))

    def test_head_request(self):
        s, h, body = self.r.preview("HEAD", f"/{self.pid}/head/app.js")
        self.assertEqual((s, body), (200, b""))
        self.assertIn("content-security-policy", h)

    def test_archived_project_is_gone_from_preview(self):
        self.store.archive_project(self.pid)
        self.assertEqual(self.get(f"/{self.pid}/head/index.html")[0], 404)


class GenerateTests(Base):
    def stream(self, pid, prompt):
        s, h, body = self.r.req(self.r.app_port, "POST", f"/api/projects/{pid}/generate", {"prompt": prompt})
        events = [json.loads(line) for line in body.decode().splitlines() if line.strip()] if s == 200 else []
        return s, events, body

    def test_generate_new_app_streams_and_commits_one_version(self):
        pid = self.store.create_project("Clock", template=None)["id"]
        s, events, _ = self.stream(pid, "a clock")
        self.assertEqual(s, 200)
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "status")
        self.assertEqual(types[-1], "done")
        self.assertEqual([e["path"] for e in events if e["type"] == "file"], ["index.html", "style.css", "app.js"])
        done = events[-1]
        self.assertEqual(done["version"], 1)
        self.assertEqual(done["summary"], "Built a clock page.")
        self.assertIn("elapsed_s", done["gen"])
        self.assertEqual(sorted(f["path"] for f in self.store.file_list(pid)), ["app.js", "index.html", "style.css"])
        v = self.store.versions(pid)[0]
        self.assertEqual(v["prompt"], "a clock")
        self.assertEqual(v["kind"], "generate")
        chat = self.store.chat(pid)
        self.assertEqual([m["role"] for m in chat], ["user", "assistant"])
        # the request really went to the configured model with the stream flag and the system prompt
        req = self.fake.requests[-1]
        self.assertTrue(req["stream"])
        self.assertEqual(req["messages"][0]["role"], "system")
        self.assertEqual(req["model"], "fake-model")

    def test_generated_app_is_served_by_preview(self):
        pid = self.store.create_project("Clock", template=None)["id"]
        self.stream(pid, "a clock")
        s, h, body = self.r.preview("GET", f"/{pid}/head/index.html")
        self.assertEqual(s, 200)
        self.assertIn(b"<h1 id=\"t\">", body)

    def test_followup_sends_current_files_and_makes_second_version(self):
        pid = self.store.create_project("Clock", template=None)["id"]
        self.stream(pid, "a clock")
        self.fake.reply = "=== FILE: style.css ===\nbody { background: red; }\n=== END FILE ===\nSUMMARY: Made it red.\n"
        s, events, _ = self.stream(pid, "make it red")
        self.assertEqual(events[-1]["version"], 2)
        self.assertEqual(events[-1]["changed"], ["style.css"])
        sent = self.fake.requests[-1]["messages"][-1]["content"]
        self.assertIn("=== CURRENT FILE: index.html ===", sent)
        self.assertIn("make it red", sent)
        self.assertEqual(self.store.read_file(pid, "app.js"), "document.getElementById('t').textContent = 'tick';\n")  # untouched
        self.assertEqual(self.store.read_file(pid, "style.css", 1), "body { background: #111; color: #eee; }\n")
        self.assertEqual(self.store.versions(pid)[1]["kind"], "edit")

    def test_unsafe_model_paths_are_skipped_not_written(self):
        self.fake.reply = ("=== FILE: ../../evil.html ===\nx\n=== END FILE ===\n=== FILE: run.sh ===\nrm -rf /\n=== END FILE ===\n"
                           "=== FILE: index.html ===\nok\n=== END FILE ===\nSUMMARY: s\n")
        pid = self.store.create_project("T", template=None)["id"]
        s, events, _ = self.stream(pid, "x")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual([f["path"] for f in self.store.file_list(pid)], ["index.html"])
        self.assertEqual(len([e for e in events if e["type"] == "warning"]), 2)
        self.assertFalse((self.store.root.parent / "evil.html").exists())

    def test_thinking_is_off_by_default_and_per_request_opt_in(self):
        pid = self.store.create_project("T", template=None)["id"]
        self.stream(pid, "a clock")
        self.assertIs(self.fake.requests[-1]["chat_template_kwargs"]["enable_thinking"], False)
        s, h, body = self.r.req(self.r.app_port, "POST", f"/api/projects/{pid}/generate", {"prompt": "again", "think": True})
        self.assertEqual(s, 200)
        self.assertIs(self.fake.requests[-1]["chat_template_kwargs"]["enable_thinking"], True)

    def test_empty_prompt_rejected(self):
        pid = self.store.create_project("T", template=None)["id"]
        s, _, _ = self.stream(pid, "   ")
        self.assertEqual(s, 400)

    def test_model_returning_prose_saves_nothing(self):
        self.fake.reply = "I'm sorry, I can't help with that."
        pid = self.store.create_project("T", template="starter")["id"]
        s, events, _ = self.stream(pid, "x")
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(self.store.get_project(pid)["head"], 1)

    def test_stream_cut_mid_file_saves_nothing(self):
        self.fake.fail_after = 3
        pid = self.store.create_project("T", template="starter")["id"]
        s, events, _ = self.stream(pid, "x")
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["error"], "model_unavailable")
        self.assertEqual(self.store.get_project(pid)["head"], 1)  # no partial version

    def test_busy_project_returns_409(self):
        pid = self.store.create_project("T", template=None)["id"]
        lock = self.r.servers.app.gen_lock(pid)
        lock.acquire()
        try:
            s, _, _ = self.stream(pid, "x")
            self.assertEqual(s, 409)
        finally:
            lock.release()


class ModelDownTests(Base):
    with_model = False

    def test_status_says_unavailable_with_reason(self):
        s, d = self.r.app("GET", "/api/status")
        self.assertEqual(s, 200)
        self.assertFalse(d["model"]["available"])
        self.assertTrue(d["model"]["reason"])

    def test_generate_returns_503_model_unavailable_and_changes_nothing(self):
        pid = self.store.create_project("T", template="starter")["id"]
        s, d = self.r.app("POST", f"/api/projects/{pid}/generate", {"prompt": "anything"})
        self.assertEqual(s, 503)
        self.assertEqual(d["error"], "model_unavailable")
        self.assertIn("edit files by hand", d["message"])
        self.assertEqual(self.store.get_project(pid)["head"], 1)

    def test_editor_still_works_when_model_is_down(self):
        s, p = self.r.app("POST", "/api/projects", {"name": "Offline", "template": "starter"})
        self.assertEqual(s, 201)
        pid = p["id"]
        s, r = self.r.app("PUT", f"/api/projects/{pid}/file", {"path": "index.html", "content": "<h1>offline edit</h1>"})
        self.assertEqual(s, 200)
        self.assertEqual(self.r.preview("GET", f"/{pid}/head/index.html")[0], 200)
        self.assertEqual(self.r.req(self.r.app_port, "GET", f"/api/projects/{pid}/download.zip")[0], 200)

    def test_wrong_model_id_counts_as_unavailable(self):
        fake = FakeModel("", model="other-model")
        self.addCleanup(fake.close)
        s = llm.model_status(llm.ModelConfig(url=fake.url, model="fake-model"))
        self.assertFalse(s["available"])
        self.assertIn("other-model", s["reason"])


class HttpErrorFromModelTests(Base):
    fake_kwargs = {"status": 503}

    def test_upstream_503_is_model_unavailable_and_nothing_saved(self):
        pid = self.store.create_project("T", template="starter")["id"]
        s, h, body = self.r.req(self.r.app_port, "POST", f"/api/projects/{pid}/generate", {"prompt": "x"})
        events = [json.loads(x) for x in body.decode().splitlines() if x.strip()]
        self.assertEqual(events[-1]["error"], "model_unavailable")
        self.assertEqual(self.store.get_project(pid)["head"], 1)


if __name__ == "__main__":
    unittest.main()
