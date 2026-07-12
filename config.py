"""全局配置管理 — 从 SQLite 读取 DB/LLM 配置。"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "app.db")

DEFAULT_CONFIG = {
    "db_server": "139.199.221.230",
    "db_port": "1400",
    "db_user": "sa",
    "db_password": "<YourStrong@Passw0rd>",
    "db_database": "B1UP_DEMO",
    "llm_api_key": "",
    "llm_base_url": "https://api.deepseek.com/v1",
    "llm_model_name": "deepseek-v4-pro",
}


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.commit()


def init_config() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    _ensure_table(conn)
    for key, value in DEFAULT_CONFIG.items():
        conn.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()
    conn.close()


def _get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def get_config(key: str, default: str = "") -> str:
    conn = _get_conn()
    row = conn.execute(
        "SELECT value FROM config WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    return row[0] if row else default


def set_config(key: str, value: str) -> None:
    conn = _get_conn()
    _ensure_table(conn)
    conn.execute(
        """INSERT INTO config (key, value, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
           updated_at=CURRENT_TIMESTAMP""",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_db_config() -> dict:
    return {
        "server": get_config("db_server"),
        "port": int(get_config("db_port", "1433")),
        "user": get_config("db_user"),
        "password": get_config("db_password"),
        "database": get_config("db_database"),
    }


def get_llm_config() -> dict:
    return {
        "api_key": get_config("llm_api_key"),
        "base_url": get_config("llm_base_url"),
        "model_name": get_config("llm_model_name"),
    }
