import os
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("ADMIN_PASSWORD", "test-password")
import server


ROOT = Path(server.__file__).parent
LARK_KEYS = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_VERIFICATION_TOKEN",
    "FEISHU_ENCRYPT_KEY",
    "FEISHU_DOMAIN",
)


class LarkSetupTests(unittest.TestCase):
    def test_lark_variables_are_registered_with_secret_metadata(self):
        definitions = {
            key: (category, secret)
            for key, _label, category, secret in server.ENV_VARS
            if key in LARK_KEYS
        }

        self.assertEqual(set(definitions), set(LARK_KEYS))
        self.assertEqual(definitions["FEISHU_APP_ID"], ("lark", False))
        self.assertEqual(definitions["FEISHU_APP_SECRET"], ("lark", True))
        self.assertEqual(definitions["FEISHU_VERIFICATION_TOKEN"], ("lark", True))
        self.assertEqual(definitions["FEISHU_ENCRYPT_KEY"], ("lark", True))
        self.assertEqual(definitions["FEISHU_DOMAIN"], ("lark", False))
        self.assertEqual(server.CHANNEL_MAP["Lark"], "FEISHU_APP_ID")

    def test_lark_variables_are_written_as_a_group(self):
        values = {key: f"value-for-{key.lower()}" for key in LARK_KEYS}

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            server.write_env(env_file, values)
            contents = env_file.read_text()

        self.assertIn("# Lark", contents)
        for key, value in values.items():
            self.assertIn(f"{key}={value}", contents)

    def test_setup_ui_can_detect_and_clear_every_lark_variable(self):
        template = (ROOT / "templates" / "index.html").read_text()

        self.assertIn("channelsEnabled.lark", template)
        self.assertIn("lark:       !!(this.vars.FEISHU_APP_ID)", template)
        for key in LARK_KEYS:
            self.assertIn(f"vars.{key}", template)
            self.assertIn(f"'{key}'", template)


if __name__ == "__main__":
    unittest.main()
