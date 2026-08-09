# Changelog

Release notes for this Railway template. The user-facing copy of this lives in
the admin UI under **What's New** (`templates/index.html`) — the two are kept in
sync; see `CLAUDE.md` → *Release workflow*.

**Branch naming:** `release/<hermes-version>/<n>`. `/1` is where that Hermes
version first landed; `/2`, `/3` … are template-only fixes on top of it. The
Hermes version never changes within a series. `main` always holds the newest
release.

---

## Unreleased — OpenAI Codex OAuth

### Features
- Added **OpenAI Codex (ChatGPT subscription)** to `/setup`, using Hermes'
  native `openai-codex` device-code OAuth flow rather than an OpenAI API key.
- The setup page now shows the verification link, copyable user code,
  expiration and terminal authorization states, then loads the account's Codex
  model choices from Hermes. Users can cancel, disconnect, or reauthorize.

### Reliability and security
- Codex credentials in Hermes' current credential pool and legacy provider
  token layout are recognized after gateway restarts and Railway redeploys.
  Readiness fails closed when the model, provider pin, credentials, or YAML
  configuration is missing or malformed.
- `write_config_yaml()` preserves dashboard-selected models and providers when
  `LLM_MODEL` is absent, avoiding blank OAuth configuration on restart.
- OAuth proxy responses are allowlisted and never expose access/refresh tokens,
  Hermes session tokens, authorization headers, or raw `auth.json` contents.
- Documented the persistent `/data` volume requirement and the security impact
  of storing account credentials in cloud infrastructure and backups.

## release/v2026.8.3/1 — August 8, 2026
**Hermes v2026.8.3 · major (Hermes upgrade, from v2026.7.20)**

### Hermes update
- Hermes Agent **v2026.7.20 → v2026.8.3**, covering two upstream releases
  (v2026.7.30 and v2026.8.3) — adds video generation tools, the Vercel AI
  Gateway and Vertex providers, outbound webhooks, and gateway health
  monitoring.
- **Fewer out-of-memory restarts** — Hermes now returns unused memory to the OS
  as it runs (`agent.memory_trim`, on by default).
- **An interrupted message is retried automatically** — a turn killed mid-answer
  by an OOM or a redeploy is re-run on the next boot. Left enabled; a message
  with real-world side effects will therefore be carried out twice.

### Changes to support upstream updates
- **Restart no longer parks the bot** — upstream added
  `agent.restart_after_turn_timeout` (default 21600s) so `/restart` defers until
  the active turn finishes. A wedged turn leaves the bot alive, healthy and
  refusing every message for up to six hours, invisibly to the supervisor.
  `HERMES_RESTART_AFTER_TURN_TIMEOUT=0` restores the immediate drain; it covers
  the in-band `/restart`, SIGUSR1 and the dashboard's own detached restart.
- **WebSocket frame size matched** — upstream set `ws_max_size` to 384 MB while
  both of our hops sat on lower library defaults (1 MB inbound from hermes,
  16 MB from the browser), so oversized frames dropped the Chat/PTY socket with
  nothing in the logs. Mirrored on both legs.
- **Loop watchdog kept on** — upstream's new watchdog exits 75 after ~2 min of a
  stalled event loop. Deliberately left enabled: the supervisor already treats
  exit 75 as a clean restart. Note it can now end a very long turn.
- **Build pinned** — upstream's new `.npmrc` sets `engine-strict=true`, turning
  the Node/npm engine range into a hard build failure (stay on setup_22.x), and
  a new `setup.py` blocks non-editable installs, making the Dockerfile's `-e`
  load-bearing. Both documented in place.

### Improvements
- **Install warning now covers the Tools tab.** `POST /api/tools/toolsets/<name>/post-setup`
  installs into the container exactly like the memory-provider button but
  shipped with no notice. Both now warn, and both are logged. MCP catalog
  installs are deliberately excluded — those land on the volume and do survive.

---

## release/v2026.7.20/2 — July 30, 2026
**Hermes v2026.7.20 · minor**

### Bug fixes
- **Backup restore on cloud browsers** — "Choose file" did nothing on streamed
  browsers, which never surface the file dialog the old hidden-input picker
  relied on. The input is now a real, focusable control. ([#76](https://github.com/praveen-ks-2001/hermes-agent-template/issues/76))

### Improvements
- **Backup restore** — a .zip can be dragged onto the Restore box, and the
  outcome (success / warning / failure reason) now shows in the box rather than
  only as a brief toast.
- **MiniMax (China)** added to the provider dropdown alongside the global one.
  They are separate MiniMax platforms with separate keys, so both can be
  configured at once. Model hints (`MiniMax-M3`, `MiniMax-M2.7`) added for both.

---

## release/v2026.7.20/1 — July 27, 2026
**Hermes v2026.7.20 · major (Hermes upgrade, from v2026.7.1)**

### Hermes update
- Hermes Agent **v2026.7.1 → v2026.7.20** — adds the Hermes Console, session
  export, and three providers (Fireworks AI, DeepInfra, Upstage Solar).

### Changes to support upstream updates
- **Hermes Console** — new WebSocket route added to the proxy's fail-closed
  allowlist, which otherwise 403s it at our edge.
- **Restart throttling** — Hermes added its own respawn brake that blocks before
  the gateway boots. Disabled via `HERMES_GATEWAY_MAX_STARTS=0` so only this
  template's supervisor throttles; repeated saves no longer take the bot offline.
- **Backups** — `hermes backup` can now drop `state.db` and still exit 0. The
  archive is verified directly: a restore aborts unless its safety snapshot is
  complete, and downloads warn instead of handing over a partial file.
- **Paired users** — Hermes now re-copies the inactive pairing dir on every
  start, resurrecting revoked users. The two dirs are consolidated after a
  restore and at boot, and the store is resolved per request rather than cached.
- **Long replies** — Hermes disabled its loopback WebSocket keepalive; the proxy
  now matches it, so Chat no longer drops mid-reply.
- **MCP sign-in** — `HERMES_DASHBOARD_PUBLIC_URL` is derived from
  `RAILWAY_PUBLIC_DOMAIN`, since Hermes builds its OAuth return address from a
  Host header this proxy must rewrite.
- **Memory providers** — a new dashboard button installs into the running
  container with no immutability check; a warning is injected before it runs.
- **Conversation auto-reset** — upstream flipped the default, which would have
  split behaviour between new and existing volumes. Now pinned explicitly.

### Bug fixes
- **Users tab** read the wrong pairing location after a restore — requests could
  be invisible and approvals ignored until the next restart.
- **Gateway shutdown** waits longer, so multiple chat platforms disconnect
  cleanly instead of being cut off.
- **Save & Start on mobile** — the bottom bar sat below the visible viewport
  with no way to scroll to it (`100vh` vs the visible area).

### Improvements
- Sidebar shows the pinned Hermes version, linking to What's New.

---

## v2026.7.1-update — July 13, 2026
**Hermes v2026.7.1 · major**

> Predates the `release/<version>/<n>` convention, so it keeps its original
> branch name.

- **Backup & Restore** added under **Data** — download a full snapshot (config,
  provider keys, channel tokens, approved users, chat history, memories, skills,
  cron jobs) as a zip and restore it, including into a fresh project. A safety
  snapshot is taken automatically before every restore.
