import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


os.environ.setdefault("ADMIN_PASSWORD", "test-password")
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import server  # noqa: E402


class DummyRequest:
    def __init__(self, *, path_params=None, body=None):
        self.path_params = path_params or {}
        self._body = body or {}

    async def json(self):
        return dict(self._body)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def response_payload(response):
    return json.loads(response.body.decode("utf-8"))


class HermesConfigTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / ".hermes"
        self.home.mkdir(parents=True)
        self.patch_home = patch.object(server, "HERMES_HOME", str(self.home))
        self.patch_env = patch.object(server, "ENV_FILE", self.home / ".env")
        self.patch_official = patch.object(
            server, "_codex_status_from_hermes", return_value=None
        )
        self.patch_home.start()
        self.patch_env.start()
        self.patch_official.start()

    def tearDown(self):
        self.patch_official.stop()
        self.patch_env.stop()
        self.patch_home.stop()
        self.tempdir.cleanup()

    def write_config(self, model="", provider=""):
        import yaml

        (self.home / "config.yaml").write_text(
            yaml.safe_dump({"model": {"default": model, "provider": provider}})
        )

    def write_auth(self, payload):
        (self.home / "auth.json").write_text(json.dumps(payload))

    def current_codex_auth(self):
        return {
            "credential_pool": {
                "openai-codex": [{
                    "source": "device_code",
                    "auth_type": "oauth",
                    "access_token": "access-current",
                    "refresh_token": "refresh-current",
                }]
            }
        }

    def test_write_config_preserves_default_without_llm_model(self):
        self.write_config("gpt-5.3-codex", "openai-codex")
        server.write_config_yaml({})
        model, provider, parsed = server._configured_model_from_yaml()
        self.assertTrue(parsed)
        self.assertEqual(model, "gpt-5.3-codex")
        self.assertEqual(provider, "openai-codex")

    def test_write_config_preserves_provider_without_explicit_change(self):
        self.write_config("gpt-5.3-codex", "openai-codex")
        server.write_config_yaml({"LLM_MODEL": "gpt-5.2-codex"})
        model, provider, _ = server._configured_model_from_yaml()
        self.assertEqual(model, "gpt-5.2-codex")
        self.assertEqual(provider, "openai-codex")

    def test_explicit_llm_model_updates_default(self):
        self.write_config("old-model", "openrouter")
        server.write_config_yaml({"LLM_MODEL": "new-model"})
        model, provider, _ = server._configured_model_from_yaml()
        self.assertEqual(model, "new-model")
        self.assertEqual(provider, "openrouter")

    def test_malformed_config_is_not_overwritten(self):
        path = self.home / "config.yaml"
        original = "model: [unterminated\n"
        path.write_text(original)
        with self.assertRaises(ValueError):
            server.write_config_yaml({"LLM_MODEL": "replacement"})
        self.assertEqual(path.read_text(), original)
        self.assertFalse(server.is_config_complete({}))

    def test_codex_current_credential_pool_is_complete(self):
        self.write_config("gpt-5.3-codex", "openai-codex")
        self.write_auth(self.current_codex_auth())
        self.assertTrue(server.is_config_complete({}))

    def test_legacy_codex_provider_tokens_are_complete(self):
        self.write_config("gpt-5.2-codex", "openai-codex")
        self.write_auth({
            "providers": {
                "openai-codex": {
                    "tokens": {
                        "access_token": "legacy-access",
                        "refresh_token": "legacy-refresh",
                    }
                }
            }
        })
        self.assertTrue(server.is_config_complete({}))

    def test_codex_provider_without_credentials_is_incomplete(self):
        self.write_config("gpt-5.3-codex", "openai-codex")
        self.assertFalse(server.is_config_complete({}))

    def test_codex_credentials_without_model_are_incomplete(self):
        self.write_config("", "openai-codex")
        self.write_auth(self.current_codex_auth())
        self.assertFalse(server.is_config_complete({}))

    def test_dead_codex_pool_entry_is_incomplete(self):
        auth = self.current_codex_auth()
        auth["credential_pool"]["openai-codex"][0]["last_status"] = "dead"
        self.write_config("gpt-5.3-codex", "openai-codex")
        self.write_auth(auth)
        self.assertFalse(server.is_config_complete({}))

    def test_existing_api_key_configuration_is_complete(self):
        self.assertTrue(server.is_config_complete({
            "LLM_MODEL": "openai/gpt-4o-mini",
            "OPENROUTER_API_KEY": "not-a-real-key",
        }))

    def test_existing_xai_oauth_configuration_is_complete(self):
        self.write_config("grok-4.3", "xai-oauth")
        self.write_auth({
            "providers": {
                "xai-oauth": {"tokens": {"refresh_token": "xai-refresh"}}
            }
        })
        self.assertTrue(server.is_config_complete({}))

    def test_codex_save_uses_hermes_model_api_and_pins_provider(self):
        import yaml

        self.write_auth(self.current_codex_auth())

        async def apply_model(provider, model, **kwargs):
            self.write_config(model, provider)
            return None

        request = DummyRequest(body={
            "vars": {"LLM_MODEL": "gpt-5.3-codex"},
            "_active_oauth_provider": "openai-codex",
            "_restart": False,
        })
        model_api = AsyncMock(side_effect=apply_model)
        with (
            patch.object(server, "guard", return_value=None),
            patch.object(server, "set_active_model_via_hermes", model_api),
        ):
            response = asyncio.run(server.api_config_put(request))

        self.assertEqual(response.status_code, 200)
        model_api.assert_awaited_once_with("openai-codex", "gpt-5.3-codex")
        config = yaml.safe_load((self.home / "config.yaml").read_text())
        self.assertEqual(config["model"]["provider"], "openai-codex")
        self.assertEqual(config["model"]["default"], "gpt-5.3-codex")
        self.assertNotIn("access-current", (self.home / ".env").read_text())


class HermesOAuthProxyTest(unittest.TestCase):
    def test_proxy_attaches_server_side_session_token(self):
        class FakeClient:
            def __init__(self):
                self.call = None

            async def request(self, method, url, **kwargs):
                self.call = (method, url, kwargs)
                return FakeResponse(payload={"ok": True})

        client = FakeClient()
        with (
            patch.object(server, "_get_hermes_session_token", AsyncMock(return_value="hermes-session-secret")),
            patch.object(server, "get_http_client", return_value=client),
        ):
            result = asyncio.run(server._hermes_api_json(
                "POST", "/api/providers/oauth/openai-codex/start", json_body={}
            ))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            client.call[2]["headers"],
            {server._SESSION_TOKEN_HEADER: "hermes-session-secret"},
        )

    def test_start_poll_cancel_disconnect_are_sanitized(self):
        secret = "oauth-access-token-that-must-not-leak"
        session_id = "session_12345678"

        async def fake_call(method, path, **kwargs):
            if path.endswith("/start"):
                return {
                    "session_id": session_id,
                    "user_code": "ABCD-EFGH",
                    "verification_url": "https://auth.openai.com/codex/device",
                    "expires_in": 900,
                    "poll_interval": 5,
                    "access_token": secret,
                }
            if "/poll/" in path:
                return {
                    "status": "denied",
                    "expires_at": 12345,
                    "error_message": f"denied with {secret}",
                }
            if "/sessions/" in path:
                return {"ok": True, "refresh_token": secret}
            return {"ok": True, "access_token": secret}

        with (
            patch.object(server, "guard", return_value=None),
            patch.object(server, "_hermes_api_json", AsyncMock(side_effect=fake_call)),
            patch.object(server, "_configured_model_from_yaml", return_value=("", "", True)),
        ):
            start = asyncio.run(server.api_oauth_codex_start(DummyRequest()))
            poll = asyncio.run(server.api_oauth_codex_poll(
                DummyRequest(path_params={"session_id": session_id})
            ))
            cancel = asyncio.run(server.api_oauth_codex_cancel(
                DummyRequest(path_params={"session_id": session_id})
            ))
            disconnect = asyncio.run(server.api_oauth_codex_disconnect(DummyRequest()))

        combined = b"".join(r.body for r in (start, poll, cancel, disconnect)).decode()
        self.assertNotIn(secret, combined)
        self.assertEqual(response_payload(start)["status"], "waiting")
        self.assertEqual(response_payload(poll)["status"], "denied")
        self.assertEqual(response_payload(cancel)["status"], "canceled")
        self.assertEqual(response_payload(disconnect)["status"], "not_connected")

    def test_provider_status_does_not_expose_tokens_or_session_credentials(self):
        secret = "provider-secret-token"
        upstream = {
            "providers": [{
                "id": "openai-codex",
                "status": {
                    "logged_in": True,
                    "token_preview": secret,
                    "api_key": secret,
                    "session_token": secret,
                },
            }]
        }
        with (
            patch.object(server, "guard", return_value=None),
            patch.object(server, "_hermes_api_json", AsyncMock(return_value=upstream)),
            patch.object(server, "_configured_model_from_yaml", return_value=("gpt-5.3-codex", "openai-codex", True)),
        ):
            response = asyncio.run(server.api_oauth_codex_status(DummyRequest()))
        rendered = response.body.decode()
        self.assertNotIn(secret, rendered)
        self.assertEqual(response_payload(response), {
            "available": True,
            "connected": True,
            "status": "connected",
            "model": "gpt-5.3-codex",
        })

    def test_model_list_is_allowlisted_and_filters_unavailable(self):
        payload = {
            "providers": [{
                "slug": "openai-codex",
                "models": ["gpt-5.3-codex", {"id": "gpt-5.2-codex"}],
                "unavailable_models": ["gpt-5.2-codex"],
                "access_token": "must-not-leak",
            }]
        }
        self.assertEqual(server._codex_models_public(payload), ["gpt-5.3-codex"])

    def test_upstream_error_body_is_not_forwarded_or_logged(self):
        secret = "raw-upstream-refresh-token"

        class RejectingClient:
            async def request(self, *args, **kwargs):
                return FakeResponse(400, {"detail": secret}, text=secret)

        with (
            patch.object(server, "_get_hermes_session_token", AsyncMock(return_value="session")),
            patch.object(server, "get_http_client", return_value=RejectingClient()),
        ):
            with self.assertRaises(server.HermesProxyError) as raised:
                asyncio.run(server._hermes_api_json("POST", "/api/providers/oauth/openai-codex/start"))
        self.assertNotIn(secret, raised.exception.message)


if __name__ == "__main__":
    unittest.main()
