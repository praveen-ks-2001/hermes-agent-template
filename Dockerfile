FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Which hermes-agent revision to install. Accepts any git ref the upstream
# repo publishes — a release tag (recommended for reproducibility) or a
# branch name (`main`) for bleeding edge.
#
# To bump: check https://github.com/NousResearch/hermes-agent/releases for the
# newest tag (format `vYYYY.M.D`, optionally with a `.PATCH` suffix, e.g.
# `v2026.5.29.2`) and update the default below. Use `main` only if you accept
# that every rebuild can pull arbitrary new upstream commits.
ARG HERMES_REF=v2026.8.31

# Persist the build arg into the runtime env so the admin UI can display which
# Hermes release this image actually pins. Reading it (rather than hardcoding a
# version in the template) keeps the badge honest when someone overrides
# HERMES_REF as a Railway service variable to pin an older release — a Railway
# runtime variable simply shadows this ENV, so the UI still shows the truth.
ENV HERMES_REF=${HERMES_REF}

# tini = tiny init that we run as PID 1. Without it, hermes's grandchild
# processes (MCP stdio servers, git, bun, browser daemons spawned by tools)
# reparent to PID 1 when their parents exit and pile up as zombies. After
# weeks of uptime that exhausts the kernel's PID table → "fork: cannot
# allocate memory" and the container dies. tini reaps zombies in the
# background and forwards SIGTERM/SIGINT to our entrypoint so Railway's
# stop signal still triggers our graceful shutdown. Standard container init
# (same as Docker's `--init` flag and Kubernetes' pause container).
#
# Node.js is required only at build time to compile the Hermes React dashboard.
# We strip the source + apt lists afterwards to keep the image lean.
#
# Keep setup_22.x. v2026.8.3's new .npmrc sets engine-strict=true, so hermes'
# `node >=22.22.0` + `npm <11.10.0 || >=11.17.0` is now a hard EBADENGINE build
# failure, not a warning — setup_24.x bundles an npm that satisfies neither.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates git tini && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install hermes-agent (provides the `hermes` CLI) and pre-build its React
# dashboard so `hermes dashboard` has nothing to build at runtime.
#
# [all] in v2026.6.5 no longer pulls in [dev]; messaging platforms, TTS, and
# other heavy backends are lazy-installed by hermes at first use. We pre-install
# the ones this template actually uses so first-message latency is instant.
# `vision` (Pillow) is a soft-dep that is NOT in [all] and is otherwise
# lazy-installed at first image use: without it hermes can't downscale an
# oversized image (>5 MB / >8000px), which then bakes into immutable history
# and bricks the session on Anthropic's non-retryable 400. We bake it in.
# When bumping HERMES_REF, re-check hermes-agent's pyproject.toml [all] and
# the extras below against the new release's pyproject.toml.
#
# The `-e` is LOAD-BEARING since v2026.8.3: upstream's new setup.py raises on
# bdist_wheel/sdist unless HERMES_NIX_BUILD=1. PEP 660 editable installs route
# through build_editable and are exempt — drop `-e` and the image won't build.
#
# v2026.8.3 also added [tool.uv] to pyproject.toml, which uv reads from this
# cwd (upstream builds from a frozen lock; we re-resolve every time):
# override-dependencies fixes discord.py's vulnerable pynacl pin, and
# exclude-newer="14 days" can fail a build on a fresh dep — override with
# `uv pip install --exclude-newer <date>`.
#
# v2026.8.13 made that escape hatch sharper: nemo-relay's floor moved to
# >=0.7.1 (published 2026-08-07), which only resolves because upstream lists
# it in exclude-newer-package. A manual `--exclude-newer <date>` re-imposes a
# GLOBAL cutoff, so any date before 2026-08-07 leaves nemo-relay>=0.7.1
# unsatisfiable and hard-fails the build. Same trap for cryptography==50.0.0
# and h2 4.4.1. If you ever need that flag, pass a date >= 2026-08-07.
RUN git clone --depth 1 --branch ${HERMES_REF} https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent && \
    cd /opt/hermes-agent && \
    uv pip install --system --no-cache -e ".[all,messaging,tts-premium,honcho,bedrock,anthropic,edge-tts,hindsight,vision]" && \
    cd /opt/hermes-agent/web && \
    npm install --silent && \
    npm run build && \
    cd /opt/hermes-agent/ui-tui && \
    npm install --silent --no-fund --no-audit --progress=false && \
    npm run build && \
    rm -rf /opt/hermes-agent/web /opt/hermes-agent/.git /root/.npm

# Why pre-build ui-tui (and why we don't delete it after):
# - The dashboard's embedded Chat tab spawns `node ui-tui/dist/entry.js`
#   on every WebSocket connect to /api/pty.
# - Without HERMES_TUI_DIR, hermes's _make_tui_argv falls through to the
#   npm install + build path (since git-editable installs don't have the
#   bundled tui_dist/ that PyPI wheels include), adding 30-60s to the
#   first chat-open and blocking the asyncio event loop.
# - Pre-building at image time surfaces build failures here rather than
#   at user request time, and makes first-chat-open instant.
# - We keep ui-tui/ entirely (node_modules + dist + src) so HERMES_TUI_DIR
#   can point at it (see below).

# Stamp the CODE-SCOPED install method next to the running package. hermes'
# detect_install_method() reads <install-tree>/.install_method FIRST (priority 1,
# authoritative) — before the home-scoped $HERMES_HOME/.install_method that
# start.sh writes (priority 2, honored only when is_container() is true). The
# install tree for our editable install is /opt/hermes-agent (parent of
# hermes_cli/, i.e. Path(config.py).parent.parent). Baking the stamp here makes
# the dashboard "Update Hermes" button refuse regardless of runtime container
# detection — exactly what upstream's own published image does (it bakes a
# docker stamp into /opt/hermes). Belt-and-suspenders with start.sh's home stamp:
# if a future hermes release changes or drops is_container()'s Railway marker
# (/run/.containerenv), the home stamp would stop being honored but this one
# still refuses. Re-verify the install-tree path if hermes stops installing
# editable from /opt/hermes-agent.
RUN printf 'docker\n' > /opt/hermes-agent/.install_method

# firecrawl-anydoc: hermes v2026.8.13's PDF / legacy-Office reader for read_file.
# It is a LAZY dep (tools/lazy_deps.py "tool.doc_extract"), NOT an extra — so it
# cannot be added to the `.[...]` string above; upstream deliberately withheld a
# `doc-extract` extra until the package clears uv's 14-day exclude-newer window
# (first release 2026-08-04). Without it baked in, the FIRST time the agent reads
# a .pdf/.docx/.xlsx/.pptx/.odt/.rtf/.epub it pip-installs mid-turn into the
# running container — which this image wipes on every redeploy, so it re-installs
# after each deploy, and a failed install is retried only every 300s
# (ANYDOC_RETRY_SECONDS) while the file silently reads as binary garbage.
#
# `cd /` is LOAD-BEARING: run from /opt/hermes-agent, uv reads that pyproject's
# [tool.uv] exclude-newer="14 days" and REJECTS this package as too new. From /
# there is no pyproject to discover, so the pin resolves normally (~2s, 3 MiB,
# no transitive deps). Drop this layer once upstream ships the mirrored extra.
RUN cd / && uv pip install --system --no-cache firecrawl-anydoc==0.1.6

COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt

RUN mkdir -p /data/.hermes

COPY server.py /app/server.py
COPY templates/ /app/templates/
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

ENV HOME=/data
ENV HERMES_HOME=/data/.hermes

# Points hermes at our pre-built TUI bundle. hermes's _make_tui_argv checks
# HERMES_TUI_DIR first: if dist/entry.js exists there, it skips the npm
# install/build entirely. This is the official packager path (Nix uses it too)
# and avoids the 30-60s npm bootstrap that git-editable installs would otherwise
# trigger on first /chat connection.
ENV HERMES_TUI_DIR=/opt/hermes-agent/ui-tui

# tini wraps start.sh so it runs as PID 1's child instead of as PID 1 itself.
# `-g` propagates signals to the whole process group so `docker stop` /
# Railway's SIGTERM cleanly terminates the entire tree, not just start.sh.
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["/app/start.sh"]
