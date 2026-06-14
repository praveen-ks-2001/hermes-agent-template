import os
import re
import pymysql
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tenant-mysql")


def get_auth_connection():
    """
    Base central donde guardás qué Telegram ID puede acceder a qué base.
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


def get_allowed_database(telegram_id: str):
    """
    Busca la base permitida para este usuario de Telegram.
    """
    sql = """
        SELECT 
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
        LIMIT 1
    """

    conn = get_auth_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (telegram_id,))
            return cur.fetchone()
    finally:
        conn.close()


def validate_sql(sql: str, can_write: bool = False):
    cleaned = sql.strip().lower()

    if not can_write:
        if not cleaned.startswith("select"):
            raise ValueError("Solo se permiten consultas SELECT.")

        blocked = [
            " insert ",
            " update ",
            " delete ",
            " drop ",
            " alter ",
            " truncate ",
            " create ",
            " grant ",
            " revoke ",
        ]

        padded = f" {cleaned} "
        for word in blocked:
            if word in padded:
                raise ValueError("Consulta no permitida.")

    if ";" in cleaned[:-1]:
        raise ValueError("Solo se permite una consulta por vez.")

    return True


def get_tenant_connection(db_config):
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
def consultar_mi_base(telegram_id: str, sql: str) -> dict:
    """
    Consulta únicamente la base de datos autorizada para el Telegram ID indicado.
    No permite consultar otras bases.
    """
    db_config = get_allowed_database(str(telegram_id))

    if not db_config:
        return {
            "ok": False,
            "error": "Este usuario de Telegram no tiene una base de datos autorizada.",
        }

    validate_sql(sql, can_write=bool(db_config.get("can_write")))

    conn = get_tenant_connection(db_config)

    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            return {
                "ok": True,
                "rows": rows,
                "database": db_config["db_name"],
            }
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
