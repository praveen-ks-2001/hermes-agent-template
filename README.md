# Hermes Agent — Railway Template

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) on [Railway](https://railway.app) with a web-based admin dashboard for configuration, gateway management, and user pairing.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-agent-ai?referralCode=QXdhdr&utm_medium=integration&utm_source=template&utm_campaign=generic)

> Hermes Agent is an autonomous AI agent by [Nous Research](https://nousresearch.com/) that lives on your server, connects to your messaging channels (Telegram, Discord, Slack, etc.), and gets more capable the longer it runs.

## Community deployments

For Dockhold, see the [community template](https://github.com/dockhold/hermes-agent-starter), maintained separately by the Dockhold team.

## Features

- **Admin Dashboard** — dark-themed setup wizard at `/setup` to configure providers, channels, tools, and manage the gateway
- **Full Hermes Dashboard** — the native Hermes web UI (Chat, Keys, Skills, Kanban, Analytics, Console) is proxied at `/`, behind the same login
- **One-Page Setup** — provider dropdown, checkbox-based channel/tool toggles — no config files to edit
- **Gateway Management** — start, stop, restart the Hermes gateway from the browser, with automatic restart if it crashes
- **Live Status** — gateway state, uptime, model, pending pairing requests, and detected chat-history storage problems
- **Live Logs** — streaming gateway log viewer
- **User Pairing** — approve or deny users who message your bot, remove pairing approvals, and see when a separate access rule still grants a user access
- **Password-Protected** — one cookie-based login guards both the setup wizard and the Hermes dashboard
- **Reset Config** — one-click reset to start fresh
- **Backup & Restore** — create a background backup, see its progress after reload, download the prepared zip of Hermes-home data (config, credentials, chat history, skills, and supported provider files), or remove an individual backup after confirmation. The download is available for 6 hours while the container stays up; download it before redeploying. It is not encrypted; a safety snapshot is taken automatically before every restore. External memory services and Hindsight's embedded PostgreSQL data are outside this zip.

## Getting Started

The easiest way to get started:

### 1. Choose an LLM provider

For ChatGPT, deploy first, then select **ChatGPT (OAuth)** in Setup and authorize your account; no OpenAI API key is needed. For an API-key provider, OpenRouter offers free models:

1. Register for free at [OpenRouter](https://openrouter.ai/)
2. Create an API key from your [OpenRouter dashboard](https://openrouter.ai/keys)
3. Pick a free model from the [model list sorted by price](https://openrouter.ai/models?order=pricing-low-to-high) (e.g. `google/gemma-3-1b-it:free`, `meta-llama/llama-3.1-8b-instruct:free`)

### 2. Set Up a Telegram Bot (fastest channel)

Hermes also has a browser Chat tab. To connect a messaging channel, Telegram is the quickest to set up:

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, follow the prompts, and copy the **Bot Token**
3. Send a message to your new bot — it will appear as a pairing request in the admin dashboard
4. To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot)

### 3. Deploy to Railway

1. Click the **Deploy on Railway** button above
2. Set the `ADMIN_PASSWORD` environment variable (or a random one will be generated and printed to deploy logs)
3. Attach a **volume** mounted at `/data` (persists config across redeploys)
4. Open your app URL — log in with username `admin` and your password

### 4. Configure in the Admin Dashboard

1. **LLM Provider** — select ChatGPT (OAuth) and connect your account, or choose an API-key provider such as OpenRouter; then choose or enter its model
2. **Messaging Channel** — check Telegram, paste the Bot Token from BotFather
3. Click **Save & Start** — the gateway will start and your bot goes live

### 5. Start Chatting

Message your Telegram bot. If you're a new user, a pairing request will appear in the admin dashboard under **Users** — click **Approve**, and you're in.

<!-- TODO: Add Telegram chat screenshot -->
<!-- ![Telegram Example](docs/telegram-example.png) -->

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Web server port (set automatically by Railway) |
| `ADMIN_USERNAME` | `admin` | Login username |
| `ADMIN_PASSWORD` | *(auto-generated)* | Login password — if unset, a random password is printed to the deploy logs. Changing it redeploys the service, which signs everyone out. |
| `HERMES_REF` | *(pinned in Dockerfile)* | Development override for the Hermes Agent git ref. For production upgrades, use the matching template release branch so version-specific compatibility changes ship with it — see [Updating Hermes](#updating-hermes). |

All other configuration (LLM provider, model, channels, tools) is managed through the admin dashboard.

## Supported Providers

Selectable from the setup wizard's dropdown:

ChatGPT (OAuth), OpenRouter, Anthropic (Claude), Google AI Studio, xAI (API key **or** SuperGrok OAuth), DeepSeek, Qwen Cloud (DashScope), GLM / Z.AI, Kimi, MiniMax (global **and** China), NVIDIA NIM, Fireworks AI, NovitaAI, Atlas Cloud, Cheaper Inference, Arcee AI, Step Plan, GMI Cloud, Hugging Face, GitHub Copilot, OpenCode Zen, OpenCode Go, Kilo Code, Ollama Cloud, Actual Computer, AWS Bedrock, Azure Foundry, **9Router**, **OmniRoute**, and any OpenAI-compatible **Custom Endpoint**.

9Router and OmniRoute are separately deployed routing gateways, not services bundled into this image. Select either one in the setup wizard and enter the gateway's reachable OpenAI-compatible `/v1` URL, its endpoint API key, and a model or combo name. Both can remain configured at the same time; OmniRoute defaults the model field to its `auto` smart-routing alias.

Every other provider Hermes supports can still be configured from the Hermes Dashboard → **Keys** tab — the wizard covers the common ones, not the limit.

> **OpenCode Free:** Hermes v2026.9.21 removed the anonymous `opencode-free` provider because its upstream relay now rejects external anonymous clients. Use OpenCode Zen or OpenCode Go with the corresponding API key instead.

## ChatGPT OAuth

Select **ChatGPT (OAuth)** in `/setup`, click **Connect ChatGPT**, open the verification link and enter the one-time code. Return to Setup, choose a model from Hermes' list and click **Confirm model**. The ready message confirms your selection is saved; **Save & Start** then applies your other settings and starts Hermes. If device login is disabled, enable it in your ChatGPT security settings or ask your workspace administrator. Model access and usage limits depend on your account.

Hermes owns token storage and refresh under `/data/.hermes/auth.json`. Keep the persistent `/data` volume attached across redeploys. Tokens are included in unencrypted backups, so store backups securely. Do not copy the same authorization into concurrently running deployments: refresh tokens rotate. This connection does not import conversations from ChatGPT.

Setup manages the default profile. For named profiles and advanced credential pools, use **Hermes Dashboard → Keys**. **Disconnect** removes all saved Codex authorizations in the default profile, including its pool; named profiles borrowing that login must reconnect. Hermes briefly stops to clear in-memory credentials. If ChatGPT is the default provider, the gateway stays stopped until you reconnect or choose another provider. Chats, files, other API keys and independent named-profile credentials are kept.

Models selected in the native dashboard are preserved across gateway restarts and unrelated setup saves. If Setup reports that settings changed, reload the page before saving to avoid replacing newer configuration.

## Supported Channels

Telegram, Discord, Slack, WhatsApp, Email, Mattermost, Matrix

## Supported Tool Integrations

Parallel (search), Firecrawl (scraping), Keenable (search), Tavily (search), Perplexity (search), FAL (image gen), Browserbase, GitHub, OpenAI Voice (Whisper/TTS), Honcho (memory)

## Architecture

Build and startup utilities live in `template_helpers/`; `server.py` and `start.sh` are the application and entrypoint.

One container runs a single public process that fronts two managed Hermes subprocesses:

```
Railway Container
└── server.py — Starlette + Uvicorn on 0.0.0.0:$PORT   (the only public surface)
    ├── /login, /logout    — cookie login (7-day, httponly)
    ├── /health            — health check (no auth)
    ├── /setup             — this template's setup wizard
    ├── /setup/api/*       — config, status, logs, gateway, pairing, backup, OAuth
    ├── /  and  /*         — reverse-proxied to the native Hermes dashboard
    │
    ├── hermes dashboard   — native Hermes web UI, bound to 127.0.0.1:9119
    └── hermes gateway     — the agent itself (Telegram, Discord, …), auto-restarted
```

The Hermes dashboard is **never exposed directly** — it binds loopback and is reachable only through the proxy, so one login covers both UIs. The gateway is supervised: if it crashes or is OOM-killed, `server.py` restarts it with backoff, giving up only if it fails repeatedly (Railway would not restart it on its own, because `server.py` is still alive and healthy). If a config save or restore respawns the dashboard while Chat is open, the terminal connection retries automatically.

Hermes uses its managed Python runtime with SQLite 3.53.1, which includes the WAL-reset fix. The image also builds shared SQLite 3.53.4 for Debian's dynamically linked consumers. Build checks exercise session databases, WAL and FTS with the actual application interpreter.

Hermes can serve all live named profiles from this one gateway. The template's `/setup` page remains the default-profile bootstrap/admin surface; use the native Hermes Dashboard's profile selector for named-profile configuration and pairing. Start, Stop, and Restart act on the shared gateway and therefore affect every profile it serves.

Config lives on the `/data` volume at `/data/.hermes/` (`.env`, `config.yaml`, `auth.json`, sessions, pairing state) and survives redeploys. Gateway output is captured into a ring buffer and streamed to the Logs panel.

## Running Locally

```bash
docker build -t hermes-agent .
docker run --rm -it -p 8080:8080 -e PORT=8080 -e ADMIN_PASSWORD=changeme -v hermes-data:/data hermes-agent
```

Open `http://localhost:8080` and log in with `admin` / `changeme`.

## Updating Hermes

This template pins a specific Hermes Agent release in the `Dockerfile` (`ARG HERMES_REF`, currently `v0.22.2`). To upgrade:

- **Recommended:** deploy the template's `release/<version>/<n>` branch for the Hermes version you want, choosing the highest available `<n>`. Each release includes changes needed for that upstream version; see the matching entry in What's New or `CHANGELOG.md` before upgrading an existing volume.
- **To prepare a new template release:** audit the new upstream tag and update the Dockerfile pin together with any required compatibility code before publishing its release branch.
- **For experimentation:** set a `HERMES_REF` service variable to override the Dockerfile's build arg. A version-only override skips the template compatibility changes and may break an existing volume. Older calendar-tag releases need their matching template branch; this release’s build uses the new PM runtime.

Before upgrading an existing volume, download a complete backup while its selected memory provider is still working. Also keep a consistent full `/data` volume backup with the gateway stopped, plus separate backups of external memory services. The Hermes ZIP covers Hermes-home data and supported provider files; a missing provider can prevent its external files from being discovered. It does not include external services or embedded PostgreSQL under `/data/.pg0`.

On startup, Setup and health checks remain reachable while Hermes settles interrupted dependency work, refreshes the selected packages for this image, and safely migrates root and named-profile settings. Setup shows progress, warnings and any preparation failure. Chat stays stopped on a preparation failure; check deployment logs and use Retry preparation after resolving the cause. Python 3.12’s old `lazy-packages` files remain on the volume for rollback but are not loaded into the new Python 3.14 runtime. Installed plugins also have their accepted dependency manifests checked against the new runtime; existing plugin source is not replaced. If acquisition is disabled or unavailable, Setup shows the pending dependencies and an explicit native Console command.

If a hand-edited or restored `.env` pins a different `HERMES_PYTHON`, `HERMES_RUNTIME_DIR`, `PYTHONPATH`, `PYTHONHOME` or `VIRTUAL_ENV`, preparation stops with the conflicting key names. The file and its credentials are preserved. Remove or update those runtime overrides through an operator volume edit to match the image, then retry preparation; normal provider and channel settings are unaffected.

After an optional integration asks for a restart, use **Status → Apply dependencies** to reload the dashboard and a running gateway without rewriting config. A deliberately stopped gateway stays stopped; an existing pause remains engaged. Restore also prepares and migrates imported settings before chat starts, even when the backup has an older schema than the currently deployed image.

For Email with authenticated senders, open **Hermes Dashboard → Channels → Email → Receiving MTA authserv-id** and enter the exact identifier from your receiving server’s topmost `Authentication-Results` header. Setup flags profiles missing this pin. An account can connect while rejecting incoming messages without it; send a new test email after saving. Do not infer the identifier from the IMAP hostname.

Changing the release branch later changes the image, not the `/data` volume. Keep a pre-upgrade full-volume backup and the matching old image for rollback: an older image can refuse a newer config schema, and changing the image does not reverse database migrations.

If you use named profiles, check each profile's `state.db` for outgoing final replies still marked `pending`, `attempting`, or `failed` before upgrading from v2026.9.21. v2026.9.21 could write these replies to a named profile's ledger even though boot recovery already checked the root ledger; v2026.9.24 fixes new writes but does not move old rows. The new image logs an advisory and continues when it finds such rows. A sender can make a new request, but that starts a new agent turn and may repeat tool calls or actions; a reply marked `attempting` may already have been delivered. Resolve important outstanding replies before redeploying, and do not copy rows between databases without a tested recovery plan.

Run `python3 template_helpers/check_pending_deliveries.py --hermes-home /path/to/stopped-volume-copy` on a consistent copy of the old volume. It prints counts without message contents and exits `0` when clear, `2` for advisory outstanding replies, or `3` when a profile could not be checked. Startup continues on `2` and stops on `3`; an unreadable ledger has an unknown impact. Do not treat a live file copy without its SQLite WAL files as a reliable clear result.

Hermes v2026.9.24 distributes Hindsight as a catalog plugin instead of bundling it. Removing the old package from this image does not delete your memory bank or saved Hindsight settings. If any profile selects `memory.provider: hindsight`, Setup shows a notice to check that profile's Memory status and test real recall and retention after the first turn and after a redeploy. Startup checks selected catalog integrations and shows recovery commands. Installation needs lazy installs and network access; if either is unavailable, install Hindsight from the Hermes plugin catalog for each affected profile. Older Hindsight source can still use dependency helpers removed in v0.22.2; review its native plugin update before setup or repair, preserving any custom changes. Source presence alone does not verify recall or external backup coverage. This image runs as root by default, and Hindsight's `local_embedded` mode refuses root; use a supported external or cloud mode unless you have customized the runtime user.

The upstream Bot Screen/Desktop viewer is not included in this Railway image. The browser dashboard and its Chat workspace picker remain available; screen viewing would require additional desktop packages and proxy support.

The "Update" button inside the Hermes dashboard is a **no-op on Railway** (it detects a container install and refuses) — the image is immutable, so a runtime self-update wouldn't survive a redeploy. Use a matching template release branch and redeploy instead. When preparing a new Hermes release for this template, re-check install extras and every integration with upstream.

## Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com/)
- UI inspired by [OpenClaw](https://github.com/praveen-ks-2001/openclaw-railway) admin template
