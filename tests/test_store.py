import os
import stat
import threading
import unittest

from localbolt import store as st

from .helpers import tmp_store


class PathTests(unittest.TestCase):
    def test_accepts_normal_paths(self):
        for p in ("index.html", "js/app.js", "a/b/c/d.css", "data_1.json", "img/logo-2.svg"):
            self.assertEqual(st.normalize_path(p), p)

    def test_rejects_bad_paths(self):
        bad = ["", "/etc/passwd", "../x.html", "a/../b.html", "a//b.html", "./a.html", "a\\b.html", ".hidden.html",
               "a/.git/x.html", "dir/", "x" * 200 + ".html", "a\x00.html", "sp ace.html", "a/b/c/d/e/f/g/h/i/j.html", None, 3]
        for p in bad:
            with self.assertRaises(st.StoreError, msg=repr(p)) as cm:
                st.normalize_path(p)
            self.assertIn(cm.exception.code, ("bad_path", "bad_type"))

    def test_rejects_unsupported_extension(self):
        for p in ("run.sh", "x.exe", "noext", "a.php", "lib.wasm"):
            with self.assertRaises(st.StoreError) as cm:
                st.normalize_path(p)
            self.assertEqual(cm.exception.code, "bad_type")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store, self._td = tmp_store()
        self.addCleanup(self._td.cleanup)
        self.pid = self.store.create_project("My Todo App!", template=None)["id"]

    def test_data_root_is_private(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.store.root).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.root / "projects" / self.pid).st_mode), 0o700)

    def test_create_with_template_makes_version_1(self):
        p = self.store.create_project("X", "starter")
        self.assertEqual(p["head"], 1)
        self.assertEqual({f["path"] for f in self.store.file_list(p["id"])}, {"index.html", "style.css", "app.js"})
        self.assertEqual(p["slug"], "x")

    def test_unknown_template(self):
        with self.assertRaises(st.StoreError):
            self.store.create_project("X", "nope")

    def test_write_read_roundtrip_and_versions(self):
        r1 = self.store.write_files(self.pid, {"index.html": "<h1>1</h1>\n"}, label="one")
        r2 = self.store.write_files(self.pid, {"index.html": "<h1>2</h1>\n", "a.js": "x\n"}, label="two")
        self.assertEqual((r1["version"], r2["version"]), (1, 2))
        self.assertEqual(self.store.read_file(self.pid, "index.html"), "<h1>2</h1>\n")
        self.assertEqual(self.store.read_file(self.pid, "index.html", 1), "<h1>1</h1>\n")
        self.assertEqual(sorted(r2["changed"]), ["a.js", "index.html"])
        with self.assertRaises(st.StoreError) as cm:
            self.store.read_file(self.pid, "a.js", 1)
        self.assertEqual(cm.exception.status, 404)
        vs = self.store.versions(self.pid)
        self.assertEqual([v["n"] for v in vs], [1, 2])
        self.assertEqual(vs[1]["label"], "two")

    def test_identical_write_makes_no_version(self):
        self.store.write_files(self.pid, {"index.html": "same"}, label="a")
        r = self.store.write_files(self.pid, {"index.html": "same"}, label="b")
        self.assertTrue(r["unchanged"])
        self.assertEqual(self.store.get_project(self.pid)["head"], 1)

    def test_blobs_are_shared_between_versions(self):
        self.store.write_files(self.pid, {"a.js": "big" * 1000, "b.js": "small"}, label="a")
        self.store.write_files(self.pid, {"b.js": "small2"}, label="b")
        blobs = list((self.store.root / "projects" / self.pid / "blobs").iterdir())
        self.assertEqual(len(blobs), 3)  # a.js stored once

    def test_delete_file_keeps_history(self):
        self.store.write_files(self.pid, {"index.html": "i", "x.js": "x"}, label="a")
        r = self.store.delete_file(self.pid, "x.js")
        self.assertEqual(r["version"], 2)
        self.assertEqual([f["path"] for f in self.store.file_list(self.pid)], ["index.html"])
        self.assertEqual(self.store.read_file(self.pid, "x.js", 1), "x")
        with self.assertRaises(st.StoreError) as cm:
            self.store.delete_file(self.pid, "x.js")
        self.assertEqual(cm.exception.status, 404)

    def test_rename_file(self):
        self.store.write_files(self.pid, {"index.html": "i", "x.js": "x"}, label="a")
        self.store.rename_file(self.pid, "x.js", "lib/y.js")
        self.assertEqual(self.store.read_file(self.pid, "lib/y.js"), "x")
        self.assertNotIn("x.js", self.store.files(self.pid))
        with self.assertRaises(st.StoreError) as cm:
            self.store.rename_file(self.pid, "index.html", "lib/y.js")
        self.assertEqual(cm.exception.code, "exists")
        with self.assertRaises(st.StoreError):
            self.store.rename_file(self.pid, "index.html", "../evil.html")
        with self.assertRaises(st.StoreError):
            self.store.rename_file(self.pid, "missing.js", "z.js")

    def test_restore_makes_new_version_and_keeps_history(self):
        self.store.write_files(self.pid, {"index.html": "one", "a.js": "a"}, label="1")
        self.store.write_files(self.pid, {"index.html": "two"}, deletes=["a.js"], label="2")
        r = self.store.restore(self.pid, 1)
        self.assertEqual(r["version"], 3)
        self.assertEqual(self.store.read_file(self.pid, "index.html"), "one")
        self.assertEqual(self.store.read_file(self.pid, "a.js"), "a")
        self.assertEqual(self.store.read_file(self.pid, "index.html", 2), "two")
        with self.assertRaises(st.StoreError):
            self.store.restore(self.pid, 99)

    def test_base_conflict_detection(self):
        self.store.write_files(self.pid, {"index.html": "one"}, label="1")
        self.store.write_files(self.pid, {"index.html": "two"}, label="2")
        with self.assertRaises(st.StoreError) as cm:
            self.store.write_files(self.pid, {"index.html": "mine"}, label="edit", base=1)
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "conflict"))
        # a different file is not in conflict
        self.store.write_files(self.pid, {"other.js": "ok"}, label="edit", base=1)
        # base at head works
        self.store.write_files(self.pid, {"index.html": "three"}, label="edit", base=3)
        self.assertEqual(self.store.read_file(self.pid, "index.html"), "three")

    def test_failed_commit_is_atomic(self):
        self.store.write_files(self.pid, {"index.html": "one"}, label="1")
        with self.assertRaises(st.StoreError):
            self.store.write_files(self.pid, {"good.js": "x", "huge.js": "y" * (st.MAX_FILE_BYTES + 1)}, label="bad")
        self.assertEqual(self.store.get_project(self.pid)["head"], 1)
        self.assertEqual([f["path"] for f in self.store.file_list(self.pid)], ["index.html"])

    def test_limits(self):
        with self.assertRaises(st.StoreError) as cm:
            self.store.write_files(self.pid, {"a.js": "y" * (st.MAX_FILE_BYTES + 1)}, label="x")
        self.assertEqual(cm.exception.status, 413)
        many = {f"f{i}.js": "x" for i in range(st.MAX_FILES + 1)}
        with self.assertRaises(st.StoreError) as cm:
            self.store.write_files(self.pid, many, label="x")
        self.assertEqual(cm.exception.code, "too_many_files")
        with self.assertRaises(st.StoreError):
            self.store.write_files(self.pid, {"a.js": "nul\x00byte"}, label="x")

    def test_binary_bytes_rejected(self):
        with self.assertRaises(st.StoreError):
            self.store.write_files(self.pid, {"a.txt": b"\xff\xfe\x00bad"}, label="x")

    def test_bad_project_ids(self):
        for bad in ("../x", "p-zzzz", "p-0123456", "", None, "p-0123456789"):
            with self.assertRaises(st.StoreError) as cm:
                self.store.get_project(bad)
            self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(st.StoreError):
            self.store.get_project("p-00000000")

    def test_rename_and_archive_project(self):
        p = self.store.rename_project(self.pid, "Brand New")
        self.assertEqual((p["name"], p["slug"]), ("Brand New", "brand-new"))
        with self.assertRaises(st.StoreError):
            self.store.rename_project(self.pid, "   ")
        name = self.store.archive_project(self.pid)
        self.assertTrue((self.store.root / "archive" / name / "meta.json").exists())
        self.assertNotIn(self.pid, [x["id"] for x in self.store.list_projects()])

    def test_chat_log(self):
        self.store.append_chat(self.pid, "user", "hi")
        self.store.append_chat(self.pid, "assistant", "done", version=1)
        c = self.store.chat(self.pid)
        self.assertEqual([m["role"] for m in c], ["user", "assistant"])
        self.assertEqual(c[1]["version"], 1)

    def test_concurrent_commits_get_distinct_versions(self):
        errs = []

        def work(i):
            try:
                self.store.write_files(self.pid, {f"f{i}.js": str(i)}, label=f"w{i}")
            except Exception as e:  # pragma: no cover
                errs.append(e)

        ts = [threading.Thread(target=work, args=(i,)) for i in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(self.store.get_project(self.pid)["head"], 12)
        self.assertEqual(len(self.store.files(self.pid)), 12)  # no lost update


if __name__ == "__main__":
    unittest.main()
