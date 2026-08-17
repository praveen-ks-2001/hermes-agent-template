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
    except OSError:
        return
    if not alternate.is_dir():
        return
    for src in sorted(alternate.glob("*.json")):
        if not src.is_file():
            continue
        stale = _pjson(src)
        if not stale:
            # Empty, or unreadable (_pjson swallows a parse error as {}). Either
            # way there is nothing to merge — and we do NOT delete it, because a
            # corrupt file may still be recoverable by hand. An empty leftover
            # is inert: hermes' own merge skips it too.
            continue
        dest = active / src.name
        merged = dict(stale)
        merged.update(_pjson(dest))   # live entries win
        try:
            _wjson(dest, merged)
        except OSError as e:
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

    # Pin the browser backend off, because this image opts into the new one by
    # ACCIDENT. v2026.8.13 added `browser.backend`, whose default "" means "use
    # Browser Use mode whenever the browser-use CLI is runnable" — and
    # _find_cli() (tools/browser_use_cli.py) counts a bare `uvx` as runnable.
    # Our base image IS ghcr.io/astral-sh/uv, which ships /usr/local/bin/uvx,
    # so the default silently resolves to Browser Use here (verified in the
    # built image: is_browser_use_cli_mode() -> True).
    #
    # That swaps the whole browser_* surface for a single `browser_exec` tool
    # (check_fn=is_browser_use_cli_mode, and present in the general/coding/
    # research toolsets), which shells out to `uvx browser-use` — a PyPI fetch
    # on first call — and then needs a CDP-reachable Chrome. This image has no
    # Chromium at all, so that tool can only fail, after burning a turn.
    # Nothing is lost by pinning: verified on BOTH the v2026.8.3 and v2026.8.13
    # images that check_browser_requirements() is already False here (no
    # Chromium), so the built-in browser_* tools are not exposed either way.
    # This keeps the model's toolbox identical to v2026.8.3 rather than handing
    # it a tool that cannot work.
    #
    # setdefault, not assignment — "off" is upstream's documented opt-out, and
    # someone who deliberately picks Browser Use in hermes' own settings (or
    # `/browser use on`) should keep it. Revisit if Chromium is ever baked in.
    merged_browser = dict(merged.get("browser") if isinstance(merged.get("browser"), dict) else {})
    merged_browser.setdefault("backend", "off")
    merged["browser"] = merged_browser

    merged_agent = dict(merged.get("agent") if isinstance(merged.get("agent"), dict) else {})
    merged_agent.setdefault("max_iterations", 50)
    merged["agent"] = merged_agent

    # Pin the conversation auto-reset policy so it doesn't depend on volume age.
    # start.sh seeds cli-config.yaml.example only on a FRESH volume, and
    # v2026.7.20 flipped that example (and SessionResetPolicy's own default)
    # from "both" to "none" — so without this an existing deployment keeps
    # resetting while a newly deployed one never does, from identical code.
    # We keep "both" (idle + daily): it bounds context growth and preserves the
    # agent's one turn to persist memories/skills before a wipe.
    # setdefault, not assignment — unlike terminal.backend this is a user
    # preference, so a value chosen in hermes' own settings survives.
    merged_session_reset = dict(merged.get("session_reset") if isinstance(merged.get("session_reset"), dict) else {})
    merged_session_reset.setdefault("mode", "both")
    merged["session_reset"] = merged_session_reset

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
    # Never hand a hermes subprocess HERMES_PARENT_PID. v2026.8.13's new
    # _start_parent_death_watchdog() (hermes_cli/web_server.py) polls that PID
    # and calls os._exit(0) once it is gone — it is the Electron desktop's
    # orphan guard, and it is NOT gated on HERMES_DESKTOP, so it arms itself on
    # any `hermes dashboard` whose environment carries the key. Killing the
    # dashboard is unrecoverable here: unlike Gateway it has no respawn
    # supervisor, so every proxied page 503s until the container is redeployed.
    # This pop covers the plausible vector — an operator pasting it in as a
    # Railway service variable, which lands in our own os.environ.
    #
    # It is deliberately NOT the whole fix: hermes also loads $HERMES_HOME/.env
    # into its own os.environ at startup, so a value sitting in that FILE
    # re-arms the watchdog no matter what env we pass. _sanitize_env_file()
    # handles that half at boot; both are needed.
    env.pop("HERMES_PARENT_PID", None)
    return env


# Keys that must never survive in $HERMES_HOME/.env, because hermes re-reads
# that file into its own os.environ and would act on them regardless of the env
# we pass to the subprocess. Verified locally against v2026.8.13: with
# HERMES_PARENT_PID=999999 in .env the dashboard os._exit(0)s seconds after
# spawn ("[dashboard] exited cleanly (code 0)") and every proxied page 503s
# permanently. Only a restored or hand-edited .env can carry it, so this is a
# boot-time heal rather than a check on every read.
ENV_FILE_FORBIDDEN_KEYS = ("HERMES_PARENT_PID",)


def _sanitize_env_file() -> None:
    """Drop keys from .env that would let hermes kill its own dashboard."""
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
          f"shut the dashboard down", flush=True)


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
            env = build_hermes_env()
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
            # 20s, not 10s: tearing down several messaging adapters (Telegram +
            # Discord + Slack polling loops, each draining in-flight sends) can
            # outrun 10s, and SIGKILL mid-teardown skips hermes' atexit pid
            # cleanup — which is exactly the leftover _clear_stale_pidfile()
            # then has to mop up. Still well inside hermes' own drain+60s
            # shutdown watchdog, and `--replace` on the next start displaces
            # anything that did survive, so waiting longer costs only latency.
            await asyncio.wait_for(self.proc.wait(), timeout=20)
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

    async def start(self):
        if self.proc and self.proc.returncode is None:
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
    return JSONResponse({"gateway": gw.status(), "providers": providers,
                         "channels": channels, "hermes_version": HERMES_VERSION,
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
    platform, code = body.get("platform",""), body.get("code","").strip()
    if not platform or not code:
        return JSONResponse({"error": "platform and code required"}, status_code=400)
    pending_path = pairing_dir() / f"{platform}-pending.json"
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
    approved_path = pairing_dir() / f"{platform}-approved.json"
    approved = _pjson(approved_path)
    approved[user_id] = {"user_name": entry.get("user_name",""), "approved_at": time.time()}
    _wjson(approved_path, approved)
    return JSONResponse({"ok": True})


async def api_pairing_deny(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, code = body.get("platform",""), body.get("code","").strip()
    p = pairing_dir() / f"{platform}-pending.json"
    pending = _pjson(p)
    if code in pending:
        del pending[code]
        _wjson(p, pending)
    return JSONResponse({"ok": True})


async def api_pairing_approved(request: Request):
    if err := guard(request): return err
    out = []
    for p in _platforms("approved"):
        for uid, info in _pjson(pairing_dir() / f"{p}-approved.json").items():
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
    p = pairing_dir() / f"{platform}-approved.json"
    approved = _pjson(p)
    if uid in approved:
        del approved[uid]
        _wjson(p, approved)
    return JSONResponse({"ok": True})


async def api_pairing_reset_lockout(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform = str(body.get("platform", "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]+", platform):
        return JSONResponse({"error": "valid platform required"}, status_code=400)
    rate_limit_path = PAIRING_DIR / "_rate_limits.json"
    limits = _pjson(rate_limit_path)
    keys_removed = [
        key
        for key in (f"_failures:{platform}", f"_lockout:{platform}")
        if key in limits
    ]
    for key in keys_removed:
        del limits[key]
    if keys_removed:
        _wjson(rate_limit_path, limits)
    return JSONResponse({"ok": True, "keys_removed": keys_removed})


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


# Mirrors hermes' own _EXCLUDED_DIRS (hermes_cli/backup.py, v2026.8.13) so
# _live_db_names() can never demand a database hermes deliberately skips. That
# direction matters: a false "incomplete" ABORTS a restore, which is strictly
# worse than the gap it closes. Re-check this against upstream on a bump.
_BACKUP_EXCLUDED_DIRS = {
    "hermes-agent", "__pycache__", ".git", "node_modules", "backups",
    "checkpoints", ".venv", "venv", "site-packages",
    ".cache", ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}


def _live_db_names() -> set[str]:
    """Base names of every SQLite DB on the volume that a backup should contain.

    Was hardcoded to state.db. v2026.8.13 keeps adding databases beside it
    (cron/notepad.db is new; kanban.db, cron/executions.db, projects.db,
    response_store.db and verification_evidence.db already existed), and
    hermes' `_safe_copy_db` fails CLOSED — it drops a database it cannot
    snapshot from the archive while `hermes backup` still exits 0. A check
    naming only state.db therefore certifies an archive as sound while other
    databases are silently absent from it.

    Names, not paths: hermes writes some of these nested (cron/…), and
    _incomplete_backup_reason compares against `Path(n).name` from the zip, so
    both sides stay prefix-agnostic. Two same-named DBs in different
    directories would compare as one — a false NEGATIVE, which is the safe
    direction here (it never blocks a restore).
    """
    root = Path(HERMES_HOME)
    found: set[str] = set()
    try:
        for path in root.rglob("*.db"):
            try:
                if not path.is_file():
                    continue
                parents = path.relative_to(root).parts[:-1]
            except (OSError, ValueError):
                continue
            if any(part in _BACKUP_EXCLUDED_DIRS for part in parents):
                continue
            found.add(path.name)
    except OSError:
        # Can't walk the volume — say nothing rather than block a restore.
        return set()
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
            names = {Path(n).name for n in zf.namelist()}
    except Exception as e:
        return f"the archive could not be read back ({e})"
    missing = sorted(_live_db_names() - names)
    if not missing:
        return None
    if "state.db" in missing:
        # Name the one users recognise first — it is the sessions/chat history.
        others = [m for m in missing if m != "state.db"]
        tail = f" (also {', '.join(others)})" if others else ""
        return f"state.db (sessions and chat history) is missing from the archive{tail}"
    return f"{', '.join(missing)} missing from the archive"


async def api_backup_download(request: Request) -> Response:
    if err := guard(request): return err
    if backup_lock.locked():
        return JSONResponse({"error": "A backup or restore is already in progress"}, status_code=409)
    async with backup_lock:
        tmp_dir = tempfile.mkdtemp(prefix="hermes-backup-")
        zip_path = Path(tmp_dir) / "backup.zip"
        rc, output = await _run_hermes_cli("backup", "-o", str(zip_path))
        if _is_backup_busy(rc, output):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return JSONResponse({"error": BACKUP_BUSY_MESSAGE, "output": output[-2000:]},
                                status_code=409)
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
        headers = {}
        # Surface a partial archive instead of handing over a file the user
        # would only discover is incomplete when a restore fails. Warn rather
        # than block: config, keys and memories are still worth exporting even
        # when the session DB could not be snapshotted.
        if reason := _incomplete_backup_reason(zip_path):
            print(f"[backup] incomplete archive — {reason}", flush=True)
            headers["X-Backup-Warning"] = f"Backup is incomplete: {reason}."
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
            _prune_pre_restore_snapshots()

            await gw.stop()
            await dash.stop()
            try:
                rc, output = await _run_hermes_cli("import", str(upload_path), "--force")
            finally:
                # An imported archive can carry the legacy `pairing/` layout,
                # which leaves two populated pairing dirs. Collapse them before
                # anything reads the store again — hermes' own merge would
                # otherwise keep resurrecting revoked users from the leftover.
                try:
                    _consolidate_pairing_dirs()
                except Exception as e:
                    print(f"[pairing] consolidate after restore failed: {e!r}", flush=True)
                # A restored .env is the only realistic way HERMES_PARENT_PID
                # reaches this volume, and the dashboard is restarted just
                # below — strip it before that spawn, not at the next boot.
                try:
                    _sanitize_env_file()
                except Exception as e:
                    print(f"[server] .env sanitize after restore failed: {e!r}", flush=True)
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

# Dashboard actions that install into the running container. Tools -> post-setup
# (hermes_cli/web_routers/tools.py) npm/pip-installs into /opt/hermes-agent and
# is just as ephemeral as the memory-provider path, but shipped with no warning.
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
# list to extend, so this will grow silently on future bumps.
#
# This is log-only, deliberately: unlike the two POST paths, we do NOT inject a
# confirm() here. The existing notice says the install is wiped on redeploy,
# and for cua_driver that is probably FALSE — its installer targets
# ~/.local/bin, and the Dockerfile sets HOME=/data, so it most likely lands on
# the Railway volume and survives (same reasoning that keeps
# POST /api/mcp/catalog/install deliberately uncovered). Telling the user their
# install is about to vanish when it will not is worse than staying quiet, so
# we take the log line — which is what was actually missing — and skip the
# popup until a path is confirmed to write into the image.
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

# Warn before any dashboard action that installs into the RUNNING container.
# Neither endpoint carries an install-method check, so the `.install_method=docker`
# stamp (invariant 4) does not refuse them, and on Railway the image is immutable:
# the package disappears on the next redeploy while config.yaml still names it.
# Only the PACKAGE is lost — settings live in config.yaml/.env on the volume — so
# re-running the install fully restores it, which is what the notice says. We warn
# rather than block so a quick trial stays possible, and point at a GitHub issue
# rather than the Dockerfile, since most people deploy this without a fork.
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
    'This template is deployed on Railway as an immutable container: the image is '
    'rebuilt from scratch on every deploy, so anything installed into the running '
    'container is wiped.\\n\\n'
    'Installing this will work right now, but only until your next deploy. '
    'After that it stays configured while its package is gone, and the agent '
    'fails to start it.\\n\\n'
    'If that happens, just install it again from here — your settings and API '
    'keys are stored on the Railway volume, not inside the container, so '
    'nothing needs reconfiguring and it resumes where it left off.\\n\\n'
    'To have it included permanently, please raise an issue here:\\n'
    'https://github.com/praveen-ks-2001/hermes-agent-template/issues\\n\\n'
    'It will be reviewed and built into the template, so next time it works out '
    'of the box with no install step.\\n\\n'
    'Install anyway (temporary)?"))'
    '{return Promise.reject(new Error("Cancelled: immutable deployment"));}'
    '}catch(e){}return f.apply(this,arguments);};})();</script>'
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
    if (request.method == "GET"
            and request.query_params.get("force") != "1"
            and not is_config_complete()):
        return RedirectResponse("/setup", status_code=302)
    return await _proxy_to_dashboard(request)


async def route_proxy(request: Request) -> Response:
    """Catch-all: forward any unmatched path to the Hermes dashboard."""
    if err := guard(request): return err
    # Leave a trail when an in-container install runs: the browser already got
    # the confirm() from IMMUTABLE_INSTALL_WARNING_JS, but this is the only
    # record in `railway logs` explaining why a provider works now and breaks
    # after the next redeploy.
    kind = _in_container_install_kind(request.method, request.url.path)
    if kind == "warned":
        print(f"[proxy] in-container install requested: {request.url.path} — "
              f"immutable image, this will not survive a redeploy", flush=True)
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
async def auto_start():
    if is_config_complete():
        asyncio.create_task(gw.start())
    else:
        print("[server] Config incomplete — gateway not started. Configure provider + model in the admin UI.", flush=True)


@asynccontextmanager
async def lifespan(app):
    _sweep_stale_backup_tmpdirs()
    # Strip .env keys that would make hermes shut its own dashboard down before
    # we spawn it — same "heal the volume before anything reads it" slot as the
    # pairing consolidation below.
    try:
        _sanitize_env_file()
    except Exception as e:
        print(f"[server] .env sanitize at boot failed: {e!r}", flush=True)
    # Heal a pairing store split across the legacy and consolidated dirs before
    # anything reads it. Only a restore can create that here, but an earlier
    # restore (or a volume carried over from a pre-fix deploy) may already have.
    try:
        _consolidate_pairing_dirs()
    except Exception as e:
        print(f"[pairing] consolidate at boot failed: {e!r}", flush=True)
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
    Route("/setup/api/pause/resume",            api_estop_resume,    methods=["POST"]),
    Route("/setup/api/pairing/pending",         api_pairing_pending),
    Route("/setup/api/pairing/approve",         api_pairing_approve, methods=["POST"]),
    Route("/setup/api/pairing/deny",            api_pairing_deny,    methods=["POST"]),
    Route("/setup/api/pairing/approved",        api_pairing_approved),
    Route("/setup/api/pairing/revoke",          api_pairing_revoke, methods=["POST"]),
    Route("/setup/api/pairing/reset-lockout",   api_pairing_reset_lockout, methods=["POST"]),
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
