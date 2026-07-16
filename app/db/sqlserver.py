"""SQL Server 连接和操作 — 语法校验、查询执行、存储过程部署。"""
import decimal
import datetime
import re
import pyodbc
from config import get_db_config


def _serialize_value(val):
    """将数据库返回的非 JSON 类型转为可序列化类型。"""
    if val is None:
        return None
    if isinstance(val, decimal.Decimal):
        return float(val)
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val.isoformat()
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return val


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
        rows.append({col: _serialize_value(val) for col, val in zip(columns, row)})
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


def _validate_sp_name(name: str) -> str:
    """校验存储过程名称，只允许 [schema.]name 格式。"""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_.]*$', name):
        raise ValueError(f"非法存储过程名称: {name}")
    return name


def deploy_procedure(name: str, code: str) -> tuple[bool, str]:
    """部署存储过程到 SQL Server。返回 (成功, 错误信息)。"""
    try:
        name = _validate_sp_name(name)
    except ValueError as e:
        return False, str(e)
    conn = get_connection()
    cursor = conn.cursor()
    try:
        safe_name = name.replace("]", "]]")
        cursor.execute(f"DROP PROCEDURE IF EXISTS [{safe_name}]")
        cursor.execute(code)
        conn.close()
        return True, ""
    except Exception as e:
        conn.close()
        return False, str(e)


def get_table_columns(table_name: str) -> list[dict]:
    """查询表的列信息（参数化查询，防注入）。"""
    sql = """
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql, (table_name,))
    columns = [col[0] for col in cursor.description] if cursor.description else []
    rows = []
    for row in cursor.fetchall():
        rows.append({col: _serialize_value(val) for col, val in zip(columns, row)})
    conn.close()
    return rows


def get_tables() -> list[str]:
    """获取当前数据库中的用户表列表。"""
    rows = execute_query(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'"
    )
    return [r["TABLE_NAME"] for r in rows]


def substitute_params(sql: str, params: dict) -> str:
    """将 SQL 中的 {param_name} 占位符替换为实际值，自动转义单引号。"""
    if not params:
        return sql
    def replacer(m):
        key = m.group(1)
        if key in params:
            val = params[key]
            if val is None or val == '':
                return 'NULL'
            if isinstance(val, str):
                escaped = val.replace("'", "''")
                return f"'{escaped}'"
            return str(val)
        return m.group(0)
    return re.sub(r'\{(\w+)\}', replacer, sql)


def get_table_relations(table_name: str) -> list[dict]:
    """查询表的外键关系，辅助理解表间关联。"""
    sql = """
        SELECT
            fk.name AS constraint_name,
            OBJECT_NAME(fk.parent_object_id) AS from_table,
            COL_NAME(fkc.parent_object_id, fkc.parent_column_id) AS from_column,
            OBJECT_NAME(fk.referenced_object_id) AS to_table,
            COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS to_column
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
        WHERE OBJECT_NAME(fk.parent_object_id) = ?
           OR OBJECT_NAME(fk.referenced_object_id) = ?
        ORDER BY from_table, from_column
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql, (table_name, table_name))
    columns = [col[0] for col in cursor.description] if cursor.description else []
    rows = []
    for row in cursor.fetchall():
        rows.append({col: _serialize_value(val) for col, val in zip(columns, row)})
    conn.close()
    return rows
