import unittest
from pathlib import Path


TEMPLATE_SOURCE = Path(__file__).resolve().parents[1] / "templates" / "index.html"


class FileBrowserUiTest(unittest.TestCase):
    def test_admin_template_exposes_file_browser_panel_without_replacing_dayshift_shell(self):
        html = TEMPLATE_SOURCE.read_text()

        self.assertIn("Dayshift Systems", html)
        self.assertIn("page==='files'", html)
        self.assertIn(">Files<", html)
        self.assertIn("/setup/api/files/list", html)
        self.assertIn("/setup/api/files/preview", html)
        self.assertIn("/setup/api/files/download", html)
        self.assertIn("Official Hermes Docker examples often use", html)


if __name__ == "__main__":
    unittest.main()
