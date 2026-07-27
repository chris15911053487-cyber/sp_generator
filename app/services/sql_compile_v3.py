"""V3 SQL Server 静态编译门。"""

from __future__ import annotations

from collections.abc import Callable


Compiler = Callable[[str, str, str, list[dict] | None], dict]


class CompileContractError(ValueError):
    def __init__(self, code: str, message: str, *, evidence: dict | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def parameter_definitions(contract) -> list[dict]:
    types = {
        "string": "nvarchar(4000)",
        "integer": "bigint",
        "decimal": "decimal(38,10)",
        "money": "decimal(19,4)",
        "date": "date",
        "datetime": "datetime2(7)",
        "boolean": "bit",
    }
    return [
        {"name": item.name, "type": types[item.logical_type]}
        for item in contract.parameters
    ]


def _logical_sql_type(sql_type: str) -> str:
    base = str(sql_type).casefold().split("(", 1)[0].strip()
    if base == "date":
        return "date"
    if base in {"datetime", "datetime2", "smalldatetime", "datetimeoffset"}:
        return "datetime"
    if base == "bit":
        return "boolean"
    if base in {"tinyint", "smallint", "int", "bigint"}:
        return "integer"
    if base in {"money", "smallmoney"}:
        return "money"
    if base in {"decimal", "numeric", "float", "real"}:
        return "decimal"
    if base in {
        "char", "nchar", "varchar", "nvarchar", "text", "ntext",
        "uniqueidentifier",
    }:
        return "string"
    return base


def validate_compiled_result_schema(
    result: dict,
    expected_schema,
    *,
    artifact: str,
) -> None:
    """对 SQL Server 返回的静态结果元数据做名称、顺序、类型和空值检查。"""
    if "result_columns" not in result:
        # 仅供不连接 SQL Server 的单元测试注入器使用；生产编译器总会返回该字段。
        return
    actual = result.get("result_columns") or []
    if len(actual) != len(expected_schema):
        raise CompileContractError(
            "RESULT_COLUMN_COUNT_MISMATCH",
            f"{artifact} 编译结果列数与关系计划不一致",
            evidence={
                "expected": [item.model_dump(mode="json") for item in expected_schema],
                "actual": actual,
            },
        )
    compatible = {
        "money": {"money", "decimal"},
        "decimal": {"decimal", "money"},
    }
    errors = []
    for index, (actual_column, expected_column) in enumerate(
        zip(actual, expected_schema)
    ):
        actual_name = str(actual_column.get("name") or "")
        actual_type = _logical_sql_type(actual_column.get("sql_type") or "")
        allowed_types = compatible.get(
            expected_column.logical_type,
            {expected_column.logical_type},
        )
        if actual_name.casefold() != expected_column.name.casefold():
            errors.append(
                {
                    "index": index,
                    "kind": "name",
                    "expected": expected_column.name,
                    "actual": actual_name,
                }
            )
        if actual_type not in allowed_types:
            errors.append(
                {
                    "index": index,
                    "kind": "type",
                    "expected": expected_column.logical_type,
                    "actual": actual_column.get("sql_type"),
                }
            )
        if (
            not expected_column.nullable
            and bool(actual_column.get("nullable"))
        ):
            errors.append(
                {
                    "index": index,
                    "kind": "nullable",
                    "expected": False,
                    "actual": True,
                }
            )
    if errors:
        raise CompileContractError(
            "RESULT_COMPILED_SCHEMA_MISMATCH",
            f"{artifact} 编译结果结构与关系计划不一致",
            evidence={"errors": errors},
        )


def compile_reference(
    sql: str,
    contract,
    compiler: Compiler | None = None,
) -> dict:
    if compiler is None:
        from app.db.sqlserver import compile_candidate

        compiler = compile_candidate
    result = compiler(
        "reference",
        "reference",
        sql,
        parameter_definitions(contract),
    )
    return {
        **result,
        "artifact": "reference",
        "executed": False,
    }


def compile_procedure(
    name: str,
    sql: str,
    contract,
    compiler: Compiler | None = None,
) -> dict:
    if compiler is None:
        from app.db.sqlserver import compile_candidate

        compiler = compile_candidate
    result = compiler(
        "procedure",
        name,
        sql,
        parameter_definitions(contract),
    )
    return {
        **result,
        "artifact": "procedure",
        "executed": False,
    }
