"""Regression checks for preserving existing Hermes configuration on upgrade."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import hermes_yaml as yaml


ROOT = Path(__file__).resolve().parents[1]


class ConfigSafetyFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-config-safety-")
        self.home = Path(self.tmp.name) / ".hermes"
        self.home.mkdir()
        self.environ = patch.dict(
            os.environ,
            {"HERMES_HOME": str(self.home), "ADMIN_PASSWORD": "test-password", "HERMES_REF": "v2026.9.24"},
            clear=True,
        )
        self.environ.start()
        name = f"server_config_safety_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(name, ROOT / "server.py")
        assert spec and spec.loader
        self.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.server)
        self.real_writer = self.server._write_config_atomic
        if importlib.util.find_spec("hermes_cli") is None:
            # The merge/error tests run from the checkout without Hermes. The
            # image-specific test below exercises the real upstream writer.
            self.server._write_config_atomic = lambda path, data: path.write_text(
                yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
            )

    def tearDown(self):
        self.environ.stop()
        self.tmp.cleanup()


class ConfigFileSafetyTests(ConfigSafetyFixture, unittest.TestCase):
    def test_gateway_config_save_refuses_malformed_existing_file(self):
        path = self.home / "config.yaml"
        original = "# keep this file for repair\nmodel: [unterminated\n"
        path.write_text(original, encoding="utf-8")
        writer = Mock(side_effect=AssertionError("writer must not run"))
        self.server._write_config_atomic = writer

        with self.assertRaisesRegex(ValueError, "invalid YAML"):
            self.server.write_config_yaml({"LLM_MODEL": "new-model"})

        self.assertEqual(path.read_text(encoding="utf-8"), original)
        writer.assert_not_called()

    def test_xai_config_save_refuses_malformed_existing_file(self):
        path = self.home / "config.yaml"
        original = "# keep existing xAI settings\nmodel: [unterminated\n"
        path.write_text(original, encoding="utf-8")
        self.server._write_config_atomic = Mock(side_effect=AssertionError("writer must not run"))

        with self.assertRaisesRegex(ValueError, "invalid YAML"):
            self.server._apply_xai_oauth_config("grok-4")

        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertFalse((self.home / ".env").exists())

    def test_xai_auth_json_refuses_to_replace_other_damaged_credentials(self):
        path = self.home / "auth.json"
        original = '{"providers": {'
        path.write_text(original, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "existing credentials were left untouched"):
            self.server._save_xai_auth_json({"refresh_token": "new"})

        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_xai_bundle_restores_auth_if_later_config_write_fails(self):
        config = self.home / "config.yaml"
        config.write_text("# keep\nmodel:\n  default: old\n", encoding="utf-8")
        auth = self.home / "auth.json"
        auth.write_text('{"providers":{"xai-oauth":{"tokens":{"refresh_token":"old"}}}}\n', encoding="utf-8")
        env = self.home / ".env"
        env.write_text("LLM_MODEL=old\n", encoding="utf-8")
        before = {path: path.read_bytes() for path in (auth, config, env)}
        self.server._write_config_atomic = Mock(side_effect=OSError("config write failed"))

        with self.assertRaisesRegex(OSError, "config write failed"):
            self.server._save_xai_oauth_bundle({"refresh_token": "new"}, "grok-new")

        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_xai_bundle_restores_config_if_later_env_write_fails(self):
        config = self.home / "config.yaml"
        config.write_text("# keep\nmodel:\n  default: old\n", encoding="utf-8")
        auth = self.home / "auth.json"
        auth.write_text('{"providers":{"xai-oauth":{"tokens":{"refresh_token":"old"}}}}\n', encoding="utf-8")
        env = self.home / ".env"
        env.write_text("LLM_MODEL=old\n", encoding="utf-8")
        before = {path: path.read_bytes() for path in (auth, config, env)}
        self.server.write_env = Mock(side_effect=OSError("env write failed"))

        with self.assertRaisesRegex(OSError, "env write failed"):
            self.server._save_xai_oauth_bundle({"refresh_token": "new"}, "grok-new")

        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_xai_bundle_does_not_roll_back_file_it_never_wrote(self):
        config = self.home / "config.yaml"
        config.write_text("model:\n  default: old\n", encoding="utf-8")

        def fail_auth(_tokens):
            # The native dashboard is another process and does not hold our
            # cfg_lock. Its edit must survive this failed wrapper operation.
            config.write_text("model:\n  default: native-new\n", encoding="utf-8")
            raise OSError("auth write failed")

        self.server._save_xai_auth_json = Mock(side_effect=fail_auth)
        with self.assertRaisesRegex(OSError, "auth write failed"):
            self.server._save_xai_oauth_bundle({"refresh_token": "new"}, "grok-new")

        self.assertIn("native-new", config.read_text(encoding="utf-8"))

    def test_atlascloud_key_pins_custom_provider_with_fixed_base_url(self):
        self.server.write_config_yaml({"LLM_MODEL": "deepseek-ai/deepseek-v4-flash", "ATLASCLOUD_API_KEY": "test-key"})

        model = yaml.safe_load((self.home / "config.yaml").read_text(encoding="utf-8"))["model"]
        self.assertEqual(model["provider"], "custom")
        self.assertEqual(model["base_url"], "https://api.atlascloud.ai/v1")
        self.assertEqual(model["api_key"], "test-key")

    def test_cheaperinference_key_pins_custom_provider_with_fixed_base_url(self):
        self.server.write_config_yaml({"LLM_MODEL": "gpt-5.4-mini", "CHEAPERINFERENCE_API_KEY": "test-key"})

        model = yaml.safe_load((self.home / "config.yaml").read_text(encoding="utf-8"))["model"]
        self.assertEqual(model["provider"], "custom")
        self.assertEqual(model["base_url"], "https://api.cheaperinference.com/v1")
        self.assertEqual(model["api_key"], "test-key")

    def test_atomic_env_writer_replaces_file_with_private_permissions(self):
        path = self.home / ".env"
        path.write_text("OLD_KEY=old\n", encoding="utf-8")
        self.server.write_env(path, {"OPENROUTER_API_KEY": "new-key"})

        self.assertIn("OPENROUTER_API_KEY=new-key", path.read_text(encoding="utf-8"))
        self.assertNotIn("OLD_KEY=old", path.read_text(encoding="utf-8"))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(importlib.util.find_spec("hermes_cli") is not None, "requires the built Hermes image")
    def test_real_hermes_writer_preserves_comments(self):
        self.server._write_config_atomic = self.real_writer
        path = self.home / "config.yaml"
        path.write_text("# keep this operator note\nmodel:\n  default: old # keep model note\nmcp_servers: {}\n", encoding="utf-8")

        self.server.write_config_yaml({"LLM_MODEL": "new-model"})

        saved = path.read_text(encoding="utf-8")
        self.assertIn("# keep this operator note", saved)
        self.assertIn("# keep model note", saved)
        self.assertEqual(yaml.safe_load(saved)["model"]["default"], "new-model")


class ConfigApiSafetyTests(ConfigSafetyFixture, unittest.IsolatedAsyncioTestCase):
    async def test_xai_disconnect_waits_for_credential_lock_and_cancels_old_poll(self):
        self.server.guard = lambda _: None
        state = {"expires_at": self.server.time.time() + 10, "interval": 0,
                 "device_code": "old", "model": "grok-new", "status": "pending"}
        self.server._xai_oauth_state = state
        await self.server.cfg_lock.acquire()
        try:
            deletion = asyncio.create_task(self.server.api_oauth_xai_delete(object()))
            await asyncio.sleep(0)
            self.assertFalse(deletion.done())
        finally:
            self.server.cfg_lock.release()
        self.assertEqual((await deletion).status_code, 200)
        self.assertIsNone(self.server._xai_oauth_state)

        response = Mock(status_code=200)
        response.json.return_value = {"refresh_token": "new"}
        client = Mock(post=AsyncMock(return_value=response))
        self.server.get_http_client = lambda: client
        self.server.gw.restart = AsyncMock()
        await self.server._poll_xai_device_auth(state)
        self.assertFalse((self.home / "auth.json").exists())
        self.server.gw.restart.assert_not_awaited()

    async def test_xai_disconnect_keeps_env_if_auth_file_is_damaged(self):
        auth = self.home / "auth.json"
        auth.write_text('{"providers": {', encoding="utf-8")
        env = self.home / ".env"
        env.write_text("_MODEL_XAI_OAUTH=grok-4\n", encoding="utf-8")
        self.server.guard = lambda _: None

        response = await self.server.api_oauth_xai_delete(object())

        self.assertEqual(response.status_code, 500)
        self.assertEqual(auth.read_text(encoding="utf-8"), '{"providers": {')
        self.assertEqual(env.read_text(encoding="utf-8"), "_MODEL_XAI_OAUTH=grok-4\n")

    async def test_setup_save_does_not_change_env_when_config_is_invalid(self):
        config = self.home / "config.yaml"
        config.write_text("model: [unterminated\n", encoding="utf-8")
        env = self.home / ".env"
        env.write_text("OPENROUTER_API_KEY=old\n", encoding="utf-8")
        self.server.guard = lambda _: None

        class Request:
            async def json(self):
                return {"vars": {"LLM_MODEL": "new", "OPENROUTER_API_KEY": "new"}, "_restart": False}

        response = await self.server.api_config_put(Request())

        self.assertEqual(response.status_code, 500)
        self.assertEqual(env.read_text(encoding="utf-8"), "OPENROUTER_API_KEY=old\n")
        self.assertEqual(config.read_text(encoding="utf-8"), "model: [unterminated\n")

    async def test_setup_save_rolls_back_env_if_atomic_config_write_fails(self):
        (self.home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
        env = self.home / ".env"
        env.write_text("OPENROUTER_API_KEY=old\n", encoding="utf-8")
        self.server.guard = lambda _: None
        self.server._write_config_atomic = Mock(side_effect=OSError("write failed"))

        class Request:
            async def json(self):
                return {"vars": {"LLM_MODEL": "new", "OPENROUTER_API_KEY": "new"}, "_restart": False}

        response = await self.server.api_config_put(Request())

        self.assertEqual(response.status_code, 500)
        self.assertEqual(env.read_text(encoding="utf-8"), "OPENROUTER_API_KEY=old\n")

    async def test_reset_refuses_invalid_config_without_stopping_gateway(self):
        config = self.home / "config.yaml"
        config.write_text("model: [unterminated\n", encoding="utf-8")
        env = self.home / ".env"
        env.write_text("OPENROUTER_API_KEY=old\n", encoding="utf-8")
        self.server.guard = lambda _: None
        self.server.gw.stop = AsyncMock()

        response = await self.server.api_config_reset(object())

        self.assertEqual(response.status_code, 500)
        self.assertEqual(env.read_text(encoding="utf-8"), "OPENROUTER_API_KEY=old\n")
        self.server.gw.stop.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
