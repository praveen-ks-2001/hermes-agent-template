"""
Hermes secrets encryption utility.

Provides at-rest encryption for all values stored in /data/.hermes/.env using
Fernet (AES-128-CBC + HMAC-SHA256). Encrypted values are stored with an
"enc:" prefix so they are distinguishable from plaintext values.

Usage modes
-----------
1. Called directly by start.sh at container boot:
       python /app/encrypt_secrets.py
   Reads the .env file, encrypts any plaintext secret values in-place, and
   exits. Idempotent — already-encrypted values are left untouched.

2. Imported by server.py for transparent decrypt-on-read:
       from encrypt_secrets import decrypt_value, get_fernet
   read_env() calls decrypt_value() on every value it reads so the rest of
   server.py always works with plaintext strings in memory.

Encryption key
--------------
The key is sourced from the HERMES_ENCRYPTION_KEY environment variable, which
should be set as a Railway service variable (never committed to git). If the
variable is absent at first boot, a new key is generated, printed to stdout
(so it appears in Railway's deploy logs), and the process exits with a
non-zero code to prompt the operator to persist it. This prevents silent
key loss across redeploys.

Key format: URL-safe base64-encoded 32-byte Fernet key (44 characters).
Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------

_FERNET_INSTANCE = None  # module-level cache


def get_fernet():
    """Return a cached Fernet instance, initialised from HERMES_ENCRYPTION_KEY.

    Raises RuntimeError if the key is missing or malformed.
    """
    global _FERNET_INSTANCE
    if _FERNET_INSTANCE is not None:
        return _FERNET_INSTANCE

    try:
        from cryptography.fernet import Fernet, InvalidToken  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "cryptography package is not installed. "
            "Add 'cryptography' to requirements.txt and rebuild the image."
        ) from exc

    raw_key = os.environ.get("HERMES_ENCRYPTION_KEY", "").strip()
    if not raw_key:
        raise RuntimeError("HERMES_ENCRYPTION_KEY is not set")

    try:
        instance = Fernet(raw_key.encode())
    except Exception as exc:
        raise RuntimeError(
            f"HERMES_ENCRYPTION_KEY is invalid: {exc}. "
            "Generate a new key with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        ) from exc

    _FERNET_INSTANCE = instance
    return instance


# ---------------------------------------------------------------------------
# Value-level helpers (used by server.py's read_env / write_env)
# ---------------------------------------------------------------------------

ENC_PREFIX = "enc:"


def is_encrypted(value: str) -> bool:
    """Return True if *value* is already Fernet-encrypted."""
    return value.startswith(ENC_PREFIX)


def encrypt_value(plaintext: str) -> str:
    """Encrypt *plaintext* and return an ``enc:<token>`` string."""
    fernet = get_fernet()
    token = fernet.encrypt(plaintext.encode()).decode()
    return f"{ENC_PREFIX}{token}"


def decrypt_value(value: str) -> str:
    """Decrypt an ``enc:<token>`` string.  Returns plaintext values unchanged."""
    if not is_encrypted(value):
        return value
    from cryptography.fernet import InvalidToken

    token = value[len(ENC_PREFIX):]
    try:
        return get_fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "Failed to decrypt a secret from .env — the HERMES_ENCRYPTION_KEY "
            "may have changed since the value was encrypted. "
            "Reset the .env file or restore the original key."
        ) from exc


# ---------------------------------------------------------------------------
# File-level encryption pass (called by start.sh)
# ---------------------------------------------------------------------------

# Keys whose values should be encrypted at rest.  Mirrors SECRET_KEYS in
# server.py — kept in sync manually; non-secret keys (model name, boolean
# flags, etc.) are stored in plaintext for readability.
_SECRET_KEY_NAMES = {
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "GLM_API_KEY",
    "KIMI_API_KEY",
    "MINIMAX_API_KEY",
    "HF_TOKEN",
    "NVIDIA_API_KEY",
    "ARCEE_API_KEY",
    "STEPFUN_API_KEY",
    "AI_GATEWAY_API_KEY",
    "GEMINI_API_KEY",
    "PARALLEL_API_KEY",
    "FIRECRAWL_API_KEY",
    "TAVILY_API_KEY",
    "FAL_KEY",
    "BROWSERBASE_API_KEY",
    "GITHUB_TOKEN",
    "VOICE_TOOLS_OPENAI_KEY",
    "HONCHO_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "EMAIL_PASSWORD",
    "MATTERMOST_TOKEN",
    "MATRIX_ACCESS_TOKEN",
    "ADMIN_PASSWORD",
}


def encrypt_env_file(env_path: Path) -> int:
    """Encrypt all plaintext secret values in *env_path* in-place.

    Returns the number of values that were newly encrypted.
    """
    if not env_path.exists():
        return 0

    original = env_path.read_text()
    lines = original.splitlines(keepends=True)
    new_lines: list[str] = []
    encrypted_count = 0

    for line in lines:
        stripped = line.rstrip("\n")
        # Preserve comments, blank lines, and non-assignment lines verbatim.
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue

        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()

        # Strip surrounding quotes if present (written by write_env).
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]

        # Only encrypt non-empty secret values that aren't already encrypted.
        if key in _SECRET_KEY_NAMES and value and not is_encrypted(value):
            encrypted_value = encrypt_value(value)
            new_lines.append(f"{key}={encrypted_value}\n")
            encrypted_count += 1
        else:
            new_lines.append(line)

    if encrypted_count:
        env_path.write_text("".join(new_lines))
        # Tighten permissions so only the process owner can read the file.
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass

    return encrypted_count


# ---------------------------------------------------------------------------
# Entry point (called by start.sh)
# ---------------------------------------------------------------------------

def _generate_and_print_key() -> None:
    """Print a freshly generated Fernet key and instructions, then exit 1."""
    from cryptography.fernet import Fernet

    new_key = Fernet.generate_key().decode()
    print(
        "\n"
        "╔══════════════════════════════════════════════════════════════════╗\n"
        "║          HERMES — ENCRYPTION KEY REQUIRED                       ║\n"
        "╠══════════════════════════════════════════════════════════════════╣\n"
        "║  HERMES_ENCRYPTION_KEY is not set.                              ║\n"
        "║  A new key has been generated for you:                          ║\n"
        "║                                                                  ║\n"
        f"║  {new_key:<64} ║\n"
        "║                                                                  ║\n"
        "║  Add it as a Railway service variable named                     ║\n"
        "║  HERMES_ENCRYPTION_KEY and redeploy.                            ║\n"
        "║                                                                  ║\n"
        "║  ⚠  Keep this key safe — losing it means losing access to all  ║\n"
        "║     secrets stored in the persistent volume.                    ║\n"
        "╚══════════════════════════════════════════════════════════════════╝\n",
        flush=True,
    )
    sys.exit(1)


def main() -> None:
    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    env_path = Path(hermes_home) / ".env"

    # If no key is configured, generate one and tell the operator.
    if not os.environ.get("HERMES_ENCRYPTION_KEY", "").strip():
        _generate_and_print_key()

    # Validate the key before touching the file.
    try:
        get_fernet()
    except RuntimeError as exc:
        print(f"[encrypt_secrets] ERROR: {exc}", flush=True)
        sys.exit(1)

    if not env_path.exists():
        print("[encrypt_secrets] No .env file found — nothing to encrypt.", flush=True)
        return

    count = encrypt_env_file(env_path)
    if count:
        print(f"[encrypt_secrets] Encrypted {count} plaintext secret(s) in {env_path}", flush=True)
    else:
        print(f"[encrypt_secrets] All secrets already encrypted in {env_path}", flush=True)


if __name__ == "__main__":
    main()
