import os
import re
import pymysql
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tenant-mysql")


def get_auth_connection():
    """
    Base central de autorización.
    Aquí viven:
    - ai_users
    - ai_databases
    - ai_user_database_access
    - ai_request_tokens
    """
    return pymysql.connect(
        host=os.environ["AUTH_DB_HOST"],
        port=int(os.environ.get("AUTH_DB_PORT", "3306")),
        user=os.environ["AUTH_DB_USER"],
        password=os.environ["AUTH_DB_PASSWORD"],
        database=os.environ["AUTH_DB_NAME"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def resolve_telegram_id_from_session(tenant_session_id: str) -> str | None:
    sql = """
        SELECT telegram_id
        FROM ai_request_sessions
        WHERE session_id = %s
          AND expires_at > NOW()
        LIMIT 1
    """

    conn = get_auth_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (tenant_session_id,))
            row = cur.fetchone()
            if not row:
                return None
            return str(row["telegram_id"])
    finally:
        conn.close()


def get_allowed_databases(telegram_id: str):
    """
    Devuelve todas las bases permitidas para el Telegram ID real.
    """
    sql = """
        SELECT 
            d.db_key,
            d.db_host,
            d.db_port,
            d.db_name,
            d.db_user,
            d.db_password_env,
            a.can_read,
            a.can_write
        FROM ai_users u
        JOIN ai_user_database_access a ON a.user_id = u.id
        JOIN ai_databases d ON d.id = a.database_id
        WHERE u.telegram_id = %s
          AND u.activo = 1
          AND d.activo = 1
          AND a.can_read = 1
        ORDER BY d.db_key
    """

    conn = get_auth_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (telegram_id,))
            return cur.fetchall()
    finally:
        conn.close()


def get_allowed_database(telegram_id: str, db_key: str | None = None):
    """
    Busca una base permitida.
    Si el usuario tiene una sola base, la usa.
    Si tiene varias, debe indicar db_key.
    """
    databases = get_allowed_databases(telegram_id)

    if not databases:
        return None

    if db_key:
        for db in databases:
            if db["db_key"] == db_key:
                return db
        return None

    if len(databases) == 1:
        return databases[0]

    raise ValueError(
        "Este usuario tiene acceso a varias bases. Debe indicar db_key."
    )


def validate_sql(sql: str, can_write: bool = False):
    """
    Valida SQL antes de ejecutarlo.
    Por defecto solo permite SELECT.
    """
    cleaned = sql.strip()
    lowered = cleaned.lower()
    padded = f" {lowered} "

    if not lowered:
        raise ValueError("La consulta SQL está vacía.")

    if ";" in lowered[:-1]:
        raise ValueError("Solo se permite una consulta por vez.")

    if not can_write:
        if not lowered.startswith("select"):
            raise ValueError("Solo se permiten consultas SELECT.")

        blocked_words = [
            " insert ",
            " update ",
            " delete ",
            " drop ",
            " alter ",
            " truncate ",
            " create ",
            " grant ",
            " revoke ",
            " replace ",
            " call ",
            " execute ",
            " use ",
            " set ",
        ]

        for word in blocked_words:
            if word in padded:
                raise ValueError("Consulta no permitida.")

    blocked_patterns = [
        r"\binformation_schema\.",
        r"\bmysql\.",
        r"\bperformance_schema\.",
        r"\bsys\.",
        r"--",
        r"/\*",
        r"\*/",
    ]

    for pattern in blocked_patterns:
        if re.search(pattern, lowered):
            raise ValueError("Consulta no permitida por seguridad.")

    return True


def get_tenant_connection(db_config):
    """
    Conecta únicamente a la base autorizada.
    """
    password_env = db_config["db_password_env"]
    password = os.environ.get(password_env)

    if not password:
        raise RuntimeError(f"No existe la variable de entorno {password_env}")

    return pymysql.connect(
        host=db_config["db_host"],
        port=int(db_config.get("db_port") or 3306),
        user=db_config["db_user"],
        password=password,
        database=db_config["db_name"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


@mcp.tool()
def listar_mis_bases(auth_token: str) -> dict:
    """
    Lista solo las bases autorizadas para el usuario real de Telegram.
    """
    telegram_id = resolve_telegram_id_from_token(auth_token)

    if not telegram_id:
        return {
            "ok": False,
            "error": "No se pudo validar la identidad del usuario.",
        }

    databases = get_allowed_databases(telegram_id)

    return {
        "ok": True,
        "telegram_id": telegram_id,
        "bases": [
            {
                "db_key": db["db_key"],
                "can_read": bool(db["can_read"]),
                "can_write": bool(db["can_write"]),
            }
            for db in databases
        ],
    }


@mcp.tool()
def consultar_mi_base(tenant_session_id: str, sql: str, db_key: str = "") -> dict:
    """
    Consulta únicamente una base autorizada para el usuario real de Telegram.
    No recibe Telegram ID.
    No recibe auth_token.
    Usa tenant_session_id interno generado por Hermes.
    """
    telegram_id = resolve_telegram_id_from_session(tenant_session_id)

    if not telegram_id:
        return {
            "ok": False,
            "error": "No se pudo validar la identidad del usuario.",
        }

    try:
        db_config = get_allowed_database(
            telegram_id=telegram_id,
            db_key=db_key.strip() or None,
        )
    except ValueError as e:
        return {
            "ok": False,
            "error": str(e),
        }

    if not db_config:
        return {
            "ok": False,
            "error": "Este usuario no tiene acceso a la base solicitada.",
        }

    try:
        validate_sql(sql, can_write=bool(db_config.get("can_write")))

        conn = get_tenant_connection(db_config)

        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()

                return {
                    "ok": True,
                    "db_key": db_config["db_key"],
                    "rows": rows,
                }
        finally:
            conn.close()

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }

@mcp.tool()
def resumen_ejecutivo(tenant_session_id: str, periodo: str = "ayer", db_key: str = "") -> dict:
    telegram_id = resolve_telegram_id_from_session(tenant_session_id)

    if not telegram_id:
        return {
            "ok": False,
            "error": "No se pudo validar la identidad del usuario.",
        }

    # resto del código igual


if __name__ == "__main__":
    mcp.run()
