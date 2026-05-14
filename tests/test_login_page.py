import unittest
from pathlib import Path


SERVER_SOURCE = Path(__file__).resolve().parents[1] / "server.py"


def login_template_source() -> str:
    source = SERVER_SOURCE.read_text()
    start = source.index('LOGIN_PAGE_HTML = """')
    end = source.index('"""\n\n\ndef _html_escape', start)
    return source[start:end]


class LoginPageDesignTest(unittest.TestCase):
    def test_login_template_uses_dayshift_design_system_tokens(self):
        html = login_template_source()

        self.assertIn("Inter Tight", html)
        self.assertIn("JetBrains Mono", html)
        self.assertIn("--ds-bg: #10100E", html)
        self.assertIn("--ds-primary: #FFFFE3", html)
        self.assertIn("--ds-accent: #7A9E90", html)
        self.assertIn("WELCOME BACK", html)
        self.assertIn("hermes", html)

    def test_login_template_preserves_auth_form_contract(self):
        html = login_template_source()

        self.assertIn('method="POST"', html)
        self.assertIn('action="/login"', html)
        self.assertIn('name="returnTo"', html)
        self.assertIn("__RETURN_TO__", html)
        self.assertIn("__ERROR__", html)
        self.assertIn('autocomplete="username"', html)
        self.assertIn('autocomplete="current-password"', html)


if __name__ == "__main__":
    unittest.main()
