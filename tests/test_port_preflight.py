import asyncio
import os
import unittest
from os import environ
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ADMIN_PASSWORD", "test-password")
import server


class PortPreflightTests(unittest.TestCase):
    def setUp(self):
        config_patch = patch.object(server, "_read_hermes_config", return_value={})
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def test_detects_enabled_api_server_collision_with_railway_port(self):
        status = server.evaluate_port_preflight(
            process_env={"PORT": "8642", "HERMES_DASHBOARD_PORT": "9119"},
            hermes_env={"API_SERVER_ENABLED": "true"},
        )

        self.assertFalse(status["ok"])
        self.assertEqual(status["ports"]["web"], 8642)
        self.assertEqual(status["ports"]["api_server"], 8642)
        self.assertEqual(
            status["conflicts"][0]["services"],
            ["web", "api_server"],
        )
        self.assertIn("PORT=8080", status["conflicts"][0]["remediation"])

    def test_ignores_api_server_port_when_adapter_is_disabled(self):
        status = server.evaluate_port_preflight(
            process_env={"PORT": "8642", "HERMES_DASHBOARD_PORT": "9119"},
            hermes_env={"API_SERVER_ENABLED": "false"},
        )

        self.assertTrue(status["ok"])
        self.assertEqual(status["conflicts"], [])

    def test_reports_dashboard_collision(self):
        status = server.evaluate_port_preflight(
            process_env={"PORT": "9119", "HERMES_DASHBOARD_PORT": "9119"},
            hermes_env={"API_SERVER_ENABLED": "false"},
        )

        self.assertFalse(status["ok"])
        self.assertEqual(
            status["conflicts"][0]["services"],
            ["web", "dashboard"],
        )

    def test_gateway_env_disables_only_conflicting_api_adapter_for_this_run(self):
        original = {
            "LLM_MODEL": "example/model",
            "API_SERVER_ENABLED": "true",
            "API_SERVER_PORT": "8642",
        }

        gateway_env, status = server.prepare_gateway_env(
            process_env={"PORT": "8642", "HERMES_DASHBOARD_PORT": "9119"},
            hermes_env=original,
        )

        self.assertFalse(status["ok"])
        self.assertEqual(status["api_server_action"], "disable_for_gateway_start")
        self.assertEqual(gateway_env["API_SERVER_ENABLED"], "false")
        self.assertEqual(gateway_env["LLM_MODEL"], "example/model")
        self.assertEqual(original["API_SERVER_ENABLED"], "true")

    def test_api_key_also_enables_and_is_suppressed_for_conflicting_run(self):
        gateway_env, status = server.prepare_gateway_env(
            process_env={"PORT": "8642", "HERMES_DASHBOARD_PORT": "9119"},
            hermes_env={"API_SERVER_ENABLED": "false", "API_SERVER_KEY": "secret"},
        )

        self.assertFalse(status["ok"])
        self.assertTrue(status["api_server_enabled"])
        self.assertNotIn("secret", repr(status))
        self.assertEqual(status["api_server_action"], "disable_for_gateway_start")
        self.assertEqual(gateway_env["API_SERVER_ENABLED"], "false")
        self.assertEqual(gateway_env["API_SERVER_KEY"], "")

    def test_gateway_env_keeps_api_server_enabled_when_ports_are_unique(self):
        gateway_env, status = server.prepare_gateway_env(
            process_env={"PORT": "8080", "HERMES_DASHBOARD_PORT": "9119"},
            hermes_env={"API_SERVER_ENABLED": "true", "API_SERVER_PORT": "8642"},
        )

        self.assertTrue(status["ok"])
        self.assertIsNone(status["api_server_action"])
        self.assertEqual(gateway_env["API_SERVER_ENABLED"], "true")

    def test_yaml_enabled_api_collision_requires_blocking_gateway_start(self):
        gateway_env, status = server.prepare_gateway_env(
            process_env={"PORT": "8642", "HERMES_DASHBOARD_PORT": "9119"},
            hermes_env={"API_SERVER_ENABLED": "false"},
            hermes_config={
                "platforms": {
                    "api_server": {"enabled": True, "extra": {"port": 8642}},
                },
            },
        )

        self.assertFalse(status["ok"])
        self.assertTrue(status["api_server_config_enabled"])
        self.assertEqual(status["api_server_action"], "block_gateway_start")
        self.assertEqual(gateway_env["API_SERVER_ENABLED"], "false")
        self.assertNotIn("API_SERVER_KEY", gateway_env)

    def test_env_api_port_overrides_yaml_api_port(self):
        status = server.evaluate_port_preflight(
            process_env={"PORT": "9999", "HERMES_DASHBOARD_PORT": "9119"},
            hermes_env={"API_SERVER_ENABLED": "true", "API_SERVER_PORT": "9999"},
            hermes_config={
                "platforms": {
                    "api_server": {"enabled": True, "extra": {"port": 8642}},
                },
            },
        )

        self.assertFalse(status["ok"])
        self.assertEqual(status["ports"]["api_server"], 9999)

    def test_inactive_env_port_does_not_override_yaml_enabled_api_port(self):
        status = server.evaluate_port_preflight(
            process_env={"PORT": "8642", "HERMES_DASHBOARD_PORT": "9119"},
            hermes_env={"API_SERVER_ENABLED": "false", "API_SERVER_PORT": "9999"},
            hermes_config={
                "platforms": {
                    "api_server": {"enabled": True, "extra": {"port": 8642}},
                },
            },
        )

        self.assertFalse(status["ok"])
        self.assertEqual(status["ports"]["api_server"], 8642)
        self.assertEqual(status["api_server_action"], "block_gateway_start")

    def test_malformed_api_port_falls_back_to_upstream_default(self):
        status = server.evaluate_port_preflight(
            process_env={"PORT": "8642", "HERMES_DASHBOARD_PORT": "9119"},
            hermes_env={"API_SERVER_ENABLED": "true", "API_SERVER_PORT": "bad"},
        )

        self.assertFalse(status["ok"])
        self.assertEqual(status["ports"]["api_server"], 8642)

    def test_persisted_dotenv_overrides_railway_api_port(self):
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("API_SERVER_ENABLED=true\nAPI_SERVER_PORT=8642\n")
            with (
                patch.object(server, "ENV_FILE", env_file),
                patch.dict(
                    environ,
                    {"API_SERVER_ENABLED": "true", "API_SERVER_PORT": "9999"},
                    clear=False,
                ),
            ):
                merged = server.build_hermes_env()

        self.assertEqual(merged["API_SERVER_PORT"], "8642")
        status = server.evaluate_port_preflight(
            process_env={"PORT": "8642", "HERMES_DASHBOARD_PORT": "9119"},
            hermes_env=merged,
        )
        self.assertFalse(status["ok"])


class RuntimePreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_does_not_start_when_yaml_api_collision_cannot_be_suppressed(self):
        gateway = server.Gateway()
        status = {
            "ok": False,
            "api_server_action": "block_gateway_start",
            "conflicts": [{
                "services": ["web", "api_server"],
                "port": 8642,
            }],
        }

        with (
            patch.object(server, "prepare_gateway_env", return_value=({}, status)),
            patch.object(asyncio, "create_subprocess_exec", new=AsyncMock()) as spawn,
        ):
            await gateway.start()

        spawn.assert_not_awaited()
        self.assertEqual(gateway.state, "error")
        self.assertIn("config.yaml", gateway.logs[-1])

    async def test_dashboard_does_not_compete_for_public_web_port(self):
        dashboard = server.Dashboard()
        conflict = server.evaluate_port_preflight(
            process_env={"PORT": "9119", "HERMES_DASHBOARD_PORT": "9119"},
            hermes_env={"API_SERVER_ENABLED": "false"},
        )

        with (
            patch.object(server, "evaluate_port_preflight", return_value=conflict),
            patch.object(asyncio, "create_subprocess_exec", new=AsyncMock()) as spawn,
        ):
            await dashboard.start()

        spawn.assert_not_awaited()
        self.assertIn("Setup remains available", dashboard.logs[-1])


class SetupWarningTests(unittest.TestCase):
    def test_setup_page_renders_port_preflight_warning(self):
        template = (Path(server.__file__).parent / "templates" / "index.html").read_text()

        self.assertIn("status.port_preflight", template)
        self.assertIn("Port configuration conflict", template)
        self.assertIn("conflict.remediation", template)
        self.assertIn("block_gateway_start", template)


if __name__ == "__main__":
    unittest.main()
