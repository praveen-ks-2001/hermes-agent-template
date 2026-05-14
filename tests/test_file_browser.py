import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

import server


class FileBrowserApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "notes.txt").write_text("hello from hermes\n")
        (self.data / "nested").mkdir()

        self.client = TestClient(server.app)
        self.client.cookies.set(server.COOKIE_NAME, server._make_auth_token())

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, path):
        return self.client.get(path)

    def test_files_endpoint_requires_admin_auth(self):
        response = TestClient(server.app).get("/setup/api/files/list")

        self.assertEqual(response.status_code, 401)

    def test_file_browser_defaults_to_data_directory_and_includes_docs_links(self):
        with patch.object(server, "FILE_BROWSER_DEFAULT_PATH", self.data):
            response = self.get("/setup/api/files/list")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["path"], str(self.data))
        self.assertEqual(body["parent"], str(self.root))
        self.assertIn(
            {
                "path": "/data/.hermes",
                "label": "Hermes runtime home",
                "description": "Persistent Hermes config, sessions, logs, memories, skills, cron, hooks, and caches for this Railway template.",
            },
            body["docs"],
        )

    def test_file_browser_lists_directory_entries(self):
        response = self.get(f"/setup/api/files/list?path={self.data}")

        self.assertEqual(response.status_code, 200)
        entries = {entry["name"]: entry for entry in response.json()["entries"]}
        self.assertEqual(entries["nested"]["type"], "directory")
        self.assertEqual(entries["notes.txt"]["type"], "file")
        self.assertEqual(entries["notes.txt"]["path"], str(self.data / "notes.txt"))

    def test_file_browser_error_responses_still_include_docs_links(self):
        response = self.get(f"/setup/api/files/list?path={self.root / 'missing'}")

        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertIn("docs", body)
        self.assertEqual(body["default_path"], "/data")

    def test_file_preview_returns_text_content(self):
        response = self.get(f"/setup/api/files/preview?path={self.data / 'notes.txt'}")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["path"], str(self.data / "notes.txt"))
        self.assertEqual(body["content"], "hello from hermes\n")
        self.assertFalse(body["truncated"])

    def test_file_preview_rejects_directories(self):
        response = self.get(f"/setup/api/files/preview?path={self.data / 'nested'}")

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
