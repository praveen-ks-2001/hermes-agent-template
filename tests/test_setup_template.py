from pathlib import Path
import unittest


TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "index.html"


def read_template() -> str:
    return TEMPLATE.read_text()


class SetupTemplateTest(unittest.TestCase):
    def test_sidebar_monitor_items_are_not_setup_locked(self):
        html = read_template()

        self.assertNotIn("locked: !isSetupDone", html)
        self.assertNotIn("isSetupDone && (page='status')", html)
        self.assertNotIn("isSetupDone && (page='logs')", html)
        self.assertNotIn("isSetupDone && (page='users')", html)

    def test_openrouter_does_not_require_model_field_in_setup(self):
        html = read_template()

        self.assertIn('x-show="selectedProvider && selectedProvider !== \'OpenRouter\'"', html)
        self.assertNotIn("OpenRouter format:", html)
        self.assertNotIn("LLM Provider\n          <span class=\"req-note\">— required</span>", html)
        self.assertNotIn("LLM Model</label>", html)
