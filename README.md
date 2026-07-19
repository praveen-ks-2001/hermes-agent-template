# Hermes Agent — Railway Template

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) on [Railway](https://railway.app) with a web-based admin dashboard for configuration, gateway management, and user pairing.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-agent-ai?referralCode=QXdhdr&utm_medium=integration&utm_source=template&utm_campaign=generic)

> Hermes Agent is an autonomous AI agent by [Nous Research](https://nousresearch.com/) that lives on your server, connects to your messaging channels (Telegram, Discord, Slack, etc.), and gets more capable the longer it runs.

<!-- TODO: Add dashboard screenshot -->
<!-- ![Dashboard](docs/dashboard.png) -->

## Features

- **Admin Dashboard** — dark-themed UI to configure providers, channels, tools, and manage the gateway
- **One-Page Setup** — provider dropdown, checkbox-based channel/tool toggles — no config files to edit
- **Gateway Management** — start, stop, restart the Hermes gateway from the browser
- **Live Status** — stat cards for gateway state, uptime, model, and pending pairing requests
- **Live Logs** — streaming gateway log viewer
- **User Pairing** — approve or deny users who message your bot, revoke access anytime
- **Basic Auth** — password-protected admin panel
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
4. Keep the Railway public `PORT` on `8080`; do not reuse the internal dashboard/API ports
5. Open your app URL — log in with username `admin` and your password

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
| `PORT` | `8080` | Public Starlette web server port. Railway routes traffic here. Do not set it to `9119` or `8642`. |
| `HERMES_DASHBOARD_PORT` | `9119` | Internal Hermes dashboard port, proxied through the public web server. |
| `API_SERVER_ENABLED` | `false` | Enables Hermes' optional OpenAI-compatible API server. |
| `API_SERVER_PORT` | `8642` | Internal Hermes API server port when enabled. Must differ from `PORT` and `HERMES_DASHBOARD_PORT`. |
| `API_SERVER_KEY` | *(unset)* | Optional bearer key for the API server. A non-empty value also enables the API server. |
| `ADMIN_USERNAME` | `admin` | Basic auth username |
| `ADMIN_PASSWORD` | *(auto-generated)* | Basic auth password — if unset, a random password is printed to logs |
| `HERMES_REF` | *(pinned in Dockerfile)* | Hermes Agent version to install (any upstream git tag/branch). Set this to override the Dockerfile default without editing code — see [Updating Hermes](#updating-hermes). |

All other configuration (LLM provider, model, channels, tools) is managed through the admin dashboard.

### Port ownership and preflight

All active listeners share one Railway container and must use distinct ports. The API port is reserved only when the optional API server is enabled (explicitly or by setting `API_SERVER_KEY`):

| Listener | Recommended port | Exposure |
|----------|------------------|----------|
| Template web server (`PORT`) | `8080` | Public, through Railway |
| Native Hermes dashboard (`HERMES_DASHBOARD_PORT`) | `9119` | Loopback only, reverse-proxied by the template |
| Optional Hermes API server (`API_SERVER_PORT`) | `8642` | Internal unless you deliberately expose it |

The template checks both the effective environment and persisted `config.yaml` before starting the gateway. It does **not** silently choose a new port. For an environment-enabled API server, it disables only the API adapter for that gateway start, keeps messaging channels running, and shows an actionable warning at the top of **Setup**. If `config.yaml` explicitly enables the conflicting API listener, the template blocks gateway start rather than mutating persistent configuration or entering a reconnect loop. Fix the conflicting port, then restart or redeploy.

#### Troubleshooting: repeated `Reconnect api_server failed`

If Railway logs repeatedly report `Reconnect api_server failed` or `address already in use`:

1. Open **Railway → Service → Variables**.
2. Set `PORT=8080`.
3. Keep `HERMES_DASHBOARD_PORT=9119` and `API_SERVER_PORT=8642`, or choose other unused, distinct internal ports.
4. Redeploy the service, then confirm the warning has disappeared from **Setup**.

If API settings were previously saved into `/data/.hermes/.env`, update that persisted Hermes configuration as well: it takes precedence over Railway variables for child processes, so a stored `API_SERVER_PORT` can still create a collision.

## Supported Providers

OpenRouter, DeepSeek, DashScope, GLM / Z.AI, Kimi, MiniMax, HuggingFace

## Supported Channels

Telegram, Discord, Slack, WhatsApp, Email, Mattermost, Matrix

## Supported Tool Integrations

Parallel (search), Firecrawl (scraping), Tavily (search), FAL (image gen), Browserbase, GitHub, OpenAI Voice (Whisper/TTS), Honcho (memory)

## Architecture

```
Railway Container
├── Python Admin Server (Starlette + Uvicorn) — 0.0.0.0:$PORT (public)
│   ├── /setup       — Template setup/admin UI (cookie auth)
│   ├── /setup/api/* — Config, status, logs, gateway, pairing
│   ├── /health      — Railway health check (no auth)
│   └── /*           — Reverse proxy to the native dashboard
├── Native Hermes Dashboard — 127.0.0.1:9119 (internal)
└── hermes gateway — managed as async subprocess
    └── Optional API Server — 127.0.0.1:8642 (internal by default)
```

The admin server runs on `$PORT` and manages the Hermes gateway as a child process. Config is stored in `/data/.hermes/.env` and `/data/.hermes/config.yaml`. Gateway stdout/stderr is captured into a ring buffer and streamed to the Logs panel.

## Running Locally

```bash
docker build -t hermes-agent .
docker run --rm -it -p 8080:8080 -e PORT=8080 -e ADMIN_PASSWORD=changeme -v hermes-data:/data hermes-agent
```

Open `http://localhost:8080` and log in with `admin` / `changeme`.

## Updating Hermes

This template pins a specific Hermes Agent release in the `Dockerfile` (`ARG HERMES_REF`, currently `v2026.7.1`). To upgrade:

- **Recommended:** set a `HERMES_REF` service variable in Railway to any upstream [release tag](https://github.com/NousResearch/hermes-agent/releases) (e.g. `v2026.7.1`), then redeploy. It's passed in as a Docker build arg and overrides the Dockerfile default — no code change needed.
- **Or** bump `ARG HERMES_REF` in the `Dockerfile` and redeploy.

The "Update" button inside the Hermes dashboard is a **no-op on Railway** (it detects a container install and refuses) — the image is immutable, so a runtime self-update wouldn't survive a redeploy. Bump `HERMES_REF` and redeploy instead. When jumping releases, re-check that the Dockerfile's install extras still match upstream's `pyproject.toml`.

## Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com/)
- UI inspired by [OpenClaw](https://github.com/praveen-ks-2001/openclaw-railway) admin template
