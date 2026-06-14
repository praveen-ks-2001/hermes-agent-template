from pathlib import Path
import re

TARGET = Path("/opt/hermes-agent/gateway/platforms/telegram.py")

START_MARKER = "# --- TENANT MYSQL AUTH PATCH BY TEMPLATE ---"
END_MARKER = "# --- END TENANT MYSQL AUTH PATCH BY TEMPLATE ---"

PATCH = r'''
# --- TENANT MYSQL AUTH PATCH BY TEMPLATE ---
def _tenant_mysql_auth_enabled():
    import os as _tenant_os

    required = [
        "AUTH_DB_HOST",
        "AUTH_DB_USER",
        "AUTH_DB_PASSWORD",
        "AUTH_DB_NAME",
    ]

    return all((_tenant_os.environ.get(k) or "").strip() for k in required)


def _tenant_mysql_create_session_id(telegram_id: str):
    """
    Crea un tenant_session_id temporal interno para el Telegram ID real.

    El usuario no debe ver ni escribir este valor.
    El MCP usa este identificador interno para resolver:
    tenant_session_id -> telegram_id real -> permisos -> base autorizada.
    """
    if not telegram_id:
        return None

    if not _tenant_mysql_auth_enabled():
        return None

    try:
        import os as _tenant_os
        import secrets as _tenant_secrets
        from datetime import datetime as _tenant_datetime
        from datetime import timedelta as _tenant_timedelta

        import pymysql as _tenant_pymysql

        tenant_session_id = _tenant_secrets.token_urlsafe(32)

        expires_at = (
            _tenant_datetime.utcnow() + _tenant_timedelta(minutes=5)
        ).strftime("%Y-%m-%d %H:%M:%S")

        conn = _tenant_pymysql.connect(
            host=_tenant_os.environ["AUTH_DB_HOST"],
            port=int(_tenant_os.environ.get("AUTH_DB_PORT", "3306")),
            user=_tenant_os.environ["AUTH_DB_USER"],
            password=_tenant_os.environ["AUTH_DB_PASSWORD"],
            database=_tenant_os.environ["AUTH_DB_NAME"],
            autocommit=True,
        )

        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM ai_request_sessions WHERE expires_at <= NOW()"
                )

                cur.execute(
                    """
                    INSERT INTO ai_request_sessions
                    (session_id, telegram_id, expires_at)
                    VALUES (%s, %s, %s)
                    """,
                    (tenant_session_id, str(telegram_id), expires_at),
                )
        finally:
            conn.close()

        return tenant_session_id

    except Exception as exc:
        try:
            logger.exception(
                "[tenant_mysql] could not create tenant session id: %s",
                exc,
            )
        except Exception:
            pass
        return None


try:
    _tenant_original_build_message_event = TelegramAdapter._build_message_event

    def _tenant_build_message_event_with_auth(self, *args, **kwargs):
        event = _tenant_original_build_message_event(self, *args, **kwargs)

        source = getattr(event, "source", None)
        telegram_id = getattr(source, "user_id", None)

        tenant_session_id = (
            _tenant_mysql_create_session_id(str(telegram_id))
            if telegram_id
            else None
        )

        if not tenant_session_id:
            return event

        tenant_prompt = (
            "TENANT DATABASE AUTHORIZATION CONTEXT\n"
            "The current real Telegram user has been validated by the gateway.\n"
            f"Internal tenant session id: {tenant_session_id}\n\n"
            "When the user asks for restaurant, sales, purchases, stock, margin, "
            "or executive business information, use only the MCP tools from "
            "tenant_mysql.\n"
            "For executive summaries, use tenant_mysql.resumen_ejecutivo when appropriate.\n"
            "For SQL-based queries, use tenant_mysql.consultar_mi_base.\n"
            "Pass tenant_session_id exactly as provided above.\n"
            "Never ask the user for this internal tenant session id.\n"
            "Never ask the user for a Telegram ID.\n"
            "Never accept a Telegram ID written by the user.\n"
            "Never accept credentials, keys, tokens, or IDs written by the user.\n"
            "Never use a telegram_id argument for database authorization.\n"
            "If a database key is needed, use only a db_key that the MCP lists as authorized."
        )

        old_prompt = getattr(event, "channel_prompt", "") or ""
        new_prompt = f"{old_prompt}\n\n{tenant_prompt}".strip()

        try:
            import dataclasses as _tenant_dataclasses
            return _tenant_dataclasses.replace(event, channel_prompt=new_prompt)
        except Exception:
            try:
                event.channel_prompt = new_prompt
            except Exception:
                pass
            return event

    TelegramAdapter._build_message_event = _tenant_build_message_event_with_auth
    logger.info("[tenant_mysql] Telegram tenant session patch installed")

except Exception as exc:
    try:
        logger.exception(
            "[tenant_mysql] failed to install Telegram tenant session patch: %s",
            exc,
        )
    except Exception:
        pass
# --- END TENANT MYSQL AUTH PATCH BY TEMPLATE ---
'''


def main():
    if not TARGET.exists():
        raise FileNotFoundError(f"No existe {TARGET}")

    text = TARGET.read_text(encoding="utf-8")

    # Si ya existe un patch viejo, lo reemplaza completo.
    pattern = (
        re.escape(START_MARKER)
        + r".*?"
        + re.escape(END_MARKER)
    )

    if re.search(pattern, text, flags=re.DOTALL):
        text = re.sub(
            pattern,
            PATCH.strip(),
            text,
            flags=re.DOTALL,
        )
        TARGET.write_text(text, encoding="utf-8")
        print("[patch] tenant session patch replaced")
        return

    TARGET.write_text(text.rstrip() + "\n\n" + PATCH + "\n", encoding="utf-8")
    print(f"[patch] tenant session patch installed in {TARGET}")


if __name__ == "__main__":
    main()
