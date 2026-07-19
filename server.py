"""
Hermes Agent — Railway admin server.

Responsibilities:
  - Admin UI / setup wizard at /setup (Starlette + Jinja, cookie-auth guarded)
  - Management API at /setup/api/* (config, status, logs, gateway, pairing)
  - Reverse proxy at / and /* → native Hermes dashboard (hermes_cli/web_server, on 127.0.0.1:9119)
  - Managed subprocesses: `hermes gateway` (agent) and `hermes dashboard` (native UI)
  - Cookie-based session auth at /login (HMAC-signed, 7-day expiry, httponly)

Auth model: Basic Auth was dropped in favor of cookies because the Hermes React
SPA's plain fetch() calls do not reliably include basic-auth creds across browsers,
and basic-auth's per-directory protection space forced separate prompts for
/setup and /. Cookies auto-include on every same-origin request, so both the
setup UI and the proxied dashboard work with a single login. The cookie signing
secret is regenerated on every process start, so any ADMIN_PASSWORD change on
Railway (which triggers a redeploy) invalidates all existing sessions.

First-visit behavior: if no provider+model config exists, GET / redirects to /setup.
Once configured, / proxies to the Hermes dashboard. A small "← Setup" widget is
injected into every proxied HTML response so users can always return to the wizard.
"""

# PEP 563 lazy annotations: keeps function/parameter type hints as strings so
# they're never evaluated at import. Avoids the startup DeprecationWarning from
# annotating against websockets.WebSocketClientProtocol (renamed in websockets
# >= 14), and is forward-compatible regardless of the installed websockets
# version. Safe here — nothing in this module introspects annotations at runtime.
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import tempfile
import time
import zipfile
from collections.abc import Mapping
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import websockets
import websockets.exceptions
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route, WebSocketRoute
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
ENV_FILE = Path(HERMES_HOME) / ".env"


def _resolve_pairing_dir() -> Path:
    """Locate the pairing store the same way hermes' get_hermes_dir() does.

    hermes resolves ``PAIRING_DIR = get_hermes_dir("platforms/pairing", "pairing")``:
    it honours the legacy ``$HERMES_HOME/pairing/`` ONLY when that dir has
    content, otherwise it uses the consolidated ``platforms/pairing/``. The rule
    changed in **v2026.7.1** — before it (v2026.6.19 and earlier) get_hermes_dir
    used a bare ``old_path.exists()``, so an *empty* ``pairing/`` (which start.sh
    used to seed on every boot) counted as "legacy in use" and both sides agreed
    on ``pairing/``. v2026.7.1 switched to ``_legacy_path_has_content()``, which
    ignores an empty stub (upstream #27602): the gateway now writes pending/
    approved files to ``platforms/pairing/`` while a hard-coded ``pairing/`` here
    would read the wrong (empty) dir — pending users vanish and approvals land
    where the gateway never looks. We mirror the exact rule so this admin panel
    and the gateway never split-brain: a *populated* legacy dir wins (preserves a
    pre-v2026.7.1 deployment's approved users with no migration), else the new
    consolidated path. Re-verify this against get_hermes_dir on the next bump.
    """
    legacy = Path(HERMES_HOME) / "pairing"
    try:
        if legacy.is_dir() and any(legacy.iterdir()):
            return legacy
    except OSError:
        # Can't inspect (e.g. permissions) — assume occupied rather than risk
        # orphaning legacy data, matching hermes' _legacy_path_has_content.
        return legacy
    return Path(HERMES_HOME) / "platforms" / "pairing"


PAIRING_DIR = _resolve_pairing_dir()
PAIRING_TTL = 3600

# Native Hermes dashboard — runs on loopback, fronted by our reverse proxy.
HERMES_DASHBOARD_HOST = "127.0.0.1"
HERMES_DASHBOARD_PORT = int(os.environ.get("HERMES_DASHBOARD_PORT", "9119"))
HERMES_DASHBOARD_URL = f"http://{HERMES_DASHBOARD_HOST}:{HERMES_DASHBOARD_PORT}"

# Header hermes' own SPA uses to present its per-process session token
# (hermes_cli/web_server.py's _SESSION_HEADER_NAME) — see
# set_active_model_via_hermes()/_get_hermes_session_token() for why our own
# server-to-server calls to the dashboard need it even on our loopback bind.
_SESSION_TOKEN_HEADER = "X-Hermes-Session-Token"

# Mirror dashboard-ref-only/auth_proxy.py: strip only `host` (httpx sets it)
# and `transfer-encoding` (httpx recomputes it from the body). Keep everything
# else — notably `authorization`, because the SPA uses Bearer tokens against
# hermes's own /api/env/reveal and OAuth endpoints, and keep `cookie` since
# some hermes endpoints read it. Aggressive stripping was masking requests in
# ways that produced spurious 401s.
HOP_BY_HOP = {"host", "transfer-encoding"}

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(16)
    print(f"[server] Admin credentials — username: {ADMIN_USERNAME}  password: {ADMIN_PASSWORD}", flush=True)
else:
    print(f"[server] Admin username: {ADMIN_USERNAME}", flush=True)

# ── Env var registry ──────────────────────────────────────────────────────────
# (key, label, category, is_secret)
ENV_VARS = [
    ("LLM_MODEL",               "Model",                    "model",     False),
    ("OPENROUTER_API_KEY",       "OpenRouter",               "provider",  True),
    ("DEEPSEEK_API_KEY",         "DeepSeek",                 "provider",  True),
    ("DASHSCOPE_API_KEY",        "Qwen Cloud (DashScope)",   "provider",  True),
    ("GLM_API_KEY",              "GLM / Z.AI",               "provider",  True),
    ("KIMI_API_KEY",             "Kimi",                     "provider",  True),
    ("MINIMAX_API_KEY",          "MiniMax",                  "provider",  True),
    ("HF_TOKEN",                 "Hugging Face",             "provider",  True),
    # Added in v2026.4.23+ (hermes v0.11.0+). All plain API-key auth — hermes
    # auto-routes by env-var presence, no extra config needed on our side.
    # OAuth-based providers (xAI Grok SuperGrok, Gemini CLI, Qwen OAuth, Claude Code)
    # are set up via the dashboard's Keys tab or HERMES_AUTH_JSON_BOOTSTRAP.
    ("NVIDIA_API_KEY",           "NVIDIA NIM",               "provider",  True),
    ("ARCEEAI_API_KEY",          "Arcee AI",                 "provider",  True),
    ("STEPFUN_API_KEY",          "Step Plan",                "provider",  True),
    ("GEMINI_API_KEY",           "Google AI Studio",         "provider",  True),
    ("NOVITA_API_KEY",           "NovitaAI",                 "provider",  True),
    ("FIREWORKS_API_KEY",        "Fireworks AI",             "provider",  True),
    ("ANTHROPIC_API_KEY",        "Anthropic (Claude)",       "provider",  True),
    ("XAI_API_KEY",              "xAI",                      "provider",  True),
    ("AWS_ACCESS_KEY_ID",        "AWS Access Key ID",        "provider",  True),
    ("AWS_SECRET_ACCESS_KEY",    "AWS Secret Access Key",    "bedrock",   True),
    ("AWS_DEFAULT_REGION",       "AWS Region",               "bedrock",   False),
    ("COPILOT_GITHUB_TOKEN",     "GitHub Copilot",           "provider",  True),
    ("GMI_API_KEY",              "GMI Cloud",                "provider",  True),
    ("OPENCODE_ZEN_API_KEY",     "OpenCode Zen",             "provider",  True),
    ("OPENCODE_GO_API_KEY",      "OpenCode Go",              "provider",  True),
    ("KILOCODE_API_KEY",         "Kilo Code",                "provider",  True),
    ("OLLAMA_API_KEY",           "Ollama Cloud",             "provider",  True),
    ("AZURE_FOUNDRY_API_KEY",    "Azure Foundry key",        "provider",  True),
    ("AZURE_FOUNDRY_BASE_URL",   "Azure Foundry URL",        "azure",     False),
    # Custom OpenAI-compatible endpoint — one slot; more via Hermes dashboard.
    # Only the API key is in category "provider" so PROVIDER_KEYS / is_config_complete
    # only trigger when an actual key is present, not just a base URL.
    ("CUSTOM_PROVIDER_API_KEY",  "Custom Provider key",      "provider",  True),
    ("CUSTOM_PROVIDER_BASE_URL", "Custom Provider base URL", "custom",    False),
    ("CUSTOM_PROVIDER_NAME",     "Custom Provider name",     "custom",    False),
    ("PARALLEL_API_KEY",         "Parallel (search)",        "tool",      True),
    ("FIRECRAWL_API_KEY",        "Firecrawl (scrape)",       "tool",      True),
    ("TAVILY_API_KEY",           "Tavily (search)",          "tool",      True),
    ("FAL_KEY",                  "FAL (image gen)",          "tool",      True),
    ("BROWSERBASE_API_KEY",      "Browserbase key",          "tool",      True),
    ("BROWSERBASE_PROJECT_ID",   "Browserbase project",      "tool",      False),
    ("GITHUB_TOKEN",             "GitHub token",             "tool",      True),
    ("VOICE_TOOLS_OPENAI_KEY",   "OpenAI (voice/TTS)",       "tool",      True),
    ("HONCHO_API_KEY",           "Honcho (memory)",          "tool",      True),
    ("TELEGRAM_BOT_TOKEN",       "Bot Token",                "telegram",  True),
    ("TELEGRAM_ALLOWED_USERS",   "Allowed User IDs",         "telegram",  False),
    ("DISCORD_BOT_TOKEN",        "Bot Token",                "discord",   True),
    ("DISCORD_ALLOWED_USERS",    "Allowed User IDs",         "discord",   False),
    ("SLACK_BOT_TOKEN",          "Bot Token (xoxb-...)",     "slack",     True),
    ("SLACK_APP_TOKEN",          "App Token (xapp-...)",     "slack",     True),
    ("WHATSAPP_ENABLED",         "Enable WhatsApp",          "whatsapp",  False),
    ("EMAIL_ADDRESS",            "Email Address",            "email",     False),
    ("EMAIL_PASSWORD",           "Email Password",           "email",     True),
    ("EMAIL_IMAP_HOST",          "IMAP Host",                "email",     False),
    ("EMAIL_SMTP_HOST",          "SMTP Host",                "email",     False),
    ("MATTERMOST_URL",           "Server URL",               "mattermost",False),
    ("MATTERMOST_TOKEN",         "Bot Token",                "mattermost",True),
    ("MATRIX_HOMESERVER",        "Homeserver URL",           "matrix",    False),
    ("MATRIX_ACCESS_TOKEN",      "Access Token",             "matrix",    True),
    ("MATRIX_USER_ID",           "User ID",                  "matrix",    False),
    ("GATEWAY_ALLOW_ALL_USERS",  "Allow all users",          "gateway",   False),
    ("ADMIN_USERNAME",           "Admin username",           "admin",     False),
    ("ADMIN_PASSWORD",           "Admin password",           "admin",     True),
]

SECRET_KEYS  = {k for k, _, _, s in ENV_VARS if s}
PROVIDER_KEYS = [k for k, _, c, _ in ENV_VARS if c == "provider"]

# Maps our own provider-key env var to hermes' OWN canonical provider id
# (hermes_cli/auth.py PROVIDER_REGISTRY, verified against v2026.7.1). Used by
# set_active_model_via_hermes() to pin an explicit model.provider via hermes'
# own POST /api/model/set instead of leaving config.yaml on "auto" once 2+
# provider keys exist in .env — see write_config_yaml()'s docstring for why
# "auto" alone is unsafe with multiple providers configured. Several ids are
# non-obvious renames upstream (dashscope->alibaba, glm->zai, kimi->kimi-coding,
# hf->huggingface, ollama->ollama-cloud) — re-verify every entry against
# hermes_cli/auth.py on a Hermes version bump (same audit as the WS allowlist).
HERMES_PROVIDER_IDS = {
    "OPENROUTER_API_KEY":    "openrouter",
    "DEEPSEEK_API_KEY":      "deepseek",
    "DASHSCOPE_API_KEY":     "alibaba",       # "Qwen Cloud" in hermes' own UI
    "GLM_API_KEY":           "zai",           # "Z.AI / GLM"
    "KIMI_API_KEY":          "kimi-coding",
    "MINIMAX_API_KEY":       "minimax",
    "HF_TOKEN":              "huggingface",
    "NVIDIA_API_KEY":        "nvidia",
    "ARCEEAI_API_KEY":       "arcee",
    "STEPFUN_API_KEY":       "stepfun",
    "GEMINI_API_KEY":        "gemini",
    "ANTHROPIC_API_KEY":     "anthropic",
    "XAI_API_KEY":           "xai",
    "AWS_ACCESS_KEY_ID":     "bedrock",
    "COPILOT_GITHUB_TOKEN":  "copilot",
    "GMI_API_KEY":           "gmi",
    "OPENCODE_ZEN_API_KEY":  "opencode-zen",
    "OPENCODE_GO_API_KEY":   "opencode-go",
    "KILOCODE_API_KEY":      "kilocode",
    "OLLAMA_API_KEY":        "ollama-cloud",
    "AZURE_FOUNDRY_API_KEY": "azure-foundry",
    # These three are NOT in hermes' own PROVIDER_REGISTRY — verified against
    # BOTH hermes_cli/auth.py (resolve_provider(), used by the CLI/"auto"
    # env-var auto-detect loop) AND hermes_cli/runtime_provider.py
    # (resolve_runtime_provider(), what the gateway/embedded Chat tab actually
    # call at agent-init) at v2026.7.1. Neither ever discovers them: "auto"
    # only scans PROVIDER_REGISTRY's known env vars (these aren't in it, so
    # they're invisible to it, full stop), and pinning one of these strings
    # as an explicit provider id raises "Unknown provider '<id>'" — both
    # produce a dead agent ("No inference provider configured" / "Unknown
    # provider"), confirmed live for a 9Router custom-endpoint deployment.
    # The only way any of them work is the same mechanism hermes' OWN
    # dashboard uses for a self-hosted/aggregator endpoint: provider="custom"
    # plus an explicit base_url + api_key written onto model.* directly
    # (hermes_cli/runtime_provider.py's bare-"custom" trust path reads
    # model.base_url/model.api_key from the model block — it does NOT consult
    # config.yaml's custom_providers[] list for this, that list is display/
    # bookkeeping only). See CUSTOM_STYLE_BASE_URLS and
    # set_active_model_via_hermes(). Re-verify FIREWORKS_API_KEY/NOVITA_API_KEY
    # base URLs against those providers' own docs (not hermes') if they ever
    # change their API surface.
    "CUSTOM_PROVIDER_API_KEY": "custom",   # base_url is user-supplied (CUSTOM_PROVIDER_BASE_URL) — any OpenAI-compatible endpoint, e.g. 9Router
    "FIREWORKS_API_KEY":       "custom",
    "NOVITA_API_KEY":          "custom",
}

# Fixed base URLs for the "custom"-style providers above whose credential is a
# plain API key against a well-known OpenAI-compatible endpoint. Absent here
# (CUSTOM_PROVIDER_API_KEY) means the base_url is user-supplied instead — see
# CUSTOM_PROVIDER_BASE_URL.
CUSTOM_STYLE_BASE_URLS = {
    "FIREWORKS_API_KEY": "https://api.fireworks.ai/inference/v1",
    "NOVITA_API_KEY":    "https://api.novita.ai/openai/v1",
}

# Every ENV_VARS "provider" key pinned to the literal "custom" id above.
# Computed, not hand-maintained, so a future provider added to
# HERMES_PROVIDER_IDS with value "custom" is automatically covered by both
# api_config_put()'s pin call and write_config_yaml()'s fallback below —
# no other code needs to change.
HERMES_CUSTOM_STYLE_KEYS = {k for k, v in HERMES_PROVIDER_IDS.items() if v == "custom"}

CHANNEL_MAP  = {
    "Telegram":    "TELEGRAM_BOT_TOKEN",
    "Discord":     "DISCORD_BOT_TOKEN",
    "Slack":       "SLACK_BOT_TOKEN",
    "WhatsApp":    "WHATSAPP_ENABLED",
    "Email":       "EMAIL_ADDRESS",
    "Mattermost":  "MATTERMOST_TOKEN",
    "Matrix":      "MATRIX_ACCESS_TOKEN",
}


# ── .env helpers ──────────────────────────────────────────────────────────────
def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def write_config_yaml(data: dict[str, str], *, reset_model: bool = False) -> None:
    """Write config.yaml — deep-merge template defaults with any existing user/cron-managed sections.

    Previously this overwrote ``$HERMES_HOME/config.yaml`` with a hardcoded template
    body on every boot, silently erasing user-managed top-level keys. The most
    common casualty is ``mcp_servers`` — Hermes reads downstream MCP servers
    *only* from this file (see ``hermes_cli/mcp_config.py:_get_mcp_servers``), so
    the wipe broke ``hermes mcp add/test/list`` state across every container
    restart and required hand-restoration after each redeploy.

    The fix: load the existing file if any, apply the deployment-managed keys
    (``model.default``, ``model.provider``, ``terminal``, ``agent``, ``data_dir``)
    on top, and write the merged result. Unknown top-level keys (``mcp_servers``,
    custom skill config, etc.) are preserved verbatim.
    """
    import yaml  # hermes-agent already pulls pyyaml; deferred import keeps cold start light

    model = data.get("LLM_MODEL", "")
    config_path = Path(HERMES_HOME) / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if config_path.exists():
        try:
            with config_path.open() as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except (yaml.YAMLError, OSError):
            # Treat unparseable as absent — we'll overwrite with template defaults.
            existing = {}

    merged = dict(existing)

    # Deployment-managed (always authoritative — these reflect the runtime env).
    if reset_model:
        # Config reset: wipe the model block to a clean slate. Preserving the old
        # provider/base_url here would leave stale routing behind (e.g. a lingering
        # `base_url: https://openrouter.ai/api/v1` that misroutes the next provider
        # the user configures). Everything else — hermes tuning defaults,
        # mcp_servers — is still deep-merged through untouched below.
        merged_model = {"default": ""}
    else:
        merged_model = dict(merged.get("model") if isinstance(merged.get("model"), dict) else {})
        merged_model["default"] = model
        current_provider = str(merged_model.get("provider") or "").strip()
        # Only default to "auto" on a config that has never had a provider
        # pinned. Once a provider is set explicitly — either by
        # set_active_model_via_hermes() below (which delegates to hermes' own
        # POST /api/model/set) or by hermes' own dashboard — PRESERVE it here.
        # This function runs on every gateway start (Gateway.start() calls it
        # fresh from .env every time a subprocess spawns), so unconditionally
        # forcing "auto" whenever any key is present — the old behavior —
        # would silently revert an explicit pin back to ambiguous "auto" on
        # the very next restart. "auto" resolves by scanning hermes' own
        # PROVIDER_REGISTRY in its OWN dict-insertion order and returning the
        # first provider with a present env var — independent of which model
        # string is configured. With exactly one provider key present this is
        # harmless (only one possible match), but with two or more configured
        # (e.g. minimax + nvidia) it silently pairs whichever provider sorts
        # first in that registry with a model string that may belong to a
        # DIFFERENT provider — the exact bug that made hermes route a
        # deepseek-v4-pro (NVIDIA) request through MiniMax's own API with an
        # unrecognized model name, producing a self-contradictory system
        # prompt and a "confused" identity response.
        if not current_provider:
            named_key = next(
                (k for k in PROVIDER_KEYS if k not in HERMES_CUSTOM_STYLE_KEYS and data.get(k)),
                None,
            )
            custom_style_key = next((k for k in HERMES_CUSTOM_STYLE_KEYS if data.get(k)), None)
            if named_key:
                merged_model["provider"] = "auto"
                current_provider = "auto"
            elif custom_style_key:
                # CUSTOM_PROVIDER_API_KEY / FIREWORKS_API_KEY / NOVITA_API_KEY are
                # NOT in hermes' own PROVIDER_REGISTRY (see HERMES_PROVIDER_IDS'
                # comment) — "auto" can never discover them, so defaulting to
                # "auto" here (the old behavior) left the agent with no usable
                # provider whenever one of these was the ONLY key configured.
                # This is the synchronous safety net for the async
                # set_active_model_via_hermes() pin in api_config_put(): this
                # function also runs directly from .env on every gateway boot
                # (Gateway.start()), so it must independently produce a
                # resolvable config even if that pin call never ran or failed.
                merged_model["provider"] = "custom"
                merged_model["base_url"] = (
                    CUSTOM_STYLE_BASE_URLS.get(custom_style_key)
                    or data.get("CUSTOM_PROVIDER_BASE_URL", "").strip()
                )
                merged_model["api_key"] = data.get(custom_style_key, "").strip()
                current_provider = "custom"
        # A known built-in provider (openrouter, minimax, nvidia, …) resolves
        # its endpoint + credentials from the provider itself, so any inline
        # model.base_url/api_key/api_mode is stale. base_url "takes precedence
        # over provider" upstream (hermes_cli/config.py), so a leftover — e.g.
        # a former `base_url: https://openrouter.ai/api/v1` from the hermes
        # dashboard — silently misroutes EVERY provider you later switch to
        # (all calls forced to that endpoint regardless of the active model).
        # Strip them here, mirroring hermes' own clear_model_endpoint_credentials()
        # on a switch-away-from-custom. Skipped only for "custom"/"local" —
        # hermes' own convention for a user-supplied (or fixed-URL aggregator)
        # endpoint that legitimately needs its own base_url/api_key set
        # directly on model.* (see the "custom_style_key" branch above —
        # hermes' runtime resolver reads model.base_url/api_key directly, NOT
        # the separate custom_providers[] block below, which is display-only).
        if current_provider and current_provider.lower() not in ("custom", "local"):
            for _stale in ("base_url", "api_key", "api", "api_mode"):
                merged_model.pop(_stale, None)
    merged["model"] = merged_model

    merged_terminal = dict(merged.get("terminal") if isinstance(merged.get("terminal"), dict) else {})
    merged_terminal["backend"] = "local"
    merged_terminal["timeout"] = 60
    merged_terminal["cwd"] = "/tmp"
    merged["terminal"] = merged_terminal

    merged_agent = dict(merged.get("agent") if isinstance(merged.get("agent"), dict) else {})
    merged_agent.setdefault("max_iterations", 50)
    merged["agent"] = merged_agent

    merged["data_dir"] = HERMES_HOME

    # Custom OpenAI-compatible endpoint — write custom_providers block when configured,
    # remove it when not (safe on Railway where users don't hand-edit config.yaml).
    custom_base_url = data.get("CUSTOM_PROVIDER_BASE_URL", "").strip()
    if custom_base_url:
        raw_name = data.get("CUSTOM_PROVIDER_NAME", "").strip() or custom_base_url
        # Sanitise to a valid hermes provider name (lowercase alphanumeric + hyphens).
        sanitized_name = re.sub(r"[^a-z0-9-]", "-", raw_name.lower()).strip("-") or "custom"
        merged["custom_providers"] = [{
            "name": sanitized_name,
            "base_url": custom_base_url,
            "key_env": "CUSTOM_PROVIDER_API_KEY",
        }]
    else:
        merged.pop("custom_providers", None)

    with config_path.open("w") as f:
        yaml.safe_dump(merged, f, sort_keys=False, default_flow_style=False)


def build_hermes_env() -> dict[str, str]:
    """Merge OS env + HERMES_HOME + .env file contents for a hermes subprocess.

    .env values take priority over Railway env vars. We build the env this way
    so hermes's own dotenv loading (which reads the same file) doesn't shadow
    our values. Shared by every hermes subprocess we spawn (gateway, dashboard)
    — a subprocess started without this (e.g. via a bare env=None, which just
    inherits our own process env from container boot) never sees provider keys
    saved later through the setup wizard, since those only ever land in
    HERMES_HOME/.env, not in our own os.environ.
    """
    env = {**os.environ, "HERMES_HOME": HERMES_HOME}
    env.update(read_env(ENV_FILE))
    return env


def _env_flag_enabled(value: str | None) -> bool:
    """Match the truthy spellings accepted by pinned Hermes v2026.7.1."""
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _coerce_port(value: str | None, default: int) -> int:
    """Mirror Hermes' malformed-port fallback for the optional API adapter."""
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _config_flag_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return _env_flag_enabled(str(value))


def _read_hermes_config() -> dict:
    """Read config.yaml for listener settings that env overrides cannot disable."""
    config_path = Path(HERMES_HOME) / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    try:
        loaded = yaml.safe_load(config_path.read_text())
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, UnicodeError, yaml.YAMLError):
        # Gateway config loading is fail-open too; a malformed/unreadable YAML
        # file is diagnosed by Hermes itself rather than breaking the Setup UI.
        return {}


def _yaml_api_server_settings(config: Mapping[str, object]) -> tuple[bool, int]:
    """Mirror the pinned gateway's YAML merge order for the API listener."""
    merged: dict = {}
    merged_extra: dict = {}

    gateway = config.get("gateway")
    gateway_platforms = gateway.get("platforms") if isinstance(gateway, Mapping) else None
    platforms = config.get("platforms")
    for source in (gateway_platforms, platforms):
        block = source.get("api_server") if isinstance(source, Mapping) else None
        if not isinstance(block, Mapping):
            continue
        extra = block.get("extra")
        if isinstance(extra, Mapping):
            merged_extra.update(extra)
        merged.update(block)

    # A top-level api_server.enabled is bridged after the nested platform maps
    # and therefore wins for enablement in Hermes v2026.7.1.
    top_level = config.get("api_server")
    if isinstance(top_level, Mapping) and "enabled" in top_level:
        merged["enabled"] = top_level["enabled"]

    return (
        _config_flag_enabled(merged.get("enabled", False)),
        _coerce_port(merged_extra.get("port"), 8642),
    )


def evaluate_port_preflight(
    *,
    process_env: Mapping[str, str] | None = None,
    hermes_env: Mapping[str, str] | None = None,
    hermes_config: Mapping[str, object] | None = None,
) -> dict:
    """Return a secret-free view of ports that must be unique in this container.

    ``PORT`` belongs to the public Starlette server, while the dashboard and
    optional Hermes API adapter are internal listeners. The API adapter's env
    must come from ``build_hermes_env()`` because values saved in the Setup UI's
    persistent .env intentionally override Railway service variables.
    """
    process_env = os.environ if process_env is None else process_env
    hermes_env = build_hermes_env() if hermes_env is None else hermes_env
    hermes_config = _read_hermes_config() if hermes_config is None else hermes_config

    config_api_enabled, config_api_port = _yaml_api_server_settings(hermes_config)
    api_server_env_enabled = (
        _env_flag_enabled(hermes_env.get("API_SERVER_ENABLED"))
        or bool(str(hermes_env.get("API_SERVER_KEY", "")).strip())
    )
    api_port = config_api_port
    raw_env_api_port = hermes_env.get("API_SERVER_PORT")
    if api_server_env_enabled and raw_env_api_port:
        # A valid env port overrides YAML. A malformed value is ignored by the
        # gateway loader, leaving the YAML/default port in effect. Hermes only
        # enters this override branch when the env itself activates the API.
        try:
            api_port = int(raw_env_api_port)
        except (TypeError, ValueError):
            pass

    ports = {
        "web": int(process_env.get("PORT", "8080")),
        "dashboard": int(process_env.get("HERMES_DASHBOARD_PORT", "9119")),
        "api_server": api_port,
    }
    # Upstream enables this adapter when either the explicit flag OR an API key
    # is present. Keep the preflight's activation rule identical so a keyed API
    # server cannot bypass collision detection.
    api_server_enabled = config_api_enabled or api_server_env_enabled
    conflicts: list[dict] = []

    if ports["web"] == ports["dashboard"]:
        conflicts.append({
            "services": ["web", "dashboard"],
            "port": ports["web"],
            "message": (
                f"The public web server and Hermes dashboard both use port {ports['web']}."
            ),
            "remediation": (
                "Set Railway PORT=8080 and HERMES_DASHBOARD_PORT=9119, then redeploy."
            ),
        })

    if api_server_enabled:
        if ports["web"] == ports["api_server"]:
            conflicts.append({
                "services": ["web", "api_server"],
                "port": ports["web"],
                "message": (
                    f"The public web server and Hermes API Server both use port {ports['web']}."
                ),
                "remediation": (
                    "Set Railway PORT=8080 (recommended), or set API_SERVER_PORT to an "
                    "unused internal port, then redeploy."
                ),
            })
        if ports["dashboard"] == ports["api_server"]:
            conflicts.append({
                "services": ["dashboard", "api_server"],
                "port": ports["dashboard"],
                "message": (
                    f"The Hermes dashboard and Hermes API Server both use port "
                    f"{ports['dashboard']}."
                ),
                "remediation": (
                    "Set HERMES_DASHBOARD_PORT=9119 and API_SERVER_PORT=8642, "
                    "then redeploy."
                ),
            })

    api_server_conflicts = any(
        "api_server" in conflict["services"] for conflict in conflicts
    )
    api_server_action = None
    if api_server_conflicts:
        api_server_action = (
            "block_gateway_start"
            if config_api_enabled
            else "disable_for_gateway_start"
        )
    return {
        "ok": not conflicts,
        "ports": ports,
        "api_server_enabled": api_server_enabled,
        "api_server_config_enabled": config_api_enabled,
        "api_server_action": api_server_action,
        "conflicts": conflicts,
    }


def prepare_gateway_env(
    *,
    process_env: Mapping[str, str] | None = None,
    hermes_env: Mapping[str, str] | None = None,
    hermes_config: Mapping[str, object] | None = None,
) -> tuple[dict[str, str], dict]:
    """Prepare a gateway env without persisting an automatic port remap.

    For env-enabled API conflicts, disable only that adapter for this subprocess
    run so messaging can still start. A config.yaml-enabled conflict is marked
    as non-suppressible for Gateway.start() to block without mutating persisted
    configuration. Setup keeps showing the actionable conflict either way.
    """
    gateway_env = dict(build_hermes_env() if hermes_env is None else hermes_env)
    status = evaluate_port_preflight(
        process_env=process_env,
        hermes_env=gateway_env,
        hermes_config=hermes_config,
    )
    if status["api_server_action"] == "disable_for_gateway_start":
        gateway_env["API_SERVER_ENABLED"] = "false"
        # Hermes also treats a non-empty key as implicit enablement, so the
        # transient subprocess env must hide both activation paths.
        gateway_env["API_SERVER_KEY"] = ""
    return gateway_env, status


def write_env(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cat_order = ["model", "provider", "bedrock", "azure", "custom", "tool",
                 "telegram", "discord", "slack", "whatsapp",
                 "email", "mattermost", "matrix", "gateway", "admin"]
    cat_labels = {
        "model": "Model", "provider": "Providers",
        "bedrock": "AWS Bedrock", "azure": "Azure Foundry",
        "custom": "Custom Endpoint", "tool": "Tools",
        "telegram": "Telegram", "discord": "Discord", "slack": "Slack",
        "whatsapp": "WhatsApp", "email": "Email",
        "mattermost": "Mattermost", "matrix": "Matrix", "gateway": "Gateway",
        "admin": "Admin",
    }
    key_cat = {k: c for k, _, c, _ in ENV_VARS}
    grouped: dict[str, list[str]] = {c: [] for c in cat_order}
    grouped["other"] = []

    for k, v in data.items():
        if not v:
            continue
        cat = key_cat.get(k, "other")
        grouped.setdefault(cat, []).append(f"{k}={v}")

    lines: list[str] = []
    for cat in cat_order:
        entries = sorted(grouped.get(cat, []))
        if entries:
            lines.append(f"# {cat_labels.get(cat, cat)}")
            lines.extend(entries)
            lines.append("")
    if grouped["other"]:
        lines.append("# Other")
        lines.extend(sorted(grouped["other"]))
        lines.append("")

    path.write_text("\n".join(lines))


# ── xAI Grok SuperGrok OAuth (Device Code — RFC 8628) ───────────────────────
# xAI's OIDC discovery at https://auth.x.ai/.well-known/openid-configuration
# declares device_authorization_endpoint, so Device Code flow works without
# any redirect URL. The client_id matches hermes's own Grok CLI credential.
_XAI_CLIENT_ID   = "b1a00492-073a-47ea-816f-4c329264a828"
_XAI_SCOPE       = "openid profile email offline_access grok-cli:access api:access"
_XAI_DEVICE_URL  = "https://auth.x.ai/oauth2/device/code"
_XAI_TOKEN_URL   = "https://auth.x.ai/oauth2/token"
_XAI_GRANT_TYPE  = "urn:ietf:params:oauth:grant-type:device_code"

_xai_oauth_state: dict | None = None  # one auth at a time (single-user deployment)


def _has_xai_oauth_tokens() -> bool:
    """True when auth.json contains a valid xAI OAuth refresh token."""
    auth_path = Path(HERMES_HOME) / "auth.json"
    if not auth_path.exists():
        return False
    try:
        data = json.loads(auth_path.read_text())
        tokens = data.get("providers", {}).get("xai-oauth", {}).get("tokens", {})
        return bool(isinstance(tokens, dict) and tokens.get("refresh_token"))
    except Exception:
        return False


def _save_xai_auth_json(tokens: dict) -> None:
    """Write xAI OAuth tokens to auth.json in hermes's expected format."""
    auth_path = Path(HERMES_HOME) / "auth.json"
    existing: dict = {}
    if auth_path.exists():
        try:
            existing = json.loads(auth_path.read_text())
        except Exception:
            pass
    if not isinstance(existing, dict):
        existing = {}

    providers = existing.setdefault("providers", {})
    providers["xai-oauth"] = {
        "tokens": tokens,
        "auth_mode": "oauth_device",
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "discovery": {
            "authorization_endpoint": "https://auth.x.ai/oauth2/authorize",
            "token_endpoint": _XAI_TOKEN_URL,
        },
        "redirect_uri": "",
    }
    existing["active_provider"] = "xai-oauth"
    existing["version"] = 2
    existing["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    auth_path.write_text(json.dumps(existing, indent=2) + "\n")
    try:
        auth_path.chmod(0o600)
    except Exception:
        pass


def _apply_xai_oauth_config(model: str) -> None:
    """Write config.yaml with provider=xai-oauth and the chosen model."""
    import yaml
    config_path = Path(HERMES_HOME) / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if config_path.exists():
        try:
            with config_path.open() as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            pass

    merged = dict(existing)
    merged_model = dict(merged.get("model") if isinstance(merged.get("model"), dict) else {})
    if model:
        merged_model["default"] = model
    merged_model["provider"] = "xai-oauth"
    merged["model"] = merged_model

    merged_terminal = dict(merged.get("terminal") if isinstance(merged.get("terminal"), dict) else {})
    merged_terminal.setdefault("backend", "local")
    merged_terminal.setdefault("timeout", 60)
    merged_terminal.setdefault("cwd", "/tmp")
    merged["terminal"] = merged_terminal

    merged_agent = dict(merged.get("agent") if isinstance(merged.get("agent"), dict) else {})
    merged_agent.setdefault("max_iterations", 50)
    merged["agent"] = merged_agent
    merged["data_dir"] = HERMES_HOME

    with config_path.open("w") as f:
        yaml.safe_dump(merged, f, sort_keys=False, default_flow_style=False)

    # Persist LLM_MODEL and track the per-provider model so the setup UI can
    # display it alongside the xAI entry in the "Configured Providers" list.
    if model:
        existing_env = read_env(ENV_FILE)
        existing_env["LLM_MODEL"] = model
        existing_env["_MODEL_XAI_OAUTH"] = model
        write_env(ENV_FILE, existing_env)


async def _poll_xai_device_auth(state: dict) -> None:
    """Background task: poll xAI token endpoint until authorized or expired."""
    client = get_http_client()
    while time.time() < state["expires_at"]:
        await asyncio.sleep(state["interval"])
        try:
            resp = await client.post(
                _XAI_TOKEN_URL,
                data={
                    "grant_type": _XAI_GRANT_TYPE,
                    "device_code": state["device_code"],
                    "client_id": _XAI_CLIENT_ID,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=httpx.Timeout(15.0),
            )
        except Exception as e:
            print(f"[xai-oauth] poll error: {e!r}", flush=True)
            continue

        if resp.status_code == 200:
            try:
                tokens = resp.json()
            except Exception:
                state["status"] = "error"
                state["error"] = "Invalid token response from xAI"
                return
            _save_xai_auth_json(tokens)
            _apply_xai_oauth_config(state.get("model", ""))
            state["status"] = "authorized"
            print("[xai-oauth] authorized — restarting gateway", flush=True)
            asyncio.create_task(gw.restart())
            return

        try:
            err_data = resp.json()
        except Exception:
            err_data = {}
        error = err_data.get("error", "")

        if error == "authorization_pending":
            continue
        elif error == "slow_down":
            state["interval"] = min(state["interval"] + 5, 30)
        else:
            state["status"] = "error"
            state["error"] = err_data.get("error_description", error) or error or "Unknown error"
            print(f"[xai-oauth] failed: {error}", flush=True)
            return

    state["status"] = "expired"
    print("[xai-oauth] device code expired", flush=True)


async def api_oauth_xai_delete(request: Request) -> Response:
    global _xai_oauth_state
    if err := guard(request):
        return err
    auth_path = Path(HERMES_HOME) / "auth.json"
    if auth_path.exists():
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8"))
            data.get("providers", {}).pop("xai-oauth", None)
            if data.get("active_provider") == "xai-oauth":
                data.pop("active_provider", None)
            auth_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
    env = read_env(ENV_FILE)
    env.pop("_MODEL_XAI_OAUTH", None)
    write_env(ENV_FILE, env)
    _xai_oauth_state = None
    return JSONResponse({"ok": True})


async def api_oauth_xai_start(request: Request) -> Response:
    global _xai_oauth_state
    if err := guard(request):
        return err

    try:
        body = await request.json()
    except Exception:
        body = {}
    model = str(body.get("model", "")).strip()

    client = get_http_client()
    try:
        resp = await client.post(
            _XAI_DEVICE_URL,
            data={"client_id": _XAI_CLIENT_ID, "scope": _XAI_SCOPE},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=httpx.Timeout(15.0),
        )
    except Exception as e:
        return JSONResponse({"error": f"Could not reach xAI: {e}"}, status_code=502)

    if resp.status_code != 200:
        return JSONResponse(
            {"error": f"xAI returned {resp.status_code}: {resp.text[:200]}"},
            status_code=502,
        )

    try:
        data = resp.json()
    except Exception:
        return JSONResponse({"error": "Invalid response from xAI"}, status_code=502)

    _xai_oauth_state = {
        "device_code": data["device_code"],
        "user_code": data["user_code"],
        "verification_uri": data.get("verification_uri_complete") or data["verification_uri"],
        "expires_at": time.time() + data.get("expires_in", 900),
        "interval": max(data.get("interval", 5), 5),
        "status": "pending",
        "model": model,
    }
    asyncio.create_task(_poll_xai_device_auth(_xai_oauth_state))

    return JSONResponse({
        "user_code": data["user_code"],
        "verification_uri": _xai_oauth_state["verification_uri"],
        "expires_in": data.get("expires_in", 900),
    })


async def api_oauth_xai_status(request: Request) -> Response:
    if err := guard(request):
        return err
    if _xai_oauth_state is None:
        # No active flow — check if a previous session left valid tokens.
        if _has_xai_oauth_tokens():
            return JSONResponse({"status": "authorized"})
        return JSONResponse({"status": "none"})
    return JSONResponse({
        "status": _xai_oauth_state["status"],
        "error": _xai_oauth_state.get("error", ""),
    })


def is_config_complete(data: dict[str, str] | None = None) -> bool:
    """Single source of truth for 'ready to run the gateway'.

    Used by: GET / redirect, auto_start on boot, admin API status.
    """
    if data is None:
        data = read_env(ENV_FILE)
    has_model = bool(data.get("LLM_MODEL"))
    has_provider = any(data.get(k) for k in PROVIDER_KEYS) or _has_xai_oauth_tokens()
    return has_model and has_provider


def mask(data: dict[str, str]) -> dict[str, str]:
    return {
        k: (v[:8] + "***" if len(v) > 8 else "***") if k in SECRET_KEYS and v else v
        for k, v in data.items()
    }


def unmask(new: dict[str, str], existing: dict[str, str]) -> dict[str, str]:
    return {
        k: (existing.get(k, "") if k in SECRET_KEYS and v.endswith("***") else v)
        for k, v in new.items()
    }


# ── Auth (cookie-based) ───────────────────────────────────────────────────────
# We use HMAC-signed cookies instead of HTTP Basic Auth because:
#   1. Basic auth's per-directory protection space means browsers cache creds
#      for /setup/* separately from /*, forcing re-prompt on navigation.
#   2. Browser behavior for sending Basic auth on XHR/fetch is inconsistent;
#      the Hermes React SPA's plain fetch() calls don't reliably include it,
#      causing every proxied API call to 401.
# Cookies are auto-included on every same-origin request (navigation + XHR)
# so both the setup UI and the proxied Hermes dashboard work with one login.
#
# The SECRET is regenerated on every process start. That means any ADMIN_PASSWORD
# change via Railway → redeploy → all existing cookies invalidate → users re-login.
import hashlib as _hashlib
import hmac as _hmac
from urllib.parse import quote as _url_quote, urlparse as _urlparse

COOKIE_NAME = "hermes_auth"
COOKIE_MAX_AGE = 7 * 86400  # 7 days
COOKIE_SECRET = secrets.token_bytes(32)

# Public paths — no auth required. Everything else is behind the cookie gate.
PUBLIC_PATHS = {"/health", "/login", "/logout"}


def _make_auth_token() -> str:
    """Build a cookie value: `<expires>.<hmac-sha256>`."""
    expires = str(int(time.time()) + COOKIE_MAX_AGE)
    sig = _hmac.new(COOKIE_SECRET, expires.encode(), _hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def _verify_auth_token(token: str) -> bool:
    try:
        expires_s, sig = token.rsplit(".", 1)
        if int(expires_s) < time.time():
            return False
        expected = _hmac.new(COOKIE_SECRET, expires_s.encode(), _hashlib.sha256).hexdigest()
        return _hmac.compare_digest(sig, expected)
    except Exception:
        return False


def _is_authenticated(request: Request) -> bool:
    return _verify_auth_token(request.cookies.get(COOKIE_NAME, ""))


def _safe_return_to(value: str) -> str:
    """Reject open-redirect attempts — only allow same-origin relative paths."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    # Strip any scheme/netloc that slipped through.
    p = _urlparse(value)
    if p.scheme or p.netloc:
        return "/"
    return value


def guard(request: Request) -> Response | None:
    """Enforce auth on protected routes.

    - HTML navigation: 302 to /login?returnTo=<path>
    - API / XHR: 401 JSON (so the SPA's fetch() can surface it cleanly)
    """
    if _is_authenticated(request):
        return None
    accept = request.headers.get("accept", "").lower()
    wants_html = "text/html" in accept
    if wants_html:
        rt = request.url.path
        if request.url.query:
            rt = f"{rt}?{request.url.query}"
        return RedirectResponse(f"/login?returnTo={_url_quote(rt)}", status_code=302)
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Agent — Sign in</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0f14;color:#c9d1d9;font-family:'IBM Plex Sans',sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:#14181f;border:1px solid #252d3d;border-radius:12px;padding:36px 32px;width:100%;max-width:380px;
  box-shadow:0 20px 40px rgba(0,0,0,0.4)}
.brand{text-align:center;margin-bottom:28px}
.brand-logo{display:inline-flex;align-items:center;gap:10px;font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:18px;color:#6272ff}
.brand-logo span{color:#6b7688;font-weight:400}
.brand-sub{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#6b7688;margin-top:8px;letter-spacing:1.5px;text-transform:uppercase}
label{display:block;font-family:'IBM Plex Mono',monospace;font-size:11px;color:#6b7688;
  letter-spacing:0.05em;text-transform:uppercase;margin-bottom:6px;margin-top:16px}
input{width:100%;background:#0d0f14;border:1px solid #252d3d;border-radius:6px;color:#c9d1d9;
  font-family:'IBM Plex Mono',monospace;font-size:13px;padding:9px 11px;outline:none;transition:border-color .15s}
input:focus{border-color:#6272ff}
button{width:100%;margin-top:24px;background:#6272ff;border:1px solid #6272ff;border-radius:6px;color:#fff;
  font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:500;padding:10px;cursor:pointer;
  transition:background .15s,border-color .15s}
button:hover{background:#7b8fff;border-color:#7b8fff}
.err{background:rgba(248,81,73,0.08);border:1px solid rgba(248,81,73,0.3);border-radius:6px;
  color:#f85149;font-family:'IBM Plex Mono',monospace;font-size:12px;padding:8px 12px;margin-bottom:14px;text-align:center}
.footnote{margin-top:18px;font-family:'IBM Plex Mono',monospace;font-size:10px;color:#6b7688;text-align:center;line-height:1.6}
</style></head>
<body>
<div class="card">
  <div class="brand">
    <div class="brand-logo">hermes<span>/admin</span></div>
    <div class="brand-sub">Sign in to continue</div>
  </div>
  __ERROR__
  <form method="POST" action="/login">
    <input type="hidden" name="returnTo" value="__RETURN_TO__">
    <label for="username">Username</label>
    <input id="username" name="username" type="text" autocomplete="username" autofocus required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
  </form>
  <p class="footnote">Credentials are the <code>ADMIN_USERNAME</code> and <code>ADMIN_PASSWORD</code><br>Railway service variables.</p>
</div>
</body></html>"""


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


async def page_login(request: Request) -> Response:
    """GET /login — render the sign-in form."""
    # Already signed in? Bounce to returnTo (or /).
    if _is_authenticated(request):
        return RedirectResponse(_safe_return_to(request.query_params.get("returnTo", "/")), status_code=302)
    rt = _safe_return_to(request.query_params.get("returnTo", "/"))
    error_html = ('<div class="err">Invalid username or password</div>'
                  if request.query_params.get("error") else "")
    html = (LOGIN_PAGE_HTML
            .replace("__ERROR__", error_html)
            .replace("__RETURN_TO__", _html_escape(rt)))
    return HTMLResponse(html)


async def login_post(request: Request) -> Response:
    """POST /login — validate creds and set the auth cookie."""
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    return_to = _safe_return_to(str(form.get("returnTo", "/")))

    valid_user = _hmac.compare_digest(username, ADMIN_USERNAME)
    valid_pw = _hmac.compare_digest(password, ADMIN_PASSWORD)
    if valid_user and valid_pw:
        resp = RedirectResponse(return_to, status_code=302)
        resp.set_cookie(
            COOKIE_NAME,
            _make_auth_token(),
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return resp
    return RedirectResponse(f"/login?returnTo={_url_quote(return_to)}&error=1", status_code=302)


async def logout(request: Request) -> Response:
    """GET /logout — clear cookie and bounce to login."""
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# ── Gateway manager ───────────────────────────────────────────────────────────
# Auto-respawn tuning. When the gateway exits without us asking it to — an
# in-band `/restart` (inside a container hermes exits 75 expecting a supervisor
# to bring it back; verified it takes the exit-75 path, NOT a detached
# self-restart, when /run/.containerenv or /.dockerenv exists), a crash, or an
# OOM kill — server.py is that supervisor and must restart it. Nothing else
# will, and /health stays 200, so the bot would otherwise sit silently dead.
# A crash-loop guard stops us hammering a gateway that genuinely can't stay up
# (e.g. a bad provider key / model).
RESPAWN_WINDOW_S   = 120     # rolling window (s) for counting unexpected exits
RESPAWN_MAX_IN_WIN = 5       # give up auto-restart after this many exits in window
RESPAWN_BASE_DELAY = 2.0     # first backoff (seconds)
RESPAWN_MAX_DELAY  = 30.0    # backoff cap


class Gateway:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.state = "stopped"
        self.logs: deque[str] = deque(maxlen=500)
        self.started_at: float | None = None
        self.restarts = 0
        # True while a deliberate stop()/restart()/reset is in flight, so the
        # exiting process's _drain() doesn't fire an auto-respawn that races the
        # intentional lifecycle.
        self._stopping = False
        # Monotonic timestamps of recent unexpected exits (crash-loop guard).
        self._recent_exits: list[float] = []

    async def start(self, *, reset_budget: bool = True):
        if self.proc and self.proc.returncode is None:
            return
        # A manual Start/Restart (or boot) grants a fresh crash-loop budget; the
        # auto-respawn path passes reset_budget=False so repeated crashes keep
        # accumulating toward the give-up threshold.
        if reset_budget:
            self._recent_exits.clear()
        self.state = "starting"
        self._stopping = False
        try:
            env, port_preflight = prepare_gateway_env()
            if port_preflight["api_server_action"] == "block_gateway_start":
                self.state = "error"
                warning = (
                    "[port preflight] Gateway start blocked: config.yaml explicitly "
                    "enables the Hermes API Server on a conflicting port. Fix Railway "
                    "PORT or platforms.api_server.extra.port in the persisted Hermes "
                    "config, then restart."
                )
                self.logs.append(warning)
                print(f"[gateway] {warning}", flush=True)
                return
            if port_preflight["api_server_action"] == "disable_for_gateway_start":
                conflict_ports = ", ".join(
                    str(port)
                    for port in sorted({
                        conflict["port"]
                        for conflict in port_preflight["conflicts"]
                        if "api_server" in conflict["services"]
                    })
                )
                warning = (
                    "[port preflight] Hermes API Server disabled for this gateway run "
                    f"because its port conflicts on {conflict_ports}. Messaging adapters "
                    "will continue. Fix Railway PORT or the persisted Hermes "
                    "API_SERVER_PORT, then restart."
                )
                self.logs.append(warning)
                print(f"[gateway] {warning}", flush=True)
            model = env.get("LLM_MODEL", "")
            provider_key = next((env.get(k, "") for k in PROVIDER_KEYS if env.get(k)), "")
            print(f"[gateway] model={model or '⚠ NOT SET'} | provider_key={'set' if provider_key else '⚠ NOT SET'}", flush=True)
            # Write config.yaml so hermes picks up the model (env vars alone aren't always enough)
            write_config_yaml(read_env(ENV_FILE))
            # --replace: force-displace any existing gateway.pid lock holder
            # before claiming it. Without this, a lock left behind by a prior
            # incarnation this supervisor doesn't recognize as "our" dead
            # process (e.g. hermes' own dashboard spawns its own detached
            # `hermes gateway restart` via its native /api/gateway/restart
            # action, entirely outside this class's tracking) makes every
            # subsequent plain `hermes gateway` invocation refuse to start
            # ("Another gateway instance is already running"), which
            # _clear_stale_pidfile() can never self-heal since it only clears
            # a pid file matching the exact pid THIS supervisor just watched
            # die. --replace is hermes' own blessed fix for exactly this
            # class of stuck-lock — it force-kills whatever holds the lock
            # (graceful SIGTERM, escalating to SIGKILL) before claiming it.
            self.proc = await asyncio.create_subprocess_exec(
                "hermes", "gateway", "run", "--replace",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            self.state = "running"
            self.started_at = time.time()
            asyncio.create_task(self._drain(self.proc))
        except Exception as e:
            self.state = "error"
            self.logs.append(f"[error] Failed to start: {e}")

    async def stop(self):
        self._stopping = True
        if not self.proc or self.proc.returncode is not None:
            self.state = "stopped"
            return
        self.state = "stopping"
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        self.state = "stopped"
        self.started_at = None

    async def restart(self):
        await self.stop()
        self.restarts += 1
        await self.start()

    async def _drain(self, proc: asyncio.subprocess.Process):
        assert proc.stdout
        async for raw in proc.stdout:
            line = ANSI_ESCAPE.sub("", raw.decode(errors="replace").rstrip())
            self.logs.append(line)
        rc = proc.returncode
        # Ignore the drain of a process we've already replaced (e.g. via restart()).
        if proc is not self.proc:
            return
        # A deliberate stop()/restart()/reset owns its own lifecycle — don't respawn.
        if self._stopping:
            return
        # Unexpected exit: in-band `/restart` (exit 75), a crash, or an OOM kill.
        # On Railway nothing else brings the gateway back, so we supervise it.
        self.state = "error"
        self.logs.append(f"[gateway] exited (code {rc}) — supervising restart")
        asyncio.create_task(self._supervise_respawn(proc.pid))

    async def _supervise_respawn(self, dead_pid: int | None):
        # Crash-loop guard: count unexpected exits inside a rolling window and
        # give up (rather than hammer) once they exceed the threshold.
        now = time.monotonic()
        self._recent_exits = [t for t in self._recent_exits if now - t < RESPAWN_WINDOW_S]
        self._recent_exits.append(now)
        if len(self._recent_exits) > RESPAWN_MAX_IN_WIN:
            self.state = "crashed"
            self.logs.append(
                f"[gateway] crash-looping ({len(self._recent_exits)} exits in "
                f"{RESPAWN_WINDOW_S}s) — giving up auto-restart. Fix the provider/"
                f"model in the admin UI, then Start/Restart the gateway."
            )
            return
        delay = min(RESPAWN_BASE_DELAY * 2 ** (len(self._recent_exits) - 1), RESPAWN_MAX_DELAY)
        self.logs.append(f"[gateway] restarting in {int(delay)}s (attempt {len(self._recent_exits)})")
        await asyncio.sleep(delay)
        # Re-check the deliberate-lifecycle conditions AFTER the backoff sleep: a
        # Stop, Reset, or shutdown issued during the wait must win over the respawn.
        if self._stopping:
            self.logs.append("[gateway] restart cancelled (stopped/reconfigured)")
            return
        if self.proc and self.proc.returncode is None:
            return  # a manual Start already brought a live gateway back
        if not is_config_complete():
            self.state = "stopped"
            self.logs.append("[gateway] restart skipped — provider/model not configured")
            return
        # Clear a pid file left stale by a hard crash (SIGKILL/OOM skips hermes'
        # atexit cleanup) so the respawn's own O_EXCL pid claim can't bail with
        # "PID file race lost". Scoped to the pid we just buried — never disturbs
        # a live gateway's lock.
        self._clear_stale_pidfile(dead_pid)
        self.restarts += 1
        await self.start(reset_budget=False)

    def _clear_stale_pidfile(self, dead_pid: int | None) -> None:
        if dead_pid is None:
            return
        pid_file = Path(HERMES_HOME) / "gateway.pid"
        try:
            rec = json.loads(pid_file.read_text())
        except Exception:
            return
        if rec.get("pid") == dead_pid:
            try:
                pid_file.unlink()
                self.logs.append(f"[gateway] cleared stale pid file (pid {dead_pid})")
            except OSError:
                pass

    def status(self) -> dict:
        uptime = int(time.time() - self.started_at) if self.started_at and self.state == "running" else None
        return {
            "state":    self.state,
            "pid":      self.proc.pid if self.proc and self.proc.returncode is None else None,
            "uptime":   uptime,
            "restarts": self.restarts,
        }


gw = Gateway()
cfg_lock = asyncio.Lock()


# ── Hermes dashboard subprocess ───────────────────────────────────────────────
class Dashboard:
    """Manages the `hermes dashboard` subprocess (native Hermes web UI).

    Bound to loopback only — we expose it to the public internet through our
    reverse proxy on $PORT, where the template's cookie auth guards every request.
    The dashboard is independent of the gateway: it reads config files
    directly and tolerates a stopped gateway.

    Spawned with the same merged env (OS env + HERMES_HOME + .env contents)
    as the gateway — see build_hermes_env(). Without it, the dashboard process
    only ever sees our own os.environ from container boot, before any
    provider key exists; the embedded Chat tab's agent-init then fails with
    "No inference provider configured" even though /setup shows a key saved,
    because hermes' own provider auto-resolution (hermes_cli/auth.py) reads
    credentials via plain os.getenv(), not by re-parsing .env from disk. Since
    the dashboard only starts once at boot, restart() must be called whenever
    a provider key is saved so the running process picks up the new env.

    All subprocess output is streamed to our stdout (→ Railway logs) with a
    `[dashboard]` prefix AND retained in a ring buffer for diagnostics.
    Unexpected exits are explicitly logged with their return code.
    """

    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.logs: deque[str] = deque(maxlen=300)
        self._drain_task: asyncio.Task | None = None

    async def start(self):
        if self.proc and self.proc.returncode is None:
            return
        port_preflight = evaluate_port_preflight()
        dashboard_conflict = next((
            conflict
            for conflict in port_preflight["conflicts"]
            if set(conflict["services"]) == {"web", "dashboard"}
        ), None)
        if dashboard_conflict:
            warning = (
                "Port preflight skipped the Hermes dashboard because it conflicts "
                f"with the public web server on port {dashboard_conflict['port']}. "
                "Setup remains available; fix the Railway variables and redeploy."
            )
            self.logs.append(warning)
            print(f"[dashboard] {warning}", flush=True)
            return
        try:
            self.proc = await asyncio.create_subprocess_exec(
                "hermes", "dashboard",
                "--host", HERMES_DASHBOARD_HOST,
                "--port", str(HERMES_DASHBOARD_PORT),
                "--no-open",
                # --skip-build: the Dockerfile pre-builds the React dashboard
                # into hermes_cli/web_dist/ at image time. This flag tells
                # hermes to trust that dist and skip its npm build check,
                # which would otherwise add ~30s to first startup (hermes >= v2026.5.16).
                "--skip-build",
                # NOTE: the embedded Chat tab (/api/pty + /api/ws + /api/events)
                # is unconditionally enabled as of hermes v2026.6.5 — the old
                # `--tui` flag was REMOVED from the dashboard subcommand. Passing
                # it now aborts startup with "unrecognized arguments: --tui",
                # which kills this subprocess and 503s the reverse proxy. The
                # Dockerfile still pre-builds ui-tui/dist/ (via HERMES_TUI_DIR)
                # so the PTY child spawns instantly on first chat connect.
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=build_hermes_env(),
            )
            print(f"[dashboard] spawned pid={self.proc.pid} → {HERMES_DASHBOARD_URL}", flush=True)
            self._drain_task = asyncio.create_task(self._drain())
        except Exception as e:
            print(f"[dashboard] FAILED to spawn: {e!r}", flush=True)

    async def _drain(self):
        """Stream subprocess output to Railway logs (prefixed) and a ring buffer."""
        assert self.proc and self.proc.stdout
        try:
            async for raw in self.proc.stdout:
                line = ANSI_ESCAPE.sub("", raw.decode(errors="replace").rstrip())
                self.logs.append(line)
                print(f"[dashboard] {line}", flush=True)
        except Exception as e:
            print(f"[dashboard] drain error: {e!r}", flush=True)
        finally:
            rc = self.proc.returncode if self.proc else None
            if rc is not None and rc != 0:
                print(f"[dashboard] EXITED with code {rc} — reverse proxy will return 503 until restart", flush=True)
            elif rc == 0:
                print(f"[dashboard] exited cleanly (code 0)", flush=True)

    async def stop(self):
        if not self.proc or self.proc.returncode is not None:
            return
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()

    async def restart(self):
        """Respawn so a freshly-saved provider key reaches the embedded Chat tab.

        Drops any live /api/pty, /api/ws, /api/events connections (the
        reverse-proxy WS pumps just see the upstream close and the SPA
        reconnects) — an acceptable trade-off since the alternative is Chat
        staying broken until a full redeploy.
        """
        await self.stop()
        await self.start()


dash = Dashboard()

# Shared async HTTP client for the reverse proxy. Created lazily so we pick up
# the running event loop, torn down in lifespan.
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
        )
    return _http_client


_HERMES_SESSION_TOKEN_RE = re.compile(r'__HERMES_SESSION_TOKEN__\s*=\s*"([^"]*)"')


async def _get_hermes_session_token() -> str:
    """Scrape the dashboard's own ephemeral session token from its SPA shell.

    Hermes gates every non-public ``/api/*`` route behind a per-process
    random ``_SESSION_TOKEN`` — a legacy check in hermes_cli/web_server.py's
    ``auth_middleware`` that's SEPARATE from (and still active alongside) the
    OAuth gate that invariant 3 already covers. Loopback bind only turns off
    the OAuth gate (``auth_required``); it does not exempt this token check.
    A tokenless call like a plain server-to-server POST 401s unconditionally.

    The only way to obtain a valid token without a browser is the same way
    the SPA itself does: on a loopback (ungated) bind, hermes injects it into
    every served HTML shell as ``window.__HERMES_SESSION_TOKEN__="..."``
    (hermes_cli/web_server.py's ``_serve_index``). That HTML-serving catch-all
    route is not under ``/api/``, so it is never itself gated — no chicken-
    and-egg problem. Not cached: cheap (one loopback GET), and self-heals
    across a dashboard restart (which rotates the token) without needing
    invalidation logic. Re-verify the injected variable name against
    hermes_cli/web_server.py on a Hermes version bump — if it's ever renamed
    or removed, this degrades to the pre-existing "no token" 401 handled
    below, not a crash.
    """
    client = get_http_client()
    resp = await client.get(f"{HERMES_DASHBOARD_URL}/", timeout=httpx.Timeout(10.0))
    resp.raise_for_status()
    match = _HERMES_SESSION_TOKEN_RE.search(resp.text)
    return match.group(1) if match else ""


async def set_active_model_via_hermes(
    provider_id: str, model: str, *, base_url: str = "", api_key: str = ""
) -> str | None:
    """Pin model.provider + model.default via hermes' own POST /api/model/set.

    Delegates to hermes_cli/web_server.py's _apply_main_model_assignment — the
    same code path its dashboard's "Switch Model" dialog and flat Config page
    use — instead of us hand-writing config.yaml's model block. Hermes always
    resolves an EXPLICIT provider there (never "auto") and correctly clears
    stale base_url/api_key only on a genuine provider switch, preserving them
    on a same-provider re-pick.

    Necessary because our own /setup wizard has a single shared "LLM Model"
    field across every configured provider: once 2+ provider keys exist in
    .env, config.yaml's model.provider="auto" (write_config_yaml()'s old
    unconditional default) lets hermes resolve to the WRONG provider — the
    first match in its own internal PROVIDER_REGISTRY dict order — paired
    with a model string that belongs to a DIFFERENT provider.

    base_url/api_key are forwarded verbatim into this same request's own
    ``base_url``/``api_key`` fields (hermes_cli/web_server.py's
    ``ModelAssignment`` schema — "Only honored for custom/local providers on
    the main slot"). REQUIRED for provider_id="custom": hermes' actual
    runtime resolver (hermes_cli/runtime_provider.py, what the gateway/Chat
    tab call at agent-init) only trusts a bare "custom" provider when
    model.base_url is ALSO set directly on the model block — it never
    consults config.yaml's separate custom_providers[] list (that's
    display/bookkeeping only, for hermes' own Keys-tab picker). Passing them
    lets hermes write model.base_url/model.api_key itself; it also
    auto-registers a matching custom_providers catalog entry as a side
    effect, mirroring its own dashboard's custom-endpoint flow.

    Best-effort: on any failure (dashboard not up yet, network hiccup, no
    session token obtainable) we leave whatever write_config_yaml() already
    wrote in place (single-provider "auto" default, or a previously-pinned
    provider preserved as-is) rather than blocking the save. Returns a
    human-readable warning string on failure, or None on success.
    """
    client = get_http_client()
    try:
        session_token = await _get_hermes_session_token()
    except httpx.HTTPError as e:
        return f"Could not fetch a Hermes session token to pin {provider_id} ({e}); using auto-resolution instead."
    headers = {_SESSION_TOKEN_HEADER: session_token} if session_token else {}

    try:
        resp = await client.post(
            f"{HERMES_DASHBOARD_URL}/api/model/set",
            json={
                "scope": "main",
                "provider": provider_id,
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
                # We have no UI to show hermes' own "this model looks
                # expensive, are you sure?" confirmation — the user already
                # confirmed intent by pasting a key and a model name here.
                "confirm_expensive_model": True,
            },
            headers=headers,
            timeout=httpx.Timeout(15.0),
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
        return f"Could not reach the Hermes dashboard to pin {provider_id} ({e}); using auto-resolution instead."
    except httpx.RequestError as e:
        return f"Hermes model/set request failed ({e}); using auto-resolution instead."

    if resp.status_code != 200:
        return f"Hermes rejected the {provider_id} model/provider pin (HTTP {resp.status_code}); using auto-resolution instead."
    try:
        data = resp.json()
    except Exception:
        return None  # 200 with an unparseable body — nothing actionable to report
    if data.get("ok") is False:
        return data.get("confirm_message") or f"Hermes did not apply the {provider_id} model/provider pin; using auto-resolution instead."
    return None


# ── Route handlers ────────────────────────────────────────────────────────────
async def page_index(request: Request):
    if err := guard(request): return err
    return templates.TemplateResponse(request, "index.html")


async def route_health(request: Request):
    return JSONResponse({"status": "ok", "gateway": gw.state})


async def api_config_get(request: Request):
    if err := guard(request): return err
    async with cfg_lock:
        data = read_env(ENV_FILE)
    defs = [{"key": k, "label": l, "category": c, "secret": s} for k, l, c, s in ENV_VARS]
    return JSONResponse({"vars": mask(data), "defs": defs})


async def api_config_put(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    try:
        restart = body.pop("_restart", False)
        # Set by the setup wizard to the ENV_VARS key of whichever provider's
        # dropdown entry was selected in this save action (e.g. "NVIDIA_API_KEY")
        # — empty when the user saved without touching a provider (e.g. just
        # toggling a messaging channel). See set_active_model_via_hermes().
        active_provider_key = str(body.pop("_active_provider_key", "") or "").strip()
        new_vars = body.get("vars", {})
        async with cfg_lock:
            existing = read_env(ENV_FILE)
            merged = unmask(new_vars, existing)
            for k, v in existing.items():
                if k not in merged:
                    merged[k] = v
            write_env(ENV_FILE, merged)
            write_config_yaml(merged)

        model_warning = None
        hermes_provider_id = HERMES_PROVIDER_IDS.get(active_provider_key)
        model_value = merged.get("LLM_MODEL", "").strip()
        if hermes_provider_id and model_value:
            pin_base_url = ""
            pin_api_key = ""
            if hermes_provider_id == "custom":
                pin_base_url = (
                    CUSTOM_STYLE_BASE_URLS.get(active_provider_key)
                    or merged.get("CUSTOM_PROVIDER_BASE_URL", "").strip()
                )
                pin_api_key = merged.get(active_provider_key, "").strip()
            model_warning = await set_active_model_via_hermes(
                hermes_provider_id, model_value, base_url=pin_base_url, api_key=pin_api_key
            )

        if restart:
            asyncio.create_task(gw.restart())
            # The dashboard (and its embedded Chat tab) only ever sees the env
            # it was spawned with — a newly-saved provider key doesn't reach
            # the already-running process otherwise. See Dashboard.restart().
            asyncio.create_task(dash.restart())
        resp = {"ok": True, "restarting": restart}
        if model_warning:
            resp["warning"] = model_warning
        return JSONResponse(resp)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_status(request: Request):
    if err := guard(request): return err
    data = read_env(ENV_FILE)
    providers = {
        k.replace("_API_KEY","").replace("_TOKEN","").replace("HF_","HuggingFace ").replace("_"," ").title():
        {"configured": bool(data.get(k))}
        for k in PROVIDER_KEYS
    }
    channels = {
        name: {"configured": bool(v := data.get(key,"")) and v.lower() not in ("false","0","no")}
        for name, key in CHANNEL_MAP.items()
    }
    return JSONResponse({
        "gateway": gw.status(),
        "providers": providers,
        "channels": channels,
        "port_preflight": evaluate_port_preflight(),
    })


async def api_logs(request: Request):
    if err := guard(request): return err
    return JSONResponse({"lines": list(gw.logs)})


async def api_gw_start(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.start())
    return JSONResponse({"ok": True})


async def api_gw_stop(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.stop())
    return JSONResponse({"ok": True})


async def api_gw_restart(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.restart())
    return JSONResponse({"ok": True})


async def api_config_reset(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.stop())
    async with cfg_lock:
        if ENV_FILE.exists():
            ENV_FILE.unlink()
        write_config_yaml({}, reset_model=True)
    return JSONResponse({"ok": True})


# ── Pairing ───────────────────────────────────────────────────────────────────
# Pending-request file format (hermes >= v0.15 / v2026.5.29.x, gateway/pairing.py):
# each `{platform}-pending.json` entry is keyed by a random opaque `entry_id`
# (secrets.token_hex), and the user-facing pairing code is stored only as a
# salted hash ({hash, salt, user_id, user_name, created_at}) — the plaintext
# code is never on disk. Our admin-approval flow is code-agnostic: the dashboard
# is already cookie-authed, so we approve by moving an entry from pending →
# approved keyed off that `entry_id` (round-tripped from the pending list as
# `code`), reading `user_id`/`user_name` straight from the entry. We must NOT
# uppercase that key — entry_ids are lowercase hex, and uppercasing them was
# what silently broke approve/deny on the v0.15 upgrade. Older plaintext-keyed
# entries still work here because we treat the key as an opaque handle.
def _pjson(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _wjson(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    try: os.chmod(path, 0o600)
    except OSError: pass


def _platforms(suffix: str) -> list[str]:
    if not PAIRING_DIR.exists(): return []
    return [f.stem.rsplit(f"-{suffix}", 1)[0] for f in PAIRING_DIR.glob(f"*-{suffix}.json")]


async def api_pairing_pending(request: Request):
    if err := guard(request): return err
    now = time.time()
    out = []
    for p in _platforms("pending"):
        for code, info in _pjson(PAIRING_DIR / f"{p}-pending.json").items():
            if now - info.get("created_at", now) <= PAIRING_TTL:
                out.append({"platform": p, "code": code,
                            "user_id": info.get("user_id",""), "user_name": info.get("user_name",""),
                            "age_minutes": int((now - info.get("created_at", now)) / 60)})
    return JSONResponse({"pending": out})


async def api_pairing_approve(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, code = body.get("platform",""), body.get("code","").strip()
    if not platform or not code:
        return JSONResponse({"error": "platform and code required"}, status_code=400)
    pending_path = PAIRING_DIR / f"{platform}-pending.json"
    pending = _pjson(pending_path)
    if code not in pending:
        return JSONResponse({"error": "Code not found"}, status_code=404)
    entry = pending.pop(code)
    user_id = (entry.get("user_id") or "").strip() if isinstance(entry, dict) else ""
    if not user_id:
        # Malformed/legacy entry without a user_id — leave it in pending (we
        # haven't written the pop yet) rather than silently discarding it.
        return JSONResponse({"error": "Pending entry has no user_id"}, status_code=422)
    _wjson(pending_path, pending)
    approved = _pjson(PAIRING_DIR / f"{platform}-approved.json")
    approved[user_id] = {"user_name": entry.get("user_name",""), "approved_at": time.time()}
    _wjson(PAIRING_DIR / f"{platform}-approved.json", approved)
    return JSONResponse({"ok": True})


async def api_pairing_deny(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, code = body.get("platform",""), body.get("code","").strip()
    p = PAIRING_DIR / f"{platform}-pending.json"
    pending = _pjson(p)
    if code in pending:
        del pending[code]
        _wjson(p, pending)
    return JSONResponse({"ok": True})


async def api_pairing_approved(request: Request):
    if err := guard(request): return err
    out = []
    for p in _platforms("approved"):
        for uid, info in _pjson(PAIRING_DIR / f"{p}-approved.json").items():
            out.append({"platform": p, "user_id": uid,
                        "user_name": info.get("user_name",""), "approved_at": info.get("approved_at",0)})
    return JSONResponse({"approved": out})


async def api_pairing_revoke(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, uid = body.get("platform",""), body.get("user_id","")
    if not platform or not uid:
        return JSONResponse({"error": "platform and user_id required"}, status_code=400)
    p = PAIRING_DIR / f"{platform}-approved.json"
    approved = _pjson(p)
    if uid in approved:
        del approved[uid]
        _wjson(p, approved)
    return JSONResponse({"ok": True})


# ── Backup & Restore ─────────────────────────────────────────────────────────
# Thin wrapper around hermes' OWN `hermes backup` / `hermes import` CLI
# (hermes_cli/backup.py, verified against v2026.7.1) rather than reimplementing
# file selection: it already excludes code checkouts/caches/venvs/lock files
# from the walk, protects against zip-slip on extract, and — critically — skips
# re-writing gateway_state.json/gateway.pid/cron.pid/gateway.lock/processes.json
# even when present in the archive (_IMPORT_SKIP_NAMES). That's exactly the
# "don't let a foreign pid file wedge the supervisor" concern invariant 6
# already documents — we deliberately do not duplicate either behavior
# ourselves. Re-verify both on a future Hermes version bump, same as every
# other upstream-CLI assumption this template makes.
BACKUP_DIR = Path(HERMES_HOME) / "backups"   # hermes' own pre-update-backup convention;
                                              # this dir is itself in hermes' backup
                                              # exclusion list, so snapshots here never
                                              # bloat a future full backup.
PRE_RESTORE_KEEP = 3
BACKUP_SUBPROCESS_TIMEOUT = 600  # 10 min ceiling for both `hermes backup` and `hermes import`
SNAPSHOT_NAME_RE = re.compile(r"^pre-restore-\d+-[0-9a-f]+\.zip$")

backup_lock = asyncio.Lock()


async def _run_hermes_cli(*args: str, timeout: float = BACKUP_SUBPROCESS_TIMEOUT) -> tuple[int, str]:
    """Run a `hermes <args>` subcommand, capturing combined stdout+stderr.

    Shares build_hermes_env() with Gateway/Dashboard so the CLI sees provider
    keys saved via /setup (not just our own os.environ). Never raises — like
    Gateway.start()/Dashboard.start(), a failed spawn (missing binary, bad env)
    is reported as a (rc, message) pair so every caller gets one uniform error
    shape instead of an unhandled exception surfacing as a generic 500.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "hermes", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=build_hermes_env(),
        )
    except OSError as e:
        return 127, f"Could not launch hermes {' '.join(args)}: {e}"
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"hermes {' '.join(args)} timed out after {timeout}s"
    return proc.returncode, raw.decode(errors="replace")


async def _hermes_version() -> str:
    """Best-effort `hermes --version`, used only for the restore-time compat hint."""
    try:
        rc, out = await _run_hermes_cli("--version", timeout=15)
        return out.strip() if rc == 0 else "unknown"
    except Exception:
        return "unknown"


def _prune_pre_restore_snapshots() -> None:
    snaps = sorted(BACKUP_DIR.glob("pre-restore-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in snaps[PRE_RESTORE_KEEP:]:
        try:
            stale.unlink()
        except OSError:
            pass


def _sweep_stale_backup_tmpdirs() -> None:
    """Clean up /tmp/hermes-backup-* left behind by a client aborting a download
    mid-stream (the BackgroundTask cleanup then never runs). Safe: this prefix
    is only ever used by api_backup_download, and /tmp is ephemeral anyway —
    this just bounds growth across many downloads within one long-lived container.
    """
    for stale in Path(tempfile.gettempdir()).glob("hermes-backup-*"):
        shutil.rmtree(stale, ignore_errors=True)


async def api_backup_download(request: Request) -> Response:
    if err := guard(request): return err
    if backup_lock.locked():
        return JSONResponse({"error": "A backup or restore is already in progress"}, status_code=409)
    async with backup_lock:
        tmp_dir = tempfile.mkdtemp(prefix="hermes-backup-")
        zip_path = Path(tmp_dir) / "backup.zip"
        rc, output = await _run_hermes_cli("backup", "-o", str(zip_path))
        if rc != 0 or not zip_path.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return JSONResponse({"error": "Backup failed", "output": output[-2000:]}, status_code=500)

        # Best-effort manifest entry for the restore-time version hint — never
        # fails the download if this step errors.
        try:
            version = await _hermes_version()
            with zipfile.ZipFile(zip_path, "a") as zf:
                zf.writestr("template_manifest.json", json.dumps({
                    "hermes_version": version,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "template": "hermes-agent-railway-template",
                }))
        except Exception:
            pass

        filename = f"hermes-backup-{int(time.time())}.zip"
        return FileResponse(
            zip_path,
            filename=filename,
            media_type="application/zip",
            background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True),
        )


async def api_backup_snapshots(request: Request) -> Response:
    if err := guard(request): return err
    out = []
    if BACKUP_DIR.exists():
        for p in sorted(BACKUP_DIR.glob("pre-restore-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "created_at": st.st_mtime})
    return JSONResponse({"snapshots": out})


async def api_backup_snapshot_download(request: Request) -> Response:
    if err := guard(request): return err
    name = request.path_params.get("name", "")
    if not SNAPSHOT_NAME_RE.match(name):
        return Response("Not Found", status_code=404, media_type="text/plain")
    path = BACKUP_DIR / name
    try:
        path.resolve().relative_to(BACKUP_DIR.resolve())
    except ValueError:
        return Response("Not Found", status_code=404, media_type="text/plain")
    if not path.is_file():
        return Response("Not Found", status_code=404, media_type="text/plain")
    return FileResponse(path, filename=name, media_type="application/zip")


async def api_backup_restore(request: Request) -> Response:
    if err := guard(request): return err
    if backup_lock.locked():
        return JSONResponse({"error": "A backup or restore is already in progress"}, status_code=409)

    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return JSONResponse({"error": "No file uploaded"}, status_code=400)

    async with backup_lock:
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".zip", prefix="hermes-restore-")
        upload_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                while chunk := await upload.read(1024 * 1024):
                    f.write(chunk)

            if not zipfile.is_zipfile(upload_path):
                return JSONResponse({"error": "Uploaded file is not a valid zip archive"}, status_code=400)

            warning = None
            with zipfile.ZipFile(upload_path) as zf:
                names = {Path(n).name for n in zf.namelist()}
                if not names & {"config.yaml", ".env", "state.db"}:
                    return JSONResponse(
                        {"error": "This doesn't look like a hermes backup (no config.yaml/.env/state.db found)"},
                        status_code=400,
                    )
                if "template_manifest.json" in names:
                    try:
                        manifest = json.loads(zf.read("template_manifest.json"))
                        backup_version = manifest.get("hermes_version", "")
                        current_version = await _hermes_version()
                        if backup_version and current_version != "unknown" and backup_version != current_version:
                            warning = (
                                f"Backup was created with hermes {backup_version}, this deployment runs "
                                f"{current_version} — some settings may not carry over cleanly."
                            )
                    except Exception:
                        pass

            # Safety snapshot BEFORE touching anything live — abort rather than
            # overwrite state with no undo copy behind it.
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            # secrets suffix avoids a same-second collision silently clobbering
            # a distinct prior snapshot (two restores fired back-to-back).
            snap_path = BACKUP_DIR / f"pre-restore-{int(time.time())}-{secrets.token_hex(4)}.zip"
            rc, output = await _run_hermes_cli("backup", "-o", str(snap_path))
            if rc != 0:
                return JSONResponse(
                    {"error": "Could not create the pre-restore safety snapshot; restore aborted.",
                     "output": output[-2000:]},
                    status_code=500,
                )
            _prune_pre_restore_snapshots()

            await gw.stop()
            await dash.stop()
            try:
                rc, output = await _run_hermes_cli("import", str(upload_path), "--force")
            finally:
                # Always bring the dashboard back; only auto-start the gateway if
                # the (possibly just-restored) config is actually complete — same
                # rule auto_start() uses on boot. This runs even if the import
                # itself failed, so a bad upload doesn't leave the bot down too.
                await dash.start()
                if is_config_complete():
                    await gw.start()

            if rc != 0:
                return JSONResponse({"error": "Restore failed", "output": output[-2000:]}, status_code=500)

            resp = {"ok": True, "output": output[-2000:]}
            if warning:
                resp["warning"] = warning
            return JSONResponse(resp)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            upload_path.unlink(missing_ok=True)


# ── Reverse proxy → Hermes dashboard ──────────────────────────────────────────
_WIDGET_LINK_STYLE = (
    "background:rgba(20,24,31,0.92);backdrop-filter:blur(8px);"
    "border:1px solid #252d3d;border-radius:6px;padding:6px 12px;"
    "color:#c9d1d9;text-decoration:none;display:inline-flex;"
    "align-items:center;gap:6px;"
)
BACK_TO_SETUP_WIDGET = (
    '<div id="hermes-back-widget" style="position:fixed;bottom:14px;right:14px;'
    'z-index:99999;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;'
    'font-size:11px;display:flex;gap:8px;">'
    f'<a href="/setup" style="{_WIDGET_LINK_STYLE}">← Setup</a>'
    f'<a href="/logout" style="{_WIDGET_LINK_STYLE}">Sign out</a>'
    '</div>'
)

DASHBOARD_UNAVAILABLE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Dashboard starting…</title>
<style>body{background:#0d0f14;color:#c9d1d9;font-family:ui-monospace,Menlo,monospace;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{max-width:480px;padding:32px;border:1px solid #252d3d;border-radius:12px;
background:#14181f;text-align:center}
h1{font-size:16px;color:#d29922;margin:0 0 12px;font-weight:600}
p{font-size:13px;color:#6b7688;line-height:1.6;margin:0 0 16px}
a{color:#6272ff;text-decoration:none;border:1px solid #252d3d;border-radius:6px;
padding:7px 14px;font-size:12px;display:inline-block}
a:hover{border-color:#6272ff}</style></head>
<body><div class="card">
<h1>⚠ Hermes dashboard unavailable</h1>
<p>The native Hermes dashboard is not responding on port %d.<br>
It may still be starting up, or it may have crashed.</p>
<p>Try refreshing in a few seconds, or head back to setup.</p>
<a href="/setup">← Back to Setup</a>
</div>
<script>setTimeout(()=>location.reload(),4000);</script>
</body></html>""" % HERMES_DASHBOARD_PORT


async def _proxy_to_dashboard(request: Request) -> Response:
    """Forward an authenticated request to the Hermes dashboard subprocess.

    Assumes edge auth (basic auth middleware) has already validated the caller.
    HTTP-only: the native Hermes dashboard does not use WebSockets.
    """
    client = get_http_client()
    target = f"{HERMES_DASHBOARD_URL}{request.url.path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    req_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
    }
    body = await request.body()

    try:
        upstream = await client.request(
            request.method,
            target,
            headers=req_headers,
            content=body,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return HTMLResponse(DASHBOARD_UNAVAILABLE_HTML, status_code=503)
    except httpx.RequestError as e:
        print(f"[proxy] upstream error for {request.method} {request.url.path}: {e}", flush=True)
        return HTMLResponse(DASHBOARD_UNAVAILABLE_HTML, status_code=502)

    # Surface non-2xx responses from hermes into Railway logs so we can
    # diagnose 401/500s without needing browser DevTools access.
    if upstream.status_code >= 400:
        body_snip = upstream.content[:200].decode("utf-8", errors="replace")
        print(
            f"[proxy] {request.method} {request.url.path} -> {upstream.status_code} "
            f"body={body_snip!r}",
            flush=True,
        )

    # Strip hop-by-hop and length/encoding headers — Starlette recomputes them.
    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in HOP_BY_HOP
        and k.lower() not in ("content-encoding", "content-length")
    }

    content = upstream.content
    content_type = upstream.headers.get("content-type", "").lower()

    # Inject the "← Setup" widget into HTML pages so users can always return.
    if "text/html" in content_type and b"</body>" in content:
        try:
            text = content.decode("utf-8", errors="replace")
            text = text.replace("</body>", BACK_TO_SETUP_WIDGET + "</body>", 1)
            content = text.encode("utf-8")
        except Exception:
            pass  # on any error, fall back to raw upstream content

    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=resp_headers,
    )


async def route_root(request: Request) -> Response:
    """GET /: first-visit smart redirect, otherwise proxy to the dashboard.

    - Unconfigured + bare GET `/` → bounce to `/setup` so new users land on
      the wizard instead of a half-empty dashboard.
    - Sidebar / in-app links pass `?force=1` to opt out of that redirect —
      users who explicitly want the dashboard (e.g. to set providers via
      the Keys tab) can still reach it without saving config first.
    - Non-GET (SPA API calls, etc.) always proxy through.
    """
    if err := guard(request): return err
    if (request.method == "GET"
            and request.query_params.get("force") != "1"
            and not is_config_complete()):
        return RedirectResponse("/setup", status_code=302)
    return await _proxy_to_dashboard(request)


async def route_proxy(request: Request) -> Response:
    """Catch-all: forward any unmatched path to the Hermes dashboard."""
    if err := guard(request): return err
    return await _proxy_to_dashboard(request)


async def route_setup_404(request: Request) -> Response:
    """Typos under /setup/* should 404 here — not fall through to the proxy."""
    if err := guard(request): return err
    return Response("Not Found", status_code=404, media_type="text/plain")


# ── App lifecycle ─────────────────────────────────────────────────────────────
async def auto_start():
    if is_config_complete():
        asyncio.create_task(gw.start())
    else:
        print("[server] Config incomplete — gateway not started. Configure provider + model in the admin UI.", flush=True)


@asynccontextmanager
async def lifespan(app):
    _sweep_stale_backup_tmpdirs()
    port_preflight = evaluate_port_preflight()
    for conflict in port_preflight["conflicts"]:
        print(
            f"[server] Port preflight: {conflict['message']} "
            f"{conflict['remediation']}",
            flush=True,
        )
    # Dashboard runs always — it's the user-facing UI after setup is done,
    # and it's independent of gateway state.
    asyncio.create_task(dash.start())
    await auto_start()
    try:
        yield
    finally:
        await asyncio.gather(
            gw.stop(),
            dash.stop(),
            return_exceptions=True,
        )
        global _http_client
        if _http_client is not None:
            await _http_client.aclose()
            _http_client = None


# ── WebSocket reverse proxy ──────────────────────────────────────────────────
# The hermes dashboard exposes several WebSocket endpoints when started with
# --tui. The browser SPA opens these and they must flow through our reverse
# proxy. /api/pub is opened only by the PTY child against loopback and is
# intentionally NOT proxied — exposing it would let an authed user spam events
# into channels. It lives at /api/pub (not under /api/plugins/), so the plugin
# prefix route below does not match it.
#
#   /api/pty                  binary stream — embedded TUI keystrokes/output
#   /api/ws                   JSON-RPC      — gateway sidecar driving Chat metadata
#   /api/events               text frames   — dashboard subscriber for /api/pub fan-out
#   /api/plugins/<name>/...   plugin-contributed sockets. Mounted by hermes
#                             under /api/plugins/<name>/ (web_server.
#                             _mount_plugin_api_routes), e.g. kanban's
#                             /api/plugins/kanban/events live task feed. Added
#                             in v0.15 — without a proxy route Starlette 403s
#                             the upgrade and the SPA retries in a tight loop.
#
# Auth model (matches the HTTP proxy):
#   * Edge: our HMAC cookie via _is_authenticated. WebSocket inherits .cookies
#     from starlette HTTPConnection so the same helper works unchanged.
#   * Upstream: hermes's own ?token=<_SESSION_TOKEN> query param. The SPA
#     fetches that token via /api/auth/session-token and includes it in the
#     WS URL, so we just forward path + query verbatim.
PROXIED_WS_PATHS = ("/api/pty", "/api/ws", "/api/events", "/api/plugins/*")


async def _ws_pump_client_to_upstream(
    client: WebSocket,
    upstream: websockets.WebSocketClientProtocol,
) -> None:
    """Forward client → upstream until the client side disconnects.

    Handles both binary (PTY bytes) and text (JSON-RPC) frames.
    """
    try:
        while True:
            msg = await client.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data is not None:
                await upstream.send(data)
                continue
            text = msg.get("text")
            if text is not None:
                await upstream.send(text)
    except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
        return
    except Exception as e:
        print(f"[ws-proxy] client→upstream error on {client.url.path}: {e!r}", flush=True)
        return


async def _ws_pump_upstream_to_client(
    upstream: websockets.WebSocketClientProtocol,
    client: WebSocket,
) -> None:
    """Forward upstream → client until upstream closes."""
    try:
        async for msg in upstream:
            if isinstance(msg, bytes):
                await client.send_bytes(msg)
            else:
                await client.send_text(msg)
    except (websockets.exceptions.ConnectionClosed, WebSocketDisconnect):
        return
    except Exception as e:
        print(f"[ws-proxy] upstream→client error on {client.url.path}: {e!r}", flush=True)
        return


async def ws_proxy(websocket: WebSocket) -> None:
    """Reverse-proxy a single WebSocket from browser → hermes dashboard.

    Order matters: connect upstream BEFORE accepting the client. If hermes
    is wedged or rejects the upgrade, we close the client with a meaningful
    code instead of accepting and then dropping silently.

    Connection lifecycle:
      1. Verify edge cookie auth → 4401 close on failure
      2. Open upstream WS with bounded open_timeout → 1011 on failure
      3. Accept client
      4. Spawn two pump tasks (bidirectional byte forwarding)
      5. When either direction ends (client navigates away, upstream PTY
         exits, etc.), cancel the other task and close both sockets
    """
    # 1. Edge auth.
    if not _is_authenticated(websocket):
        # Close before accept — browser sees the handshake fail (expected
        # for unauthenticated calls).
        await websocket.close(code=4401)
        return

    # 2. Build upstream URL preserving the SPA's path + query (the query
    #    contains the hermes session token + channel id).
    path = websocket.url.path
    qs = websocket.url.query
    upstream_url = f"ws://{HERMES_DASHBOARD_HOST}:{HERMES_DASHBOARD_PORT}{path}"
    if qs:
        upstream_url = f"{upstream_url}?{qs}"

    try:
        upstream = await websockets.connect(
            upstream_url,
            open_timeout=5,
            # Don't forward client cookies/headers — hermes WS auth is
            # purely token-based via the URL, and forwarding random
            # headers risks future upstream surprises.
        )
    except (asyncio.TimeoutError, OSError, websockets.exceptions.WebSocketException) as e:
        # Hermes dashboard down, restarting, or rejected the upgrade
        # (e.g. bad/missing session token).
        print(f"[ws-proxy] upstream connect failed for {path}: {e!r}", flush=True)
        # 1011 = internal error; client SPA will surface a generic close.
        await websocket.close(code=1011)
        return

    # 3. Both sides ready — accept and start pumping.
    await websocket.accept()

    pump_in = asyncio.create_task(_ws_pump_client_to_upstream(websocket, upstream))
    pump_out = asyncio.create_task(_ws_pump_upstream_to_client(upstream, websocket))

    try:
        # First side to finish wins; cancel the other.
        done, pending = await asyncio.wait(
            (pump_in, pump_out),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        # websockets.connect() outside `async with` doesn't auto-close;
        # do it explicitly. Same for the client side if still open.
        try:
            await upstream.close()
        except Exception:
            pass
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close()
            except Exception:
                pass


ANY_METHOD = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

routes = [
    # Public — no auth required.
    Route("/health",                            route_health),
    Route("/login",                             page_login,          methods=["GET"]),
    Route("/login",                             login_post,          methods=["POST"]),
    Route("/logout",                            logout),

    # Our setup wizard + management API, all under /setup/* (cookie-auth guarded).
    Route("/setup",                             page_index),
    Route("/setup/",                            page_index),
    Route("/setup/api/config",                  api_config_get,      methods=["GET"]),
    Route("/setup/api/config",                  api_config_put,      methods=["PUT"]),
    Route("/setup/api/status",                  api_status),
    Route("/setup/api/logs",                    api_logs),
    Route("/setup/api/gateway/start",           api_gw_start,        methods=["POST"]),
    Route("/setup/api/gateway/stop",            api_gw_stop,         methods=["POST"]),
    Route("/setup/api/gateway/restart",         api_gw_restart,      methods=["POST"]),
    Route("/setup/api/config/reset",            api_config_reset,    methods=["POST"]),
    Route("/setup/api/pairing/pending",         api_pairing_pending),
    Route("/setup/api/pairing/approve",         api_pairing_approve, methods=["POST"]),
    Route("/setup/api/pairing/deny",            api_pairing_deny,    methods=["POST"]),
    Route("/setup/api/pairing/approved",        api_pairing_approved),
    Route("/setup/api/pairing/revoke",          api_pairing_revoke,  methods=["POST"]),
    Route("/setup/api/oauth/xai/start",         api_oauth_xai_start,  methods=["POST"]),
    Route("/setup/api/oauth/xai/status",        api_oauth_xai_status),
    Route("/setup/api/oauth/xai",               api_oauth_xai_delete, methods=["DELETE"]),
    Route("/setup/api/backup/download",         api_backup_download),
    Route("/setup/api/backup/restore",          api_backup_restore,  methods=["POST"]),
    Route("/setup/api/backup/snapshots",        api_backup_snapshots),
    Route("/setup/api/backup/snapshots/{name}", api_backup_snapshot_download),

    # /setup/* typos return a real 404 — not a silent proxy fallthrough.
    Route("/setup/{path:path}",                 route_setup_404,     methods=ANY_METHOD),

    # Reverse-proxy hermes's dashboard WebSockets (Chat tab + sidecar).
    # WebSocketRoute is matched independently of HTTP routes, so order
    # relative to the catch-all HTTP `Route("/{path:path}", ...)` below
    # doesn't matter — but listing them as a group keeps the surface
    # area auditable. Only paths in PROXIED_WS_PATHS are forwarded;
    # /api/pub is intentionally omitted (not under /api/plugins/, so the
    # prefix route below does not match it).
    WebSocketRoute("/api/pty",                  ws_proxy),
    WebSocketRoute("/api/ws",                   ws_proxy),
    WebSocketRoute("/api/events",               ws_proxy),
    # Plugin-contributed sockets, mounted by hermes under /api/plugins/<name>/
    # (e.g. kanban's /api/plugins/kanban/events). Prefix-matched so new plugin
    # WS endpoints in future hermes releases proxy without re-touching this list.
    WebSocketRoute("/api/plugins/{path:path}",  ws_proxy),

    # Root: redirect to /setup if unconfigured, otherwise proxy the dashboard.
    Route("/",                                  route_root,          methods=ANY_METHOD),

    # Catch-all: everything else proxies to the Hermes dashboard subprocess.
    Route("/{path:path}",                       route_proxy,         methods=ANY_METHOD),
]

# No middleware — auth is enforced per-handler via guard(). This keeps /health
# and /login truly unauthenticated without middleware gymnastics.
app = Starlette(routes=routes, lifespan=lifespan)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)

    def _shutdown():
        loop.create_task(gw.stop())
        loop.create_task(dash.stop())
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown)

    loop.run_until_complete(server.serve())
