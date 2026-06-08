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

## Running Multiple Profile Gateways

Hermes profiles let one Railway container host multiple independent agents, each with its own persona, memory, skills, sessions, and bot token. The template always manages the `default` gateway. To keep additional profile gateways alive across Railway restarts, configure them through environment variables or a file on the persistent volume — no repository edits required.

### Option A: environment variable

Set this Railway variable:

```bash
HERMES_MANAGED_PROFILES=documenter,finance,golf-coach
```

On startup, the wrapper starts:

- the default gateway, if default setup is complete
- each listed profile gateway with `hermes -p <profile> gateway run`

### Option B: auto-discover configured profiles

Set either:

```bash
HERMES_MANAGED_PROFILES=all
```

or:

```bash
HERMES_AUTO_START_PROFILES=true
```

The wrapper scans `/data/.hermes/profiles/` and starts every profile that has a messaging channel configured in its profile `.env`.

### Option C: persistent JSON file

Write `/data/.hermes/managed-profiles.json` on the Railway volume:

```json
{
  "profiles": [
    "documenter",
    { "name": "finance", "enabled": true },
    { "name": "trading", "enabled": false }
  ],
  "auto_discover": false
}
```

This is useful when you want runtime configuration to live entirely on the persistent volume rather than in Railway variables.

### Safety rules

Each profile needs its own bot token/account. Telegram, Discord, Slack, WhatsApp, and similar platforms do not support two active gateway processes using the same bot token. The supervisor checks channel credentials and skips an extra profile if it duplicates a token already used by the default gateway or another managed profile.

The startup script also clears stale `gateway.pid` files for both the default profile and named profiles before the server starts, so persistent volumes do not block clean restarts after Railway redeploys.

<!-- TODO: Add Telegram chat screenshot -->
<!-- ![Telegram Example](docs/telegram-example.png) -->

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Web server port (set automatically by Railway) |
| `ADMIN_USERNAME` | `admin` | Basic auth username |
| `ADMIN_PASSWORD` | *(auto-generated)* | Basic auth password — if unset, a random password is printed to logs |
| `HERMES_MANAGED_PROFILES` | *(empty)* | Optional comma-separated profile gateways to supervise in addition to `default`, e.g. `documenter,finance`. Also accepts `all` for discovery or JSON. |
| `HERMES_AUTO_START_PROFILES` | `false` | If `true`, auto-discover every profile under `/data/.hermes/profiles/` that has a messaging channel configured. |
| `HERMES_MANAGED_PROFILES_FILE` | `/data/.hermes/managed-profiles.json` | Optional JSON config file on the persistent volume for managed profile gateways. |

The default profile's LLM provider, model, channels, and tools are managed through the admin dashboard. Additional Hermes profiles keep their own `.env`, `config.yaml`, memory, skills, and gateway state under `/data/.hermes/profiles/<name>/`.

## Supported Providers

OpenRouter, DeepSeek, DashScope, GLM / Z.AI, Kimi, MiniMax, HuggingFace

## Supported Channels

Telegram, Discord, Slack, WhatsApp, Email, Mattermost, Matrix

## Supported Tool Integrations

Parallel (search), Firecrawl (scraping), Tavily (search), FAL (image gen), Browserbase, GitHub, OpenAI Voice (Whisper/TTS), Honcho (memory)

## Architecture

```
Railway Container
├── Python Admin Server (Starlette + Uvicorn)
│   ├── /            — Admin dashboard (cookie auth)
│   ├── /health      — Health check (no auth)
│   └── /api/*       — Config, status, logs, gateway, pairing
├── hermes gateway   — Default profile gateway, managed as async subprocess
├── hermes -p <name> gateway run
│                    — Optional managed profile gateways
└── hermes dashboard — Native Hermes web dashboard, reverse-proxied by the server
```

The admin server runs on `$PORT` and manages the Hermes gateway fleet as child processes. Default config is stored in `/data/.hermes/.env` and `/data/.hermes/config.yaml`; profile config lives under `/data/.hermes/profiles/<name>/`. Gateway stdout/stderr is captured into a ring buffer and streamed to the Logs panel.

## Running Locally

```bash
docker build -t hermes-agent .
docker run --rm -it -p 8080:8080 -e PORT=8080 -e ADMIN_PASSWORD=changeme -v hermes-data:/data hermes-agent
```

Open `http://localhost:8080` and log in with `admin` / `changeme`.

## Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com/)
- UI inspired by [OpenClaw](https://github.com/praveen-ks-2001/openclaw-railway) admin template
