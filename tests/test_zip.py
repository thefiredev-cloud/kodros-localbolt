import io
import unittest
import zipfile

from localbolt import store as st

from .helpers import tmp_store


class ZipTests(unittest.TestCase):
    def setUp(self):
        self.store, self._td = tmp_store()
        self.addCleanup(self._td.cleanup)
        self.pid = self.store.create_project("Café Menu", template=None)["id"]
        self.store.write_files(self.pid, {"index.html": "<h1>v1</h1>\n", "css/site.css": "b{}\n"}, label="one")
        self.store.write_files(self.pid, {"index.html": "<h1>v2</h1>\n", "app.js": "1;\n"}, label="two")

    def open(self, data):
        return zipfile.ZipFile(io.BytesIO(data))

    def test_head_zip_contents_and_prefix(self):
        name, data = self.store.zip_bytes(self.pid)
        self.assertEqual(name, "caf-menu.zip")
        zf = self.open(data)
        self.assertIsNone(zf.testzip())
        self.assertEqual(sorted(zf.namelist()), ["caf-menu/app.js", "caf-menu/css/site.css", "caf-menu/index.html"])
        self.assertEqual(zf.read("caf-menu/index.html"), b"<h1>v2</h1>\n")

    def test_old_version_zip(self):
        name, data = self.store.zip_bytes(self.pid, 1)
        self.assertEqual(name, "caf-menu-v1.zip")
        zf = self.open(data)
        self.assertEqual(sorted(zf.namelist()), ["caf-menu/css/site.css", "caf-menu/index.html"])
        self.assertEqual(zf.read("caf-menu/index.html"), b"<h1>v1</h1>\n")

    def test_deterministic(self):
        self.assertEqual(self.store.zip_bytes(self.pid)[1], self.store.zip_bytes(self.pid)[1])

    def test_no_unsafe_member_names(self):
        for n in self.open(self.store.zip_bytes(self.pid)[1]).namelist():
            self.assertFalse(n.startswith("/") or ".." in n.split("/"), n)

    def test_permissions_not_executable(self):
        for info in self.open(self.store.zip_bytes(self.pid)[1]).infolist():
            self.assertEqual(info.external_attr >> 16, 0o644)

    def test_empty_project_and_bad_version(self):
        empty = self.store.create_project("E", template=None)["id"]
        with self.assertRaises(st.StoreError) as cm:
            self.store.zip_bytes(empty)
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(st.StoreError):
            self.store.zip_bytes(self.pid, 9)

    def test_roundtrip_matches_store(self):
        zf = self.open(self.store.zip_bytes(self.pid)[1])
        for path in self.store.files(self.pid):
            self.assertEqual(zf.read(f"caf-menu/{path}"), self.store.read_bytes(self.pid, path))


if __name__ == "__main__":
    unittest.main()
