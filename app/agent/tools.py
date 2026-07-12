"""LangChain Tools — 包装 SQL Server 操作为 Agent 可调用的工具。"""
from langchain.tools import tool
from app.db.sqlserver import check_syntax, execute_query, get_table_columns, get_tables


@tool
def run_sql_tool(sql: str) -> str:
    """在 SQL Server 上执行只读查询，返回结果集。仅用于 SELECT 语句。
    参数 sql: 要执行的 SELECT 语句。"""
    upper = sql.strip().upper()
    if not upper.startswith("SELECT") and not upper.startswith("WITH"):
        return "错误：仅允许执行 SELECT 查询"
    try:
        rows = execute_query(sql)
        if not rows:
            return "查询返回空结果集"
        return str(rows[:50])
    except Exception as e:
        return f"查询执行失败: {e}"


@tool
def get_table_info_tool(table_name: str) -> str:
    """获取指定表的列信息（列名、数据类型、长度）。
    参数 table_name: 表名，如 'OINV', 'INV1'。"""
    try:
        cols = get_table_columns(table_name)
        if not cols:
            return f"未找到表 {table_name} 的列信息"
        lines = [f"{c['COLUMN_NAME']}: {c['DATA_TYPE']}" for c in cols]
        return "\n".join(lines)
    except Exception as e:
        return f"查询失败: {e}"


@tool
def get_table_list_tool(_dummy: str = "") -> str:
    """获取当前数据库中所有用户表列表。"""
    try:
        tables = get_tables()
        return "\n".join(tables)
    except Exception as e:
        return f"查询失败: {e}"


def create_tools() -> list:
    return [run_sql_tool, get_table_info_tool, get_table_list_tool]
