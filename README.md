# Hermes Agent — Railway Template

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) on [Railway](https://railway.app) with a web-based admin dashboard for configuration, gateway management, and user pairing.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-agent-ai?referralCode=QXdhdr&utm_medium=integration&utm_source=template&utm_campaign=generic)

> Hermes Agent is an autonomous AI agent by [Nous Research](https://nousresearch.com/) that lives on your server, connects to your messaging channels (Telegram, Discord, Slack, etc.), and gets more capable the longer it runs.

<!-- TODO: Add dashboard screenshot -->
<!-- ![Dashboard](docs/dashboard.png) -->

## Features

- **Admin Dashboard** — dark-themed setup wizard at `/setup` to configure providers, channels, tools, and manage the gateway
- **Full Hermes Dashboard** — the native Hermes web UI (Chat, Keys, Skills, Kanban, Analytics, Console) is proxied at `/`, behind the same login
- **One-Page Setup** — provider dropdown, checkbox-based channel/tool toggles — no config files to edit
- **Gateway Management** — start, stop, restart the Hermes gateway from the browser, with automatic restart if it crashes
- **Live Status** — stat cards for gateway state, uptime, model, and pending pairing requests
- **Live Logs** — streaming gateway log viewer
- **User Pairing** — approve or deny users who message your bot, revoke access anytime
- **Password-Protected** — one cookie-based login guards both the setup wizard and the Hermes dashboard
- **Reset Config** — one-click reset to start fresh
- **Backup & Restore** — download a full snapshot (config, credentials, chat history, memories, skills) as a zip, and restore it — including into a fresh project — to clone a deployment. Not encrypted; a safety snapshot is taken automatically before every restore.

## Getting Started

The easiest way to get started:

### 1. Get an LLM Provider Key (free)

1. Register for free at [OpenRouter](https://openrouter.ai/)
2. Create an API key from your [OpenRouter dashboard](https://openrouter.ai/keys)
3. Pick a free model from the [model list sorted by price](https://openrouter.ai/models?order=pricing-low-to-high) (e.g. `google/gemma-3-1b-it:free`, `meta-llama/llama-3.1-8b-instruct:free`)

### 2. Set Up a Telegram Bot (fastest channel)

Hermes Agent interacts entirely through messaging channels — there is no chat UI like ChatGPT. Telegram is the quickest to set up:

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

1. **LLM Provider** — select OpenRouter from the dropdown, paste your API key, enter the model name
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
| `HERMES_REF` | *(pinned in Dockerfile)* | Hermes Agent version to install (any upstream git tag/branch). Set this to override the Dockerfile default without editing code — see [Updating Hermes](#updating-hermes). |

All other configuration (LLM provider, model, channels, tools) is managed through the admin dashboard.

## Supported Providers

Selectable from the setup wizard's dropdown:

OpenRouter, Anthropic (Claude), Google AI Studio, xAI (API key **or** SuperGrok OAuth), DeepSeek, Qwen Cloud (DashScope), GLM / Z.AI, Kimi, MiniMax (global **and** China), NVIDIA NIM, Fireworks AI, NovitaAI, Arcee AI, Step Plan, GMI Cloud, Hugging Face, GitHub Copilot, OpenCode Zen, OpenCode Go, Kilo Code, Ollama Cloud, Actual Computer, AWS Bedrock, Azure Foundry, and any OpenAI-compatible **Custom Endpoint**.

Every other provider Hermes supports can still be configured from the Hermes Dashboard → **Keys** tab — the wizard covers the common ones, not the limit.

## Supported Channels

Telegram, Discord, Slack, WhatsApp, Email, Mattermost, Matrix

## Supported Tool Integrations

Parallel (search), Firecrawl (scraping), Tavily (search), FAL (image gen), Browserbase, GitHub, OpenAI Voice (Whisper/TTS), Honcho (memory)

## Architecture

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

The Hermes dashboard is **never exposed directly** — it binds loopback and is reachable only through the proxy, so one login covers both UIs. The gateway is supervised: if it crashes or is OOM-killed, `server.py` restarts it with backoff, giving up only if it fails repeatedly (Railway would not restart it on its own, because `server.py` is still alive and healthy).

Config lives on the `/data` volume at `/data/.hermes/` (`.env`, `config.yaml`, `auth.json`, sessions, pairing state) and survives redeploys. Gateway output is captured into a ring buffer and streamed to the Logs panel.

## Running Locally

```bash
docker build -t hermes-agent .
docker run --rm -it -p 8080:8080 -e PORT=8080 -e ADMIN_PASSWORD=changeme -v hermes-data:/data hermes-agent
```

Open `http://localhost:8080` and log in with `admin` / `changeme`.

## Updating Hermes

This template pins a specific Hermes Agent release in the `Dockerfile` (`ARG HERMES_REF`, currently `v2026.8.31`). To upgrade:

- **Recommended:** set a `HERMES_REF` service variable in Railway to any upstream [release tag](https://github.com/NousResearch/hermes-agent/releases) (e.g. `v2026.8.31`) or branch (`main`), then redeploy. It's passed in as a Docker build arg and overrides the Dockerfile default — no code change needed. Use `main` only for unreleased fixes (e.g. the `EMAIL_*_TLS_VERIFY` toggle for self-hosted email bridges) and expect every rebuild to pull new upstream commits.
- **Or** bump `ARG HERMES_REF` in the `Dockerfile` and redeploy.

The "Update" button inside the Hermes dashboard is a **no-op on Railway** (it detects a container install and refuses) — the image is immutable, so a runtime self-update wouldn't survive a redeploy. Bump `HERMES_REF` and redeploy instead. When jumping releases, re-check that the Dockerfile's install extras still match upstream's `pyproject.toml`.

## Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com/)
- UI inspired by [OpenClaw](https://github.com/praveen-ks-2001/openclaw-railway) admin template
