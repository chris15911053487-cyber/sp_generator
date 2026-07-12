"""SQL Server 连接和操作 — 语法校验、查询执行、存储过程部署。"""
import pyodbc
from config import get_db_config


def _build_conn_str() -> str:
    cfg = get_db_config()
    return (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={cfg['server']},{cfg['port']};"
        f"DATABASE={cfg['database']};"
        f"UID={cfg['user']};"
        f"PWD={cfg['password']};"
        "Encrypt=no;TrustServerCertificate=yes;"
        "Connection Timeout=10;"
    )


def get_connection() -> pyodbc.Connection:
    return pyodbc.connect(_build_conn_str(), autocommit=True)


def execute_query(sql: str) -> list[dict]:
    """执行 SQL 查询，返回字典列表。"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql)
    columns = [col[0] for col in cursor.description] if cursor.description else []
    rows = []
    for row in cursor.fetchall():
        rows.append(dict(zip(columns, row)))
    conn.close()
    return rows


def check_syntax(sql: str) -> tuple[bool, str]:
    """用 SET PARSEONLY ON 检查 SQL 语法。返回 (通过, 错误信息)。"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SET PARSEONLY ON")
        cursor.execute(sql)
        cursor.execute("SET PARSEONLY OFF")
        conn.close()
        return True, ""
    except Exception as e:
        try:
            cursor.execute("SET PARSEONLY OFF")
        except Exception:
            pass
        conn.close()
        return False, str(e)


def deploy_procedure(name: str, code: str) -> tuple[bool, str]:
    """部署存储过程到 SQL Server。返回 (成功, 错误信息)。"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"DROP PROCEDURE IF EXISTS [{name}]")
        cursor.execute(code)
        conn.close()
        return True, ""
    except Exception as e:
        conn.close()
        return False, str(e)


def get_table_columns(table_name: str) -> list[dict]:
    """查询表的列信息。"""
    sql = f"""
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = '{table_name}'
        ORDER BY ORDINAL_POSITION
    """
    return execute_query(sql)


def get_tables() -> list[str]:
    """获取当前数据库中的用户表列表。"""
    rows = execute_query(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'"
    )
    return [r["TABLE_NAME"] for r in rows]
