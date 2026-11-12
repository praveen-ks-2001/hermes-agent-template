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
import stat
import sys
import tempfile
import time
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

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

# The Hermes release this image pins. The Dockerfile promotes its `ARG
# HERMES_REF` to an ENV so we can read it here; a Railway service variable of
# the same name (the documented way to pin an older release) shadows that ENV,
# so this always reflects what actually got built rather than a hardcoded
# string that would go stale — or worse, misreport after a deliberate rollback.
HERMES_VERSION = os.environ.get("HERMES_REF", "").strip()


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


def pairing_dir() -> Path:
    """Resolve the pairing store on every call — never cache it at import.

    `/setup/api/backup/restore` shells out to `hermes import`, which can create
    a *populated* legacy ``pairing/`` mid-process. That flips the dir hermes
    itself resolves (gateway/pairing.py's ``PAIRING_DIR``), so a value frozen at
    import would leave this admin panel reading the dir the gateway abandoned:
    pending users invisible, approvals written where nothing reads them, until
    the next container restart. Costs two stat calls per request.
    """
    return _resolve_pairing_dir()


def _consolidate_pairing_dirs() -> None:
    """Union the inactive pairing dir into the active one, then clear it.

    hermes >= v2026.7.20 added ``_migrate_split_pairing_dirs()``
    (gateway/pairing.py), which copies the inactive dir into the active one on
    EVERY ``PairingStore()`` construction and never prunes the source. A revoke
    therefore only clears the active copy and is silently resurrected from the
    stale one on the next gateway boot — a de-authorized user regains access
    while this panel shows them removed. hermes' own ``revoke()`` has the same
    hole, so calling its API instead would not help; the fix is to make sure a
    second populated dir never survives.

    Only `/setup/api/backup/restore` can create that split here (start.sh seeds
    only the consolidated path), so we run this after a restore and once at
    boot to heal deployments split by an earlier restore.

    Safety: the active dir always wins on key conflict (matching hermes'
    ``_merge_pairing_dir``), and the alternate's files are removed only after
    the union has been written. Deleting them cannot flip the resolution —
    when legacy is active it stays populated; when it is not active it is empty
    by definition.
    """
    active = _resolve_pairing_dir()
    legacy = Path(HERMES_HOME) / "pairing"
    consolidated = Path(HERMES_HOME) / "platforms" / "pairing"
    try:
        alternate = legacy if active.resolve() == consolidated.resolve() else consolidated
        # An old volume may have pairing/ symlinked to platforms/pairing/.
        # Both spellings then refer to the same files: merging followed by
        # unlinking the "alternate" would delete live approvals.
        if alternate.resolve() == active.resolve():
            return
    except (OSError, RuntimeError):
        return
    if not alternate.is_dir():
        return
    for src in sorted(alternate.glob("*.json")):
        if not src.is_file():
            continue
        try:
            stale = _read_pairing_json(src)
        except (OSError, ValueError) as e:
            print(f"[pairing] keeping unreadable alternate {src}: {e}", flush=True)
            continue
        if not stale:
            # An empty leftover is inert: hermes' own merge skips it too.
            continue
        dest = active / src.name
        merged = dict(stale)
        try:
            merged.update(_read_pairing_json(dest))   # live entries win
            _wjson(dest, merged)
        except (OSError, ValueError) as e:
            print(f"[pairing] consolidate failed for {src.name}: {e}", flush=True)
            continue          # leave the source intact — never drop the only copy
        src.unlink(missing_ok=True)
        print(f"[pairing] consolidated {src.name}: {alternate} -> {active}", flush=True)


# ── Global emergency stop / pause (hermes >= v2026.8.13) ─────────────────────
ESTOP_FILE = Path(HERMES_HOME) / "ESTOP"


def estop_state() -> dict | None:
    """Pause details, or None when the bot is accepting work.

    v2026.8.13 added `hermes pause` and the in-chat `/pause`, which write a
    sentinel at ``$HERMES_HOME/ESTOP`` (agent/estop.py). While it exists hermes
    refuses every NEW gateway turn, cron dispatch and kanban dispatch with
    "⏸️ Hermes is paused" — but the process stays alive, so `/health` keeps
    returning 200, `gateway_state.json` still reads "running" and the platform
    still shows the bot online. Without this check the admin panel would show a
    green, healthy deployment while the bot answers nothing.

    Two details make it worse than an ordinary setting, and are why this is
    surfaced rather than ignored: the sentinel lives on the Railway volume, so
    it SURVIVES a redeploy (the reflexive "just redeploy" fix does not clear
    it), and `/pause` is `gateway_only` with no owner gate, so any paired
    messaging user can engage it — it is not necessarily operator-driven.

    Upstream's fail-SAFE bias is copied deliberately: an unreadable sentinel
    counts as paused. Failing open here would report "running" for exactly the
    deployment that is refusing every message. A corrupt or empty body is still
    a pause, with the metadata reported as None (same as upstream get_state()).
    """
    try:
        if not ESTOP_FILE.exists():
            return None
    except OSError:
        return {"reason": None, "engaged_at": None}
    reason = engaged_at = None
    try:
        raw = json.loads(ESTOP_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            reason = raw.get("reason") or None
            engaged_at = raw.get("engaged_at") or None
    except (OSError, ValueError):
        pass
    return {"reason": reason, "engaged_at": engaged_at}


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
    # MiniMax runs two separate platforms with separate accounts and keys:
    # global (api.minimax.io) and China (api.minimaxi.com). Hermes ships both as
    # first-class providers, so a CN key is a normal provider entry here — no
    # custom-endpoint plumbing needed, and both can be configured at once.
    ("MINIMAX_CN_API_KEY",       "MiniMax (China)",          "provider",  True),
    ("HF_TOKEN",                 "Hugging Face",             "provider",  True),
    # Added in v2026.4.23+ (hermes v0.11.0+). All plain API-key auth — hermes
    # auto-routes by env-var presence, no extra config needed on our side.
    # OAuth-based providers (xAI Grok SuperGrok, Gemini CLI, Qwen OAuth, Claude Code)
    # are set up via the dashboard's Keys tab or HERMES_AUTH_JSON_BOOTSTRAP.
    # New in hermes v2026.8.13. Plain API key (ac_…); ACTUAL_BASE_URL is
    # deliberately NOT surfaced — it defaults to https://api.actual.inc/v1 and
    # only needs overriding for their local offline daemon, which cannot be
    # reached from a Railway container anyway. A new ENV_VARS *category* would
    # also have to be added to write_env()'s cat_order or the value is silently
    # dropped from .env, so keeping this on "provider" is the safe shape.
    ("ACTUAL_API_KEY",           "Actual Computer",          "provider",  True),
    ("NVIDIA_API_KEY",           "NVIDIA NIM",               "provider",  True),
    ("ARCEEAI_API_KEY",          "Arcee AI",                 "provider",  True),
    ("STEPFUN_API_KEY",          "Step Plan",                "provider",  True),
    ("GEMINI_API_KEY",           "Google AI Studio",         "provider",  True),
    ("NOVITA_API_KEY",           "NovitaAI",                 "provider",  True),
    ("FIREWORKS_API_KEY",        "Fireworks AI",             "provider",  True),
    ("ATLASCLOUD_API_KEY",       "Atlas Cloud",              "provider",  True),
    ("UBERLLM_API_KEY",          "UberLLM",                  "provider",  True),
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
    # OpenAI-compatible routing gateways. These are separate saved slots rather
    # than aliases for CUSTOM_PROVIDER_* so users can keep both routers (and a
    # generic custom endpoint) configured at the same time. Their URLs are
    # deployment-specific — especially on Railway private networking — so only
    # the setup-page examples are fixed; the values themselves never are.
    ("NINEROUTER_API_KEY",        "9Router endpoint key",      "provider",  True),
    ("NINEROUTER_BASE_URL",       "9Router base URL",          "router",    False),
    ("OMNIROUTE_API_KEY",         "OmniRoute endpoint key",    "provider",  True),
    ("OMNIROUTE_BASE_URL",        "OmniRoute base URL",        "router",    False),
    # Custom OpenAI-compatible endpoint — one slot; more via Hermes dashboard.
    # Only the API key is in category "provider" so PROVIDER_KEYS / is_config_complete
    # only trigger when an actual key is present, not just a base URL.
    ("CUSTOM_PROVIDER_API_KEY",  "Custom Provider key",      "provider",  True),
    ("CUSTOM_PROVIDER_BASE_URL", "Custom Provider base URL", "custom",    False),
    ("CUSTOM_PROVIDER_NAME",     "Custom Provider name",     "custom",    False),
    ("PARALLEL_API_KEY",         "Parallel (search)",        "tool",      True),
    ("FIRECRAWL_API_KEY",        "Firecrawl (scrape)",       "tool",      True),
    ("KEENABLE_API_KEY",         "Keenable (search)",        "tool",      True),
    # Tavily was DELETED in v2026.8.31 (so we dropped this field) and RESTORED in
    # v2026.9.11 — upstream reverted the removal, and tools/tool_backend_helpers.py
    # now carries an empty REMOVED_BACKENDS registry naming that revert. Anyone who
    # had selected Tavily as their web backend before the deletion has had a hard
    # error on every search since, because tools/web_tools.py:_get_backend() returns
    # a stored selection strictly with no fallback; restoring the field lets them see
    # and re-enter the key. Optional by design: Tavily also works keyless once it is
    # the selected backend, so an empty value here is a valid state.
    ("TAVILY_API_KEY",           "Tavily (search)",          "tool",      True),
    # New vendor in v2026.9.11 (plugins/web/perplexity). Key is REQUIRED — unlike
    # Tavily/Keenable it has no keyless tier — and it now sits second in the
    # autodetect ladder, so a key here is picked up on a never-configured install.
    ("PERPLEXITY_API_KEY",       "Perplexity (search)",      "tool",      True),
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
# Display names for the admin UI, straight from the registry above.
ENV_LABELS = {k: l for k, l, _, _ in ENV_VARS}

# Maps our own provider-key env var to hermes' OWN canonical provider id
# (hermes_cli/auth.py PROVIDER_REGISTRY, verified against v2026.7.1). Used by
# set_active_model_via_hermes() to pin an explicit model.provider via hermes'
# own POST /api/model/set instead of leaving config.yaml on "auto" once 2+
# provider keys exist in .env — see write_config_yaml()'s docstring for why
# "auto" alone is unsafe with multiple providers configured. Several ids are
# non-obvious renames upstream (dashscope->alibaba, glm->zai, kimi->kimi-coding,
# hf->huggingface, ollama->ollama-cloud) — re-verify every entry against
# hermes_cli/auth.py on a Hermes version bump (same audit as the WS allowlist).
# Re-verified against v2026.8.13's PROVIDER_REGISTRY: all ids below still exist,
# none renamed, `actual` added. Note "openrouter" is NOT a PROVIDER_REGISTRY
# entry in either release — resolve_provider() special-cases it (auth.py's
# `if normalized == "openrouter": return "openrouter"`), so it stays valid.
HERMES_PROVIDER_IDS = {
    "OPENROUTER_API_KEY":    "openrouter",
    "DEEPSEEK_API_KEY":      "deepseek",
    "DASHSCOPE_API_KEY":     "alibaba",       # "Qwen Cloud" in hermes' own UI
    "GLM_API_KEY":           "zai",           # "Z.AI / GLM"
    "KIMI_API_KEY":          "kimi-coding",
    "MINIMAX_API_KEY":       "minimax",
    "MINIMAX_CN_API_KEY":    "minimax-cn",    # China platform (api.minimaxi.com)
    "HF_TOKEN":              "huggingface",
    "ACTUAL_API_KEY":        "actual",        # Actual Computer (v2026.8.13+)
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
    # Named user providers written under config.yaml's `providers:` mapping.
    # These are not built-in Hermes plugins: writing the definition first lets
    # Hermes' own model/set path resolve the id, preserve slash-prefixed router
    # model names verbatim, and carry key_env instead of copying the secret into
    # config.yaml's model block.
    "NINEROUTER_API_KEY":     "ninerouter",
    "OMNIROUTE_API_KEY":      "omniroute",
    # Fireworks, Novita, Atlas Cloud and UberLLM are routed through provider="custom" with a fixed
    # base_url (CUSTOM_STYLE_BASE_URLS below) rather than their native ids.
    #
    # CORRECTION (v2026.8.27 audit): the old comment here claimed they are absent
    # from hermes' PROVIDER_REGISTRY. That is false and was false when written —
    # the registry auto-extends from plugins/model-providers/, and both ids are
    # present with the right env vars in the BUILT IMAGE at v2026.8.13 and
    # v2026.8.27 (registry grew 47 -> 58 entries this bump). The custom routing
    # is kept anyway: it is what existing deployments have saved in config.yaml,
    # and switching to native ids is a migration, not a comment fix. Note
    # "openrouter" is genuinely absent from the registry yet still valid — it is
    # hermes' built-in default, resolved outside the plugin registry
    # (resolve_provider("openrouter") -> "openrouter", verified in-image).
    #
    # CUSTOM_PROVIDER_API_KEY is different: its base_url really is user-supplied
    # (CUSTOM_PROVIDER_BASE_URL), so it has no entry in CUSTOM_STYLE_BASE_URLS.
    # hermes' bare-"custom" trust path reads model.base_url / model.api_key from
    # the model block — config.yaml's custom_providers[] list is display-only.
    "CUSTOM_PROVIDER_API_KEY": "custom",   # base_url is user-supplied (CUSTOM_PROVIDER_BASE_URL) — any OpenAI-compatible endpoint, e.g. 9Router
    "FIREWORKS_API_KEY":       "custom",
    "NOVITA_API_KEY":          "custom",
    "ATLASCLOUD_API_KEY":      "custom",      # OpenAI-compatible gateway, fixed base_url below
    "UBERLLM_API_KEY":         "custom",      # OpenAI-compatible gateway, fixed base_url below
}

# Fixed base URLs for the "custom"-style providers above whose credential is a
# plain API key against a well-known OpenAI-compatible endpoint. Absent here
# (CUSTOM_PROVIDER_API_KEY) means the base_url is user-supplied instead — see
# CUSTOM_PROVIDER_BASE_URL.
CUSTOM_STYLE_BASE_URLS = {
    "FIREWORKS_API_KEY": "https://api.fireworks.ai/inference/v1",
    "NOVITA_API_KEY":    "https://api.novita.ai/openai/v1",
    "ATLASCLOUD_API_KEY": "https://api.atlascloud.ai/v1",
    "UBERLLM_API_KEY":    "https://api.uberllm.dev/v1",
}

# Every ENV_VARS "provider" key pinned to the literal "custom" id above.
# Computed, not hand-maintained, so a future provider added to
# HERMES_PROVIDER_IDS with value "custom" is automatically covered by both
# api_config_put()'s pin call and write_config_yaml()'s fallback below —
# no other code needs to change.
HERMES_CUSTOM_STYLE_KEYS = {k for k, v in HERMES_PROVIDER_IDS.items() if v == "custom"}

# Template-managed named OpenAI-compatible routing gateways. `base_url_key` is
# deliberately independent for each entry; unlike the one generic custom slot,
# both routers can coexist and be switched through Hermes' normal provider path.
MANAGED_ROUTER_PROVIDERS = {
    "NINEROUTER_API_KEY": {
        "provider_id": "ninerouter",
        "name": "9Router",
        "base_url_key": "NINEROUTER_BASE_URL",
    },
    "OMNIROUTE_API_KEY": {
        "provider_id": "omniroute",
        "name": "OmniRoute",
        "base_url_key": "OMNIROUTE_BASE_URL",
    },
}


def _router_base_url(data: dict[str, str], provider_key: str) -> str:
    spec = MANAGED_ROUTER_PROVIDERS.get(provider_key)
    if not spec:
        return ""
    return data.get(spec["base_url_key"], "").strip().rstrip("/")


def _router_provider_is_configured(data: dict[str, str], provider_key: str) -> bool:
    return bool(data.get(provider_key, "").strip() and _router_base_url(data, provider_key))


def _router_base_url_error(value: str) -> str | None:
    """Return a setup-facing error for an unusable router endpoint URL."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "Base URL is invalid."
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "Base URL must be a complete http:// or https:// URL."
    if parsed.username or parsed.password:
        return "Base URL must not contain embedded credentials; use the API key field."
    return None

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


def _read_config_for_write(path: Path) -> dict:
    """Read the whole existing config, refusing to replace a damaged file.

    Hermes v2026.9.24 makes every native config writer fail closed. The admin
    panel and xAI OAuth write this same file, so they must reject a bad read
    before changing either config.yaml or its companion .env/auth.json files.
    """
    import hermes_yaml as yaml

    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("config.yaml is not a regular file; existing settings were left untouched")
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError("Could not read config.yaml; existing settings were left untouched") from exc
    try:
        loaded = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError("config.yaml is invalid YAML; existing settings were left untouched") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("config.yaml must contain a mapping; existing settings were left untouched")
    return loaded


def _write_config_atomic(path: Path, data: dict) -> None:
    """Use the pinned Hermes writer for atomic, comment-preserving config saves."""
    from hermes_cli.config import atomic_config_replace

    # This is the complete desired state: model switches and provider removal
    # deliberately delete stale fields. atomic_config_write now rejects omissions.
    atomic_config_replace(path, data)


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace a credential file without exposing a truncated intermediate file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1  # the stream owns and closes it from here on
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        if fd >= 0:
            os.close(fd)
        Path(tmp_name).unlink(missing_ok=True)


def _file_snapshot(path: Path) -> tuple[bytes, int] | None:
    """Keep exact pre-save bytes and mode for a multi-file credential update."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path.name} is not a regular file")
        return stream.read(), stat.S_IMODE(info.st_mode)


def _restore_file_snapshot(path: Path, snapshot: tuple[bytes, int] | None) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    contents, mode = snapshot
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.restore.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def write_config_yaml(data: dict[str, str], *, reset_model: bool = False, preserve_model: bool = False) -> None:
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
    model = data.get("LLM_MODEL", "")
    config_path = Path(HERMES_HOME) / "config.yaml"
    existing = _read_config_for_write(config_path)

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
        # Restarts must not replay a stale .env model over a native-dashboard
        # selection. Only an explicit setup edit owns model changes.
        preserving_model = preserve_model and bool(merged_model.get("default") or merged_model.get("model"))
        if not preserving_model:
            if model or not preserve_model:
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
            router_key = next(
                (k for k in MANAGED_ROUTER_PROVIDERS if _router_provider_is_configured(data, k)),
                None,
            )
            named_key = next(
                (
                    k for k in PROVIDER_KEYS
                    if k not in HERMES_CUSTOM_STYLE_KEYS
                    and k not in MANAGED_ROUTER_PROVIDERS
                    and data.get(k)
                ),
                None,
            )
            custom_style_key = next((k for k in HERMES_CUSTOM_STYLE_KEYS if data.get(k)), None)
            if named_key:
                merged_model["provider"] = "auto"
                current_provider = "auto"
            elif router_key:
                # Unlike built-in env-var providers, user-defined `providers:`
                # entries are not discoverable through Hermes' `auto` registry.
                # Pin the only available router synchronously so a cold boot is
                # still usable if the dashboard model/set request never ran.
                merged_model["provider"] = MANAGED_ROUTER_PROVIDERS[router_key]["provider_id"]
                current_provider = merged_model["provider"]
            elif custom_style_key:
                # CUSTOM_PROVIDER_API_KEY / FIREWORKS_API_KEY / NOVITA_API_KEY / ATLASCLOUD_API_KEY / UBERLLM_API_KEY are
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
        # When writing a new built-in assignment, clear old inline routing:
        # base_url takes precedence over provider upstream, so a former custom
        # endpoint can silently misroute the newly selected model. Custom/local
        # providers instead need their inline endpoint and credentials; the
        # separate custom_providers[] catalog does not supply runtime routing.
        # Hermes v0.22.2 writes routing fields even for built-ins (Codex writes
        # base_url/api_mode). A restart or unrelated save must keep the entire
        # saved assignment; explicit selections still clear stale routing here
        # or through Hermes' own model/set transaction below.
        if not preserving_model and current_provider and current_provider.lower() not in ("custom", "local"):
            for _stale in ("base_url", "api_key", "api", "api_mode"):
                merged_model.pop(_stale, None)
    merged["model"] = merged_model

    merged_terminal = dict(merged.get("terminal") if isinstance(merged.get("terminal"), dict) else {})
    merged_terminal["backend"] = "local"
    merged_terminal["timeout"] = 60
    merged_terminal["cwd"] = "/tmp"
    merged["terminal"] = merged_terminal

    # This image has no browser runtime. Keep upstream's explicit opt-out as
    # the default; preserve a backend the operator configured in Hermes.
    merged_browser = dict(merged.get("browser") if isinstance(merged.get("browser"), dict) else {})
    merged_browser.setdefault("backend", "off")
    merged["browser"] = merged_browser

    merged_agent = dict(merged.get("agent") if isinstance(merged.get("agent"), dict) else {})
    merged_agent.setdefault("max_iterations", 50)
    merged["agent"] = merged_agent

    # NOTE: we used to pin `session_reset.mode = "both"` here so conversation
    # auto-reset didn't depend on volume age. v2026.9.11 RETIRED the feature:
    # SessionResetPolicy is now documented as an "inert legacy value type ...
    # Gateway configuration and session lifecycle do not consume this datatype"
    # (gateway/config.py), the config->gateway bridge that read it is gone
    # (`default_reset_policy` went from 10 references to 0), and hermes dropped
    # `session_reset` from its own known-root-keys list. Writing it now only
    # leaves a dead key on disk — unknown top-level keys are deliberately not
    # warned about, so it would fail silently. Conversations persist until an
    # explicit /new or /reset; context growth is handled by compression.

    merged["data_dir"] = HERMES_HOME

    # Named routing gateways — merge only the two entries this template owns.
    # User-authored providers and extra per-router fields (model metadata,
    # headers, context limits, etc.) survive. A blank setup entry removes the
    # corresponding provider only when its key_env proves we created it; a
    # manually-authored provider with the same id is never deleted implicitly.
    existing_providers = merged.get("providers")
    providers = dict(existing_providers) if isinstance(existing_providers, dict) else {}
    providers_changed = False
    for provider_key, spec in MANAGED_ROUTER_PROVIDERS.items():
        provider_id = spec["provider_id"]
        base_url = _router_base_url(data, provider_key)
        current_entry = providers.get(provider_id)
        managed_entry = (
            isinstance(current_entry, dict)
            and str(current_entry.get("key_env") or "").strip() == provider_key
        )
        if data.get(provider_key, "").strip() and base_url:
            entry = dict(current_entry) if isinstance(current_entry, dict) else {}
            # Avoid retaining a conflicting legacy URL alias: Hermes accepts
            # api/url/base_url, in that precedence order.
            entry.pop("url", None)
            entry.pop("base_url", None)
            entry.update({
                "name": spec["name"],
                "api": base_url,
                "key_env": provider_key,
                "transport": "chat_completions",
                "discover_models": True,
            })
            providers[provider_id] = entry
            providers_changed = True
        elif managed_entry:
            providers.pop(provider_id, None)
            providers_changed = True
    if providers_changed:
        if providers:
            merged["providers"] = providers
        else:
            merged.pop("providers", None)

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

    _write_config_atomic(config_path, merged)


# ── Hermes dashboard auth (so MCP OAuth redirects resolve to the real host) ──
# v2026.8.27 routes `dashboard.public_url` into should_require_dashboard_auth()
# (hermes_cli/web_server.py): declaring a public URL turns hermes' own auth gate
# on even for a loopback bind. That URL is ALSO the base hermes builds MCP OAuth
# redirect_uris from (_mcp_oauth_callback_url), so suppressing it — the previous
# fix — left every OAuth MCP server redirecting to http://127.0.0.1:9119, a dead
# page in the user's browser.
#
# So we satisfy the gate instead of dodging it: hermes' bundled `basic` auth
# provider is configured from the same admin credentials the setup page uses,
# and HermesSession below logs in on the user's behalf so nobody ever sees a
# second login screen. Verified live on v2026.8.27: with the gate on, the
# redirect_uri comes back as https://<public-domain>/api/mcp/oauth/callback/<x>.


def hermes_dashboard_credentials() -> tuple[str, str]:
    """Credentials hermes' `basic` auth provider accepts.

    Single source of truth: the same pair is handed to the dashboard subprocess
    AND used by HermesSession to log in. Resolving them in two places would let
    them drift, and a drift means every proxied page 401s.
    """
    user = os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "").strip()
    pw = os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "").strip()
    return (user or ADMIN_USERNAME), (pw or ADMIN_PASSWORD)


def hermes_dashboard_auth_secret() -> str:
    """Token-signing key for hermes' basic-auth sessions, stable across deploys.

    Generated once and kept on the volume. Without a fixed secret hermes mints a
    random key per process, so every dashboard restart (each config save calls
    Dashboard.restart()) would invalidate the session HermesSession holds and
    force a needless re-login.
    """
    explicit = os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_SECRET", "").strip()
    if explicit:
        return explicit
    path = Path(HERMES_HOME) / ".dashboard_auth_secret"
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    secret = secrets.token_hex(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret)
        path.chmod(0o600)
    except OSError as e:
        # Falls back to a per-process value: sessions then die on each dashboard
        # restart, which HermesSession recovers from silently.
        print(f"[server] could not persist dashboard auth secret: {e}", flush=True)
    return secret


# Hermes otherwise generates a new legacy SPA/WS token for every dashboard
# process. Config saves and restores deliberately respawn that process while an
# existing Chat page is still open; the page then retries with its old token and
# every /api/pty + /api/events upgrade is rejected with 403 until the user
# reloads. Keep the token stable only for this wrapper process's lifetime. It
# still rotates on every container restart, and gated/public dashboards reject
# this legacy token in favour of their normal single-use WS tickets.
_DASHBOARD_SESSION_TOKEN = secrets.token_urlsafe(32)


def hermes_dashboard_public_url() -> str:
    """Public origin hermes should build OAuth redirect_uris from, or ""."""
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{domain}" if domain else ""


# Env keys that kill the dashboard if a hermes subprocess ever sees them. The
# dashboard has no respawn supervisor, so each one means every proxied page 503s
# until the container is redeployed, while /setup and /health stay green.
#
#   HERMES_PARENT_PID            v2026.8.13's _start_parent_death_watchdog()
#                                (hermes_cli/web_server.py) polls it and
#                                os._exit(0)s once that pid is gone. It is the
#                                Electron desktop's orphan guard and is NOT
#                                gated on HERMES_DESKTOP, so it arms on any
#                                `hermes dashboard` carrying the key.
#   HERMES_DASHBOARD_PUBLIC_URL  v2026.8.27 feeds it to
#                                should_require_dashboard_auth(); a non-loopback
#                                value turns the auth gate on even for a
#                                loopback bind, and with no hermes auth provider
#                                configured the dashboard SystemExits at
#                                startup. build_hermes_env() re-adds it AFTER
#                                this strip, paired with the basic-auth
#                                credentials that satisfy the gate — an inbound
#                                value would arrive without them.
#
# Stripped from BOTH the subprocess env and $HERMES_HOME/.env: hermes loads that
# file into its own os.environ at startup, so popping alone is not enough
# (reproduced locally for HERMES_PARENT_PID against v2026.8.13).
DASHBOARD_KILLING_KEYS = ("HERMES_PARENT_PID", "HERMES_DASHBOARD_PUBLIC_URL")

# Keys that don't kill the dashboard but change WHO it trusts. hermes loads
# $HERMES_HOME/.env into its own os.environ with override=True at startup —
# i.e. AFTER the env build_hermes_env() hands the subprocess — so a value in the
# FILE beats the credentials we pass. hermes' `basic` provider then registers a
# different user/password than HermesSession signs in with, every proxied page
# 502s with DASHBOARD_AUTH_FAILED_HTML, and nothing says why. A restore, a hand
# edit, or hermes' own Keys tab can all put them there.
#
# Only the FILE is policed: hermes_dashboard_credentials() deliberately honours
# these as explicit overrides when they arrive as real Railway variables.
DASHBOARD_TRUST_KEYS = (
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
    "HERMES_DASHBOARD_SESSION_TOKEN",
)

# Only a restored or hand-edited .env can carry these, so .env is healed once at
# boot and after a restore rather than checked on every read.
ENV_FILE_FORBIDDEN_KEYS = DASHBOARD_KILLING_KEYS + DASHBOARD_TRUST_KEYS


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
    from template_helpers.migrate_hermes_configs import IMAGE_EXECUTION_ENV_KEYS, validate_profile_execution_environment

    validate_profile_execution_environment(Path(HERMES_HOME))
    env = {**os.environ, "HERMES_HOME": HERMES_HOME}
    env.update(read_env(ENV_FILE))
    for key in IMAGE_EXECUTION_ENV_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
        else:
            env.pop(key, None)
    env.pop("HERMES_LAZY_INSTALL_TARGET", None)
    # Retire hermes' own respawn-storm breaker (new in v2026.7.20,
    # hermes_cli/gateway.py `run_gateway`): once >5 starts land in a rolling
    # 120s window it BLOCKS with time.sleep() before the gateway boots, counted
    # in a persistent ledger ($HERMES_HOME/gateway-starts.log) that this
    # supervisor can neither see nor reset. It duplicates our own crash-loop
    # guard (RESPAWN_MAX_IN_WIN/RESPAWN_WINDOW_S) — except ours deliberately
    # clears its budget on a manual Start/Restart, so six quick /setup saves
    # during first-run setup would otherwise stall the gateway ~10s with the bot
    # offline while Gateway.start() has already reported "running" and /health
    # 200s. `HERMES_GATEWAY_MAX_STARTS` is upstream's documented escape hatch:
    # <= 0 skips the check and the ledger write entirely. setdefault so a
    # Railway service variable or .env can still re-enable it.
    env.setdefault("HERMES_GATEWAY_MAX_STARTS", "0")
    # v2026.8.3's `agent.restart_after_turn_timeout` (default 21600) makes
    # /restart wait for the active turn, so a wedged turn leaves the bot alive,
    # /health 200 and refusing every message for up to 6h. `0` is upstream's
    # documented disable and restores v2026.7.20's immediate drain. Read before
    # config.yaml, and inherited by all three restart paths (in-band, SIGUSR1,
    # and the dashboard's detached restart). setdefault: set e.g. 120 to re-arm.
    env.setdefault("HERMES_RESTART_AFTER_TURN_TIMEOUT", "0")
    # v2026.8.31 moved the terminal/code-exec scratch root from /tmp to
    # $HERMES_HOME/cache/terminal whenever TERMINAL_TEMP_DIR and TMPDIR are both
    # unset (tools/environments/local.py). Upstream's motive was a tmpfs /tmp
    # filling up; on Railway /tmp is ordinary container disk and $HERMES_HOME is
    # the PERSISTENT VOLUME, so the new default parks sandbox dirs and
    # background-job logs on /data, ships them inside every `hermes backup`
    # (cache/ is not in upstream's _EXCLUDED_DIRS), and lets a locked *.db an
    # agent script leaves there fail _safe_copy_db — which _live_db_names() then
    # reports as an incomplete snapshot and ABORTS the restore. Pin the
    # v2026.8.27 behaviour. Must be an existing absolute dir or hermes ignores it
    # (/tmp always exists here).
    env.setdefault("TERMINAL_TEMP_DIR", "/tmp")
    # v2026.9.21 widened that persistent-scratch default beyond the terminal:
    # hermes_bootstrap now points generic TMPDIR/TMP/TEMP at
    # $HERMES_HOME/cache/scratch for EVERY hermes entry point when none is set.
    # On this Railway image /tmp is ordinary ephemeral container disk, while
    # HERMES_HOME is the durable volume; browser spools, PTY probes and Python
    # tempfile users therefore belong in /tmp for the same reason terminal
    # sandboxes do. Setting only TMPDIR is sufficient: upstream treats any
    # user-supplied temp variable as authoritative and leaves all three alone.
    # setdefault preserves an explicit Railway/.env override.
    env.setdefault("TMPDIR", "/tmp")
    # Pin every hermes subprocess to the root profile. hermes_cli/main.py's
    # _apply_profile_override() runs at IMPORT — before argparse — so the
    # `--external-supervisor` flag we pass (which only sets
    # HERMES_GATEWAY_EXTERNAL_SUPERVISOR after parsing) is too late to stop it.
    # It follows $HERMES_HOME/active_profile, which hermes' own dashboard and a
    # `hermes profile use` in the Chat terminal both write. One such switch would
    # silently re-home the gateway, the dashboard AND the dashboard's detached
    # restart under profiles/<name>: pairing, config and the pid record then
    # diverge from what this admin panel reads, and the next `--replace` refuses
    # with "pid record belongs to a different HERMES_HOME". v2026.8.31 added this
    # generic marker (#74872) as the opt-out; it is read ONLY by that guard
    # (main.py `_under_gateway_supervisor`), never by is_gateway_supervisor_process()
    # or the s6 redirect, so it cannot alter the exit-75 restart contract.
    env.setdefault("HERMES_SUPERVISED_CHILD", "1")
    # Keep hermes' per-token gateway locks OFF the Railway volume. They are
    # MACHINE-local by intent (gateway/status.py `_get_lock_dir`), but they
    # default to $HOME/.local/state/hermes/gateway-locks and the Dockerfile sets
    # HOME=/data — so they land on the volume and outlive a redeploy, while
    # start.sh only sweeps gateway.pid/.lock/.sock under $HERMES_HOME. That
    # matters more since v2026.9.11: `is_global_startup_conflict`
    # (gateway/restart.py, new) reclassifies a `<scope>_lock` conflict at STARTUP
    # from retryable to fatal, so a single-platform deployment whose one adapter
    # hits it exits 78 — which this supervisor deliberately never respawns. One
    # container runs exactly one gateway, so the lock only ever needs to live as
    # long as the container. /tmp gives it that and nothing more; hermes mkdirs
    # the path itself. setdefault, so a Railway variable can move it back.
    env.setdefault("HERMES_GATEWAY_LOCK_DIR", "/tmp/hermes-gateway-locks")
    # Drop inbound values first: the template is the only thing allowed to
    # decide these (this pop covers a Railway service variable, which lands in
    # our own os.environ; _sanitize_env_file() covers the .env file).
    for key in DASHBOARD_KILLING_KEYS:
        env.pop(key, None)
    # Configure hermes' own auth provider, then declare the public URL. The URL
    # engages hermes' auth gate, and the gate SystemExits at startup unless a
    # provider is registered — so it is only ever set alongside credentials that
    # satisfy it. ADMIN_PASSWORD is always populated (generated when unset), so
    # in practice this is always on; the guard keeps the ordering explicit.
    user, password = hermes_dashboard_credentials()
    if user and password:
        env["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"] = user
        env["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] = password
        env["HERMES_DASHBOARD_BASIC_AUTH_SECRET"] = hermes_dashboard_auth_secret()
        public_url = hermes_dashboard_public_url()
        if public_url:
            env["HERMES_DASHBOARD_PUBLIC_URL"] = public_url
    return env


def build_dashboard_env() -> dict[str, str]:
    """Build the dashboard env with a token stable across in-container respawns.

    Keep this dashboard-only: the gateway neither needs nor should inherit the
    browser's loopback session credential. An inbound Railway/.env value is
    intentionally overwritten; this wrapper owns both ends of that trust
    boundary, and `_sanitize_env_file()` prevents Hermes' override=True dotenv
    load from replacing it after spawn.
    """
    env = build_hermes_env()
    env["HERMES_DASHBOARD_SESSION_TOKEN"] = _DASHBOARD_SESSION_TOKEN
    return env


def _sanitize_env_file() -> None:
    """Drop .env keys that would kill the dashboard or break its sign-in."""
    try:
        data = read_env(ENV_FILE)
    except OSError:
        return
    removed = [k for k in ENV_FILE_FORBIDDEN_KEYS if k in data]
    if not removed:
        return
    for key in removed:
        data.pop(key, None)
    try:
        write_env(ENV_FILE, data)
    except OSError as e:
        print(f"[server] could not strip {removed} from .env: {e}", flush=True)
        return
    print(f"[server] removed {', '.join(removed)} from .env — it would have "
          f"shut the dashboard down or broken its sign-in", flush=True)


def write_env(path: Path, data: dict[str, str]) -> None:
    # Single chokepoint keeping ENV_FILE_FORBIDDEN_KEYS out of the FILE, not just
    # out of the subprocess env. _sanitize_env_file() only runs at boot and after
    # a restore, but /setup/api/config re-reads .env, merges it and writes it back
    # — so a key added to the file between boots (hand edit, hermes' own Keys tab)
    # survived that round-trip and reached the dashboard restart that same save
    # triggers. Verified live: with a stray HERMES_DASHBOARD_BASIC_AUTH_PASSWORD
    # the dashboard accepted THAT password and 401'd the template's own, which a
    # still-valid persisted session hid until the next re-login.
    forbidden = [k for k in ENV_FILE_FORBIDDEN_KEYS if k in data]
    if forbidden:
        data = {k: v for k, v in data.items() if k not in ENV_FILE_FORBIDDEN_KEYS}
        print(f"[server] refused to write {', '.join(forbidden)} to .env — "
              f"it would have shut the dashboard down or broken its sign-in",
              flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    cat_order = ["model", "provider", "bedrock", "azure", "router", "custom", "tool",
                 "telegram", "discord", "slack", "whatsapp",
                 "email", "mattermost", "matrix", "gateway", "admin"]
    cat_labels = {
        "model": "Model", "provider": "Providers",
        "bedrock": "AWS Bedrock", "azure": "Azure Foundry",
        "router": "Router Endpoints",
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

    _atomic_write_text(path, "\n".join(lines))


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
    try:
        existing = json.loads(auth_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        existing = {}
    except (OSError, ValueError) as exc:
        raise ValueError("Could not read auth.json; existing credentials were left untouched") from exc
    if not isinstance(existing, dict):
        raise ValueError("auth.json must contain an object; existing credentials were left untouched")
    providers = existing.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("auth.json providers must be an object; existing credentials were left untouched")
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

    _atomic_write_text(auth_path, json.dumps(existing, indent=2) + "\n")


def _apply_xai_oauth_config(model: str, *, on_config_saved=None) -> None:
    """Write config.yaml with provider=xai-oauth and the chosen model."""
    config_path = Path(HERMES_HOME) / "config.yaml"
    existing = _read_config_for_write(config_path)

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

    _write_config_atomic(config_path, merged)
    if on_config_saved is not None:
        on_config_saved()

    # Persist LLM_MODEL and track the per-provider model so the setup UI can
    # display it alongside the xAI entry in the "Configured Providers" list.
    if model:
        existing_env = read_env(ENV_FILE)
        existing_env["LLM_MODEL"] = model
        existing_env["_MODEL_XAI_OAUTH"] = model
        write_env(ENV_FILE, existing_env)


def _save_xai_oauth_bundle(tokens: dict, model: str) -> None:
    """Save tokens, config and env together, restoring old files on failure.

    The caller holds cfg_lock so the setup panel cannot interleave a save.
    This is a best-effort filesystem transaction: if rollback itself fails,
    surface that explicitly instead of claiming the old state was restored.
    """
    from hermes_cli.auth import _auth_store_lock

    # Share the native cross-process lock with rotating OAuth grants.
    with _auth_store_lock(target_path=Path(HERMES_HOME) / "auth.json"):
        config_path = Path(HERMES_HOME) / "config.yaml"
        auth_path = Path(HERMES_HOME) / "auth.json"
        _read_config_for_write(config_path)
        paths = (auth_path, config_path, ENV_FILE)
        snapshots = {path: _file_snapshot(path) for path in paths}
        written: dict[Path, tuple[bytes, int] | None] = {}

        def mark_config_saved() -> None:
            written[config_path] = None
            written[config_path] = _file_snapshot(config_path)

        try:
            _save_xai_auth_json(tokens)
            written[auth_path] = None
            written[auth_path] = _file_snapshot(auth_path)
            _apply_xai_oauth_config(model, on_config_saved=mark_config_saved)
        except Exception as exc:
            failures = []
            for path in reversed(paths):
                if path not in written:
                    continue
                try:
                    if written[path] is None:
                        failures.append(f"{path.name} could not be verified after write; left untouched")
                    elif _file_snapshot(path) == written[path]:
                        _restore_file_snapshot(path, snapshots[path])
                    else:
                        failures.append(f"{path.name} changed concurrently; left untouched")
                except (OSError, ValueError) as restore_exc:
                    failures.append(f"{path.name}: {restore_exc}")
            if failures:
                raise RuntimeError(f"{exc}; rollback incomplete ({'; '.join(failures)})") from exc
            raise


async def _poll_xai_device_auth(state: dict) -> None:
    """Background task: poll xAI token endpoint until authorized or expired."""
    client = get_http_client()
    while time.time() < state["expires_at"]:
        await asyncio.sleep(state["interval"])
        if state is not _xai_oauth_state:
            return
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
            try:
                async with lifecycle_lock:
                    async with cfg_lock:
                        if state is not _xai_oauth_state or preparation["state"] != "ready":
                            return
                        await asyncio.to_thread(_save_xai_oauth_bundle, tokens, state.get("model", ""))
                        state["status"] = "authorized"
                    await gw.restart()
                    await dash.restart()
            except Exception as exc:
                state["status"] = "error"
                state["error"] = f"Could not save xAI authentication: {exc}"
                print(f"[xai-oauth] save failed: {exc!r}", flush=True)
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


def _delete_xai_credentials() -> Response | None:
    from hermes_cli.auth import _auth_store_lock

    auth_path = Path(HERMES_HOME) / "auth.json"
    with _auth_store_lock(target_path=auth_path):
        try:
            auth_before = _file_snapshot(auth_path)
            _file_snapshot(ENV_FILE)  # refuse a linked or non-regular env file
        except (OSError, ValueError) as exc:
            return JSONResponse({"error": f"Could not inspect xAI credentials safely: {exc}"}, status_code=500)
        auth_written = None
        auth_save_done = False
        try:
            if auth_before is not None:
                data = json.loads(auth_before[0].decode("utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("providers", {}), dict):
                    raise ValueError("auth.json must contain provider objects")
                data.get("providers", {}).pop("xai-oauth", None)
                if data.get("active_provider") == "xai-oauth":
                    data.pop("active_provider", None)
                _atomic_write_text(auth_path, json.dumps(data, indent=2) + "\n")
                auth_save_done = True
                auth_written = _file_snapshot(auth_path)
            env = read_env(ENV_FILE)
            env.pop("_MODEL_XAI_OAUTH", None)
            write_env(ENV_FILE, env)
        except (OSError, ValueError) as exc:
            restore_errors = []
            if auth_save_done and auth_written is None:
                restore_errors.append("auth.json could not be verified after write; left untouched")
            elif auth_written is not None:
                try:
                    if _file_snapshot(auth_path) == auth_written:
                        _restore_file_snapshot(auth_path, auth_before)
                    else:
                        restore_errors.append("auth.json changed concurrently; left untouched")
                except (OSError, ValueError) as restore_exc:
                    restore_errors.append(f"auth.json: {restore_exc}")
            if restore_errors:
                return JSONResponse({"error": f"Could not disconnect xAI safely: {exc}; rollback incomplete ({'; '.join(restore_errors)})"}, status_code=500)
            return JSONResponse({"error": f"Could not disconnect xAI safely: {exc}"}, status_code=500)
    return None


async def api_oauth_xai_delete(request: Request) -> Response:
    global _xai_oauth_state
    if err := guard(request): return err
    async with lifecycle_lock, cfg_lock:
        if err := guard(request): return err
        try:
            error = await asyncio.to_thread(_delete_xai_credentials)
        except Exception:
            return JSONResponse({"error": "Could not access the credential store. Retry after other authorization work finishes."}, status_code=503)
        if error is not None:
            return error
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

    state = {
        "device_code": data["device_code"],
        "user_code": data["user_code"],
        "verification_uri": data.get("verification_uri_complete") or data["verification_uri"],
        "expires_at": time.time() + data.get("expires_in", 900),
        "interval": max(data.get("interval", 5), 5),
        "status": "pending",
        "model": model,
    }
    async with lifecycle_lock, cfg_lock:
        if err := guard(request): return err
        if _codex_session is not None:
            return JSONResponse({"error": "Finish or cancel the ChatGPT authorization first."}, status_code=409)
        _xai_oauth_state = state
    asyncio.create_task(_poll_xai_device_auth(state))

    return JSONResponse({
        "user_code": data["user_code"],
        "verification_uri": state["verification_uri"],
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


def configured_model() -> tuple[str, str]:
    config = _read_config_for_write(Path(HERMES_HOME) / "config.yaml")
    model = config.get("model", {})
    if not isinstance(model, dict):
        return "", ""
    return str(model.get("default") or model.get("model") or "").strip(), str(model.get("provider") or "").strip().lower()


def config_revision() -> str:
    """Reject stale setup forms without putting credentials in the response."""
    digest = _hashlib.sha256()
    for path in (ENV_FILE, Path(HERMES_HOME) / "config.yaml"):
        snapshot = _file_snapshot(path)
        content = snapshot[0] if snapshot is not None else b""
        digest.update(str(len(content)).encode() + b":" + content)
    return digest.hexdigest()


def _codex_connected(*, allow_access_only: bool = False) -> bool:
    """Stored OAuth authorization, not a live entitlement/availability probe.

    v0.22.2's get_codex_auth_status calls load_pool, which can seed/heal and
    persist credentials despite its read-only docstring. Inspect a regular-file
    snapshot instead: setup polling must not mutate or refresh a grant. Native
    Hermes remains responsible for validating/refreshing it on actual use.
    """
    try:
        snapshot = _file_snapshot(Path(HERMES_HOME) / "auth.json")
        data = json.loads(snapshot[0]) if snapshot else {}
        if not isinstance(data, dict):
            return False
        pool = data.get("credential_pool")
        entries = pool.get("openai-codex", []) if isinstance(pool, dict) else []
        entries = [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
        providers = data.get("providers")
        state = providers.get("openai-codex", {}) if isinstance(providers, dict) else {}
        tokens = state.get("tokens", {}) if isinstance(state, dict) else {}
        candidates = [entry for entry in entries if entry.get("last_status") != "dead"]
        if isinstance(tokens, dict) and not any(
                entry.get("last_status") == "dead" and entry.get("access_token") == tokens.get("access_token")
                for entry in entries):
            candidates.append(tokens)
        if any(all(isinstance(entry.get(key), str) and entry[key].strip()
                   for key in ("access_token", "refresh_token")) for entry in candidates):
            return True
        if allow_access_only:
            # Native Codex also accepts manually managed/gateway pool keys.
            # Preserve those existing deployments without presenting them as a
            # durable ChatGPT OAuth login in the setup card.
            from hermes_cli.auth_codex import _codex_access_token_is_expiring
            return any(isinstance(entry.get("access_token"), str) and entry["access_token"].strip()
                       and not _codex_access_token_is_expiring(entry["access_token"], 0)
                       for entry in entries if entry.get("last_status") != "dead")
        return False
    except (OSError, ValueError):
        return False


def is_config_complete(data: dict[str, str] | None = None) -> bool:
    """Single source of truth for 'ready to run the gateway'.

    Used by: GET / redirect, auto_start on boot, admin API status.
    """
    if data is None:
        data = read_env(ENV_FILE)
    try:
        model, provider = configured_model()
    except ValueError:
        return False
    has_model = bool(data.get("LLM_MODEL")) or bool(model and provider not in {"", "auto"})
    if provider == "openai-codex":
        return bool(model) and _codex_connected(allow_access_only=True)
    if provider == "xai-oauth":
        return has_model and _has_xai_oauth_tokens()
    has_provider = any(
        _router_provider_is_configured(data, k) if k in MANAGED_ROUTER_PROVIDERS else bool(data.get(k))
        for k in PROVIDER_KEYS
    ) or _has_xai_oauth_tokens()
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
PUBLIC_PATHS = {"/health", "/setup/login", "/logout"}


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

    - HTML navigation: 302 to /setup/login?returnTo=<path>
    - API / XHR: 401 JSON (so the SPA's fetch() can surface it cleanly)
    """
    if _is_authenticated(request):
        recovery_restore = (
            request.url.path == "/setup/api/backup/restore"
            and preparation["state"] == "failed"
            and (_preparation_task is None or _preparation_task.done())
        )
        if (getattr(request, "method", "GET") not in {"GET", "HEAD", "OPTIONS"}
                and preparation["state"] != "ready" and not recovery_restore
                and request.url.path != "/setup/api/preparation/retry"):
            return JSONResponse({"error": "Hermes preparation must finish before making changes. See Setup for progress.",
                                 "preparation": preparation["state"]}, status_code=503)
        return None
    accept = request.headers.get("accept", "").lower()
    wants_html = "text/html" in accept
    if wants_html:
        rt = request.url.path
        if request.url.query:
            rt = f"{rt}?{request.url.query}"
        return RedirectResponse(f"/setup/login?returnTo={_url_quote(rt)}", status_code=302)
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
  <form method="POST" action="/setup/login">
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
    """GET /setup/login — render the sign-in form."""
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
    """POST /setup/login — validate creds and set the auth cookie."""
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
    return RedirectResponse(f"/setup/login?returnTo={_url_quote(return_to)}&error=1", status_code=302)


async def login_redirect(request: Request) -> Response:
    """GET /login — keep old bookmarks working after the move to /setup/login."""
    rt = request.query_params.get("returnTo") or request.query_params.get("next") or "/"
    return RedirectResponse(
        f"/setup/login?returnTo={_url_quote(_safe_return_to(rt))}", status_code=302
    )


async def logout(request: Request) -> Response:
    """GET /logout — clear cookie and bounce to login."""
    resp = RedirectResponse("/setup/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# ── Gateway manager ───────────────────────────────────────────────────────────
# Auto-respawn tuning. When the gateway exits unexpectedly — an in-band
# `/restart` (inside a container hermes exits 75 expecting a supervisor to
# bring it back; verified it takes the exit-75 path, NOT a detached
# self-restart, when /run/.containerenv or /.dockerenv exists), a crash, or an
# OOM kill — server.py is that supervisor and must restart it. A clean exit 0
# from Hermes' native Stop is intentional and stays stopped. Nothing else
# revives unexpected exits, and /health stays 200, so the bot would otherwise
# sit silently dead.
# A crash-loop guard stops us hammering a gateway that genuinely can't stay up
# (e.g. a bad provider key / model).
RESPAWN_WINDOW_S   = 120     # rolling window (s) for counting unexpected exits
RESPAWN_MAX_IN_WIN = 5       # give up auto-restart after this many exits in window
RESPAWN_BASE_DELAY = 2.0     # first backoff (seconds)
RESPAWN_MAX_DELAY  = 30.0    # backoff cap

# The window above only ever sees loops FASTER than itself: a failure cycle
# longer than RESPAWN_WINDOW_S / RESPAWN_MAX_IN_WIN (~24s) drops its own history
# on the next prune, so the counter never reaches the threshold and the guard
# never fires. v2026.9.11 made that reachable: hermes_startup_watchdog.py (new)
# os._exit(75)s a gateway that hasn't reached a live event loop in 300s, which
# is a ~5-minute cycle — the bot never answers, /health stays 200, and the only
# symptom is one log line every 5 minutes, forever. Upstream hit the same shape
# and says so in gateway/restart_loop_guard.py: "A fixed-window prune only sees
# cycles faster than the window (a slower loop drops its history every boot and
# never trips)". So we also CHAIN exits that are close together AND short-lived.
# Both conditions matter: the gap bound keeps unrelated failures days apart from
# chaining, and the uptime bound means a gateway that actually served for a
# while (an in-band /restart, say) breaks the chain instead of extending it.
RESPAWN_CHAIN_GAP_S    = 1800   # exits further apart than this start a new chain
RESPAWN_CHAIN_UPTIME_S = 600    # an exit after MORE uptime than this breaks the chain
RESPAWN_MAX_CHAIN      = 8      # give up after this many chained short-lived exits


# v2026.8.27's cross-profile ownership gate (gateway/run.py) logs this and
# exits 1 rather than displacing a PID it cannot attribute to this HERMES_HOME.
REPLACE_REFUSED_MARKER = "Refusing --replace"


class Gateway:
    def __init__(self):
        from weakref import WeakSet

        self.proc: asyncio.subprocess.Process | None = None
        self.state = "stopped"
        self.logs: deque[str] = deque(maxlen=500)
        self.started_at: float | None = None
        self.restarts = 0
        # True while a deliberate stop()/restart()/reset is in flight, so the
        # exiting process's _drain() doesn't fire an auto-respawn that races the
        # intentional lifecycle.
        self._stopping = False
        # _stopping is reset when a new start begins, possibly before the old
        # process's stdout reaches EOF. Keep the stop verdict on the process
        # itself so that delayed drain cannot charge a planned SIGTERM as a
        # crash. Weak references avoid retaining an already-drained old child.
        self._planned_stops = WeakSet()
        # Monotonic timestamps of recent unexpected exits (crash-loop guard).
        self._recent_exits: list[float] = []
        # Slow-loop guard: consecutive short-lived exits, and when the last one
        # landed. Monotonic (never wall clock) so an NTP step can't extend a chain.
        self._exit_chain = 0
        self._last_exit_at: float | None = None
        self._started_monotonic: float | None = None

    async def start(self, *, reset_budget: bool = True, write_config: bool = True):
        if self.proc and self.proc.returncode is None:
            return True
        # A manual Start/Restart (or boot) grants a fresh crash-loop budget; the
        # auto-respawn path passes reset_budget=False so repeated crashes keep
        # accumulating toward the give-up threshold.
        if reset_budget:
            self._recent_exits.clear()
            self._exit_chain = 0
            self._last_exit_at = None
        self.state = "starting"
        self._stopping = False
        try:
            env = build_hermes_env()
            if configured_model()[1] == "openai-codex" and not _codex_connected(allow_access_only=True):
                self.state = "stopped"
                self.logs.append("[gateway] ChatGPT authorization is missing. Reconnect from Setup.")
                return False
            model = configured_model()[0] or env.get("LLM_MODEL", "")
            provider_key = next((env.get(k, "") for k in PROVIDER_KEYS if env.get(k)), "")
            print(f"[gateway] model={model or '⚠ NOT SET'} | provider_key={'set' if provider_key else '⚠ NOT SET'}", flush=True)
            # Write config.yaml so hermes picks up the model (env vars alone aren't always enough)
            if write_config:
                async with cfg_lock:
                    write_config_yaml(read_env(ENV_FILE), preserve_model=True)
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
            # --external-supervisor: tells hermes a process manager owns this
            # gateway. v2026.8.27 narrowed its self-stop guard from the inherited
            # _HERMES_GATEWAY marker to _is_supervised_gateway_process(), which
            # also requires a supervisor marker — none of systemd/launchd/s6
            # applies here, so without this flag the agent's own terminal and
            # execute_code tools will happily run `hermes gateway stop` on
            # themselves (they run in-process, so they satisfy the PID-file
            # ownership half). It does not change the exit-75 restart contract:
            # /restart already takes the via_service branch on container
            # detection (gateway/slash_commands.py), which this only ORs with.
            self.proc = await asyncio.create_subprocess_exec(
                "hermes", "gateway", "run", "--replace", "--external-supervisor",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            self.state = "running"
            self.started_at = time.time()
            self._started_monotonic = time.monotonic()
            asyncio.create_task(self._drain(self.proc))
            return True
        except Exception as e:
            self.state = "error"
            self.logs.append(f"[error] Failed to start: {e}")
            return False

    async def stop(self):
        self._stopping = True
        proc = self.proc
        if proc is not None:
            # The process may already have exited while its stdout is still
            # draining. Mark it even in that case before restart() calls start().
            self._planned_stops.add(proc)
        if not proc or proc.returncode is not None:
            self.state = "stopped"
            return
        self.state = "stopping"
        proc.terminate()
        try:
            # 70s, not 45s. The stop path is a chain, not one timeout:
            # `agent.cron_drain_timeout` (30) waits out an in-flight cron job,
            # + CRON_DRAIN_CLEANUP_RESERVE_S (10), + the new v2026.8.31
            # `gateway.signal_interrupt_grace_timeout`, + adapter teardown (~5s
            # each for Telegram/Discord/Slack) + a bounded MCP shutdown + a
            # PASSIVE WAL checkpoint in SessionDB.close(). With a cron job in
            # flight that runs ~56s, so 45s landed our SIGKILL mid-teardown.
            # v2026.8.31 sizes its OWN supervisors at
            # max(60, max(drain, cron+10) + 30) = 70s
            # (gateway/restart.py resolve_systemd_timeout_stop_sec) and raised
            # its orphan-reaper grace 5s -> 30s after a SIGKILL during that
            # checkpoint corrupted state.db (the 2026-08-31 incident named in
            # hermes_cli/gateway.py). Matching 70 puts us BEHIND hermes' own 60s
            # shutdown watchdog, which hard-exits and releases the pid file and
            # lock itself — so this SIGKILL should now never fire, and the
            # pid-file mess _clear_stale_pidfile() mops up stops happening.
            # On a container stop Railway's own grace period is the real
            # deadline; this bound only governs Restart / config-save.
            await asyncio.wait_for(proc.wait(), timeout=70)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        self.state = "stopped"
        self.started_at = None

    async def restart(self):
        await self.stop()
        self.restarts += 1
        return await self.start()

    async def _drain(self, proc: asyncio.subprocess.Process):
        assert proc.stdout
        async for raw in proc.stdout:
            line = ANSI_ESCAPE.sub("", raw.decode(errors="replace").rstrip())
            self.logs.append(line)
        # Pipe EOF can arrive before asyncio has published returncode. Wait for
        # the process verdict so a native Dashboard Stop (exit 0) is not mistaken
        # for an unexpected exit and immediately respawned.
        rc = await proc.wait()
        planned_stop = proc in self._planned_stops
        self._planned_stops.discard(proc)
        # Ignore the drain of a process we've already replaced (e.g. via restart()).
        if proc is not self.proc:
            return
        # A deliberate stop()/restart()/reset owns its own lifecycle — don't respawn.
        if planned_stop or self._stopping:
            return
        # Hermes' native dashboard Stop writes a planned-stop marker before
        # signalling the gateway. A clean stop exits 0; respect that verdict
        # even though this wrapper did not initiate the stop itself. Restarts
        # still exit 75, while crashes and unexpected signals exit nonzero.
        if rc == 0:
            self.state = "stopped"
            self.started_at = None
            self.logs.append("[gateway] exited cleanly — stopped")
            return
        # Exit 78 = GATEWAY_FATAL_CONFIG_EXIT_CODE (gateway/restart.py): a
        # config error that no retry can fix — an invalid multiplexer config, or
        # every enabled platform failing non-retryably. Upstream's own generated
        # systemd unit pairs Restart=always with
        # RestartPreventExitStatus=78 for exactly this, and deliberately does
        # NOT use 78 when the failure is mixed/transient, so honouring it here
        # cannot suppress a recoverable restart. Respawning would just burn the
        # crash-loop budget on the same error and bury the reason.
        if rc == 78:
            self.state = "crashed"
            msg = ("[gateway] exited (code 78: fatal config) — not respawning. "
                   "Fix the configuration, then Start the gateway.")
            self.logs.append(msg)
            print(msg, flush=True)
            return
        # Unexpected exit: in-band `/restart` (exit 75), a crash, or an OOM kill.
        # On Railway nothing else brings the gateway back, so we supervise it.
        self.state = "error"
        # print() as well as the ring buffer: an unexpected exit is the one
        # supervisor event worth having in `railway logs` without opening /setup.
        self.logs.append(f"[gateway] exited (code {rc}) — supervising restart")
        print(f"[gateway] exited (code {rc}) — supervising restart", flush=True)
        asyncio.create_task(self._supervise_respawn(proc.pid))

    async def _supervise_respawn(self, dead_pid: int | None):
        # Crash-loop guard: count unexpected exits inside a rolling window and
        # give up (rather than hammer) once they exceed the threshold.
        now = time.monotonic()
        self._recent_exits = [t for t in self._recent_exits if now - t < RESPAWN_WINDOW_S]
        self._recent_exits.append(now)
        # Chain short-lived exits that arrive close together (see RESPAWN_CHAIN_*).
        uptime = now - self._started_monotonic if self._started_monotonic is not None else 0.0
        gap = now - self._last_exit_at if self._last_exit_at is not None else None
        if uptime > RESPAWN_CHAIN_UPTIME_S or (gap is not None and gap > RESPAWN_CHAIN_GAP_S):
            self._exit_chain = 1
        else:
            self._exit_chain += 1
        self._last_exit_at = now
        if len(self._recent_exits) > RESPAWN_MAX_IN_WIN or self._exit_chain > RESPAWN_MAX_CHAIN:
            self.state = "crashed"
            if self._exit_chain > RESPAWN_MAX_CHAIN:
                detail = (f"{self._exit_chain} short-lived exits in a row "
                          f"(each under {RESPAWN_CHAIN_UPTIME_S}s of uptime)")
            else:
                detail = f"{len(self._recent_exits)} exits in {RESPAWN_WINDOW_S}s"
            msg = (f"[gateway] crash-looping ({detail}) — giving up auto-restart. "
                   f"Check the Logs panel, fix the cause, then Start/Restart the gateway.")
            self.logs.append(msg)
            # print() too: a give-up is the terminal state of the supervisor and
            # must be visible in `railway logs` without opening /setup.
            print(msg, flush=True)
            return
        attempt = max(len(self._recent_exits), self._exit_chain)
        delay = min(RESPAWN_BASE_DELAY * 2 ** (attempt - 1), RESPAWN_MAX_DELAY)
        self.logs.append(f"[gateway] restarting in {int(delay)}s (attempt {attempt})")
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
        self._warn_if_replace_refused()
        self.restarts += 1
        await self.start(reset_budget=False)

    def _warn_if_replace_refused(self) -> None:
        """Surface v2026.8.27's `--replace` ownership gate, which no retry clears.

        The gate refuses to displace a live PID it cannot attribute to this
        HERMES_HOME and exits 1, so the respawn loop would otherwise burn its
        whole budget printing nothing an operator can act on. A container
        restart is the fix — start.sh sweeps the runtime records at boot.
        """
        recent = list(self.logs)[-30:]  # this exit's output, not the whole buffer
        if not any(REPLACE_REFUSED_MARKER in line for line in recent):
            return
        self.logs.append(
            "[gateway] hermes refused --replace: a live gateway holds the pid "
            "record and cannot be proven to belong to this profile. Redeploy "
            "the service to clear it (start.sh sweeps gateway.pid/lock/sock)."
        )

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
lifecycle_lock = asyncio.Lock()

# The admin server binds before volume preparation. Network-bound PM work must
# not consume Railway's pre-bind health deadline or leave users without progress.
preparation = {"state": "ready", "phase": "idle", "message": "", "warnings": [], "integrations": []}
_preparation_task: asyncio.Task | None = None
_preparation_mode = "startup"
PREPARATION_TIMEOUT = 900


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    """Stop an owned command and any acquisition workers it launched."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
    # The immediate process may have exited before its children did.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def _prepare_hermes(mode: str) -> bool:
    global _preparation_mode
    _preparation_mode = mode
    preparation.update(state="running", phase="starting", message="Preparing Hermes data and dependencies…",
                       warnings=[], integrations=[], requires_restore=False)
    proc = None
    readers = []
    try:
        proc = await asyncio.create_subprocess_exec(
            os.environ.get("HERMES_PYTHON", sys.executable),
            "-E", "-s", str(Path(__file__).parent / "template_helpers" / "prepare_hermes.py"), "--mode", mode,
            env={**os.environ, "HERMES_HOME": HERMES_HOME},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        async def drain(stream, *, events=False):
            async for raw in stream:
                line = raw.decode(errors="replace").rstrip()
                print(f"[preparation] {line}", flush=True)
                if not events:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                for key in ("phase", "message", "integrations", "warnings"):
                    if key in event:
                        preparation[key] = event[key]
                if event.get("warning"):
                    preparation["warnings"] = [*preparation["warnings"][-19:], str(event.get("message", event["warning"]))]

        readers = [asyncio.create_task(drain(proc.stdout, events=True)),
                   asyncio.create_task(drain(proc.stderr))]
        await asyncio.wait_for(asyncio.gather(proc.wait(), *readers), timeout=PREPARATION_TIMEOUT)
        if proc.returncode:
            raise RuntimeError(f"Preparation exited {proc.returncode}. Check the deployment logs before retrying.")
        preparation.update(state="ready", phase="complete", message="Hermes preparation complete.")
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = (f"Preparation exceeded {PREPARATION_TIMEOUT} seconds. Check deployment logs and retry."
                   if isinstance(exc, asyncio.TimeoutError) else str(exc))
        preparation.update(state="failed", phase="failed", message=message)
        print(f"[preparation] ERROR: {message}", flush=True)
        return False
    finally:
        # Includes PM workers/git children: an orphan must not publish later
        # while an operator retries or restores this volume.
        if proc is not None:
            await _terminate_process_group(proc)
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


# ── Hermes dashboard subprocess ───────────────────────────────────────────────
class Dashboard:
    """Manages the `hermes dashboard` subprocess (native Hermes web UI).

    Bound to loopback only — we expose it to the public internet through our
    reverse proxy on $PORT, where edge basic auth guards every request.
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
        self._maintenance_stopped = False

    async def start(self):
        if self.proc and self.proc.returncode is None:
            return True
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
                env=build_dashboard_env(),
            )
            self._maintenance_stopped = False
            print(f"[dashboard] spawned pid={self.proc.pid} → {HERMES_DASHBOARD_URL}", flush=True)
            # One line describing the auth shape this dashboard was started
            # with. Nearly every dashboard-side problem in this template comes
            # down to these two facts, so a user can paste this instead of
            # needing shell access to diagnose it.
            public_url = hermes_dashboard_public_url()
            user, _ = hermes_dashboard_credentials()
            if public_url:
                print(f"[dashboard] auth gate ON — public_url={public_url} user={user!r}; "
                      f"this proxy signs in for you, so no second login should appear",
                      flush=True)
            else:
                print("[dashboard] auth gate OFF — no RAILWAY_PUBLIC_DOMAIN, so hermes "
                      "builds MCP OAuth redirects from the loopback address and those "
                      "sign-ins will not complete", flush=True)
            self._drain_task = asyncio.create_task(self._drain())
            return True
        except Exception as e:
            print(f"[dashboard] FAILED to spawn: {e!r}", flush=True)
            return False

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
            return False
        self.proc.terminate()
        try:
            # v2026.9.21 joins its non-daemon state.db reconciliation worker on
            # shutdown, after stopping hosted rooms and closing attached Chat
            # PTYs. Upstream's own dashboard process manager gives that path a
            # 10s SIGTERM grace: killing earlier can strand a ui-tui descendant
            # holding a deleted state.db-wal generation, which makes the next
            # dashboard start refuse. Keep 5s of headroom over upstream's floor.
            await asyncio.wait_for(self.proc.wait(), timeout=15)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        return True

    async def restart(self):
        """Respawn so a freshly-saved provider key reaches the embedded Chat tab.

        Drops any live /api/pty, /api/ws, /api/events connections (the
        reverse-proxy WS pumps just see the upstream close and the SPA
        reconnects) — an acceptable trade-off since the alternative is Chat
        staying broken until a full redeploy.
        """
        _, error = await _gateway_maintenance_check()
        if error is not None:
            print(f"[dashboard] restart deferred: {error.body.decode(errors='replace')}", flush=True)
            return False
        await self.stop()
        return await self.start()


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
    if resp.status_code != 200:
        # Gated mode (invariant 8): `/` 302s to the login page, so there is no
        # SPA shell and no token to scrape. Callers authenticate with the
        # HermesSession cookies instead. Returning "" rather than raising keeps
        # that a normal path — raise_for_status() here made every provider pin
        # fail with "Could not fetch a Hermes session token".
        return ""
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

    async def _post(session: dict[str, str]) -> httpx.Response:
        """One attempt, authenticated for whichever mode the dashboard is in.

        The token header authenticates an ungated dashboard, the session cookies
        a gated one. This template-to-dashboard call carries its own credentials.
        """
        headers = {_SESSION_TOKEN_HEADER: session_token} if session_token else {}
        if session:
            headers["cookie"] = "; ".join(f"{k}={v}" for k, v in session.items())
        return await client.post(
            f"{HERMES_DASHBOARD_URL}/api/model/set",
            params={"profile": "default"},
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

    generation, session = hermes_session.snapshot()
    try:
        resp = await _post(session)
        if resp.status_code == 401 and await hermes_session.refresh(generation):
            _, session = hermes_session.snapshot()
            resp = await _post(session)
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


# ── ChatGPT setup: Hermes owns the OAuth protocol and credential store ─────────
_codex_session: dict | None = None  # only non-secret, temporary device-flow metadata
_CODEX_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


class HermesAPIError(Exception):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


async def _codex_api(method: str, path: str, *, params: dict | None = None) -> dict:
    """Bounded loopback calls using the same gated/ungated auth as model/set.

    Never forward an upstream error body: auth errors can contain credentials.
    The explicit default profile also avoids implicit-profile worker context
    differences in Hermes v0.22.2. No browser-supplied URL or profile is accepted.
    """
    try:
        token = await _get_hermes_session_token()

        async def send(session):
            headers = {_SESSION_TOKEN_HEADER: token} if token else {}
            if session:
                headers["cookie"] = "; ".join(f"{k}={v}" for k, v in session.items())
            return await get_http_client().request(
                method, f"{HERMES_DASHBOARD_URL}{path}", headers=headers,
                params={**(params or {}), "profile": "default"}, timeout=httpx.Timeout(25.0),
            )

        generation, session = hermes_session.snapshot()
        response = await send(session)
        if response.status_code == 401 and await hermes_session.refresh(generation):
            token = await _get_hermes_session_token()
            _, session = hermes_session.snapshot()
            response = await send(session)
    except httpx.HTTPError as exc:
        raise HermesAPIError("Hermes could not be reached. Wait for the dashboard to start, then retry.") from exc
    if response.status_code == 404:
        raise HermesAPIError("This authorization session has ended. Connect again to get a new code.", 404)
    if response.status_code >= 400:
        raise HermesAPIError(
            "Hermes could not complete the request. For login, enable device-code authorization in your ChatGPT security or workspace settings, then retry.",
            502 if response.status_code >= 500 else 409,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise HermesAPIError("Hermes returned an invalid response. Retry the request.", 502) from exc
    if not isinstance(payload, dict):
        raise HermesAPIError("Hermes returned an invalid response. Retry the request.", 502)
    return payload


async def _cancel_codex_session() -> None:
    global _codex_session
    if _codex_session is None:
        return
    session = _codex_session
    try:
        result = await _codex_api("DELETE", f"/api/providers/oauth/sessions/{session['session_id']}")
    except HermesAPIError as exc:
        if exc.status_code != 404:
            raise
    else:
        if result.get("ok") is not True and result.get("message") != "session not found":
            raise HermesAPIError("Cancellation was not confirmed. Check authorization status before retrying.")
    if _codex_session is session:
        _codex_session = None


async def api_oauth_codex_status(request: Request) -> Response:
    global _codex_session
    if err := guard(request): return err
    if preparation["state"] != "ready":
        return JSONResponse({"error": "Wait for Hermes preparation to finish."}, status_code=503)
    try:
        model, provider = configured_model()
        result = {"connected": await asyncio.to_thread(_codex_connected),
                  "model": model if provider == "openai-codex" else "",
                  "config_revision": config_revision()}
        session = _codex_session
        if session is not None:
            try:
                progress = await _codex_api("GET", f"/api/providers/oauth/openai-codex/poll/{session['session_id']}")
            except HermesAPIError as exc:
                if exc.status_code != 404:
                    raise
                progress = {"status": "expired"}
            # A response from before a cancel/new attempt cannot revive its code.
            if _codex_session is session:
                state = progress.get("status")
                if state == "pending":
                    return JSONResponse({**result, **session, "status": "waiting"})
                _codex_session = None
                if state != "approved":
                    return JSONResponse({**result, "status": state if state in {"expired", "denied"} else "error",
                                         "error": "Authorization ended. If it failed, check ChatGPT device-login permissions, then connect again."})
                result["connected"] = await asyncio.to_thread(_codex_connected)
        return JSONResponse({**result, "status": "connected" if result["connected"] else "not_connected"})
    except (HermesAPIError, ValueError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=getattr(exc, "status_code", 503))


async def api_oauth_codex_start(request: Request) -> Response:
    global _codex_session
    if err := guard(request): return err
    async with lifecycle_lock:
        if err := guard(request): return err
        if _codex_session is not None:
            return await api_oauth_codex_status(request)
        if _xai_oauth_state and _xai_oauth_state.get("status") == "pending":
            return JSONResponse({"error": "Finish or cancel the xAI authorization first."}, status_code=409)
        try:
            try:
                body = await request.json()
            except (ValueError, AttributeError):
                body = {}
            previous_revision = config_revision()
            revision = body.get("_config_revision") if isinstance(body, dict) else None
            if revision is not None and not isinstance(revision, str):
                return JSONResponse({"error": "Invalid settings revision."}, status_code=400)
            if revision is not None and revision != previous_revision:
                return JSONResponse({"error": "Settings changed. Reload before connecting ChatGPT."}, status_code=409)
            model, provider = configured_model()
            if model and provider in {"", "auto"} and is_config_complete():
                # Native login updates auth.active_provider. Freeze a legacy
                # auto choice first so approval cannot switch an existing model.
                from hermes_cli.auth import resolve_provider
                try:
                    resolved = await asyncio.to_thread(resolve_provider, "auto")
                except Exception as exc:
                    raise HermesAPIError("Could not resolve the current provider. Select it in the Hermes dashboard before connecting ChatGPT.") from exc
                if await set_active_model_via_hermes(resolved, model):
                    raise HermesAPIError("Could not preserve the current provider. Select it in the Hermes dashboard, then reconnect.")
            preserved_revision = config_revision()
            payload = await _codex_api("POST", "/api/providers/oauth/openai-codex/start")
            sid, code = payload.get("session_id"), payload.get("user_code")
            url = _urlparse(str(payload.get("verification_url") or ""))
            if (not isinstance(sid, str) or not _CODEX_SESSION_ID.fullmatch(sid)
                    or not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9-]{3,64}", code)
                    or url.scheme != "https" or url.netloc != "auth.openai.com"):
                raise HermesAPIError("Hermes returned an invalid authorization code. Retry login.", 502)
            expires, interval = payload.get("expires_in", 900), payload.get("poll_interval", 5)
            if type(expires) is not int or type(interval) is not int:
                raise HermesAPIError("Hermes returned an invalid authorization expiry. Retry login.", 502)
            _codex_session = {"session_id": sid, "user_code": code, "verification_url": url.geturl(),
                              "expires_at": time.time() + min(900, max(1, expires)),
                              "poll_interval": min(30, max(3, interval))}
            return JSONResponse({**_codex_session, "status": "waiting", "config_revision": preserved_revision,
                                 "previous_revision": previous_revision})
        except (HermesAPIError, ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=getattr(exc, "status_code", 503))


async def api_oauth_codex_cancel(request: Request) -> Response:
    if err := guard(request): return err
    async with lifecycle_lock:
        if err := guard(request): return err
        try:
            await _cancel_codex_session()
        except HermesAPIError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    return JSONResponse({"ok": True})


async def _codex_models() -> list[str]:
    payload = await _codex_api("GET", "/api/model/options", params={"explicit_only": "true"})
    rows = payload.get("providers", [])
    if not isinstance(rows, list):
        raise HermesAPIError("Hermes returned an invalid model list. Reload it and try again.", 502)
    for row in rows:
        if isinstance(row, dict) and row.get("slug") == "openai-codex":
            unavailable = row.get("unavailable_models") or []
            if not isinstance(row.get("models", []), list):
                raise HermesAPIError("Hermes returned an invalid model list. Reload it and try again.", 502)
            return list(dict.fromkeys(model for model in row.get("models", [])
                                      if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,200}", model)
                                      and model not in unavailable))
    return []


async def api_oauth_codex_models(request: Request) -> Response:
    if err := guard(request): return err
    async with lifecycle_lock:
        if preparation["state"] != "ready":
            return JSONResponse({"error": "Wait for Hermes preparation to finish."}, status_code=503)
        try:
            if not await asyncio.to_thread(_codex_connected):
                return JSONResponse({"error": "Connect ChatGPT before loading models."}, status_code=409)
            return JSONResponse({"models": await _codex_models()})
        except HermesAPIError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)


async def api_oauth_codex_delete(request: Request) -> Response:
    global _codex_session
    if err := guard(request): return err
    if backup_lock.locked():
        return JSONResponse({"error": "Wait for the backup or restore to finish."}, status_code=409)
    async with backup_lock, lifecycle_lock:
        if err := guard(request): return err
        try:
            model, provider = configured_model()
            snapshot = _file_snapshot(Path(HERMES_HOME) / "auth.json")
            if snapshot is not None and not isinstance(json.loads(snapshot[0]), dict):
                raise ValueError("auth.json is invalid. Repair it before disconnecting; no credentials were removed.")
            pid, error = await _gateway_maintenance_check()
            if error is not None: return error
            await _cancel_codex_session()
            was_running = gw.state == "starting" or (gw.proc is not None and gw.proc.returncode is None) or pid is not None
            # Stop all credential holders BEFORE deleting the grant. Otherwise a
            # chat/gateway refresh can publish it again after 'Disconnect'.
            dashboard_stopped = await dash.stop()
            await _stop_gateway_for_maintenance(dashboard_stopped=dashboard_stopped)
            _codex_session = None
            from hermes_cli.auth import clear_provider_auth
            await asyncio.to_thread(clear_provider_auth, "openai-codex")
            if await dash.start() is False:
                raise RuntimeError("Authorization removed, but the dashboard could not restart. Check Logs.")
            if was_running and provider != "openai-codex":
                if await gw.start(write_config=False) is False:
                    raise RuntimeError("Authorization removed, but the gateway could not restart. Check Logs.")
            return JSONResponse({"ok": True})
        except (HermesAPIError, ValueError, OSError, RuntimeError) as exc:
            # Restore UI access, but never restart inference with uncertain auth.
            await dash.start()
            return JSONResponse({"error": str(exc)}, status_code=getattr(exc, "status_code", 503))


# ── Route handlers ────────────────────────────────────────────────────────────
async def page_index(request: Request):
    if err := guard(request): return err
    return templates.TemplateResponse(request, "index.html")


async def route_health(request: Request):
    return JSONResponse({"status": "ok", "gateway": gw.state, "preparation": preparation["state"]})


async def api_config_get(request: Request):
    if err := guard(request): return err
    if preparation["state"] != "ready":
        return JSONResponse({"error": "Hermes is preparing. Settings will load when it finishes."}, status_code=503)
    async with cfg_lock:
        if preparation["state"] != "ready":
            return JSONResponse({"error": "Hermes is preparing. Settings will load when it finishes."}, status_code=503)
        revision = config_revision()
        data = read_env(ENV_FILE)
        try:
            model, provider = configured_model()
            if revision != config_revision():
                return JSONResponse({"error": "Settings changed while loading. Reload the page."}, status_code=409)
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    defs = [{"key": k, "label": l, "category": c, "secret": s} for k, l, c, s in ENV_VARS]
    return JSONResponse({"vars": mask(data), "defs": defs, "config_revision": revision,
                         "config_complete": is_config_complete(data),
                         "model_config": {"model": model, "provider": provider}})


async def api_config_put(request: Request):
    if err := guard(request): return err
    async with lifecycle_lock:
        if err := guard(request): return err
        rollback_files = ()
        strict_pin = False
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        if not isinstance(body, dict) or not isinstance(body.get("vars", {}), dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in body.get("vars", {}).items()):
            return JSONResponse({"error": "Settings must be a mapping of text values."}, status_code=400)
        try:
            restart = body.pop("_restart", False)
            # Set by the setup wizard to the ENV_VARS key of whichever provider's
            # dropdown entry was selected in this save action (e.g. "NVIDIA_API_KEY")
            # — empty when the user saved without touching a provider (e.g. just
            # toggling a messaging channel). See set_active_model_via_hermes().
            active_provider_key = str(body.pop("_active_provider_key", "") or "").strip()
            if active_provider_key and active_provider_key not in HERMES_PROVIDER_IDS:
                return JSONResponse({"error": "Unsupported provider."}, status_code=400)
            oauth_provider = str(body.pop("_active_oauth_provider", "") or "").strip()
            if oauth_provider not in {"", "openai-codex"} or (oauth_provider and active_provider_key):
                return JSONResponse({"error": "Choose one supported provider."}, status_code=400)
            revision = body.pop("_config_revision", None)
            if revision is not None and not isinstance(revision, str):
                return JSONResponse({"error": "Invalid settings revision."}, status_code=400)
            if revision is None and getattr(request, "headers", {}).get("referer"):
                return JSONResponse({"error": "This setup page is outdated. Reload before saving; existing settings were not changed."}, status_code=409)
            if revision is not None and revision != config_revision():
                return JSONResponse({"error": "Settings changed since this page loaded. Reload before saving; your saved settings were not changed."}, status_code=409)
            new_vars = body.get("vars", {})
            if oauth_provider:
                if not await asyncio.to_thread(_codex_connected):
                    return JSONResponse({"error": "Connect ChatGPT before saving."}, status_code=409)
                if new_vars.get("LLM_MODEL", "").strip() not in await _codex_models():
                    return JSONResponse({"error": "Choose a ChatGPT model from the model list, then retry."}, status_code=409)
            async with cfg_lock:
                if revision is not None and revision != config_revision():
                    return JSONResponse({"error": "Settings changed while saving. Reload before trying again."}, status_code=409)
                config_path = Path(HERMES_HOME) / "config.yaml"
                current_cfg = _read_config_for_write(config_path)
                existing = read_env(ENV_FILE)
                current_model, current_provider = configured_model()
                strict_pin = bool(oauth_provider or current_provider == "openai-codex")
                explicit_model = bool(active_provider_key or oauth_provider) or (
                    "LLM_MODEL" in new_vars and new_vars["LLM_MODEL"] not in {existing.get("LLM_MODEL", ""), current_model})
                if current_provider == "openai-codex" and explicit_model and not (active_provider_key or oauth_provider):
                    return JSONResponse({"error": "Choose ChatGPT or another provider before changing its model."}, status_code=409)
                merged = unmask(new_vars, existing)
                for k, v in existing.items():
                    if k not in merged:
                        merged[k] = v
                if not explicit_model and current_model:
                    merged["LLM_MODEL"] = current_model
                if oauth_provider:
                    merged["_MODEL_OPENAI_CODEX"] = merged["LLM_MODEL"].strip()
                # Removing the router that currently owns model.provider must not
                # leave a dangling provider id + model behind. That state looks
                # configured to the supervisor but can never resolve at runtime.
                # Clear only the shared active model; other saved providers and
                # their per-provider model hints remain available for the next pick.
                reset_removed_router_model = False
                removed_router_ids = {
                    spec["provider_id"]
                    for key, spec in MANAGED_ROUTER_PROVIDERS.items()
                    if _router_provider_is_configured(existing, key)
                    and not _router_provider_is_configured(merged, key)
                }
                if removed_router_ids:
                    current_model = current_cfg.get("model", {})
                    current_id = str(current_model.get("provider") or "").strip().lower() if isinstance(current_model, dict) else ""
                    reset_removed_router_model = any(
                        current_id in {provider_id, f"custom:{provider_id}"}
                        for provider_id in removed_router_ids
                    )
                if reset_removed_router_model:
                    merged["LLM_MODEL"] = ""
                router_spec = MANAGED_ROUTER_PROVIDERS.get(active_provider_key)
                if router_spec:
                    if not merged.get(active_provider_key, "").strip():
                        return JSONResponse(
                            {"error": f"{router_spec['name']} endpoint API key is required."},
                            status_code=400,
                        )
                    router_base_url = _router_base_url(merged, active_provider_key)
                    if not router_base_url:
                        return JSONResponse(
                            {"error": f"{router_spec['name']} base URL is required."},
                            status_code=400,
                        )
                    if url_error := _router_base_url_error(router_base_url):
                        return JSONResponse(
                            {"error": f"{router_spec['name']}: {url_error}"},
                            status_code=400,
                        )
                previous_env = _file_snapshot(ENV_FILE)
                previous_config = _file_snapshot(config_path)
                write_env(ENV_FILE, merged)
                try:
                    # Keep the old model/provider pair until the native API
                    # atomically commits an explicit OAuth switch. Our staged
                    # write must not masquerade as a successful native pin.
                    write_config_yaml(merged, reset_model=reset_removed_router_model, preserve_model=strict_pin or not explicit_model)
                except Exception:
                    # Config and .env are separate files. If the guarded config
                    # write refuses or fails, restore the exact prior credentials
                    # rather than leaving the next boot with a half-saved setup.
                    _restore_file_snapshot(ENV_FILE, previous_env)
                    raise
                written_env = _file_snapshot(ENV_FILE)
                written_config = _file_snapshot(config_path)
                if strict_pin:
                    rollback_files = ((ENV_FILE, previous_env, written_env), (config_path, previous_config, written_config))

            model_warning = None
            hermes_provider_id = oauth_provider or HERMES_PROVIDER_IDS.get(active_provider_key)
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
                if oauth_provider or current_provider == "openai-codex":
                    # A timeout can arrive AFTER Hermes commits the model. Read
                    # back the file before deciding whether the pin failed.
                    applied_model, applied_provider = configured_model()
                    if (applied_model, applied_provider) != (model_value, hermes_provider_id):
                        async with cfg_lock:
                            if _file_snapshot(ENV_FILE) == written_env:
                                _restore_file_snapshot(ENV_FILE, previous_env)
                            if _file_snapshot(config_path) == written_config:
                                _restore_file_snapshot(config_path, previous_config)
                        return JSONResponse({"error": "Hermes could not confirm the provider change. Existing credentials were retained. Reload settings and retry."}, status_code=503)
                    model_warning = None
                # Log the outcome either way. A failed pin is a SILENT downgrade —
                # hermes falls back to provider auto-resolution, which works for a
                # single-provider setup and fails opaquely for custom endpoints
                # (their base_url/api_key are only written by this pin). The reason
                # otherwise only exists in the HTTP response body, which nobody sees.
                if model_warning:
                    print(f"[config] provider pin FAILED — provider={hermes_provider_id} "
                          f"model={model_value!r}: {model_warning}", flush=True)
                else:
                    print(f"[config] provider pin ok — provider={hermes_provider_id} "
                          f"model={model_value!r}", flush=True)

            # The save is committed. A later process-start failure must not
            # roll back credentials underneath an already-started child.
            rollback_files = ()
            if restart:
                if not is_config_complete(merged):
                    return JSONResponse({"ok": True, "restarting": False, "warning": "Settings saved. Complete provider authorization and model selection before starting Hermes."})
                gateway_started = await gw.restart()
                # The dashboard (and its embedded Chat tab) only ever sees the env
                # it was spawned with — a newly-saved provider key doesn't reach
                # the already-running process otherwise. See Dashboard.restart().
                dashboard_started = await dash.restart()
                if gateway_started is False or dashboard_started is False:
                    model_warning = (model_warning + " " if model_warning else "") + "Settings saved, but Hermes could not restart. Check Logs and deployment logs."
            resp = {"ok": True, "restarting": restart, "config_revision": config_revision(),
                    "config_complete": is_config_complete(merged)}
            if model_warning:
                resp["warning"] = model_warning
            return JSONResponse(resp)
        except (Exception, asyncio.CancelledError) as e:
            # Roll back our own staged bytes only. An independent native writer
            # may have saved newer data while the model API was in flight.
            for path, previous, written in rollback_files:
                if _file_snapshot(path) == written:
                    _restore_file_snapshot(path, previous)
            if isinstance(e, asyncio.CancelledError):
                raise
            return JSONResponse({"error": str(e)}, status_code=500)


async def _dashboard_storage_status() -> dict[str, str]:
    """Read Hermes' bounded state.db check without making setup depend on it.

    The dashboard's GET /api/status is public within Hermes' own auth gates,
    but it is bound to loopback here and this result only reaches an admin via
    /setup/api/status. Only copy its storage status and documented `corrupt`
    reason; never return the larger upstream status payload.
    """
    unknown = {"status": "unknown"}
    try:
        response = await get_http_client().get(
            f"{HERMES_DASHBOARD_URL}/api/status",
            timeout=httpx.Timeout(3.0, connect=0.5),
        )
        if response.status_code != 200:
            return unknown
        payload = response.json()
    except (httpx.RequestError, ValueError):
        return unknown
    components = payload.get("components") if isinstance(payload, dict) else None
    storage = components.get("storage") if isinstance(components, dict) else None
    if not isinstance(storage, dict) or storage.get("status") not in {"ok", "degraded"}:
        return unknown
    result = {"status": storage["status"]}
    if storage["status"] == "degraded" and storage.get("reason") == "corrupt":
        result["reason"] = "corrupt"
    return result


def _profile_config_paths() -> list[Path]:
    """Read-only roster for upgrade notices; never follow profile links."""
    root = Path(HERMES_HOME)
    configs = [root / "config.yaml"]
    profiles = root / "profiles"
    try:
        if not profiles.is_symlink() and profiles.is_dir():
            tombstones = profiles / ".deleted"
            has_tombstones = not tombstones.is_symlink() and tombstones.is_dir()
            for entry in profiles.iterdir():
                if entry.name == "default" or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", entry.name):
                    continue
                if entry.is_symlink() or not entry.is_dir():
                    continue
                marker = tombstones / entry.name
                if has_tombstones and (marker.is_symlink() or marker.exists()):
                    continue
                configs.append(entry / "config.yaml")
    except OSError:
        pass  # Informational notice only; Status remains available.
    return configs


def _hindsight_configured() -> bool:
    for path in _profile_config_paths():
        try:
            cfg = _read_config_for_write(path)
        except (OSError, ValueError):
            continue
        memory = cfg.get("memory")
        if isinstance(memory, dict) and str(memory.get("provider") or "").strip().lower() == "hindsight":
            return True
    return False


def _email_authserv_missing() -> list[str]:
    from gateway.config import PlatformConfig
    from gateway.config_loader import merge_platform_sections

    profiles = []
    for path in _profile_config_paths():
        try:
            cfg = _read_config_for_write(path)
            env = read_env(path.with_name(".env"))
            legacy = _file_snapshot(path.with_name("gateway.json"))
            base = json.loads(legacy[0].decode("utf-8-sig")) if legacy else {}
            if not isinstance(base, dict):
                base = {}
            platforms = merge_platform_sections(cfg, cfg.get("gateway"), base, also=frozenset({"email"}))
            email = PlatformConfig.from_dict(platforms.get("email", {})).extra
        except (OSError, ValueError):
            continue
        if path.parent == Path(HERMES_HOME):
            env = {**os.environ, **env}
        enabled = env.get("EMAIL_ADDRESS") or email.get("address")
        required = email.get("require_authenticated_sender")
        if required is None:
            required = str(env.get("EMAIL_TRUST_FROM_HEADER", "")).lower() not in {"1", "true", "yes", "on"}
        if enabled and required and not str(email.get("authserv_id") or env.get("EMAIL_AUTHSERV_ID", "")).strip():
            profiles.append("default" if path.parent == Path(HERMES_HOME) else path.parent.name)
    return profiles


async def api_status(request: Request):
    if err := guard(request): return err
    data = read_env(ENV_FILE)
    # Label from the ENV_VARS registry rather than munging the env-var name.
    # Deriving it produced things like "Minimax Cn" and "Glm" while the registry
    # already carries the proper display name; a new provider now reads correctly
    # on the Status page without another string-replace being bolted on here.
    providers = {
        (ENV_LABELS.get(k) or k.replace("_API_KEY", "").replace("_TOKEN", "").replace("_", " ").title()):
        {"configured": bool(data.get(k))}
        for k in PROVIDER_KEYS
    }
    channels = {
        name: {"configured": bool(v := data.get(key,"")) and v.lower() not in ("false","0","no")}
        for name, key in CHANNEL_MAP.items()
    }
    storage = await _dashboard_storage_status()
    return JSONResponse({"gateway": gw.status(), "providers": providers,
                         "channels": channels, "hermes_version": HERMES_VERSION,
                         "storage": storage,
                         "preparation": preparation,
                         "hindsight_configured": _hindsight_configured(),
                         "email_authserv_missing": _email_authserv_missing(),
                         # None when running; a dict (possibly with null fields)
                         # when hermes' ESTOP sentinel is engaged — see
                         # estop_state() for why a green panel would otherwise
                         # be actively misleading.
                         "paused": estop_state()})


async def api_estop_resume(request: Request):
    """Clear the pause sentinel — the "Resume" control in the admin panel.

    Mirrors hermes' own `hermes resume` / `/pause off`, which is just
    ``disengage()`` unlinking ``$HERMES_HOME/ESTOP`` (agent/estop.py). hermes
    re-stats the sentinel on every check with no caching, so the next inbound
    message is served immediately — deliberately no gateway restart here, which
    would drop adapter connections for no reason.

    Resuming something that was never paused is a success, not an error: the
    button exists to guarantee the end state, and a 404 would be a confusing
    way to say "already running".
    """
    if err := guard(request): return err
    try:
        ESTOP_FILE.unlink()
    except FileNotFoundError:
        return JSONResponse({"ok": True, "resumed": False})
    except OSError as e:
        return JSONResponse({"error": f"Could not clear the pause: {e}"}, status_code=500)
    print("[estop] pause cleared from the admin panel", flush=True)
    return JSONResponse({"ok": True, "resumed": True})


async def api_logs(request: Request):
    if err := guard(request): return err
    return JSONResponse({"lines": list(gw.logs)})


async def api_gw_start(request: Request):
    if err := guard(request): return err
    async with lifecycle_lock:
        if err := guard(request): return err
        if await gw.start() is False:
            return JSONResponse({"error": "Hermes could not start. Check Logs."}, status_code=503)
    return JSONResponse({"ok": True})


async def api_gw_stop(request: Request):
    if err := guard(request): return err
    async with lifecycle_lock:
        if err := guard(request): return err
        _, error = await _gateway_maintenance_check()
        if error is not None:
            return error
        try:
            await _stop_gateway_for_maintenance()
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse({"ok": True})


async def api_gw_restart(request: Request):
    if err := guard(request): return err
    async with lifecycle_lock:
        if err := guard(request): return err
        if await gw.restart() is False:
            return JSONResponse({"error": "Hermes could not start. Check Logs."}, status_code=503)
    return JSONResponse({"ok": True})


NATIVE_GATEWAY_ACTIONS = ("gateway-start", "gateway-stop", "gateway-restart", "gateway-migrate")
NATIVE_GATEWAY_LIFECYCLE_PATHS = frozenset({
    "/api/gateway/start", "/api/gateway/stop", "/api/gateway/restart",
    "/api/gateway/migrate", "/api/gateway/drain", "/api/webhooks/enable",
})
NATIVE_SETUP_WRITE_PATHS = frozenset({"/api/config", "/api/env", "/api/model/set"})
NATIVE_GATEWAY_ONBOARDING_PATH = re.compile(r"/api/messaging/(?:telegram|whatsapp)/onboarding/[^/]+/apply")
GATEWAY_STOP_TIMEOUT = 70.0


def _verified_root_gateway_pid() -> int | None:
    """Read native live-process identity; ambiguity is never an absent gateway."""
    from gateway.status import get_running_pid, get_running_pid_identity_strict

    path = Path(HERMES_HOME) / "gateway.pid"
    identity = get_running_pid_identity_strict(path)
    scoped_pid = get_running_pid(path, cleanup_stale=False)
    if (identity[0] if identity is not None else None) != scoped_pid:
        raise RuntimeError("Gateway identity or home could not be verified. Check deployment logs before retrying.")
    return scoped_pid


async def _native_gateway_action_busy(gateway_pid: int | None) -> str | None:
    """Check the native action registry before stopping its Dashboard owner."""
    if dash.proc is None:
        # This container has never started a Dashboard, including recovery from
        # an interrupted restore after a container restart: no native action ran.
        return None
    if dash.proc.returncode is not None:
        if dash._maintenance_stopped and gateway_pid is None:
            return None
        raise RuntimeError("Dashboard exited; native gateway actions cannot be checked. Redeploy before retrying maintenance.")
    client = get_http_client()
    token = await _get_hermes_session_token()

    async def fetch(name: str, session: dict[str, str]) -> httpx.Response:
        headers = {_SESSION_TOKEN_HEADER: token} if token else {}
        if session:
            headers["cookie"] = "; ".join(f"{key}={value}" for key, value in session.items())
        return await client.get(
            f"{HERMES_DASHBOARD_URL}/api/actions/{name}/status?lines=1",
            headers=headers, timeout=httpx.Timeout(3.0, connect=0.5),
        )

    for name in NATIVE_GATEWAY_ACTIONS:
        generation, session = hermes_session.snapshot()
        response = await fetch(name, session)
        if _needs_dashboard_login(response.status_code, response.headers.get("location", "")):
            if await hermes_session.refresh(generation):
                _, session = hermes_session.snapshot()
                response = await fetch(name, session)
        if response.status_code != 200:
            raise RuntimeError("Could not verify native gateway actions. Wait for Dashboard to recover before retrying.")
        payload = response.json()
        if (not isinstance(payload, dict) or payload.get("name") != name
                or not isinstance(payload.get("running"), bool)):
            raise RuntimeError("Native gateway action status is invalid; maintenance was not started.")
        if payload["running"]:
            # The manual restart fallback becomes the gateway itself. Its
            # action stays running forever; its verified PID is safe to stop.
            if name == "gateway-restart" and gateway_pid is not None and payload.get("pid") == gateway_pid:
                continue
            return name
    return None


async def _gateway_maintenance_check() -> tuple[int | None, Response | None]:
    """Called under lifecycle_lock, before stopping either child or importing."""
    try:
        pid = await asyncio.to_thread(_verified_root_gateway_pid)
        action = await _native_gateway_action_busy(pid)
    except (ImportError, OSError, RuntimeError, ValueError, httpx.RequestError) as exc:
        return None, JSONResponse({"error": str(exc)}, status_code=503)
    if action:
        return pid, JSONResponse({"error": f"Native {action} is still running. Wait for it to finish before retrying."}, status_code=409)
    return pid, None


async def _stop_gateway_for_maintenance(*, dashboard_stopped: bool = False) -> None:
    """Stop owned and native-detached root gateways, then prove safe absence."""
    deadline = time.monotonic() + GATEWAY_STOP_TIMEOUT
    await gw.stop()
    pid = await asyncio.to_thread(_verified_root_gateway_pid)
    if pid is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Gateway did not stop within its graceful shutdown limit. No restore or reload was started.")
        rc, _ = await _run_hermes_cli("gateway", "stop", timeout=remaining)
        if rc != 0:
            raise RuntimeError("Native gateway stop failed. No restore or reload was started; check deployment logs.")
    # The native stop command may return after ten seconds while the gateway
    # continues draining. Exit zero alone is not authority to overwrite data.
    while await asyncio.to_thread(_verified_root_gateway_pid) is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Gateway is still draining. No restore or reload was started; retry once it stops.")
        await asyncio.sleep(min(0.2, remaining))
    if dashboard_stopped:
        dash._maintenance_stopped = True


async def api_dependencies_apply(request: Request):
    """Load the selected PM environment without rewriting user configuration."""
    if err := guard(request): return err
    if backup_lock.locked():
        return JSONResponse({"error": "Wait for the backup or restore to finish."}, status_code=409)
    async with backup_lock, lifecycle_lock:
        if err := guard(request): return err
        pid, error = await _gateway_maintenance_check()
        if error is not None:
            return error
        was_running = (gw.state == "starting" or (gw.proc is not None and gw.proc.returncode is None)
                       or pid is not None)
        dashboard_stopped = await dash.stop()
        try:
            await _stop_gateway_for_maintenance(dashboard_stopped=dashboard_stopped)
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            await dash.start()
            return JSONResponse({"error": str(exc)}, status_code=503)
        if await dash.start() is False:
            return JSONResponse({"error": "Dashboard could not restart. Check deployment logs."}, status_code=503)
        if was_running:
            if await gw.start(write_config=False) is False:
                return JSONResponse({"error": "Gateway could not restart. Check Logs."}, status_code=503)
    return JSONResponse({"ok": True})


async def api_preparation_retry(request: Request):
    if err := guard(request): return err
    global _preparation_task
    if preparation.get("requires_restore"):
        return JSONResponse({"error": "Restore did not finish. Restore the pre-restore safety snapshot before starting Hermes."}, status_code=409)
    if preparation["state"] != "failed" or (_preparation_task and not _preparation_task.done()):
        return JSONResponse({"error": "Preparation is not waiting for a retry."}, status_code=409)
    preparation.update(state="running", phase="starting", message="Retrying Hermes preparation…")
    _preparation_task = asyncio.create_task(_initialize_hermes(_preparation_mode))
    return JSONResponse({"ok": True}, status_code=202)


async def api_config_reset(request: Request):
    if err := guard(request): return err
    async with lifecycle_lock:
        if err := guard(request): return err
        try:
            await _cancel_codex_session()
            async with cfg_lock:
                _read_config_for_write(Path(HERMES_HOME) / "config.yaml")
                previous_env = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else None
                ENV_FILE.unlink(missing_ok=True)
                try:
                    write_config_yaml({}, reset_model=True)
                except Exception:
                    if previous_env is not None:
                        _atomic_write_text(ENV_FILE, previous_env)
                    raise
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        await gw.stop()
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
        return _read_pairing_json(path)
    except Exception:
        return {}


def _read_pairing_json(path: Path) -> dict:
    """Read a pairing object for a write; preserve malformed files for recovery."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeError) as e:
        raise ValueError(f"invalid pairing JSON in {path.name}") from e
    if not isinstance(data, dict):
        raise ValueError(f"pairing JSON in {path.name} is not an object")
    return data


def _wjson(path: Path, data: dict):
    """Replace a pairing file only after a complete private, synced JSON write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def _platforms(suffix: str) -> list[str]:
    d = pairing_dir()
    if not d.exists(): return []
    return [f.stem.rsplit(f"-{suffix}", 1)[0] for f in d.glob(f"*-{suffix}.json")]


async def api_pairing_pending(request: Request):
    if err := guard(request): return err
    now = time.time()
    out = []
    for p in _platforms("pending"):
        for code, info in _pjson(pairing_dir() / f"{p}-pending.json").items():
            if now - info.get("created_at", now) <= PAIRING_TTL:
                out.append({"platform": p, "code": code,
                            "user_id": info.get("user_id",""), "user_name": info.get("user_name",""),
                            "age_minutes": int((now - info.get("created_at", now)) / 60)})
    return JSONResponse({"pending": out})


async def api_pairing_approve(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if err := guard(request): return err
    platform, code = body.get("platform",""), body.get("code","").strip()
    if not platform or not code:
        return JSONResponse({"error": "platform and code required"}, status_code=400)
    store = pairing_dir()
    pending_path = store / f"{platform}-pending.json"
    approved_path = store / f"{platform}-approved.json"
    try:
        pending = _read_pairing_json(pending_path)
        approved = _read_pairing_json(approved_path)
    except (OSError, ValueError) as e:
        return JSONResponse({"error": f"Could not read pairing data: {e}"}, status_code=500)
    if code not in pending:
        return JSONResponse({"error": "Code not found"}, status_code=404)
    entry = pending[code]
    user_id = (entry.get("user_id") or "").strip() if isinstance(entry, dict) else ""
    if not user_id:
        # Malformed/legacy entry without a user_id — leave it in pending (we
        # haven't written the pop yet) rather than silently discarding it.
        return JSONResponse({"error": "Pending entry has no user_id"}, status_code=422)
    approved[user_id] = {"user_name": entry.get("user_name",""), "approved_at": time.time()}
    # Approval must be durable before deleting the pending request. If the
    # second write fails, a retry can finish cleanup without losing the grant.
    try:
        _wjson(approved_path, approved)
        del pending[code]
        _wjson(pending_path, pending)
    except (OSError, ValueError) as e:
        return JSONResponse({"error": f"Could not finish pairing approval; retry safely: {e}"}, status_code=500)
    return JSONResponse({"ok": True})


async def api_pairing_deny(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if err := guard(request): return err
    platform, code = body.get("platform",""), body.get("code","").strip()
    p = pairing_dir() / f"{platform}-pending.json"
    try:
        pending = _read_pairing_json(p)
    except (OSError, ValueError) as e:
        return JSONResponse({"error": f"Could not read pairing data: {e}"}, status_code=500)
    if code in pending:
        del pending[code]
        try:
            _wjson(p, pending)
        except (OSError, ValueError) as e:
            return JSONResponse({"error": f"Could not deny pairing request: {e}"}, status_code=500)
    return JSONResponse({"ok": True})


async def api_pairing_approved(request: Request):
    if err := guard(request): return err
    out = []
    for p in _platforms("approved"):
        for uid, info in _pjson(pairing_dir() / f"{p}-approved.json").items():
            out.append({"platform": p, "user_id": uid,
                        "user_name": info.get("user_name",""), "approved_at": info.get("approved_at",0)})
    return JSONResponse({"approved": out})


# These names mirror gateway/pairing.py's _PLATFORM_ALLOWLIST_ENV at v2026.9.24.
# The gateway authorizes pairing and static allowlists as a union. Removing a
# pairing row cannot honestly be called a revoke while another grant remains.
_PAIRING_ALLOWLIST_ENV = {
    "telegram": "TELEGRAM_ALLOWED_USERS", "discord": "DISCORD_ALLOWED_USERS",
    "whatsapp": "WHATSAPP_ALLOWED_USERS", "whatsapp_cloud": "WHATSAPP_CLOUD_ALLOWED_USERS",
    "slack": "SLACK_ALLOWED_USERS", "signal": "SIGNAL_ALLOWED_USERS",
    "email": "EMAIL_ALLOWED_USERS", "sms": "SMS_ALLOWED_USERS",
    "mattermost": "MATTERMOST_ALLOWED_USERS", "matrix": "MATRIX_ALLOWED_USERS",
    "dingtalk": "DINGTALK_ALLOWED_USERS", "feishu": "FEISHU_ALLOWED_USERS",
    "wecom": "WECOM_ALLOWED_USERS", "wecom_callback": "WECOM_CALLBACK_ALLOWED_USERS",
    "weixin": "WEIXIN_ALLOWED_USERS", "bluebubbles": "BLUEBUBBLES_ALLOWED_USERS",
    "qqbot": "QQ_ALLOWED_USERS", "yuanbao": "YUANBAO_ALLOWED_USERS",
    "buzz": "BUZZ_ALLOWED_USERS", "simplex": "SIMPLEX_ALLOWED_USERS",
}


def _pairing_matching_ids(platform: str, approved: dict, user_id: str) -> list[str]:
    """Use upstream's transitive phone/LID aliases for WhatsApp revocation."""
    if platform in {"whatsapp", "whatsapp_cloud"}:
        # LID mappings can make different numeric IDs the same person. A local
        # string comparison could leave a second approved alias authorized.
        from gateway.pairing import _matching_ids
        return _matching_ids(platform, approved, user_id)
    return [user_id] if user_id in approved else []


def _pairing_allowlist_ids(raw: str) -> set[str]:
    """Parse Hermes' CSV or JSON-list allowlist spellings conservatively."""
    raw = raw.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"*"}  # An unknown nonempty policy must block a false revoke.
        if isinstance(parsed, list):
            return {str(value).strip() for value in parsed if str(value).strip()}
        return {"*"}
    return {part.strip() for part in raw.split(",") if part.strip()}


def _pairing_config_allowlist_ids(value: object) -> set[str]:
    if value is None or value == "":
        return set()
    if isinstance(value, str):
        # Hermes expands ${VAR} / ${env:VAR} before authorization. Comparing a
        # raw template to a user ID would miss a live grant; fail closed.
        if "${" in value:
            return {"*"}
        return _pairing_allowlist_ids(value)
    if isinstance(value, list):
        if any(isinstance(item, (dict, list, bool)) or item is None for item in value):
            return {"*"}
        if any("${" in str(item) for item in value):
            return {"*"}
        return {str(item).strip() for item in value if str(item).strip()}
    # A malformed or unfamiliar nonempty policy must not certify a revoke.
    return {"*"}


def _pairing_revoke_grants(platform: str, user_id: str) -> tuple[list[str], list[str]]:
    """Return proven direct grants and separate context-dependent policies."""
    env = {**os.environ, **read_env(ENV_FILE)}
    platform_key = _PAIRING_ALLOWLIST_ENV.get(platform, f"{platform.upper()}_ALLOWED_USERS")
    candidates = [platform_key, "GATEWAY_ALLOWED_USERS"]
    blockers = []
    advisories = []
    for key in ("GATEWAY_ALLOW_ALL_USERS", platform_key.replace("_ALLOWED_USERS", "_ALLOW_ALL_USERS")):
        if env.get(key, "").strip().lower() in {"true", "1", "yes"}:
            blockers.append(key)
    for key in candidates:
        allowed = _pairing_allowlist_ids(env.get(key, ""))
        if "*" in allowed or _pairing_matching_ids(platform, dict.fromkeys(allowed), user_id):
            blockers.append(key)
        elif allowed and platform in {"buzz", "simplex"}:
            # Upstream also matches Buzz npub/hex and SimpleX display names.
            # A raw ID alone cannot prove whether a different entry matches.
            advisories.append(key)
    # Group/chat/role rules may grant access in a different context, but a
    # configured group alone does not prove this DM user is admitted. Let the
    # admin remove the pairing row and report the possible remaining rule.
    if platform == "telegram":
        advisories.extend(key for key in ("TELEGRAM_GROUP_ALLOWED_USERS", "TELEGRAM_GROUP_ALLOWED_CHATS")
                          if env.get(key, "").strip())
    if platform == "discord" and env.get("DISCORD_ALLOWED_ROLES", "").strip():
        advisories.append("DISCORD_ALLOWED_ROLES")

    # Native dashboard and plugin settings can grant access independently of
    # the template's .env. Upstream authz_mixin reads these from platform extra.
    config = _read_config_for_write(Path(HERMES_HOME) / "config.yaml")
    gateway = config.get("gateway")
    platforms = gateway.get("platforms") if isinstance(gateway, dict) else None
    settings = platforms.get(platform) if isinstance(platforms, dict) else None
    extra = settings.get("extra") if isinstance(settings, dict) else None
    if isinstance(extra, dict):
        for key in ("allow_from", "allowed_users"):
            allowed = _pairing_config_allowlist_ids(extra.get(key))
            if "*" in allowed or _pairing_matching_ids(platform, dict.fromkeys(allowed), user_id):
                blockers.append(f"config.yaml gateway.platforms.{platform}.extra.{key}")
            elif allowed and platform in {"buzz", "simplex"}:
                advisories.append(f"config.yaml gateway.platforms.{platform}.extra.{key}")
        for key in ("group_allow_from", "group_allowed_chats", "groups", "allowed_roles"):
            if extra.get(key):
                advisories.append(f"config.yaml gateway.platforms.{platform}.extra.{key}")
    return blockers, advisories


async def api_pairing_revoke(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if err := guard(request): return err
    platform, uid = body.get("platform",""), body.get("user_id","")
    if not platform or not uid:
        return JSONResponse({"error": "platform and user_id required"}, status_code=400)
    p = pairing_dir() / f"{platform}-approved.json"
    try:
        approved = _read_pairing_json(p)
        matching = _pairing_matching_ids(platform, approved, uid)
        if not matching:
            return JSONResponse({"error": "Pairing approval no longer exists"}, status_code=404)
        blockers, advisories = _pairing_revoke_grants(platform, uid)
    except (OSError, ValueError, ImportError) as e:
        return JSONResponse({"error": f"Could not verify pairing access safely: {e}"}, status_code=500)
    if blockers:
        return JSONResponse(
            {"error": f"Access may also be granted by {', '.join(blockers)}. Remove that rule in "
                      "Hermes settings or Railway variables, restart the gateway, then revoke the pairing approval."},
            status_code=409,
        )
    for approved_id in matching:
        del approved[approved_id]
    try:
        _wjson(p, approved)
    except (OSError, ValueError) as e:
        return JSONResponse({"error": f"Could not revoke pairing approval: {e}"}, status_code=500)
    result = {"ok": True, "scope": "pairing"}
    if advisories:
        result["warning"] = (
            "Pairing approval removed. Other access rules may still apply through "
            + ", ".join(advisories) + "."
        )
    return JSONResponse(result)


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

# v2026.9.21 makes a partial archive an explicit result: the zip is kept, but
# `hermes backup` exits 1 so unattended timers never publish it as a success.
# The manual download endpoint intentionally still hands useful config/keys/
# memories to the operator with a warning; the pre-restore safety snapshot
# remains fail-closed on every non-zero rc.
BACKUP_INCOMPLETE_RC = 1

# hermes >= v2026.8.13 serializes backups across processes: `run_backup` takes a
# flock on $HERMES_HOME/.backup.lock with a 0.25s acquire timeout and, on a
# miss, raises SystemExit(2) after printing "another Hermes backup is already
# running". Our own asyncio `backup_lock` cannot prevent this — it only
# serializes OUR two callers, while hermes' snapshot path is reachable
# independently (e.g. `/snapshot` typed in the proxied Chat tab, or the native
# dashboard's own detached backup action). Distinguishing rc 2 matters most on
# the restore path, where a generic failure is reported as "could not create a
# complete pre-restore safety snapshot" — which reads as "your data is
# unbackupable" when the real cause is a quarter-second lock collision that
# succeeds on retry.
BACKUP_BUSY_RC = 2
# rc 2 alone is NOT sufficient evidence of a lock collision: argparse also exits
# 2 on an unrecognised flag (verified — `hermes backup --nonsense` -> rc 2).
# Our argv is fixed, so that can only happen if a future hermes renames `-o`,
# but then we would be telling the user "another backup is running" forever
# while the real problem is a broken CLI. Require upstream's marker text too and
# fall back to the generic failure (which prints the real output) otherwise —
# an upstream reword degrades to today's behaviour, never to a false diagnosis.
BACKUP_BUSY_MARKER = "already running"
BACKUP_BUSY_MESSAGE = ("Another Hermes backup or snapshot is running right now "
                       "(they cannot run at the same time). Try again in a moment.")


def _is_backup_busy(rc: int, output: str) -> bool:
    """True when `hermes backup` bailed because it lost the cross-process lock."""
    return rc == BACKUP_BUSY_RC and BACKUP_BUSY_MARKER in (output or "").lower()
SNAPSHOT_NAME_RE = re.compile(r"^pre-restore-\d+-[0-9a-f]+\.zip$")

backup_lock = asyncio.Lock()

# The manual backup UI uses a short create request, a polled job, and a separate
# download. Railway closes an HTTP request after 5 minutes without response
# bytes; creating the entire ZIP inside GET /download can exceed that limit.
# Keep tiny job records on the volume so a reload/redeploy explains what
# happened, while large ZIPs stay on ephemeral disk and never bloat the volume.
MANUAL_BACKUP_INDEX = BACKUP_DIR / ".template-manual-backup-jobs.json"
MANUAL_BACKUP_TMP = Path(tempfile.gettempdir()) / "hermes-template-manual-backups"
MANUAL_BACKUP_ID_RE = re.compile(r"^[0-9a-f]{24}$")
MANUAL_BACKUP_TIMEOUT = 30 * 60
MANUAL_BACKUP_READY_TTL = 6 * 60 * 60
MANUAL_BACKUP_KEEP_READY = 2
MANUAL_BACKUP_KEEP_RECORDS = 5
_manual_backup_start_lock = asyncio.Lock()
_manual_backup_task: asyncio.Task | None = None
_manual_backup_active_id: str | None = None
_manual_backup_downloads: set[str] = set()


def _manual_backup_paths(job_id: str) -> tuple[Path, Path, Path]:
    if not MANUAL_BACKUP_ID_RE.fullmatch(job_id):
        raise ValueError("Invalid backup job ID")
    directory = MANUAL_BACKUP_TMP / job_id
    return directory, directory / "backup.zip", directory / "output.log"


def _manual_backup_jobs_read() -> list[dict]:
    try:
        mode = MANUAL_BACKUP_INDEX.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("Backup job state is not a regular file")
        data = json.loads(MANUAL_BACKUP_INDEX.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise ValueError("Could not read backup job state safely") from exc
    if not isinstance(data, list) or any(
        not isinstance(job, dict)
        or not MANUAL_BACKUP_ID_RE.fullmatch(str(job.get("id", "")))
        or job.get("status") not in {"running", "ready", "failed", "expired"}
        or not isinstance(job.get("started_at"), (int, float))
        or (job.get("finished_at") is not None and not isinstance(job["finished_at"], (int, float)))
        for job in data
    ):
        raise ValueError("Backup job state has an unrecognized format")
    return data


def _manual_backup_jobs_write(jobs: list[dict]) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if not stat.S_ISDIR(BACKUP_DIR.lstat().st_mode):
        raise ValueError("Backup directory is not a real directory")
    _atomic_write_text(MANUAL_BACKUP_INDEX, json.dumps(jobs, separators=(",", ":")) + "\n")


def _manual_backup_remove_artifact(job_id: str) -> bool:
    if job_id in _manual_backup_downloads:
        return False
    directory, _, _ = _manual_backup_paths(job_id)
    if directory.is_symlink():
        raise ValueError("Backup job directory is linked")
    shutil.rmtree(directory, ignore_errors=True)
    return True


def _manual_backup_jobs_reconcile(jobs: list[dict]) -> list[dict]:
    """Explain interrupted/expired work and bound retained ZIPs on every read."""
    now = time.time()
    changed = False
    jobs.sort(key=lambda job: float(job.get("started_at") or 0), reverse=True)
    for job in jobs:
        job_id = job["id"]
        _, archive, _ = _manual_backup_paths(job_id)
        if job["status"] == "running" and job_id != _manual_backup_active_id:
            job.update(status="failed", phase="failed", finished_at=now,
                       error="Backup was interrupted by a service restart. Create another backup.")
            _manual_backup_remove_artifact(job_id)
            changed = True
        elif job["status"] == "ready":
            expired = now >= float(job.get("finished_at") or 0) + MANUAL_BACKUP_READY_TTL
            missing = not archive.is_file()
            if (expired or missing) and _manual_backup_remove_artifact(job_id):
                job.update(status="expired", phase="expired",
                           error="Prepared backup expired or is unavailable after a container restart. Create another backup.")
                changed = True
    ready = [job for job in jobs if job["status"] == "ready"]
    for job in ready[MANUAL_BACKUP_KEEP_READY:]:
        if _manual_backup_remove_artifact(job["id"]):
            job.update(status="expired", phase="expired",
                       error="A newer backup replaced this download. Create another backup if needed.")
            changed = True
    while len(jobs) > MANUAL_BACKUP_KEEP_RECORDS and jobs[-1]["id"] not in _manual_backup_downloads:
        old = jobs.pop()
        _manual_backup_remove_artifact(old["id"])
        changed = True
    if changed:
        _manual_backup_jobs_write(jobs)
    return jobs


def _manual_backup_job_update(job_id: str, **changes) -> dict:
    jobs = _manual_backup_jobs_read()
    job = next((item for item in jobs if item["id"] == job_id), None)
    if job is None:
        raise ValueError("Backup job record disappeared")
    job.update(changes)
    _manual_backup_jobs_write(jobs)
    return job


def _manual_backup_log_progress(job_id: str) -> tuple[str, int | None, int | None]:
    """Read only a bounded log tail; Hermes reports file counts every 500 files."""
    _, _, log_path = _manual_backup_paths(job_id)
    try:
        with log_path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 64 * 1024))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return "preparing", None, None
    phase, completed, total = "scanning", None, None
    for line in lines:
        stripped = line.strip()
        if match := re.fullmatch(r"Backing up (\d+) files \.\.\.", stripped):
            phase, total = "archiving", int(match.group(1))
        elif match := re.fullmatch(r"(\d+)/(\d+) files \.\.\.", stripped):
            phase, completed, total = "archiving", int(match.group(1)), int(match.group(2))
    return phase, completed, total


def _manual_backup_job_public(job: dict) -> dict:
    status = job["status"]
    phase, completed, total = _manual_backup_log_progress(job["id"]) if status == "running" else (
        job.get("phase", status), job.get("completed_files"), job.get("total_files"))
    if status == "running" and job.get("phase") == "verifying":
        phase = "verifying"
    result = {key: job.get(key) for key in (
        "id", "status", "started_at", "finished_at", "size_bytes", "warning", "error"
    )}
    result.update(phase=phase, completed_files=completed, total_files=total,
                  elapsed_seconds=max(0, int((job.get("finished_at") or time.time()) - float(job["started_at"]))))
    if status == "ready":
        result["download_url"] = f"/setup/api/backup/jobs/{job['id']}/download"
        result["expires_at"] = float(job["finished_at"]) + MANUAL_BACKUP_READY_TTL
    return result


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
            start_new_session=True,
        )
    except OSError as e:
        return 127, f"Could not launch hermes {' '.join(args)}: {e}"
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate_process_group(proc)
        return 124, f"hermes {' '.join(args)} timed out after {timeout}s"
    except BaseException:
        await _terminate_process_group(proc)
        raise
    return proc.returncode, raw.decode(errors="replace")


async def _hermes_version() -> str:
    """Best-effort `hermes --version`, used only for the restore-time compat hint."""
    try:
        rc, out = await _run_hermes_cli("--version", timeout=15)
        return out.strip() if rc == 0 else "unknown"
    except Exception:
        return "unknown"


RESTORE_STATE_NAME = "template-restore-state.json"


def _restore_state() -> dict | None:
    snapshot = _file_snapshot(BACKUP_DIR / RESTORE_STATE_NAME)
    if snapshot is None:
        return None
    state = json.loads(snapshot[0].decode("utf-8"))
    if (not isinstance(state, dict) or state.get("phase") not in {"importing", "imported"}
            or not isinstance(state.get("snapshot"), str) or not SNAPSHOT_NAME_RE.fullmatch(state["snapshot"])):
        raise ValueError("Invalid restore recovery record. Keep the safety snapshots and check deployment logs.")
    return state


def _write_restore_state(state: dict | None) -> None:
    path = BACKUP_DIR / RESTORE_STATE_NAME
    if state is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write_text(path, json.dumps(state) + "\n")
    fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _prune_pre_restore_snapshots(protected_name: str | None = None) -> None:
    pending = _restore_state()
    snaps = sorted(BACKUP_DIR.glob("pre-restore-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in snaps[PRE_RESTORE_KEEP:]:
        if stale.name == protected_name or (pending and stale.name == pending["snapshot"]):
            continue
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

    # v2026.8.13 made `hermes backup -o` atomic: it builds the archive at
    # `.<name>.<pid>-<tid>.partial` beside the target and os.replace()s it on a
    # clean close. Good change — a failed backup no longer leaves a truncated
    # zip — but a hard kill (OOM, redeploy mid-snapshot) strands the partial,
    # and it is invisible to every cleanup we have: both
    # _prune_pre_restore_snapshots() and api_backup_snapshots() glob
    # `pre-restore-*.zip`, which never matches a dot-prefixed name. Left alone
    # these accumulate on the volume forever.
    #
    # Age guard, not a blanket delete: our own calls are serialized by
    # backup_lock, but hermes' dashboard has its own detached `hermes backup`
    # action, so a fresh partial may belong to a run that is still writing.
    # One hour is far beyond BACKUP_SUBPROCESS_TIMEOUT (10 min), so anything
    # older cannot still be in flight.
    cutoff = time.time() - 3600
    try:
        for partial in BACKUP_DIR.glob(".pre-restore-*.partial"):
            try:
                if partial.stat().st_mtime < cutoff:
                    partial.unlink(missing_ok=True)
                    print(f"[backup] removed stale partial {partial.name}", flush=True)
            except OSError:
                pass
    except OSError:
        pass


# Mirrors hermes' own _EXCLUDED_DIRS (hermes_cli/backup.py, re-verified
# byte-identical at v2026.9.21) so _live_db_names() can never demand a database
# hermes deliberately skips. That direction matters: a false "incomplete" ABORTS
# a restore, which is strictly worse than the gap it closes. Re-check this
# against upstream on a bump — and check _BACKUP_EXCLUDED_ROOT_DIRS below too,
# because upstream now has TWO exclusion mechanisms, not one.
_BACKUP_EXCLUDED_DIRS = {
    "hermes-agent", "__pycache__", ".git", "node_modules", "backups",
    "checkpoints", ".venv", "venv", "site-packages",
    ".cache", ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    # Added upstream in v2026.8.27: quick/pre-update state snapshots and the two
    # live browser-profile dirs (Chromium holds their SQLite files locked).
    "state-snapshots", "browser-profiles", "browser-profile",
}

# v2026.9.11 added a SECOND mechanism upstream (_EXCLUDED_ROOT_DIRS +
# _in_excluded_root_dir in hermes_cli/backup.py): three trees skipped ONLY at the
# top of HERMES_HOME (and at profiles/<name>/), holding machine-scoped bulk data
# the new local-model runtime downloads — GGUF weights, llama.cpp binaries and a
# managed Node install, which upstream's own comment calls "routinely tens to
# hundreds of GB". Root-scoped, not flat: a skills/foo/models/ directory is still
# backed up, so mirroring these into _BACKUP_EXCLUDED_DIRS would over-exclude.
# Without this mirror a *.db under any of the three is demanded by
# _live_db_names() while `hermes backup` deliberately omits it, and the resulting
# false "incomplete" ABORTS a restore.
_BACKUP_EXCLUDED_ROOT_DIRS = {"models", "runtimes", "node", "installs", "tools"}

# v2026.9.21 added backup-only exclusions that are root-scoped for the same
# reason as the runtime trees above. browser_profiles is Browser Use CLI's live
# Chromium user-data dir (locked SQLite + credentials); a skill's own nested
# browser_profiles/ remains user data and must still be archived.
_BACKUP_EXCLUDED_BACKUP_ROOT_DIRS = {"browser_profiles"}

# cache/ now mixes regenerable scratch/catalog/browser data with durable media
# delivered to or received from users and the grounded-citations evidence
# ledger. Hermes archives ONLY these subdirs at the root of each profile home.
# This mirror is deliberately positive: adding "cache" to the flat exclusions
# would silently drop the durable directories too.
_BACKUP_KEPT_CACHE_SUBDIRS = {
    "images", "audio", "videos", "documents", "screenshots", "citations", "generated",
}


def _in_excluded_root_dir(rel: Path) -> bool:
    """Mirror of hermes' `_in_excluded_root_dir`: profile-home roots only.

    `rel` is the path relative to HERMES_HOME, filename included — same shape
    upstream passes. Strip ``profiles/<name>`` first so the same rules apply to
    both the default home and every named profile without over-excluding a
    nested skill/project directory that happens to use one of these names.
    """
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == "profiles":
        parts = parts[2:]
    if not parts:
        return False
    if parts[0] in _BACKUP_EXCLUDED_ROOT_DIRS or parts[0] in _BACKUP_EXCLUDED_BACKUP_ROOT_DIRS:
        return True
    return (
        parts[0] == "cache"
        and len(parts) >= 2
        and parts[1] not in _BACKUP_KEPT_CACHE_SUBDIRS
    )


def _live_db_names() -> set[str]:
    """Home-relative paths of SQLite DBs that Hermes backup should contain.

    Was hardcoded to state.db. v2026.8.13 keeps adding databases beside it
    (cron/notepad.db is new; kanban.db, cron/executions.db, projects.db,
    response_store.db and verification_evidence.db already existed), and
    hermes' `_safe_copy_db` fails CLOSED — it drops a database it cannot
    snapshot from the archive while `hermes backup` still exits 0. A check
    naming only state.db therefore certifies an archive as sound while other
    databases are silently absent from it.

    Full paths matter: root and named profiles can each have response_store.db.
    A root copy must never hide a missing profile copy in a pre-restore snapshot.
    The legacy function name is retained for existing callers.
    """
    root = Path(HERMES_HOME)
    if not root.is_dir():
        raise OSError(f"Hermes home is not a readable directory: {root}")
    found: set[str] = set()

    def scan_error(error: OSError) -> None:
        raise error

    # Match upstream's os.walk(followlinks=False) and directory pruning. onerror
    # is mandatory: its default silently skips unreadable trees, which could
    # certify a partial safety snapshot as complete.
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=scan_error):
        rel_dir = Path(dirpath).relative_to(root)
        is_root = rel_dir == Path(".")
        dirnames[:] = [
            name for name in dirnames
            if (name not in _BACKUP_EXCLUDED_DIRS or (name == "hermes-agent" and not is_root))
            and not _in_excluded_root_dir(rel_dir / name)
        ]
        for name in filenames:
            if not name.endswith(".db"):
                continue
            rel = rel_dir / name
            if _in_excluded_root_dir(rel):
                continue
            path = root / rel
            if stat.S_ISREG(path.lstat().st_mode):
                found.add(rel.as_posix())
    return found


def _incomplete_backup_reason(zip_path: Path) -> str | None:
    """Why `zip_path` is not a trustworthy backup, or None when it looks sound.

    `rc == 0` stopped proving that in hermes v2026.7.20: `_safe_copy_db`
    (hermes_cli/backup.py) lost its raw-copy fallback and now fails CLOSED, so a
    SQLite snapshot that can't be taken is DROPPED from the archive while
    `hermes backup` still exits 0 — the only trace is the word "incomplete" in
    its summary. That matters most for the pre-restore safety snapshot, the one
    copy standing between a bad restore and permanent data loss.

    We inspect the artifact rather than grepping the summary text, so an
    upstream reword can't silently disable the guard. state.db is required only
    when the live deployment actually has one — a bot that has never held a
    conversation legitimately has no session DB yet, and must still be able to
    back up.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = {Path(n).as_posix() for n in zf.namelist()}
    except Exception as e:
        return f"the archive could not be read back ({e})"
    try:
        missing = sorted(_live_db_names() - names)
    except OSError as e:
        # Download still returns the useful partial archive with a warning;
        # pre-restore treats this reason as a hard stop before changing data.
        return f"could not inspect live databases ({e})"
    if not missing:
        return None
    if "state.db" in missing:
        # Name the one users recognise first — it is the sessions/chat history.
        others = [m for m in missing if m != "state.db"]
        tail = f" (also {', '.join(others)})" if others else ""
        return f"state.db (sessions and chat history) is missing from the archive{tail}"
    return f"{', '.join(missing)} missing from the archive"


def _manual_backup_prepare_dir(job_id: str) -> Path:
    MANUAL_BACKUP_TMP.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(MANUAL_BACKUP_TMP.lstat().st_mode):
        raise ValueError("Temporary backup directory is not a real directory")
    directory, archive, _ = _manual_backup_paths(job_id)
    directory.mkdir(mode=0o700)
    return archive


def _manual_backup_log_tail(job_id: str) -> str:
    _, _, log_path = _manual_backup_paths(job_id)
    try:
        with log_path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 16 * 1024))
            return stream.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _manual_backup_failed(job_id: str, message: str) -> None:
    try:
        _manual_backup_job_update(job_id, status="failed", phase="failed",
                                  finished_at=time.time(), error=message)
    except (OSError, ValueError) as exc:
        print(f"[backup-job] could not persist failure for {job_id}: {exc}", flush=True)
    try:
        _manual_backup_remove_artifact(job_id)
    except (OSError, ValueError) as exc:
        print(f"[backup-job] could not remove failed artifact for {job_id}: {exc}", flush=True)
    print(f"[backup-job] {job_id} failed: {message}", flush=True)


async def _manual_backup_run(job_id: str) -> None:
    """Build one ZIP off-request so the browser never waits through generation."""
    global _manual_backup_active_id, _manual_backup_task
    _, archive, log_path = _manual_backup_paths(job_id)
    proc = None
    try:
        env = {**build_hermes_env(), "PYTHONUNBUFFERED": "1"}
        with log_path.open("wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            proc = await asyncio.create_subprocess_exec(
                "hermes", "backup", "-o", str(archive),
                stdout=stream, stderr=asyncio.subprocess.STDOUT, env=env,
            )
            print(f"[backup-job] {job_id} started pid={proc.pid}", flush=True)
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=MANUAL_BACKUP_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                _manual_backup_failed(job_id, "Backup exceeded 30 minutes. Check deployment logs and try again.")
                return

        output = _manual_backup_log_tail(job_id)
        if _is_backup_busy(rc, output):
            _manual_backup_failed(job_id, BACKUP_BUSY_MESSAGE)
            return
        incomplete_exit = rc == BACKUP_INCOMPLETE_RC and archive.is_file()
        if (rc != 0 and not incomplete_exit) or not archive.is_file():
            _manual_backup_failed(job_id, f"Hermes backup failed (exit {rc}). Check deployment logs and try again.")
            return

        _manual_backup_job_update(job_id, phase="verifying")
        def verify_archive() -> None:
            with zipfile.ZipFile(archive) as zf:
                if zf.testzip() is not None:
                    raise ValueError("Archive contains a damaged member")
        try:
            await asyncio.to_thread(verify_archive)
        except Exception:
            _manual_backup_failed(job_id, "Backup ZIP could not be read back. Nothing was offered for download.")
            return

        # Preserve the legacy endpoint's version hint and partial-backup checks.
        try:
            version = await _hermes_version()
            def add_manifest() -> None:
                with zipfile.ZipFile(archive, "a") as zf:
                    zf.writestr("template_manifest.json", json.dumps({
                        "hermes_version": version,
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "template": "hermes-agent-railway-template",
                    }))
            await asyncio.to_thread(add_manifest)
        except Exception as exc:
            print(f"[backup-job] {job_id} manifest hint skipped: {exc}", flush=True)

        warning_parts = []
        if incomplete_exit:
            warning_parts.append("Hermes reported that one or more files could not be included")
        if reason := await asyncio.to_thread(_incomplete_backup_reason, archive):
            warning_parts.append(reason)
        phase, completed, total = _manual_backup_log_progress(job_id)
        warning = "; ".join(warning_parts) or None
        _manual_backup_job_update(
            job_id, status="ready", phase="ready", finished_at=time.time(),
            completed_files=total if total is not None else completed,
            total_files=total, size_bytes=archive.stat().st_size, warning=warning,
        )
        print(f"[backup-job] {job_id} ready bytes={archive.stat().st_size} warning={bool(warning)}", flush=True)
        try:
            _manual_backup_jobs_reconcile(_manual_backup_jobs_read())
        except (OSError, ValueError) as exc:
            print(f"[backup-job] cleanup after {job_id} failed: {exc}", flush=True)
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        _manual_backup_failed(job_id, "Backup was interrupted by a service restart. Create another backup.")
        raise
    except Exception as exc:
        _manual_backup_failed(job_id, f"Could not finish backup: {exc}")
    finally:
        _manual_backup_active_id = None
        _manual_backup_task = None
        backup_lock.release()


async def api_backup_jobs_get(request: Request) -> Response:
    if err := guard(request): return err
    try:
        jobs = _manual_backup_jobs_reconcile(_manual_backup_jobs_read())
        return JSONResponse({"jobs": [_manual_backup_job_public(job) for job in jobs]},
                            headers={"Cache-Control": "private, no-store"})
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": f"Could not inspect backup jobs: {exc}"}, status_code=500)


async def api_backup_jobs_start(request: Request) -> Response:
    global _manual_backup_active_id, _manual_backup_task
    if err := guard(request): return err
    async with _manual_backup_start_lock:
        try:
            jobs = _manual_backup_jobs_reconcile(_manual_backup_jobs_read())
            active = next((job for job in jobs if job["status"] == "running"), None)
            if active is not None:
                return JSONResponse({"job": _manual_backup_job_public(active)}, status_code=202,
                                    headers={"Cache-Control": "private, no-store"})
            if backup_lock.locked():
                return JSONResponse({"error": "A backup or restore is already in progress"}, status_code=409)
            await backup_lock.acquire()
            job_id = None
            try:
                job_id = secrets.token_hex(12)
                _manual_backup_prepare_dir(job_id)
                job = {"id": job_id, "status": "running", "phase": "preparing",
                       "started_at": time.time(), "finished_at": None,
                       "size_bytes": None, "warning": None, "error": None}
                jobs.insert(0, job)
                _manual_backup_jobs_write(jobs)
                _manual_backup_active_id = job_id
                _manual_backup_task = asyncio.create_task(_manual_backup_run(job_id))
            except Exception:
                _manual_backup_active_id = None
                _manual_backup_task = None
                try:
                    if job_id is not None:
                        _manual_backup_remove_artifact(job_id)
                except (OSError, ValueError) as exc:
                    print(f"[backup-job] could not clean failed start: {exc}", flush=True)
                finally:
                    backup_lock.release()
                raise
            return JSONResponse({"job": _manual_backup_job_public(job)}, status_code=202,
                                headers={"Cache-Control": "private, no-store"})
        except (OSError, ValueError) as exc:
            return JSONResponse({"error": f"Could not start backup: {exc}"}, status_code=500)


async def api_backup_job_delete(request: Request) -> Response:
    if err := guard(request): return err
    job_id = request.path_params.get("job_id", "")
    if not MANUAL_BACKUP_ID_RE.fullmatch(job_id):
        return JSONResponse({"error": "Invalid backup job ID"}, status_code=404)
    try:
        jobs = _manual_backup_jobs_reconcile(_manual_backup_jobs_read())
        job = next((item for item in jobs if item["id"] == job_id), None)
        if job is None:
            return JSONResponse({"error": "Backup job not found"}, status_code=404)
        if job["status"] == "running":
            return JSONResponse({"error": "Wait for this backup to finish before removing it."}, status_code=409)
        if not _manual_backup_remove_artifact(job_id):
            return JSONResponse({"error": "This backup is being downloaded. Try again when the download finishes."},
                                status_code=409)
        directory, _, _ = _manual_backup_paths(job_id)
        if directory.exists():
            raise OSError("Could not remove the prepared backup file")
        _manual_backup_jobs_write([item for item in jobs if item["id"] != job_id])
        return JSONResponse({"ok": True}, headers={"Cache-Control": "private, no-store"})
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": f"Could not remove backup: {exc}"}, status_code=500)


async def api_backup_job_download(request: Request) -> Response:
    if err := guard(request): return err
    job_id = request.path_params.get("job_id", "")
    if not MANUAL_BACKUP_ID_RE.fullmatch(job_id):
        return JSONResponse({"error": "Invalid backup job ID"}, status_code=404)
    try:
        jobs = _manual_backup_jobs_reconcile(_manual_backup_jobs_read())
        job = next((item for item in jobs if item["id"] == job_id), None)
        if job is None:
            return JSONResponse({"error": "Backup job not found"}, status_code=404)
        if job["status"] != "ready":
            return JSONResponse({"error": "Backup is not available for download; check its status."}, status_code=409)
        directory, archive, _ = _manual_backup_paths(job_id)
        if directory.is_symlink() or not stat.S_ISREG(archive.lstat().st_mode):
            raise ValueError("Backup archive is not a regular file")
        headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
        if job.get("warning"):
            headers["X-Backup-Warning"] = "Backup is incomplete: " + str(job["warning"])[:500]
        _manual_backup_downloads.add(job_id)
        return FileResponse(
            archive, filename=f"hermes-backup-{int(job['started_at'])}.zip",
            media_type="application/zip", headers=headers,
            background=BackgroundTask(_manual_backup_downloads.discard, job_id),
        )
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": f"Could not download backup: {exc}"}, status_code=500)


async def api_backup_download(request: Request) -> Response:
    if err := guard(request): return err
    if backup_lock.locked():
        return JSONResponse({"error": "A backup or restore is already in progress"}, status_code=409)
    async with backup_lock:
        tmp_dir = tempfile.mkdtemp(prefix="hermes-backup-")
        zip_path = Path(tmp_dir) / "backup.zip"
        # NOTE: v2026.9.11 gave `hermes backup` a `-k/--keep` (default 3) that
        # DELETES older archives in the output directory. It is gated on the
        # output filename starting with hermes' own "hermes-backup-" prefix, so
        # "backup.zip" here is inert — but that is the only thing making it
        # inert. Do not rename these outputs to the upstream prefix shape.
        rc, output = await _run_hermes_cli("backup", "-o", str(zip_path))
        if _is_backup_busy(rc, output):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return JSONResponse({"error": BACKUP_BUSY_MESSAGE, "output": output[-2000:]},
                                status_code=409)
        # rc 1 with a real zip is v2026.9.21's explicit "archive kept, but
        # incomplete" result. It is useful for a MANUAL export and is surfaced
        # below with a warning. All other non-zero exits remain hard failures.
        incomplete_exit = rc == BACKUP_INCOMPLETE_RC and zip_path.exists()
        if (rc != 0 and not incomplete_exit) or not zip_path.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return JSONResponse({"error": "Backup failed", "output": output[-2000:]}, status_code=500)

        # An exit-1 path is useful only if Hermes actually closed a readable
        # archive. Parsing the central directory is cheap and prevents us from
        # sending a truncated/corrupt file merely because it exists.
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.infolist()
        except Exception as e:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return JSONResponse(
                {"error": f"Backup archive could not be read back: {e}", "output": output[-2000:]},
                status_code=500,
            )

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
        headers = {}
        warning_parts: list[str] = []
        if incomplete_exit:
            # The omitted item may be a non-DB file, which the artifact-level DB
            # check below cannot name. Preserve that information even when every
            # live SQLite database is present.
            warning_parts.append("Hermes reported that one or more files could not be included")
        # Surface a partial archive instead of handing over a file the user
        # would only discover is incomplete when a restore fails. Warn rather
        # than block: config, keys and memories are still worth exporting even
        # when the session DB could not be snapshotted.
        if reason := _incomplete_backup_reason(zip_path):
            warning_parts.append(reason)
        if warning_parts:
            warning = "; ".join(warning_parts)
            print(f"[backup] incomplete archive — {warning}", flush=True)
            headers["X-Backup-Warning"] = f"Backup is incomplete: {warning}."
        return FileResponse(
            zip_path,
            filename=filename,
            media_type="application/zip",
            headers=headers,
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

    async with backup_lock, lifecycle_lock, cfg_lock:
        if err := guard(request): return err
        previous_preparation = dict(preparation)
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
                if RESTORE_STATE_NAME in names:
                    return JSONResponse({"error": "Archive contains local restore recovery state; it cannot be imported."}, status_code=400)
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
            # "pre-restore-*" deliberately does not match hermes' own
            # "hermes-backup-" prefix, so its v2026.9.11 `--keep 3` auto-prune
            # never touches these; _prune_pre_restore_snapshots() owns them.
            preparation.update(state="running", phase="safety_backup", message="Creating the pre-restore safety snapshot…")
            print("[restore] creating pre-restore safety snapshot", flush=True)
            rc, output = await _run_hermes_cli("backup", "-o", str(snap_path))
            # A lock collision is transient and retryable — say so, instead of
            # reporting it as "the backup command failed", which reads as data
            # loss. Nothing has been touched yet at this point, so the restore
            # is simply not started. 409, matching the in-progress guard above.
            if _is_backup_busy(rc, output):
                snap_path.unlink(missing_ok=True)
                return JSONResponse(
                    {"error": f"{BACKUP_BUSY_MESSAGE} Nothing was changed — the restore did not start.",
                     "output": output[-2000:]},
                    status_code=409,
                )
            # rc alone is no longer sufficient — see _incomplete_backup_reason().
            # A snapshot missing a live database is not an undo copy, so treat
            # it the same as a failed one and refuse to touch live state.
            snap_problem = _incomplete_backup_reason(snap_path) if rc == 0 else None
            if rc != 0 or snap_problem:
                detail = snap_problem or "the backup command failed"
                snap_path.unlink(missing_ok=True)
                return JSONResponse(
                    {"error": f"Could not create a complete pre-restore safety snapshot ({detail}); "
                              f"restore aborted so nothing is overwritten without an undo copy.",
                     "output": output[-2000:]},
                    status_code=500,
                )
            _, error = await _gateway_maintenance_check()
            if error is not None:
                return error
            pending_restore = _restore_state()
            _prune_pre_restore_snapshots(protected_name=snap_path.name)
            # Keep the original complete copy across failed recovery attempts;
            # a new snapshot may describe an already partially imported home.
            restore_state = {"phase": "importing", "snapshot": pending_restore["snapshot"] if pending_restore else snap_path.name}
            try:
                _write_restore_state(restore_state)
            except (OSError, ValueError) as publication_error:
                # The rename can succeed before directory fsync fails. No
                # importer ran, so restore the prior authority if it can be
                # made durable; otherwise keep activation gated.
                try:
                    _write_restore_state(pending_restore)
                except (OSError, ValueError) as rollback_error:
                    # Rollback may itself remove/replace the marker before its
                    # sync fails. Leave the importing gate present if possible.
                    try:
                        _write_restore_state(restore_state)
                    except (OSError, ValueError) as gate_error:
                        print(f"[restore] recovery record durability remains uncertain: {gate_error}", flush=True)
                    message = f"No import was started, but restore recovery state could not be made durable: {rollback_error}. Keep the safety snapshot and check deployment logs."
                    preparation.update(state="failed", phase="failed", requires_restore=True, message=message)
                    return JSONResponse({"error": message}, status_code=503)
                preparation.update(previous_preparation)
                return JSONResponse({"error": f"No import was started; the prior restore recovery state was retained: {publication_error}"}, status_code=503)
            global _xai_oauth_state, _codex_session
            _xai_oauth_state = None  # An earlier OAuth poll must not overwrite restored credentials.
            preparation.update(state="running", phase="restore", message="Stopping Hermes before import…")
            # Close native lifecycle admission before stopping the real root
            # gateway. A detached native restart may not be self.proc.
            try:
                dashboard_stopped = await dash.stop()
                _codex_session = None  # native device workers stopped with the dashboard
                await _stop_gateway_for_maintenance(dashboard_stopped=dashboard_stopped)
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                preparation.update(previous_preparation)
                try:
                    _write_restore_state(pending_restore)
                except (OSError, ValueError) as rollback_error:
                    message = f"No import was started, but the restore recovery record could not be reset: {rollback_error}. Keep the safety snapshot and check deployment logs."
                    preparation.update(state="failed", phase="failed", requires_restore=True, message=message)
                    return JSONResponse({"error": message}, status_code=503)
                # A failed recovery can already contain partial imported data.
                # Only resume a previously ready, unchanged installation.
                if (previous_preparation["state"] == "ready"
                        and not previous_preparation.get("requires_restore")
                        and await dash.start() is False):
                    preparation.update(state="failed", phase="failed", requires_restore=False,
                                       message="No import was started, but Dashboard could not restart. Retry preparation after checking deployment logs.")
                return JSONResponse({"error": str(exc)}, status_code=503)
            preparation.update(state="running", phase="restore", message="Importing backup; Hermes is stopped…")
            rc, output = await _run_hermes_cli("import", str(upload_path), "--force")
            if rc != 0:
                preparation.update(state="failed", phase="failed", requires_restore=True,
                                   message="Restore did not finish. Restore the pre-restore safety snapshot before starting Hermes.")
                return JSONResponse({"error": preparation["message"], "output": output[-2000:]}, status_code=500)
            _write_restore_state({**restore_state, "phase": "imported"})
            # Import can restore an older schema after native boot maintenance
            # has already completed for this exact code identity. Migrate it
            # explicitly before either child reads the restored state. Run the
            # bounded worker after this response so a slow PM refresh does not
            # turn a successful import into a proxy timeout.
            global _preparation_task, _preparation_mode
            _preparation_mode = "restore"
            preparation.update(state="running", phase="restore", message="Archive imported; preparing restored Hermes state…")
            _preparation_task = asyncio.create_task(_initialize_hermes("restore"))

            resp = {"ok": True, "preparing": True, "output": output[-2000:]}
            if warning:
                resp["warning"] = warning
            return JSONResponse(resp)
        except asyncio.CancelledError:
            if preparation["phase"] == "restore":
                preparation.update(state="failed", phase="failed", requires_restore=True,
                                   message="Restore was interrupted. Restore the pre-restore safety snapshot before starting Hermes.")
            raise
        except Exception as e:
            if preparation["state"] == "running" and preparation["phase"] == "restore":
                preparation.update(state="failed", phase="failed", requires_restore=True,
                                   message=f"Restore was interrupted: {e}. Keep the pre-restore safety snapshot and check deployment logs.")
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            # Before the importing marker is published, the home is unchanged.
            # A failed/busy/cancelled safety copy must not strand a loading state.
            if preparation["phase"] == "safety_backup":
                preparation.update(previous_preparation)
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

# Dashboard actions that can install software at runtime. Their persistence is
# provider-specific: PM selections and tool installs can live on the data
# volume, while some provider hooks still mutate the disposable image.
# MCP catalog install is deliberately NOT here: it installs under
# $HERMES_HOME/mcp-installs on the volume, so it does survive a redeploy.
_IN_CONTAINER_INSTALL_RE = re.compile(
    r"^/api/(?:memory/providers/[^/]+/setup|tools/toolsets/[^/]+/post-setup)$"
)

# v2026.8.13 added a SECOND way to start an install from the Tools tab, on a
# different verb: PUT /api/tools/toolsets/<name> ("install-on-enable",
# hermes_cli/web_routers/tools.py). Flipping a toolset ON now spawns
# `hermes tools post-setup <key>` in the background whenever that toolset's
# provider has a post_setup hook with an UNSATISFIED install-state predicate.
# Today `_POST_SETUP_INSTALLED` (hermes_cli/tools_config.py) holds exactly one
# entry — cua_driver, i.e. Computer Use — but upstream documents that dict as a
# list to extend, so this will grow silently on future bumps. Re-verified on
# v2026.8.31: still just cua_driver. Do not confuse it with `_POST_SETUP_READY`
# in the same file, which DID gain entries (lightpanda) — that one only gates an
# interactive `hermes tools` prompt and never runs from an HTTP request.
#
# This is log-only: cua_driver's external installer may target ~/.local/bin,
# which is on /data here, and a toolset toggle is not a clear install consent
# moment. The POST warning below now describes mixed persistence accurately.
_IN_CONTAINER_INSTALL_PUT_RE = re.compile(r"^/api/tools/toolsets/[^/]+$")


def _in_container_install_kind(method: str, path: str) -> str | None:
    """Name the in-container install this request starts, or None.

    Method-aware because the two families differ: the memory-provider and
    post-setup endpoints are POST, while install-on-enable is a PUT on a path
    that has no POST equivalent (a GET of the same shape is the read side).
    """
    verb = method.upper()
    if verb == "POST" and _IN_CONTAINER_INSTALL_RE.match(path):
        return "warned"
    if verb == "PUT" and _IN_CONTAINER_INSTALL_PUT_RE.match(path):
        return "install-on-enable"
    return None

# Warn without claiming every install vanishes on redeploy. The upstream
# installer may use the durable lazy target, a persistent home path, or a
# provider-specific shell hook. The Docker stamp blocks only core self-update.
IMMUTABLE_INSTALL_WARNING_JS = (
    '<script>(function(){'
    'var f=window.fetch;if(!f||window.__hermesImmutableWarn)return;'
    'window.__hermesImmutableWarn=1;'
    'window.fetch=function(input,init){try{'
    'var u=(typeof input==="string")?input:(input&&input.url)||"";'
    'var m=((init&&init.method)||(input&&input.method)||"GET").toUpperCase();'
    'if(m==="POST"&&/\\/api\\/(memory\\/providers\\/[^\\/]+\\/setup|tools\\/toolsets\\/[^\\/]+\\/post-setup)/.test(u)&&'
    '!window.confirm("MESSAGE FROM THE TEMPLATE CREATOR\\n'
    '----------------------------------------\\n\\n'
    'This setup may install software at runtime. Hermes packages installed in '
    'the persistent /data directory survive redeploys; some provider-specific '
    'installers write into the disposable image and may need to be run again.\\n\\n'
    'Your saved settings and credentials remain on the volume. After a redeploy, '
    'check that this integration is still available.\\n\\n'
    'Continue with setup?"))'
    '{return Promise.reject(new Error("Cancelled: immutable deployment"));}'
    '}catch(e){}return f.apply(this,arguments);};})();</script>'
)

# Cookie names hermes' dashboard-auth sessions use. Stripped from inbound
# requests so a stale browser copy can never shadow the session we hold.
HERMES_SESSION_COOKIE_PREFIX = "__Host-hermes_session"


class HermesSession:
    """The dashboard session this proxy logs in with, on the user's behalf.

    Users authenticate once at our own login. Hermes' auth gate exists only so a
    public URL can be declared (which is what makes MCP OAuth redirects resolve
    to the real host), so nobody should ever meet a second login screen: we hold
    a hermes session here and inject it into every proxied request.

    Recovery is always possible because we hold the password — an expired or
    invalidated session is just a re-login, not a dead end. Callers pass the
    generation they last saw so a burst of concurrent 401s triggers ONE login
    rather than one per request.
    """

    def __init__(self) -> None:
        self._cookies: dict[str, str] = {}
        self._generation = 0
        self._lock = asyncio.Lock()
        self._failed_note = False

    def snapshot(self) -> tuple[int, dict[str, str]]:
        """Current (generation, cookies) — cheap, lock-free read path."""
        return self._generation, dict(self._cookies)

    async def refresh(self, seen_generation: int) -> bool:
        """Log in unless another caller already did since `seen_generation`."""
        async with self._lock:
            if self._generation != seen_generation:
                return bool(self._cookies)
            if self._cookies:
                # Expected on a dashboard restart or after the 12h access-token
                # TTL. Logged so a burst of these is visibly a session problem
                # rather than an unexplained slowdown.
                print("[dashboard-auth] dashboard rejected the held session — "
                      "signing in again", flush=True)
            if await self._login():
                self._generation += 1
                return True
            return False

    async def _login(self) -> bool:
        user, password = hermes_dashboard_credentials()
        try:
            resp = await get_http_client().post(
                f"{HERMES_DASHBOARD_URL}/auth/password-login",
                json={"provider": "basic", "username": user, "password": password},
            )
        except httpx.RequestError as e:
            print(f"[dashboard-auth] login request failed: {e!r}", flush=True)
            return False
        if resp.status_code != 200:
            # Don't spam every request once credentials are genuinely wrong.
            if not self._failed_note:
                self._failed_note = True
                print(
                    f"[dashboard-auth] login rejected ({resp.status_code}). The "
                    f"dashboard will 401 until ADMIN_PASSWORD (or the explicit "
                    f"HERMES_DASHBOARD_BASIC_AUTH_* vars) match what the "
                    f"dashboard subprocess was started with.",
                    flush=True,
                )
            return False
        self._cookies = {k: v for k, v in resp.cookies.items()}
        self._failed_note = False
        print(f"[dashboard-auth] signed in as {user!r} ({len(self._cookies)} cookies)", flush=True)
        return bool(self._cookies)


hermes_session = HermesSession()


def _merge_session_cookie(header: str | None, session: dict[str, str]) -> str:
    """Replace any inbound hermes session cookies with the ones we hold."""
    kept = [
        part.strip() for part in (header or "").split(";")
        if part.strip() and not part.strip().startswith(HERMES_SESSION_COOKIE_PREFIX)
    ]
    kept.extend(f"{k}={v}" for k, v in session.items())
    return "; ".join(kept)


def _needs_dashboard_login(status: int, location: str) -> bool:
    """True when hermes is telling us the injected session is not (or no longer) valid."""
    if status == 401:
        return True
    return status in (302, 303, 307) and location.split("?", 1)[0].endswith("/login")


# Shown only when hermes rejects our login — i.e. the credentials the dashboard
# subprocess started with no longer match the ones we resolve. The usual cause
# is ADMIN_PASSWORD changing without the dashboard being restarted.
DASHBOARD_AUTH_FAILED_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Dashboard sign-in failed</title>
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
<h1>⚠ Could not sign in to the Hermes dashboard</h1>
<p>The admin panel is fine — only the embedded dashboard rejected our sign-in.</p>
<p>This usually means the admin password changed after the dashboard started.
Use Apply dependencies from Status to restart the Hermes processes, or redeploy.</p>
<a href="/setup">← Back to Setup</a>
</div></body></html>"""


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

    async def send(session: dict[str, str]) -> httpx.Response:
        headers = dict(req_headers)
        merged = _merge_session_cookie(headers.pop("cookie", None), session)
        if merged:
            headers["cookie"] = merged
        return await client.request(request.method, target, headers=headers, content=body)

    generation, session = hermes_session.snapshot()
    try:
        upstream = await send(session)
        # hermes' auth gate is on (we declare a public URL so MCP OAuth
        # redirects resolve to the real host), so a 401 — or a bounce to
        # /login — means the session we injected is absent or expired. Re-login
        # once and replay. The browser must never receive that redirect: /login
        # is OUR route, so forwarding it would bounce the user straight back
        # here, forever.
        if _needs_dashboard_login(upstream.status_code, upstream.headers.get("location", "")):
            if await hermes_session.refresh(generation):
                _, session = hermes_session.snapshot()
                upstream = await send(session)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return HTMLResponse(DASHBOARD_UNAVAILABLE_HTML, status_code=503)
    except httpx.RequestError as e:
        print(f"[proxy] upstream error for {request.method} {request.url.path}: {e}", flush=True)
        return HTMLResponse(DASHBOARD_UNAVAILABLE_HTML, status_code=502)

    # Still unauthenticated after a fresh login — credentials genuinely don't
    # match what the dashboard subprocess started with. Fail visibly rather
    # than forwarding a redirect that would loop.
    if _needs_dashboard_login(upstream.status_code, upstream.headers.get("location", "")):
        user, _ = hermes_dashboard_credentials()
        print(f"[dashboard-auth] still unauthenticated after signing in as {user!r} — "
              f"{request.method} {request.url.path}. The dashboard subprocess was started "
              f"with different credentials; use Apply dependencies from Status or redeploy.", flush=True)
        return HTMLResponse(DASHBOARD_AUTH_FAILED_HTML, status_code=502)

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

    # Inject the "← Setup" widget into HTML pages so users can always return,
    # plus the immutable-install confirm shim (see IMMUTABLE_INSTALL_WARNING_JS).
    if "text/html" in content_type and b"</body>" in content:
        try:
            text = content.decode("utf-8", errors="replace")
            text = text.replace(
                "</body>",
                BACK_TO_SETUP_WIDGET + IMMUTABLE_INSTALL_WARNING_JS + "</body>",
                1,
            )
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
    if preparation["state"] != "ready":
        return RedirectResponse("/setup", status_code=302)
    if (request.method == "GET"
            and request.query_params.get("force") != "1"
            and not is_config_complete()):
        return RedirectResponse("/setup", status_code=302)
    return await _proxy_to_dashboard(request)


async def route_proxy(request: Request) -> Response:
    """Catch-all: forward any unmatched path to the Hermes dashboard."""
    if err := guard(request): return err
    settings_write = request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path in NATIVE_SETUP_WRITE_PATHS
    lifecycle_change = request.method == "POST" and (
        request.url.path in NATIVE_GATEWAY_LIFECYCLE_PATHS
        or NATIVE_GATEWAY_ONBOARDING_PATH.fullmatch(request.url.path)
    )
    if settings_write or lifecycle_change:
        # Native routes launch detached lifecycle commands. Serialize their
        # admission with Save/Apply/Restore. Native config/env/model saves share
        # that lock so the two UIs cannot interleave our staged settings write.
        # Maintenance then checks the native
        # action registry before stopping Dashboard and touching user state.
        async with lifecycle_lock:
            if err := guard(request): return err
            return await _proxy_to_dashboard(request)
    # Leave a trail when an in-container install runs: the browser already got
    # the confirm() from IMMUTABLE_INSTALL_WARNING_JS, but this is the only
    # record in `railway logs` explaining why a provider works now and breaks
    # after the next redeploy.
    kind = _in_container_install_kind(request.method, request.url.path)
    if kind == "warned":
        print(f"[proxy] runtime setup requested: {request.url.path} — "
              "persistence depends on the install target", flush=True)
    elif kind == "install-on-enable":
        # No confirm() fires for this one — see _IN_CONTAINER_INSTALL_PUT_RE.
        # This line is the only trace that a toolset toggle kicked off a
        # background `hermes tools post-setup`, so keep it even though the
        # install itself most likely lands on the volume.
        print(f"[proxy] toolset enable may trigger an install-on-enable: "
              f"{request.method} {request.url.path} (hermes >= v2026.8.13)", flush=True)
    return await _proxy_to_dashboard(request)


async def route_setup_404(request: Request) -> Response:
    """Typos under /setup/* should 404 here — not fall through to the proxy."""
    if err := guard(request): return err
    return Response("Not Found", status_code=404, media_type="text/plain")


# ── App lifecycle ─────────────────────────────────────────────────────────────
async def _initialize_hermes(mode: str = "startup"):
    async with backup_lock, lifecycle_lock:
        try:
            try:
                pending = _restore_state()
            except (OSError, ValueError) as exc:
                preparation.update(state="failed", phase="failed", requires_restore=True, message=str(exc))
                return
            if pending and pending["phase"] == "importing":
                preparation.update(state="failed", phase="failed", requires_restore=True,
                                   message=f"A restore was interrupted. Restore safety snapshot {pending['snapshot']} before starting Hermes.")
                return
            if pending:
                mode = "restore"
            async with cfg_lock:
                if not await _prepare_hermes(mode):
                    return
                _sanitize_env_file()
                _consolidate_pairing_dirs()
                if pending:
                    _write_restore_state(None)
            if await dash.start() is False:
                raise RuntimeError("Dashboard could not start. Check deployment logs.")
            if is_config_complete():
                if await gw.start() is False:
                    raise RuntimeError("Gateway could not start. Check Logs.")
            else:
                print("[server] Configure a provider and model in Setup to start the gateway.", flush=True)
        except Exception as exc:
            preparation.update(state="failed", phase="failed", message=str(exc))
            print(f"[preparation] ERROR: {exc}", flush=True)


@asynccontextmanager
async def lifespan(app):
    global _preparation_task
    _sweep_stale_backup_tmpdirs()
    try:
        _manual_backup_jobs_reconcile(_manual_backup_jobs_read())
    except (OSError, ValueError) as exc:
        print(f"[backup-job] could not reconcile jobs at boot: {exc}", flush=True)
    preparation.update(state="running", phase="starting", message="Preparing Hermes…")
    _preparation_task = asyncio.create_task(_initialize_hermes())
    try:
        yield
    finally:
        if _preparation_task is not None:
            _preparation_task.cancel()
            await asyncio.gather(_preparation_task, return_exceptions=True)
        if _manual_backup_task is not None:
            _manual_backup_task.cancel()
            await asyncio.gather(_manual_backup_task, return_exceptions=True)
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
#   /api/console              text frames   — Hermes Console modal (System tab →
#                             "Open console"), added in v2026.7.20. Same
#                             pre-accept gates and same ?token= credential as
#                             /api/pty, so plain forwarding is enough. Without
#                             this route the button dies on one failed connect
#                             with "Console connection failed before the server
#                             handshake" (it does NOT retry, so nothing shows
#                             up in railway logs).
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
PROXIED_WS_PATHS = ("/api/pty", "/api/ws", "/api/events", "/api/console", "/api/plugins/*")

# Mirrors hermes' own ws_max_size (v2026.8.3, web_server.py:362) so this proxy
# is never the narrow end. Our hops default lower — 1 MiB inbound from hermes,
# 16 MiB from the browser — and an oversized frame just closes the socket, so
# Chat/PTY vanishes mid-message with nothing in the logs. Same "both ends must
# agree" trap as the keepalive pairing below. A cap, not a preallocation.
HERMES_WS_MAX_BYTES = 384 * 1024 * 1024

# RFC 6455 reserves 1005/1006/1015 for local reporting; an endpoint must never
# put them on the wire. Uvicorn sends 1012 while the dashboard shuts down for a
# service restart, but Hermes' current SPA reconnects on 1001/1006 and treats a
# clean 1012 as a finished Chat. Normalize all of those restart/abrupt-loss
# cases to 1001 (server going away). Valid application codes such as 4410 (PTY
# exited) still pass through unchanged.
_WS_CLOSE_CODES_REQUIRING_BROWSER_RECONNECT = frozenset({1005, 1006, 1012, 1015})


def _browser_ws_close_code(upstream_code: object) -> int:
    try:
        code = int(upstream_code)
    except (TypeError, ValueError):
        return 1001
    if code in _WS_CLOSE_CODES_REQUIRING_BROWSER_RECONNECT or not 1000 <= code <= 4999:
        return 1001
    return code


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
    if preparation["state"] != "ready":
        await websocket.close(code=1013)
        return
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
            # No keepalive on the loopback hop. hermes v2026.7.20 disabled its
            # own side for a loopback bind (uvicorn ws_ping_interval/timeout
            # went 30s/60s -> None/None) because a GIL-bound agent turn can
            # stall its event loop for minutes, and the ping then kills a
            # healthy socket. It reasoned "loopback means no proxy in front" —
            # but we ARE that proxy, so leaving the websockets client on its
            # 20s/20s default would re-create the bug hermes just fixed: a long
            # tool call drops Chat/sidecar mid-turn. Liveness is unaffected — a
            # dead hermes closes the socket for real (ConnectionClosed in the
            # pumps), and the browser-facing hop keeps uvicorn's own ping.
            ping_interval=None,
            ping_timeout=None,
            # hermes -> us leg: the narrower hop, 1 MiB by default.
            max_size=HERMES_WS_MAX_BYTES,
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
        # do it explicitly. Preserve a meaningful upstream close for the SPA:
        # in particular, dashboard shutdown is 1012 (or sometimes local 1006),
        # both mapped to 1001 so the browser reconnects instead of treating the
        # Chat session as intentionally finished.
        upstream_close_code = getattr(upstream, "close_code", None)
        try:
            await upstream.close()
        except Exception:
            pass
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close(code=_browser_ws_close_code(upstream_close_code))
            except Exception:
                pass


ANY_METHOD = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

routes = [
    # Public — no auth required.
    Route("/health",                            route_health),
    # Our sign-in lives under /setup/* so the bare /login path stays free.
    # hermes' own gated dashboard redirects unauthenticated requests there, and
    # a route of ours at /login would answer instead — the browser would bounce
    # between the two forever (reproduced on v2026.8.27: 8 redirects, then the
    # browser gives up). The proxy re-logins internally so that redirect should
    # never reach a browser, but the path stays clear regardless.
    Route("/login",                             login_redirect,      methods=["GET"]),
    Route("/logout",                            logout),

    # Our setup wizard + management API, all under /setup/* (cookie-auth guarded).
    # /setup/login must precede the /setup/{path:path} catch-all further down.
    Route("/setup/login",                       page_login,          methods=["GET"]),
    Route("/setup/login",                       login_post,          methods=["POST"]),
    Route("/setup",                             page_index),
    Route("/setup/",                            page_index),
    Route("/setup/api/config",                  api_config_get,      methods=["GET"]),
    Route("/setup/api/config",                  api_config_put,      methods=["PUT"]),
    Route("/setup/api/status",                  api_status),
    Route("/setup/api/preparation/retry",       api_preparation_retry, methods=["POST"]),
    Route("/setup/api/dependencies/apply",      api_dependencies_apply, methods=["POST"]),
    Route("/setup/api/logs",                    api_logs),
    Route("/setup/api/gateway/start",           api_gw_start,        methods=["POST"]),
    Route("/setup/api/gateway/stop",            api_gw_stop,         methods=["POST"]),
    Route("/setup/api/gateway/restart",         api_gw_restart,      methods=["POST"]),
    Route("/setup/api/config/reset",            api_config_reset,    methods=["POST"]),
    Route("/setup/api/pause/resume",            api_estop_resume,    methods=["POST"]),
    Route("/setup/api/pairing/pending",         api_pairing_pending),
    Route("/setup/api/pairing/approve",         api_pairing_approve, methods=["POST"]),
    Route("/setup/api/pairing/deny",            api_pairing_deny,    methods=["POST"]),
    Route("/setup/api/pairing/approved",        api_pairing_approved),
    Route("/setup/api/pairing/revoke",          api_pairing_revoke,  methods=["POST"]),
    Route("/setup/api/oauth/xai/start",         api_oauth_xai_start,  methods=["POST"]),
    Route("/setup/api/oauth/xai/status",        api_oauth_xai_status),
    Route("/setup/api/oauth/xai",               api_oauth_xai_delete, methods=["DELETE"]),
    Route("/setup/api/oauth/codex",             api_oauth_codex_status, methods=["GET"]),
    Route("/setup/api/oauth/codex",             api_oauth_codex_delete, methods=["DELETE"]),
    Route("/setup/api/oauth/codex/start",       api_oauth_codex_start, methods=["POST"]),
    Route("/setup/api/oauth/codex/cancel",      api_oauth_codex_cancel, methods=["POST"]),
    Route("/setup/api/oauth/codex/models",      api_oauth_codex_models),
    Route("/setup/api/backup/jobs",             api_backup_jobs_get, methods=["GET"]),
    Route("/setup/api/backup/jobs",             api_backup_jobs_start, methods=["POST"]),
    Route("/setup/api/backup/jobs/{job_id}",    api_backup_job_delete, methods=["DELETE"]),
    Route("/setup/api/backup/jobs/{job_id}/download", api_backup_job_download),
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
    # Hermes Console modal, new in v2026.7.20 (hermes_cli/web_server.py's
    # @app.websocket("/api/console")). Fail-closed list, so it 403s at our edge
    # until listed here — see the PROXIED_WS_PATHS block above.
    WebSocketRoute("/api/console",              ws_proxy),
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
    # ws_max_size: browser -> us leg of the same pairing (uvicorn defaults 16 MiB).
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", loop="asyncio",
                            ws_max_size=HERMES_WS_MAX_BYTES)
    server = uvicorn.Server(config)

    def _shutdown():
        loop.create_task(gw.stop())
        loop.create_task(dash.stop())
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown)

    loop.run_until_complete(server.serve())
