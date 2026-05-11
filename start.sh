#!/bin/bash

# Configure GitHub CLI with token
if [ -n "$GITHUB_TOKEN" ]; then
    mkdir -p ~/.config/gh
    cat > ~/.config/gh/hosts.yml <<EOF
github.com:
    oauth_token: $GITHUB_TOKEN
    git_protocol: https
EOF
    chmod 600 ~/.config/gh/hosts.yml
fi

set -e

# Mirror dashboard-ref-only's startup: create every directory hermes expects
# and seed a default config.yaml if the volume is empty. Without these,
# `hermes dashboard` endpoints that hit logs/, sessions/, cron/, etc. can fail
# with opaque errors even though no auth is actually involved.
mkdir -p /data/.hermes/cron /data/.hermes/sessions /data/.hermes/logs \
         /data/.hermes/memories /data/.hermes/skills /data/.hermes/pairing \
         /data/.hermes/hooks /data/.hermes/image_cache /data/.hermes/audio_cache \
         /data/.hermes/workspace

if [ ! -f /data/.hermes/config.yaml ] && [ -f /opt/hermes-agent/cli-config.yaml.example ]; then
  cp /opt/hermes-agent/cli-config.yaml.example /data/.hermes/config.yaml
fi

[ ! -f /data/.hermes/.env ] && touch /data/.hermes/.env

# Clear any stale gateway PID file left over from the previous container.
# `hermes gateway` writes /data/.hermes/gateway.pid on start but does not
# remove it on SIGTERM. Since /data is a persistent volume, the file
# survives container restarts and causes every subsequent boot to exit with
# "ERROR gateway.run: PID file race lost to another gateway instance".
# No hermes process can be running at this point (we're pre-exec in a fresh
# container), so removing the file unconditionally is safe.
rm -f /data/.hermes/gateway.pid

# Step 1 — Encrypt: read .env (or .env.encrypted if it already exists) and
# write all secret values in encrypted form to .env.encrypted.
# Requires HERMES_ENCRYPTION_KEY to be set as a Railway service variable.
# If the key is absent, the script prints a newly generated key and exits
# with a non-zero code so the deploy fails loudly rather than running with
# unencrypted secrets. Set HERMES_ENCRYPTION_KEY in Railway and redeploy.
python /app/encrypt_secrets.py

# Step 2 — Decrypt to plaintext: read .env.encrypted and write a fully
# plaintext /data/.hermes/.env so the Hermes gateway subprocess can load it
# directly with its own dotenv reader without encountering "enc:..." tokens.
# The plaintext .env is ephemeral — it is recreated on every boot from the
# encrypted backup, which remains the persistent source of truth on disk.
python /app/encrypt_secrets.py --decrypt-to-plaintext

exec python /app/server.py
