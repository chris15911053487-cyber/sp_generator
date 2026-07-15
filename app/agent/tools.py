"""LangChain Tools — 包装 SQL Server 操作为 Agent 可调用的工具。"""
from langchain.tools import tool
from app.db.sqlserver import (
    check_syntax, execute_query, get_table_columns, get_tables, get_table_relations,
)


@tool
def run_sql_tool(sql: str) -> str:
    """在 SQL Server 上执行只读查询，返回结果集。仅用于单条 SELECT 语句。
    参数 sql: 要执行的 SELECT 语句。"""
    stripped = sql.strip()

    # 防护 1: 禁止分号（阻止多语句注入）
    if ";" in stripped.rstrip(";"):
        return "错误：仅允许执行单条 SQL 语句"

    upper = stripped.upper()

    # 防护 2: 必须以 SELECT 或 WITH 开头
    if not upper.startswith("SELECT") and not upper.startswith("WITH"):
        return "错误：仅允许执行 SELECT 查询"

    # 防护 3: 关键字黑名单
    forbidden = [
        "INTO ", "INSERT ", "UPDATE ", "DELETE ", "DROP ",
        "ALTER ", "EXEC ", "EXECUTE ", "XP_", "TRUNCATE ",
    ]
    for kw in forbidden:
        if kw in upper:
            return f"错误：SQL 中包含禁止的关键字 {kw.strip()}"

    try:
        rows = execute_query(stripped)
        if not rows:
            return "查询返回空结果集"
        return str(rows[:50])  # 限制返回行数
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


@tool
def check_syntax_tool(sql: str) -> str:
    """校验 T-SQL 语法是否正确，使用 SET PARSEONLY ON 检查。
    参数 sql: 要校验的 SQL 语句。"""
    ok, err = check_syntax(sql)
    if ok:
        return "语法校验通过"
    return f"语法错误: {err}"


@tool
def get_table_relations_tool(table_name: str) -> str:
    """查询表的外键关系，了解表间如何关联。
    参数 table_name: 表名，如 'OINV', 'INV1'。"""
    try:
        rels = get_table_relations(table_name)
        if not rels:
            return f"表 {table_name} 没有外键关系"
        lines = []
        for r in rels:
            lines.append(
                f"{r['from_table']}.{r['from_column']} → "
                f"{r['to_table']}.{r['to_column']}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"查询失败: {e}"


def create_tools() -> list:
    return [
        run_sql_tool, get_table_info_tool, get_table_list_tool,
        check_syntax_tool, get_table_relations_tool,
    ]
