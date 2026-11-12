# Changelog

Release notes for this Railway template. The user-facing copy of this lives in
the admin UI under **What's New** (`templates/index.html`) — the two are kept in
sync; see `CLAUDE.md` → *Release workflow*.

**Branch naming:** `release/<hermes-version>/<n>`. `/1` is where that Hermes
version first landed; `/2`, `/3` … are template-only fixes on top of it. The
Hermes version never changes within a series. `main` always holds the newest
release.

---

## release/v0.22.2/3 — November 12, 2026
**Hermes v0.22.2 · minor**

### Improvements
- **UberLLM** is now in the Setup provider dropdown (OpenAI-compatible gateway, `UBERLLM_API_KEY`).

---

## release/v0.22.2/2 — October 19, 2026
**Hermes v0.22.2 · minor**

### Improvements
- Connect ChatGPT from Setup with a one-time code, confirm a Hermes-listed model before starting, and reconnect or disconnect your account.

### Bug fixes
- Dashboard-selected models and endpoint settings survive gateway restarts and unrelated setup saves; outdated setup forms are rejected before they can overwrite newer settings.
- Changing or removing a SuperGrok connection preserves other saved OAuth accounts during token refresh.

---

## release/v0.22.2/1 — October 17, 2026
**Hermes v0.22.2 · major (Hermes upgrade, from v2026.9.24)**

### Hermes update
- Hermes Agent v2026.9.24 → v0.22.2, with the release’s managed Python and toolchain built into the image.
- Email accounts using authenticated senders need Receiving MTA authserv-id in the native Channels → Email settings. Setup highlights missing pins; test with a new email after saving.
- Several integrations now come from Hermes’ plugin catalog. Startup checks selected integrations and shows missing-plugin recovery commands; existing customized plugins are preserved. Older Hindsight plugins may need a reviewed update before setup or repair.

### Changes to support upstream updates
- Setup stays available while dependencies and root/named-profile settings are prepared. Progress, warnings, failures and retry controls are visible; chat starts after preparation completes.
- Setup saves, provider changes and removal use Hermes’ new config rules while preserving unrelated settings and refusing damaged YAML.
- Apply dependencies restarts the dashboard and a running gateway without changing configuration. A stopped gateway stays stopped and paused bots stay paused.
- Restore prepares and migrates the imported root and named profiles before restarting Hermes. Preparation failures leave chat stopped and the safety snapshot available; an interrupted import stays blocked across redeploys until recovered.

---

## release/v2026.9.24/2 — October 15, 2026
**Hermes v2026.9.24 · minor**

### Improvements
- **Atlas Cloud** is now in the Setup provider dropdown (OpenAI-compatible gateway, `ATLASCLOUD_API_KEY`).

---

## release/v2026.9.24/1 — October 6, 2026
**Hermes v2026.9.24 · major (Hermes upgrade, from v2026.9.21)**

### Hermes update
- Hermes Agent **v2026.9.21 → v2026.9.24** (package 0.21.4 → 0.21.5); the native browser Chat now lets you choose a workspace for a new session.
- **Hindsight is now a catalog plugin rather than a bundled memory provider.** This update does not delete saved settings or the memory bank. Setup shows affected deployments a notice to check each profile, install a missing plugin from the catalog, and test recall and retention. Embedded mode remains unavailable in the default root-running image.
- **Hindsight users need a separate data backup for rollback or cloning.** The Hermes ZIP does not include external memory or embedded PostgreSQL under `/data/.pg0`; this limit predates the upgrade and is now called out in the Backup page.
- **Bot Screen remains unavailable in this Railway image.** Browser Chat and its new workspace picker are available; the Desktop screen viewer needs a separate runtime and proxy integration.
- **Old named-profile replies may need attention.** Hermes now records new outgoing replies in the root ledger; older unfinished replies remain in the named profile's ledger. Startup reports them and continues, so senders may need to make a new request; doing so can repeat tool actions. An unreadable ledger still stops the upgrade.
- **Hand-written allowlists need full sender IDs.** A short entry such as `alice` no longer matches `alice@example.com`; users approved through the panel already have full IDs.

### Changes to support upstream updates
- **Existing root and named-profile configs run through Hermes' backed-up migration before startup.** Older profile instructions and the template's model setting are preserved if a migration fails; check warnings and confirm an old MCP server marked disabled now stays off under Hermes' current setting.
- **Restore refuses an incomplete safety snapshot.** Databases in different profiles are checked by their full paths, so a same-named root database cannot hide a missing profile database.

### Bug fixes
- **Large manual backups now run as visible background jobs.** Create one from Backup & Restore, watch its phase and file count, and download the prepared ZIP in a separate request. The job and its outcome remain visible after a page reload. Prepared downloads remain available for 6 hours while the container stays up; a restart clearly marks an interrupted or unavailable job so it can be retried.
- **Backup entries can be removed individually.** Remove clears an expired or failed entry; removing a ready backup also deletes its prepared ZIP after confirmation. A running backup or active download is protected.
- **Unreadable config is preserved.** Save, gateway start, and xAI OAuth now report a config error instead of replacing the saved file with an empty one.
- **Pairing approvals survive legacy folder aliases and interrupted writes.** Revoke warns when another access rule may still grant access, instead of claiming the user was blocked.
- **Status now flags detected chat-history storage problems; Dashboard Stop stays stopped, and planned Restarts no longer count as crashes.**
- **Runtime setup warnings now explain what survives redeploy.** Hermes packages installed on `/data` persist; provider-specific installers may still need to be run again.

---

## release/v2026.9.21/1 — September 23, 2026
**Hermes v2026.9.21 · major (Hermes upgrade, from v2026.9.11)**

### Hermes update
- Hermes Agent **v2026.9.11 → v2026.9.21** (package 0.21.2 → 0.21.4).
- **One gateway now serves every live profile on the host when the migration
  preflight says that is safe.** The former `gateway.multiplex_profiles: false`
  opt-out is retired. This template's `/setup` panel remains the default-profile
  bootstrap UI; use the native Hermes dashboard's profile selector for named
  profile configuration and pairing. Start/Stop/Restart controls the shared
  gateway and therefore affects every served profile.
- **Temporary provider outages recover for longer instead of ending the turn
  immediately.** After ordinary retries and fallbacks are exhausted, Hermes now
  performs up to five bounded recovery cycles with a visible countdown.
- **Very large conversations compact sooner.** The default absolute compression
  ceiling is now 256K tokens, preventing million-token contexts from growing to
  roughly 500K before their first compaction.
- **New capabilities:** HEIF/HEIC/AVIF image decoding, GPT-Live voice-session
  negotiation, OpenAI-native web search through Codex OAuth, OpenRouter video
  generation, lazy MCP startup, and n8n's official HTTP/OAuth MCP integration.
- **The anonymous `opencode-free` provider was removed upstream** because its
  relay now rejects external anonymous clients. Existing users must select
  OpenCode Zen or OpenCode Go and provide that service's API key; the template
  does not auto-migrate to a credentialed, potentially paid provider.
- **Fresh installs show model reasoning by default.** Existing volumes keep
  their saved `display.show_reasoning` value, normally `false`; no user setting
  is overwritten.

### Changes to support upstream updates
- **Backup completeness now mirrors all of Hermes' exclusions.** v2026.9.21
  excludes root/profile `browser_profiles/` plus regenerable `cache/*` trees,
  while retaining user media and citation evidence. Without the same rules,
  this template could reject a valid pre-restore safety snapshot because a
  disposable database was deliberately absent.
- **Partial manual backups remain downloadable.** Hermes now keeps a partial
  ZIP but exits 1. The download endpoint accepts that exact result only when
  the archive is readable, returns it with a warning, and keeps restore safety
  snapshots fail-closed.
- **Generic temporary files stay on ephemeral container disk.** Hermes now
  defaults `TMPDIR`/`TMP`/`TEMP` to `$HERMES_HOME/cache/scratch`; the template
  pins `TMPDIR=/tmp`, matching its existing terminal-scratch policy and keeping
  disposable spools off the Railway volume.
- **SQLite is pinned to the fixed 3.53.4 runtime used by upstream's official
  image.** Bookworm's 3.40.1 contains the WAL-reset corruption bug and makes
  Hermes' new FTS write-health probe fail even on a fresh database. The image
  build now verifies the version and FTS5 trigram support before publishing.
- **Dashboard restarts get a 15-second graceful shutdown window.** Hermes now
  joins its SQLite reconciliation worker and closes hosted-room/Chat PTY state
  during teardown; the former five-second wrapper deadline could SIGKILL it
  mid-cleanup.
- **Open Chat pages recover across a dashboard restart.** The loopback
  dashboard token now stays stable for the wrapper process lifetime, and the
  proxy translates Hermes' service-restart close into the reconnect signal its
  SPA understands. Public/gated mode still mints fresh single-use WS tickets.

### Verified unchanged (audited, no action)
Exact gateway/dashboard/backup/import argv and dispatch · WebSocket routes used
by the browser dashboard · loopback Host/Origin gates · dashboard password
login, cookies and WS tickets · `/api/model/set` payload · pairing directory
resolution and pending/approved schema · gateway PID/lock/socket records · exit
codes 75/78 and container markers · Docker install-method refusal · all nine
named install extras · `web_dist` and `HERMES_TUI_DIR` build outputs · xAI OAuth
device flow and auth-store layout.

---

## release/v2026.9.11/1 — September 12, 2026
**Hermes v2026.9.11 · major (Hermes upgrade, from v2026.8.31)**

Upstream's "September decomposition" (PR #102117) split nearly every large module
into focused files, so ~100% of the source churn is code movement. Zero files in
`hermes_cli/`, `gateway/`, `tools/` or `agent/` are byte-identical; the findings
below were verified by executing the new tree, not by reading the diff.

### Hermes update
- Hermes Agent **v2026.8.31 → v2026.9.11** (package 0.21.0 → 0.21.2).
- **`session_reset` was RETIRED.** `SessionResetPolicy` is now documented as an
  "inert legacy value type … Gateway configuration and session lifecycle do not
  consume this datatype" (`gateway/config.py`), `default_reset_policy` went from
  10 references in `gateway/config.py` to 0, and the key left hermes'
  known-root-keys list. Conversations persist until an explicit `/new`/`/reset`.
- **`sessions.auto_prune` False → True** (`config_defaults.py`), with
  `retention_days: 90`, `min_interval_hours: 24`, `vacuum_after_prune: true`,
  `min_vacuum_interval_days: 30`. Inherited by every existing volume through the
  config deep-merge, with no migration and no notice. Only ENDED sessions are
  deleted — open, pinned and mid-turn rows are never touched. **Reachability on
  this template is narrower than the config comment implies:** the sole caller is
  `cli.py:_run_state_db_auto_maintenance`, invoked from
  `HermesCLI._init_session_store` (`cli.py:2862`), and `HermesCLI` is constructed
  in exactly two places — `cli.py:4262` (the CLI entry point) and
  `tui_gateway/slash_worker.py:111` (one worker per TUI session). Neither
  `gateway/`, `cron/` nor `run_agent` builds it, so a deployment whose users only
  talk to the bot over Telegram never triggers the sweep; opening the Chat tab
  does. All knobs are config.yaml-only — there is no env-var reader for any of
  them, so the template would have to pin it in `write_config_yaml()` (the way it
  used to pin `session_reset`) to change the default. Left as upstream ships it:
  there is no fresh-vs-existing divergence here (both inherit the same default),
  and upstream reports multi-GB `state.db` growth within weeks without it.
- **`tool_loop_guardrails.non_interactive_hard_stop_enabled: True`** (new).
  `agent/tool_guardrails.py:_is_non_interactive_platform()` treats every platform
  outside `{cli, tui, desktop, acp, subagent, api_server}` as unattended and
  force-sets `hard_stop_enabled`, so a gateway turn now HALTS on a looping tool
  (exact_failure 5, same_tool_failure 8, idempotent_no_progress 5).
- **`plugins/web/tavily` was RESTORED** after v2026.8.31 deleted it;
  `tools/tool_backend_helpers.py` now carries an empty `REMOVED_BACKENDS`
  registry whose comment names the revert. `plugins/web/perplexity` and
  `plugins/image_gen/meta-ai` are new. No plugin was removed this bump.
- **Autodetect ladder reordered** (`tools/web_tools.py:_get_backend`): `tavily`
  and `perplexity` now head the list, ahead of `exa`/`parallel`/`keenable`. Only
  reached on a never-configured install; a stored selection is still returned
  strictly.
- **`hermes_startup_watchdog.py` (new).** Armed at import for the adjacent argv
  pair `gateway run` — our exact spawn — it dumps all-thread stacks and
  `os._exit(75)`s a gateway that has not reached a live event loop in 300s. 75 is
  the planned-restart code this supervisor already respawns on, so a pre-loop
  wedge now self-heals. Not armed for `dashboard`/`backup`/`import`.
- **`is_global_startup_conflict()` (new, `gateway/restart.py`).** A
  `<scope>_lock` / `lock_conflict` fatal error is emitted `retryable=True` for
  mid-run reconnects but is now forced NON-retryable at startup
  (`run_startup.py:1099`). With `connected_count == 0` — a single-platform
  deployment, this template's typical shape — that exits **78**, which
  `Gateway._drain()` deliberately never respawns. Previously it retry-queued and
  self-healed.
- **`$HERMES_HOME/shared-state.db` (new).** `gateway/hosted_rooms.py`
  `default_db_path()` moved off `state.db`; the worker is awaited inline at every
  gateway start, so the file exists on every deployment. Confirmed live: absent
  on v2026.8.31, present on v2026.9.11. Existing Group Chat state is NOT migrated.
- **`hermes backup` gained `-k/--keep` (default 3)** which DELETES older
  `hermes-backup-*.zip` in the output directory. Both template call sites use
  names that do not match that prefix, so the sweep is inert here.
- **Plugin-import compat layer expires 2026-09-14.** `hermes_cli/plugin_compat.py`
  compares `date.today()`, so a pinned image changes behaviour by calendar date.
  `plugins_loader.py` then refuses to load an EXTERNAL plugin whose source
  imports a moved internal path; bundled plugins are exempt
  (`_scan_root` returns None for `source == "bundled"`). Escape hatch:
  `plugins.allow_deprecated_imports: true`.

### Changes to support upstream updates
- `Dockerfile`: `ARG HERMES_REF` → `v2026.9.11`. Also re-dated the
  `--exclude-newer` escape-hatch floor: `nemo-relay` moved `>=0.7.1` (2026-08-07)
  → `>=0.8.3,<0.9` (0.8.3 published 2026-09-02), so the documented recovery
  procedure would have failed as written.
- `server.py` `ENV_VARS` + `templates/index.html`: **`TAVILY_API_KEY` restored**
  (optional — Tavily works keyless once selected) and **`PERPLEXITY_API_KEY`
  added** (required; no keyless tier).
- `server.py` `write_config_yaml()`: dropped the `session_reset.mode = "both"`
  pin. Unknown top-level keys are deliberately un-warned upstream, so it would
  have failed silently as a dead key on disk.
- `server.py` `_BACKUP_EXCLUDED_ROOT_DIRS` + `_in_excluded_root_dir()` (new):
  mirrors upstream's **second** exclusion mechanism (`_EXCLUDED_ROOT_DIRS` +
  `_in_excluded_root_dir`, `hermes_cli/backup.py`), which skips `models`,
  `runtimes` and `node` only at the root of `HERMES_HOME` and at
  `profiles/<name>/`. Root-scoped, not flat — `skills/foo/models/` must still be
  backed up. Without the mirror a `*.db` under any of the three is demanded by
  `_live_db_names()` while `hermes backup` omits it, and the false "incomplete"
  ABORTS a restore. Unit-tested against upstream's own function across 14 paths.
- `server.py` `build_hermes_env()`: `HERMES_GATEWAY_LOCK_DIR=/tmp/hermes-gateway-locks`
  (`setdefault`). Hermes' per-token locks are machine-local by intent but default
  to `$HOME/.local/state/hermes/gateway-locks`, and `HOME=/data` puts them on the
  volume where they outlive a redeploy — `start.sh` only sweeps
  `gateway.pid`/`.lock`/`.sock`. With the new exit-78 rule above, a stale token
  lock is no longer a recoverable state, so it must not be able to persist.

### Bug fixes
- **The crash-loop guard could never fire on a slow loop.** `_supervise_respawn`
  pruned `_recent_exits` on a fixed 120s window and needed 5 entries, so any
  failure cycle slower than ~24s dropped its own history every boot and the
  counter never advanced — the exact defect upstream names in its new
  `gateway/restart_loop_guard.py`. v2026.9.11's 300s startup watchdog makes that
  reachable: respawn every 5 minutes, forever, with Status reading "running".
  Added `RESPAWN_CHAIN_GAP_S` / `RESPAWN_CHAIN_UPTIME_S` / `RESPAWN_MAX_CHAIN`,
  which chain exits that are both close together AND short-lived. Simulated
  across 8 scenarios: fast loops still trip at 6 exits, the 300s loop now trips
  at 9, and healthy patterns (daily crash, hourly `/restart`, 12-minute manual
  restarts) never trip. A give-up now also `print()`s to `railway logs`.

### Housekeeping
- Documented, at both `hermes backup` call sites, that only the output filename
  keeps the new `--keep 3` auto-prune inert.
- `README.md`: pinned version ×2 and the supported-tools list.

### Verified unchanged (audited, no action)
Install extras (all 9 still exist; resolve executed on x86_64 and aarch64) ·
subprocess argv (all five parsed against the real `_build_cli_parser`, with a
negative control) · the WebSocket route set (enumerated from the live
`app.routes` on both tags — 7 unique paths, identical) · `HOP_BY_HOP`
host-stripping and the loopback Host gate · the auth gate, `resolve_public_url`
and the four `HERMES_DASHBOARD_BASIC_AUTH_*` keys · `ws_max_size` 384 MiB and
loopback `ws_ping_interval=None` · pairing directory resolution and the
pending/approved JSON schema (agreement re-proved inside the container on a
fresh volume) · `gateway.pid`/`.lock`/`.sock`, exit codes 75/78, container
markers · `detect_install_method` precedence and the Update-button refusal ·
`_EXCLUDED_DIRS`, `_IMPORT_SKIP_NAMES`, `_validate_backup_zip`, the `--force`
prompt · `HERMES_SUPERVISED_CHILD`, `HERMES_GATEWAY_MAX_STARTS`,
`HERMES_RESTART_AFTER_TURN_TIMEOUT`, `TERMINAL_TEMP_DIR` precedence ·
`firecrawl-anydoc==0.2.4` agreeing in core deps and `tools/lazy_deps.py` ·
`_POST_SETUP_INSTALLED` (still only `cua_driver`) · `_PROFILE_MANAGED_ENV_KEYS`
(still the 6 ACP/Copilot keys) · `KNOWN_PROVIDER_KEY_PREFIXES` ·
`web_dist` outDir, `HERMES_TUI_DIR`, node/npm engines · `cron_drain_timeout` 30,
`gateway_startup_warmup_timeout` 20, `gateway_turn_lease_timeout` 5 · bundled
skills (none removed).

---

## release/v2026.8.31/1 — September 6, 2026
**Hermes v2026.8.31 · major (Hermes upgrade, from v2026.8.27)**

### Hermes update
- Hermes Agent **v2026.8.27 → v2026.8.31** (package 0.20.6 → 0.21.0). Three new
  providers (Tencent TokenPlan, Ramp Router, Nebius Token Factory), Kanban board
  export/import, and hosted Group Chat rooms (off by default,
  `gateway.room_link_url` unset).
- **Anthropic subscription OAuth was REMOVED from the dashboard.** Upstream
  deleted `_start_anthropic_pkce`/`_submit_anthropic_pkce` and flipped the
  catalog entry to `flow: "external"` — an unattended HTTP endpoint minting
  Claude Pro/Max tokens sat on the wrong side of Anthropic's OAuth policy.
  `POST /api/providers/oauth/anthropic/start` now 400s. The terminal path
  (`hermes auth add anthropic`, runnable from the Chat tab) is unaffected.
- **`agent.gateway_turn_lease_timeout` 1800 → 5.** A second message on a session
  whose turn is still running is now rejected with a resend notice after ~5s
  instead of blocking for up to 30 minutes. Still config.yaml-only —
  `gateway/run.py` writes `HERMES_TURN_LEASE_TIMEOUT` into `os.environ`
  unconditionally at import, so a Railway variable is clobbered.
- **Scanned PDFs can now be OCR'd** when a `FIRECRAWL_API_KEY` is present:
  firecrawl-anydoc 0.2.4 adds a typed `NeedsOcrError` and a hosted OCR path
  (`tools/read_extract.py`). Opt out with `file_tools.hosted_ocr: false`.

### Changes to support upstream updates
- **PDF and Office reading would have broken on every deploy.** v2026.8.31 moved
  `firecrawl-anydoc` out of lazy-install and into CORE `dependencies` at
  `==0.2.4`, and bumped the lazy self-heal pin in `tools/lazy_deps.py` to match.
  This template carried a later Dockerfile layer pinning `==0.1.6` (correct when
  the package was lazy-only), which **downgrades** the core version at build
  time. `_is_satisfied()` compares versions rather than presence, so the first
  `.pdf/.docx/.xlsx/.pptx/.odt/.rtf/.epub` read tried to heal into
  `HERMES_LAZY_INSTALL_TARGET` with a `--constraint` file generated from every
  installed distribution — which pins `firecrawl-anydoc==0.1.6` — and uv
  hard-failed `No solution found`. Reproduced locally with uv: the downgrade,
  the resolver conflict, and `hasattr(anydoc, "NeedsOcrError") == False`. Every
  document read would have failed, on every deploy, retried every 300s
  (`ANYDOC_RETRY_SECONDS`) and never succeeding. The layer is deleted; core now
  supplies 0.2.4.
- **Tavily is gone from Hermes, so it is gone from Setup.** Upstream deleted
  `plugins/web/tavily/` and every `TAVILY_API_KEY` reader with it (11 modules →
  1 stale comment in `agent/redact.py`); `hermes_cli/setup.py` now advertises
  "Exa, Parallel, Firecrawl, or Keenable". `ENV_VARS` and the setup UI offered a
  field whose value nothing reads. Worse for anyone who had explicitly stored
  `web.backend: tavily` from hermes' own Tools tab: `_get_backend()`
  (`tools/web_tools.py`) returns a stored selection **strictly**, with no
  availability probe and no fallback, so `web_search` returns
  `no registered web search provider has that name` on every call. Replaced
  with `KEENABLE_API_KEY`. An existing `TAVILY_API_KEY` on the volume is left
  alone (config saves preserve keys outside `ENV_VARS`) — it is simply inert.
- **Restarts no longer risk a torn `state.db`.** `Gateway.stop()` waited 45s
  before SIGKILL. The v2026.8.31 stop path is a chain — `cron_drain_timeout`
  (30) + `CRON_DRAIN_CLEANUP_RESERVE_S` (10) + the new
  `gateway.signal_interrupt_grace_timeout` + adapter teardown + a newly bounded
  MCP shutdown + a PASSIVE WAL checkpoint in `SessionDB.close()` — which with an
  in-flight cron job runs ~56s. Upstream sizes its own supervisors at
  `max(60, max(drain, cron+10) + 30)` = 70s
  (`gateway/restart.py: resolve_systemd_timeout_stop_sec`) and raised its
  orphan-reaper grace 5s → 30s after a SIGKILL during that checkpoint corrupted
  `state.db` (the 2026-08-31 incident named in `hermes_cli/gateway.py`, which
  also added a boot-time `PRAGMA quick_check`). Raised to 70s, which sits
  *behind* hermes' own 60s shutdown watchdog — the watchdog hard-exits and
  releases the pid file and lock itself, so this SIGKILL should now never fire.
- **Terminal scratch stays off the volume.** v2026.8.31 changed the default
  temp root in `tools/environments/local.py` from `/tmp` to
  `$HERMES_HOME/cache/terminal` whenever `TERMINAL_TEMP_DIR` and `TMPDIR` are
  both unset (upstream's motive was a tmpfs `/tmp` filling up). Here
  `$HERMES_HOME` is the persistent volume: sandbox dirs and background-job logs
  would accumulate on `/data`, ship inside every `hermes backup` (`cache/` is
  not in upstream's `_EXCLUDED_DIRS`), and a locked `*.db` left there by an
  agent script would fail `_safe_copy_db` — which `_live_db_names()` reports as
  an incomplete snapshot and which **aborts a restore**. `build_hermes_env()`
  now sets `TERMINAL_TEMP_DIR=/tmp`, restoring the v2026.8.27 behaviour.

### Bug fixes
- **A profile switch can no longer hijack the gateway.** `hermes_cli/main.py`'s
  `_apply_profile_override()` runs at *import*, before argparse, and follows
  `$HERMES_HOME/active_profile` — written by hermes' own dashboard and by
  `hermes profile use` in the Chat terminal. Our `--external-supervisor` flag
  only sets `HERMES_GATEWAY_EXTERNAL_SUPERVISOR` *after* parsing, so it was
  always too late. One switch silently re-homed the gateway, the dashboard and
  the dashboard's detached restart under `profiles/<name>`: pairing, config and
  the pid record then diverged from what the admin panel reads, and the next
  `--replace` refused with "pid record belongs to a different HERMES_HOME".
  v2026.8.31 added `HERMES_SUPERVISED_CHILD` (#74872) as the opt-out and
  `build_hermes_env()` now sets it. It is read *only* by that guard, never by
  `is_gateway_supervisor_process()` or the s6 redirect, so the exit-75 contract
  is untouched. (Hazard predates this release; the opt-out is new.)
- **Dashboard credentials in `.env` can no longer lock every page out.** hermes
  loads `$HERMES_HOME/.env` into its own `os.environ` with `override=True` at
  startup — i.e. *after* the env `build_hermes_env()` hands the subprocess — so
  a `HERMES_DASHBOARD_BASIC_AUTH_*` value in the FILE beats the credentials we
  pass. hermes' `basic` provider then registers a different pair than
  `HermesSession` signs in with and every proxied page 502s with no explanation.
  A restore, a hand edit, or hermes' own Keys tab can all put them there. The
  four keys join `ENV_FILE_FORBIDDEN_KEYS` (file only — they remain honoured as
  genuine Railway variables, which `hermes_dashboard_credentials()` reads from
  the same `os.environ`). Same class as the existing `HERMES_PARENT_PID` strip.
- **A fatal config error no longer crash-loops.** Exit 78
  (`GATEWAY_FATAL_CONFIG_EXIT_CODE`) means an invalid multiplexer config or every
  enabled platform failing non-retryably — upstream pairs `Restart=always` with
  `RestartPreventExitStatus=78` for exactly this, and deliberately avoids 78 when
  the failure is mixed/transient. The supervisor now reports it and stops instead
  of burning the crash-loop budget on the same error. Unexpected-exit lines are
  also `print()`ed now, so they reach `railway logs` and not only the Logs panel.

### Housekeeping
- `requirements.txt` floor `uvicorn>=0.30` → `>=0.31`, tracking hermes' own core
  pin (CIDR-aware `forwarded_allow_ips`). Same system site-packages either way.

### Verified unchanged (audited, no action)
Two independent passes covered all 1,657 changed upstream files. Still identical:
the WebSocket route set (`pty`/`ws`/`events`/`console`/`pub`/kanban) and the
SPA's socket usage, so the fail-closed allowlist is complete; `ws_max_size`
(384 MiB) and the loopback ping settings on both hops; `host_header_middleware`
and the whole middleware stack, so Host-stripping still satisfies it;
`should_require_auth` / `should_require_dashboard_auth` / `_dashboard_public_hosts`
/ `resolve_public_url` and the `basic` provider, so the auth-gate pairing and
`/auth/password-login` are unchanged (the new `_desktop_loopback_auth_exempt`
needs `HERMES_DESKTOP=1` plus a session token, neither of which is ever set
here); `detect_install_method` and `is_container` (both container markers), so
the docker stamp still refuses the Update button; the exit-75 restart contract
and all six `--replace` refusal branches; `gateway/pairing.py` and the
`get_hermes_dir("platforms/pairing", "pairing")` rule byte-for-byte; every
backup constant (`_EXCLUDED_DIRS`, `_IMPORT_SKIP_NAMES`, the validator markers,
`backup -o`, `import --force`); `state.db` `SCHEMA_VERSION` 26, so a v2026.8.27
archive restores onto v2026.8.31 unmigrated; all nine install extras; the web
build `outDir` and the `HERMES_TUI_DIR` contract.

Two earlier notes in this repo were **wrong** and are corrected: the dashboard
*lifespan* orphan reaper is gated on `HERMES_DESKTOP=1` and never runs here
(only `_spawn_gateway_restart` reaps, and its exemption set is built from the
raw `gateway.pid` + `gateway.lock` records plus an unbounded parent walk, not
"up to 4 hops"); and `lightpanda` was added to `_POST_SETUP_READY`, an
interactive-CLI prompt gate, **not** to `_POST_SETUP_INSTALLED`, which still
holds only `cua_driver` — so the install-on-enable HTTP path is unchanged.

---

## release/v2026.8.27/1 — August 29, 2026
**Hermes v2026.8.27 · major (Hermes upgrade, from v2026.8.13)**

### Hermes update
- Hermes Agent **v2026.8.13 → v2026.8.27**, covering five upstream releases
  (8.16, 8.16.2, 8.18, 8.19, 8.27). The provider registry grew 47 → 58 entries;
  none of the 22 ids this template maps were renamed or removed.
- **Sub-agent limits raised upstream.** `delegation.max_iterations` (per-child
  tool-call budget) goes 50 → 250 and `max_concurrent_children` 3 → 10. Both are
  left at upstream's defaults, but they land differently: `max_iterations` is
  written into an existing `config.yaml`, so existing deployments keep 50 until
  they run a config migration, while `max_concurrent_children` is *not* written
  — so it inherits the new default immediately on upgrade. Existing bots
  therefore run up to 3× more sub-agents in parallel straight away, which can
  raise provider spend.

### Changes to support upstream updates
- **The dashboard would not have started at all, and MCP sign-ins would have
  broken.** v2026.8.27 routes `dashboard.public_url` into the new
  `should_require_dashboard_auth()` (`hermes_cli/web_server.py`), so a
  non-loopback public URL engages hermes' auth gate *even on a loopback bind*;
  with no auth provider the dashboard `SystemExit`s at startup. `start.sh`
  derived that value from `RAILWAY_PUBLIC_DOMAIN`, so every deploy would have
  503'd every proxied page while `/setup` and `/health` stayed green — `Dashboard`
  has no respawn supervisor. Reproduced in Docker with an A/B pair (identical
  images, only `RAILWAY_PUBLIC_DOMAIN` differing): `HERMES_DASHBOARD_READY` vs
  `EXITED with code 1`.

  Simply suppressing the URL fixes the dashboard but breaks something else: it
  is *also* the base hermes builds MCP OAuth `redirect_uri`s from
  (`_mcp_oauth_callback_url`), so every OAuth MCP server would redirect to
  `http://127.0.0.1:9119/...` — a dead page, silently. Verified live on both
  releases: v2026.8.13 returned the real public URL, v2026.8.27 returned
  loopback.

  So the template **satisfies the gate** rather than dodging it. Hermes' bundled
  `basic` auth provider is configured from the same admin credentials the setup
  page already uses, and `server.py` signs in to the dashboard on the user's
  behalf, injecting that session into proxied requests. **Nobody sees a second
  login screen**, and MCP redirects resolve to the real host. On by default,
  nothing to configure; `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` / `_PASSWORD` /
  `_SECRET` are available as explicit overrides.
- **The sign-in page moved to `/setup/login`.** Gated hermes redirects
  unauthenticated requests to `/login`, and a route of ours at that path answered
  instead — the browser bounced between the two until it gave up (8 redirects,
  reproduced). `/login` now redirects to the new path so bookmarks keep working,
  and the proxy re-signs-in and replays internally so that redirect should never
  reach a browser at all.
- **Dashboard sessions survive restarts.** Hermes signs its session tokens with a
  per-process key unless one is supplied, and every config save restarts the
  dashboard. The key is now generated once and kept on the volume.
- **Restores could abort on a sound backup.** Upstream's `_EXCLUDED_DIRS` gained
  `state-snapshots`, `browser-profiles` and `browser-profile`; our
  `_BACKUP_EXCLUDED_DIRS` mirror still had the old 15, so any `.db` under those
  paths was demanded from an archive hermes deliberately never writes — and a
  failed completeness check *aborts the restore*. Verified both directions in
  the container: the old set demanded `snap.db`/`Cookies.db`/`History.db`, the
  new set demands only the three real databases.
- **The agent could stop its own gateway.** v2026.8.27 narrowed the self-stop
  guard from the inherited `_HERMES_GATEWAY` marker to
  `_is_supervised_gateway_process()`, which additionally requires a supervisor
  marker — none of systemd/launchd/s6 applies here, so the agent's `terminal`
  and `execute_code` tools (which run in-process, satisfying the PID-file half)
  could shut the bot down. Now spawned with `--external-supervisor`, which is
  simply true. Verified: guard `False` without the flag, `True` with it. The
  exit-75 restart contract is untouched — `/restart` already takes the
  `via_service` branch on container detection.
- **Shutdown no longer kills a running scheduled job.** New
  `agent.cron_drain_timeout` (default 30s) makes hermes wait for an in-flight
  cron job before tearing down adapters; `Gateway.stop()` killed at 20s, so the
  job died mid-run and stayed marked running. Raised to 45s, still inside
  hermes' own 60s shutdown watchdog.
- **Stale gateway lock files cleared at boot.** A new cross-profile ownership
  gate makes `--replace` *refuse* a PID it cannot attribute to this
  `HERMES_HOME`, which no retry clears. `start.sh` now sweeps `gateway.lock` and
  `gateway.sock` alongside `gateway.pid`, and the supervisor logs an actionable
  line if the refusal is ever hit.

### Improvements
- **OpenRouter keys are checked before you save.** Upstream added
  `KNOWN_PROVIDER_KEY_PREFIXES`, and a key not starting with `sk-or-` is now
  silently skipped with only a log line — producing a bot that never replies,
  for this template's first-listed, README-recommended provider. Setup now warns
  inline as soon as the pasted value can't work.

---

## release/v2026.8.13/1 — August 15, 2026
**Hermes v2026.8.13 · major (Hermes upgrade, from v2026.8.3)**

### Hermes update
- Hermes Agent **v2026.8.3 → v2026.8.13** — community plugin catalogue, Kanban
  review workflows, and a new **Actual Computer** provider (`ACTUAL_API_KEY`,
  added to `ENV_VARS` and `HERMES_PROVIDER_IDS`). All 22 existing provider-id
  mappings re-verified against the new `PROVIDER_REGISTRY`; none renamed.
- **cgroup-aware agent-cache shedding** (`agent.agent_cache.memory_high_mb:
  auto`, on by default) reads the container's memory limit and evicts LRU
  transcripts before the OOM killer fires — it directly reduces the respawn
  cycle invariant 6 exists to survive.
- **PDF / legacy-Office `read_file`** via `firecrawl-anydoc`, baked into the
  image (see below).

### Changes to support upstream updates
- **`browser.backend` pinned to `off`.** Upstream's new default (`""`) means
  "use Browser Use mode whenever the browser-use CLI is runnable", and
  `_find_cli()` counts a bare `uvx` — which our base image
  (`ghcr.io/astral-sh/uv`) ships. Verified in the built image:
  `is_browser_use_cli_mode()` was `True`, which hides the whole `browser_*`
  surface behind a single `browser_exec` that shells `uvx browser-use` and then
  needs a Chrome this image does not contain. Also verified on **both** the
  v2026.8.3 and v2026.8.13 images that `check_browser_requirements()` is
  already `False` here (no Chromium), so nothing working was lost either way —
  the pin just stops the model being handed a tool that cannot succeed.
  `setdefault`, so an explicit choice in hermes' own settings still wins.
- **`firecrawl-anydoc==0.1.6` baked in.** It is a *lazy* dep
  (`tools/lazy_deps.py` → `tool.doc_extract`), not an extra, so it cannot go in
  the Dockerfile's `.[...]` string; without it the first PDF read pip-installs
  mid-turn into an image that is wiped on every redeploy, retrying only every
  300s while the file reads as binary garbage. Installed from `/` — from
  `/opt/hermes-agent`, uv reads that pyproject's `exclude-newer="14 days"` and
  rejects the package as too new.
- **Pause (ESTOP) surfaced.** v2026.8.13 added `hermes pause` / the in-chat
  `/pause`, which writes `$HERMES_HOME/ESTOP` and makes hermes refuse every new
  turn while the process stays alive — `/health` 200, gateway "running",
  platform online. It is on the volume, so it survives a redeploy, and `/pause`
  is `gateway_only` with no owner gate. `/setup/api/status` now reports
  `paused`, the header shows it, and the Status panel offers **Resume**
  (`POST /setup/api/pause/resume`).
- **`hermes backup` rc 2 handled.** Upstream added a cross-process flock with a
  0.25s acquire timeout and `SystemExit(2)` on contention. Our own
  `backup_lock` cannot prevent it — hermes' snapshot path is reachable
  independently (e.g. `/snapshot` in the proxied Chat tab). Reported as a 409
  "another backup is running, try again" instead of a 500; on the restore path
  this previously surfaced as "the backup command failed", which reads as data
  loss for a quarter-second collision.
- **Install-on-enable logged.** `PUT /api/tools/toolsets/<name>` now spawns
  `hermes tools post-setup` on enable, on a verb the existing warning never
  watched. Deliberately log-only: the one registered predicate today
  (`cua_driver`) installs to `~/.local/bin`, and `HOME=/data`, so it most
  likely lands on the volume — firing the "this will be wiped" notice would
  misinform. The log line was the part that was actually missing.
- **Backup completeness generalised** from `state.db` to every `*.db` on the
  volume (v2026.8.13 adds `cron/notepad.db`; `kanban.db` and
  `cron/executions.db` already existed), mirroring hermes' own `_EXCLUDED_DIRS`
  so it can never demand a file hermes deliberately skips — a false positive
  here aborts a restore.

### Bug fixes
- **A restored `.env` could kill the dashboard permanently.** v2026.8.13's new
  `_start_parent_death_watchdog()` is not gated on `HERMES_DESKTOP`, so a
  `HERMES_PARENT_PID` naming a dead process makes `hermes dashboard`
  `os._exit(0)` seconds after spawn — and unlike `Gateway`, `Dashboard` has no
  respawn supervisor, so every proxied page 503s until the container is
  redeployed. Reproduced locally, then fixed: `build_hermes_env()` drops the
  key (covers a Railway service variable) **and** `_sanitize_env_file()` strips
  it from `$HERMES_HOME/.env` at boot and after a restore — the pop alone is
  not enough, because hermes loads that file into its own `os.environ`.
- **Stale `.partial` backup files swept.** Upstream made `hermes backup -o`
  atomic via a dot-prefixed `.partial` sibling, which no existing cleanup
  matched (`pre-restore-*.zip` never matches a dotted name), so a backup killed
  mid-write leaked a file forever. Swept at boot with a 1-hour age guard so it
  cannot race an in-flight backup.

---

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
