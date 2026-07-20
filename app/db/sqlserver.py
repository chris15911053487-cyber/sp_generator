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
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={cfg['server']},{cfg['port']};"
        f"DATABASE={cfg['database']};"
        f"UID={cfg['user']};"
        f"PWD={cfg['password']};"
        "Encrypt=no;TrustServerCertificate=yes;"
        "Connection Timeout=10;"
    )


def get_connection(*, autocommit: bool = True) -> pyodbc.Connection:
    return pyodbc.connect(_build_conn_str(), autocommit=autocommit)


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
    """用 SET PARSEONLY ON 检查 SQL 语法。返回 (通过, 错误信息)。

    对 ALTER PROCEDURE 做兼容处理：如果 SP 在服务器上不存在，
    临时将 ALTER 转为 CREATE 来做语法检查（仅检查语法，不实际执行）。
    """
    conn = get_connection()
    cursor = conn.cursor()

    # 判断是否是 ALTER PROCEDURE，如果是且 SP 不存在则转为 CREATE 检查语法
    sql_to_check = sql
    sql_stripped = sql.strip().upper()
    if sql_stripped.startswith("ALTER") and ("PROC" in sql_stripped[:50]):
        # 提取 SP 名称并检查是否存在
        m = re.match(r'(?i)^\s*ALTER\s+PROC(?:EDURE)?\s+(?:\[?(\w+)\]?\.)?(\[?\w+\]?)', sql)
        if m:
            sp_name = m.group(2).strip("[]") if m.group(2) else ""
            if sp_name:
                try:
                    cursor.execute("SELECT 1 FROM sys.procedures WHERE name = ?", (sp_name,))
                    if cursor.fetchone() is None:
                        # SP 不存在，临时转为 CREATE 来做语法检查
                        sql_to_check = re.sub(r'(?i)^\s*ALTER\s+PROC(EDURE)?', 'CREATE PROCEDURE', sql, count=1)
                except Exception:
                    pass

    try:
        cursor.execute("SET PARSEONLY ON")
        cursor.execute(sql_to_check)
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


def _deployable_code(name: str, code: str) -> str:
    """将已校验代码规范为 CREATE OR ALTER，并校验代码内过程名。"""
    name = _validate_sp_name(name)
    match = re.match(
        r'(?is)^\s*(?:CREATE\s+OR\s+ALTER|CREATE|ALTER)\s+PROC(?:EDURE)?\s+'
        r'((?:\[[^\]]+\]|[A-Za-z_][\w$#]*)(?:\.(?:\[[^\]]+\]|[A-Za-z_][\w$#]*))?)',
        code,
    )
    if not match:
        raise ValueError("部署代码不是有效的存储过程定义")
    code_name = match.group(1).replace("[", "").replace("]", "")
    record_name = name.lower()
    definition_name = code_name.lower()
    record_base = record_name[4:] if record_name.startswith("dbo.") else record_name
    definition_base = (
        definition_name[4:] if definition_name.startswith("dbo.") else definition_name
    )
    if record_base != definition_base:
        raise ValueError(f"记录名称 {name} 与代码过程名 {code_name} 不一致")
    return re.sub(
        r'(?is)^\s*(?:CREATE\s+OR\s+ALTER|CREATE|ALTER)\s+PROC(?:EDURE)?',
        'CREATE OR ALTER PROCEDURE', code, count=1,
    )


def deploy_procedures_atomically(procedures: list[dict]) -> list[dict]:
    """在一个事务中部署全部 SP，任一失败则整体回滚。"""
    prepared = [
        (item["id"], item["name"], _deployable_code(item["name"], item["code"]))
        for item in procedures
    ]
    conn = get_connection(autocommit=False)
    results = []
    try:
        cursor = conn.cursor()
        cursor.execute("SET XACT_ABORT ON")
        for sp_id, name, code in prepared:
            cursor.execute(code)
            results.append({"sp_id": sp_id, "name": name, "success": True, "error": ""})
        conn.commit()
        return results
    except Exception as exc:
        conn.rollback()
        error = str(exc)
        return [
            {"sp_id": sp_id, "name": name, "success": False, "error": error}
            for sp_id, name, _ in prepared
        ]
    finally:
        conn.close()

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


def execute_sp_with_params(sp_name: str, params: dict, param_defs: list = None) -> list[dict]:
    """执行存储过程并返回结果集。

    Args:
        sp_name: 存储过程名称
        params: 参数字典 {name: value}
        param_defs: 参数定义列表 [{"name": ..., "type": ..., "default": ...}]
    Returns:
        字典列表结果集
    Raises:
        Exception: 执行失败时抛出
    """
    # 构建 EXEC 语句
    param_parts = []
    if param_defs:
        for p in param_defs:
            pname = p.get("name", "")
            if not pname.startswith("@"):
                pname = "@" + pname
            key = pname.lstrip("@")
            if key in params:
                val = params[key]
                if val is None or val == "":
                    param_parts.append(f"{pname} = NULL")
                elif isinstance(val, (int, float)):
                    param_parts.append(f"{pname} = {val}")
                else:
                    escaped = str(val).replace("'", "''")
                    param_parts.append(f"{pname} = '{escaped}'")
    elif params:
        for key, val in params.items():
            pname = key if key.startswith("@") else f"@{key}"
            if val is None or val == "":
                param_parts.append(f"{pname} = NULL")
            elif isinstance(val, (int, float)):
                param_parts.append(f"{pname} = {val}")
            else:
                escaped = str(val).replace("'", "''")
                param_parts.append(f"{pname} = '{escaped}'")

    safe_name = sp_name.replace("]", "]]")
    exec_sql = f"EXEC [{safe_name}]"
    if param_parts:
        exec_sql += " " + ", ".join(param_parts)

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(exec_sql)
    columns = [col[0] for col in cursor.description] if cursor.description else []
    rows = []
    if columns:
        for row in cursor.fetchall():
            rows.append({col: _serialize_value(val) for col, val in zip(columns, row)})
    conn.close()
    return rows


def compare_sp_results(sp_rows: list[dict], verify_rows: list[dict],
                       compare_columns: str) -> dict:
    """对比 SP 执行结果和校验 SQL 执行结果的指定列。

    Args:
        sp_rows: SP 执行结果集
        verify_rows: 校验 SQL 执行结果集
        compare_columns: 逗号分隔的列名，如 "TotalAmount,InvoiceCount"
    Returns:
        {
            "match": True/False,
            "details": [...],  # 每列的对比详情
            "summary": "..."   # 汇总文字
        }
    """
    if not compare_columns or not compare_columns.strip():
        return {"match": True, "details": [], "summary": "未指定对比列，跳过数据对比"}

    columns = [c.strip() for c in compare_columns.split(",") if c.strip()]
    if not columns:
        return {"match": True, "details": [], "summary": "未指定对比列，跳过数据对比"}

    details = []
    all_match = True

    for col in columns:
        # 在 SP 结果中查找该列（不区分大小写）
        sp_val = _find_column_value(sp_rows, col)
        vq_val = _find_column_value(verify_rows, col)

        # 对比值
        match = _values_match(sp_val, vq_val)
        if not match:
            all_match = False

        details.append({
            "column": col,
            "sp_value": sp_val,
            "verify_value": vq_val,
            "match": match,
        })

    # 生成汇总
    if all_match:
        summary = f"✅ 数据一致（对比了 {len(columns)} 列）"
    else:
        mismatched = [d for d in details if not d["match"]]
        summary = f"❌ 数据不一致（{len(mismatched)}/{len(columns)} 列不匹配）"
        for d in mismatched:
            summary += f"\n  · {d['column']}: SP={d['sp_value']} vs 校验={d['verify_value']}"

    return {"match": all_match, "details": details, "summary": summary}


def _find_column_value(rows: list[dict], col_name: str):
    """从结果集中查找指定列的值。支持大小写不敏感匹配。

    对于单行结果，直接返回该列值。
    对于多行结果，返回值的列表。
    """
    if not rows:
        return None

    # 找到实际的列名（大小写不敏感）
    actual_col = None
    if rows:
        for key in rows[0].keys():
            if key.lower() == col_name.lower():
                actual_col = key
                break
    if actual_col is None:
        return None

    if len(rows) == 1:
        return rows[0].get(actual_col)
    else:
        return [row.get(actual_col) for row in rows]


def _values_match(val1, val2) -> bool:
    """对比两个值是否匹配，支持数值容差和类型转换。"""
    # 都是 None
    if val1 is None and val2 is None:
        return True
    if val1 is None or val2 is None:
        return False

    # 列表对比
    if isinstance(val1, list) and isinstance(val2, list):
        if len(val1) != len(val2):
            return False
        return all(_values_match(a, b) for a, b in zip(val1, val2))

    # 单值与列表（如 SP 返回 1 行，校验返回多行的同一聚合列）
    if isinstance(val1, list) and not isinstance(val2, list):
        if len(val1) == 1:
            return _values_match(val1[0], val2)
        return False
    if isinstance(val2, list) and not isinstance(val1, list):
        if len(val2) == 1:
            return _values_match(val1, val2[0])
        return False

    # 尝试数值对比（允许 0.01 容差，覆盖浮点精度问题）
    try:
        n1 = float(val1)
        n2 = float(val2)
        if n1 == 0 and n2 == 0:
            return True
        # 相对误差 < 0.001% 或绝对误差 < 0.01
        if abs(n1 - n2) < 0.01:
            return True
        if max(abs(n1), abs(n2)) > 0 and abs(n1 - n2) / max(abs(n1), abs(n2)) < 0.00001:
            return True
        return False
    except (ValueError, TypeError):
        pass

    # 字符串对比（去空格、不区分大小写）
    return str(val1).strip().lower() == str(val2).strip().lower()
