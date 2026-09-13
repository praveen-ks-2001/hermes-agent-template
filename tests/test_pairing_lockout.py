import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("ADMIN_PASSWORD", "test-password")
import server


ROOT = Path(server.__file__).parent


class JsonRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class PairingLockoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_removes_only_lockout_metadata_for_selected_platform(self):
        limits = {
            "_failures:telegram": 4,
            "_lockout:telegram": 9999999999,
            "telegram:user-1": 1234,
            "discord:telegram": 5678,
            "_failures:discord": 2,
            "_lockout:discord": 9999999999,
        }

        with tempfile.TemporaryDirectory() as directory:
            pairing_dir = Path(directory)
            rate_limits = pairing_dir / "_rate_limits.json"
            rate_limits.write_text(json.dumps(limits))
            with (
                patch.object(server, "PAIRING_DIR", pairing_dir),
                patch.object(server, "guard", return_value=None),
            ):
                response = await server.api_pairing_reset_lockout(JsonRequest({"platform": " Telegram "}))
            saved = json.loads(rate_limits.read_text())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.body)["keys_removed"],
            ["_failures:telegram", "_lockout:telegram"],
        )
        self.assertEqual(
            saved,
            {
                "telegram:user-1": 1234,
                "discord:telegram": 5678,
                "_failures:discord": 2,
                "_lockout:discord": 9999999999,
            },
        )

    async def test_reset_rejects_invalid_platform_identifiers(self):
        with patch.object(server, "guard", return_value=None):
            response = await server.api_pairing_reset_lockout(JsonRequest({"platform": "../telegram"}))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), {"error": "valid platform required"})

    def test_setup_ui_exposes_the_reset_action(self):
        template = (ROOT / "templates" / "index.html").read_text()

        self.assertIn("@click=\"resetLockout(r.platform)\"", template)
        self.assertIn("fetch('/setup/api/pairing/reset-lockout'", template)


if __name__ == "__main__":
    unittest.main()
