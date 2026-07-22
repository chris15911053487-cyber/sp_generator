"""统一的 SP 语法、执行和业务校验服务。"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from app.db.sqlserver import _serialize_value, check_syntax, get_connection
from config import get_db_config, is_explicit_test_database

ALLOWED_OPERATION_TYPES = {"query", "insert", "update", "delete", "mixed"}
DIRECT_COMPARE_MODES = {"scalar", "aggregate", "keyed_rows"}
WRITE_COMPARE_MODE = "change_set"
MAX_RESULT_ROWS = 50000
QUERY_TIMEOUT_SECONDS = 60


class ValidationError(ValueError):
    """校验输入或安全检查失败。"""


def _json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"校验规格不是有效 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("校验规格必须是 JSON 对象")
    return parsed


def normalize_verify_queries(verify_queries: list[dict]) -> list[dict]:
    normalized = []
    for query in verify_queries:
        item = dict(query)
        spec = _json_object(item.get("validation_spec"))
        if not spec.get("mode"):
            spec["mode"] = "scalar" if item.get("compare_columns") else "zero_rows"
        if not spec.get("compare_columns") and item.get("compare_columns"):
            spec["compare_columns"] = [
                col.strip() for col in item["compare_columns"].split(",") if col.strip()
            ]
        spec.setdefault("required", True)
        item["validation_spec"] = spec
        normalized.append(item)
    return normalized


def compute_bundle_hash(sp: dict, verify_queries: list[dict]) -> str:
    normalized_queries = normalize_verify_queries(verify_queries)
    payload = {
        "name": sp.get("name", ""),
        "code": sp.get("code", "").strip(),
        "parameters": _normalized_json_value(sp.get("parameters", "[]")),
        "operation_type": sp.get("operation_type", "query"),
        "query_spec_json": _normalized_json_value(sp.get("query_spec_json", "{}")),
        "schema_fingerprint": sp.get("schema_fingerprint", ""),
        "verify_queries": [
            {
                "name": query.get("name", ""),
                "sql_code": query.get("sql_code", "").strip(),
                "compare_columns": query.get("compare_columns", ""),
                "validation_spec": query["validation_spec"],
            }
            for query in normalized_queries
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalized_json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _strip_comments_and_literals(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\r\n]*", " ", sql)
    sql = re.sub(r"N?'(?:''|[^'])*'", "''", sql, flags=re.IGNORECASE)
    return sql


def _normalized_identifier(value: str) -> str:
    return value.replace("[", "").replace("]", "").strip().lower()


def _procedure_parts(code: str) -> tuple[str, str]:
    clean = re.sub(r"(?im)^\s*GO\s*;?\s*$", "", code).strip()
    match = re.match(
        r"(?is)^\s*(?:CREATE\s+OR\s+ALTER|CREATE|ALTER)\s+PROC(?:EDURE)?\s+"
        r"((?:\[[^\]]+\]|[A-Za-z_][\w$#]*)(?:\.(?:\[[^\]]+\]|[A-Za-z_][\w$#]*))?)(?=\s|\()",
        clean,
    )
    if not match:
        raise ValidationError("SP 必须以 CREATE/ALTER PROCEDURE 开头")
    as_match = re.search(r"(?i)\bAS\b", clean[match.end():])
    if not as_match:
        raise ValidationError("SP 缺少 AS 主体")
    body_start = match.end() + as_match.end()
    return clean, clean[body_start:]


def _collect_specs(verify_queries: list[dict]) -> list[dict]:
    return [query["validation_spec"] for query in normalize_verify_queries(verify_queries)]


def _declared_tables(specs: list[dict]) -> set[str]:
    tables = set()
    for spec in specs:
        for item in spec.get("affected_tables", []):
            if isinstance(item, str):
                tables.add(_normalized_identifier(item))
            elif isinstance(item, dict) and item.get("table"):
                tables.add(_normalized_identifier(item["table"]))
    return tables


def _change_target(spec: dict) -> dict:
    targets = spec.get("affected_tables") or []
    if len(targets) != 1 or not isinstance(targets[0], dict):
        raise ValidationError("每条 change_set 规则必须且只能声明一个 affected_tables 对象")
    target = targets[0]
    required = ("table", "operation", "key_columns", "compare_columns",
                "max_affected_rows")
    missing = [name for name in required if target.get(name) in (None, "")]
    if missing:
        raise ValidationError(f"change_set 目标表缺少字段: {', '.join(missing)}")
    if target["operation"] not in {"insert", "update", "delete"}:
        raise ValidationError("change_set operation 必须是 insert、update 或 delete")
    if not isinstance(target["key_columns"], list) or not target["key_columns"]:
        raise ValidationError("change_set key_columns 必须是非空数组")
    if not isinstance(target["compare_columns"], list):
        raise ValidationError("change_set compare_columns 必须是数组")
    if target["operation"] == "update" and not target["compare_columns"]:
        raise ValidationError("update change_set 必须声明 compare_columns")
    try:
        maximum = int(target["max_affected_rows"])
    except (TypeError, ValueError) as exc:
        raise ValidationError("change_set max_affected_rows 必须是正整数") from exc
    if maximum <= 0:
        raise ValidationError("change_set max_affected_rows 必须是正整数")
    if not spec.get("snapshot_sql"):
        raise ValidationError("change_set 缺少 snapshot_sql")
    return target


def validate_reporting_procedure(code: str, operation_type: str,
                                 verify_queries: list[dict]) -> dict:
    if operation_type not in ALLOWED_OPERATION_TYPES:
        raise ValidationError(f"不支持的 SP 操作类型: {operation_type}")
    clean, body = _procedure_parts(code)
    inspected = _strip_comments_and_literals(body)
    upper = inspected.upper()

    forbidden = {
        "动态 SQL": r"\b(?:EXEC|EXECUTE)\b|\bSP_EXECUTESQL\b",
        "事务控制": r"\b(?:BEGIN\s+TRAN(?:SACTION)?|COMMIT|ROLLBACK|SAVE\s+TRAN(?:SACTION)?)\b",
        "数据库级操作": r"\b(?:ALTER|CREATE|DROP)\s+DATABASE\b|\b(?:BACKUP|RESTORE)\b",
        "对象级 DDL": r"\b(?:CREATE|ALTER|DROP)\s+(?:PROC(?:EDURE)?|VIEW|FUNCTION|TRIGGER|INDEX|SCHEMA|USER|ROLE)\b",
        "上下文或管理操作": r"\b(?:USE|DBCC|KILL|SHUTDOWN)\b",
        "权限操作": r"\b(?:GRANT|DENY|REVOKE|CREATE\s+LOGIN|ALTER\s+LOGIN)\b",
        "外部调用": r"\b(?:XP_CMDSHELL|OPENQUERY|OPENROWSET|OPENDATASOURCE|BULK\s+INSERT)\b",
        "高风险清空": r"\bTRUNCATE\s+TABLE\b",
        "全局临时表": r"##[A-Za-z_]\w*",
        "MERGE（首期不支持）": r"\bMERGE\b",
    }
    for label, pattern in forbidden.items():
        if re.search(pattern, upper, flags=re.IGNORECASE):
            raise ValidationError(f"SP 包含禁止的{label}")

    identifier = r"(?:\[[^\]]+\]|[A-Za-z_][\w$]*)"
    if re.search(
        rf"{identifier}\s*\.\s*{identifier}\s*\.\s*{identifier}\s*\.\s*{identifier}",
        inspected,
    ):
        raise ValidationError("SP 不允许访问链接服务器")

    for pattern, label in (
        (r"\b(?:CREATE|DROP)\s+TABLE\s+([^\s(;]+)", "表 DDL"),
        (r"\bALTER\s+TABLE\s+([^\s(;]+)", "表结构修改"),
    ):
        for match in re.finditer(pattern, inspected, flags=re.IGNORECASE):
            target = _normalized_identifier(match.group(1))
            if not target.startswith("#"):
                raise ValidationError(f"SP 只能对本地临时表执行{label}: {target}")

    for match in re.finditer(
        r"\bSELECT\b[^;]*?\bINTO\s+([^\s(;]+)", inspected,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        target = _normalized_identifier(match.group(1))
        if not target.startswith(("#", "@")):
            raise ValidationError(f"SELECT INTO 只能写入本地临时对象: {target}")

    dml_targets: list[tuple[str, str]] = []
    for operation, pattern in (
        ("insert", r"\bINSERT\s+(?:INTO\s+)?([^\s(;]+)"),
        ("update", r"\bUPDATE\s+([^\s(;]+)"),
        ("delete", r"\bDELETE\s+(?:FROM\s+)?([^\s(;]+)"),
    ):
        for match in re.finditer(pattern, inspected, flags=re.IGNORECASE):
            target = _normalized_identifier(match.group(1))
            if target.startswith(("#", "@")):
                continue
            if target.count(".") > 1:
                raise ValidationError(f"SP 不允许跨数据库或链接服务器写入: {target}")
            dml_targets.append((operation, target))

    specs = _collect_specs(verify_queries)
    declared_tables = _declared_tables(specs)
    if operation_type == "query" and dml_targets:
        raise ValidationError("query 类型 SP 不允许修改正式表")
    if operation_type != "query" and not dml_targets:
        raise ValidationError(f"{operation_type} 类型 SP 未检测到正式表写入")
    if dml_targets and not declared_tables:
        raise ValidationError("写入型 SP 必须在校验规格中声明 affected_tables")
    undeclared = sorted({target for _, target in dml_targets if target not in declared_tables})
    if undeclared:
        raise ValidationError(f"SP 写入了未声明的表: {', '.join(undeclared)}")

    detected_ops = {operation for operation, _ in dml_targets}
    if operation_type in {"insert", "update", "delete"} and detected_ops != {operation_type}:
        raise ValidationError(
            f"声明类型为 {operation_type}，实际写操作为 {', '.join(sorted(detected_ops))}"
        )
    if operation_type == "mixed" and len(detected_ops) < 2:
        raise ValidationError("mixed 类型必须包含至少两种正式表写操作")

    if operation_type != "query":
        declared_pairs = set()
        for spec in specs:
            if spec.get("mode") != "change_set" or not spec.get("required", True):
                continue
            target = _change_target(spec)
            declared_pairs.add(
                (target["operation"], _normalized_identifier(target["table"]))
            )
        actual_pairs = set(dml_targets)
        if declared_pairs != actual_pairs:
            missing = sorted(actual_pairs - declared_pairs)
            extra = sorted(declared_pairs - actual_pairs)
            raise ValidationError(
                f"change_set 与实际写操作不一致；缺少 {missing or '无'}；"
                f"多余 {extra or '无'}"
            )

    return {"code": clean, "dml_targets": dml_targets}


def validate_readonly_query(sql: str) -> None:
    inspected = _strip_comments_and_literals(sql).strip()
    if not re.match(r"(?is)^(SELECT\b|WITH\b)", inspected):
        raise ValidationError("校验 SQL 只允许单条 SELECT 或 WITH 查询")
    if ";" in inspected.rstrip().rstrip(";"):
        raise ValidationError("校验 SQL 只允许单条语句")
    forbidden = (
        r"\b(?:INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CREATE|ALTER|DROP|INTO)\b",
        r"\b(?:EXEC|EXECUTE|SP_EXECUTESQL|DECLARE|GRANT|DENY|REVOKE)\b",
        r"\b(?:XP_CMDSHELL|OPENQUERY|OPENROWSET|OPENDATASOURCE|BULK\s+INSERT)\b",
    )
    if any(re.search(pattern, inspected, flags=re.IGNORECASE) for pattern in forbidden):
        raise ValidationError("校验 SQL 包含写入、执行或外部操作")
    if re.search(r"(?<!#)##?[A-Za-z_]\w*", inspected):
        raise ValidationError("校验 SQL 不允许使用临时表")
    identifier = r"(?:\[[^\]]+\]|[A-Za-z_][\w$]*)"
    if re.search(
        rf"{identifier}\s*\.\s*{identifier}\s*\.\s*{identifier}\s*\.\s*{identifier}",
        inspected,
    ):
        raise ValidationError("校验 SQL 不允许访问链接服务器")


def _temporary_procedure(code: str) -> tuple[str, str]:
    temp_name = "#verify_" + uuid.uuid4().hex[:16]
    rewritten = re.sub(
        r"(?is)^\s*(?:CREATE\s+OR\s+ALTER|CREATE|ALTER)\s+PROC(?:EDURE)?\s+"
        r"(?:\[[^\]]+\]|[A-Za-z_][\w$#]*)(?:\.(?:\[[^\]]+\]|[A-Za-z_][\w$#]*))?",
        f"CREATE PROCEDURE {temp_name}",
        code,
        count=1,
    )
    return temp_name, rewritten


def _bound_sql(sql: str, params: dict) -> tuple[str, list[Any]]:
    values = []

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in params:
            raise ValidationError(f"缺少校验参数: {key}")
        values.append(params[key])
        return "?"

    return re.sub(r"\{(\w+)\}", replace, sql), values


def _parameter_defs(sp: dict) -> list[dict]:
    try:
        parsed = json.loads(sp.get("parameters") or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _exec_statement(temp_name: str, sp: dict, params: dict) -> tuple[str, list[Any]]:
    parts = []
    values = []
    for definition in _parameter_defs(sp):
        name = str(definition.get("name", "")).lstrip("@")
        if not name:
            continue
        if name in params:
            value = params[name]
        elif definition.get("default") not in (None, ""):
            value = definition["default"]
        else:
            continue
        parts.append(f"@{name} = ?")
        values.append(value)
    sql = f"EXEC {temp_name}"
    if parts:
        sql += " " + ", ".join(parts)
    return sql, values


def _fetch_rows(cursor, max_rows: int = MAX_RESULT_ROWS) -> list[dict]:
    while cursor.description is None:
        if not cursor.nextset():
            return []
    columns = [column[0] for column in cursor.description]
    rows = cursor.fetchmany(max_rows + 1)
    if len(rows) > max_rows:
        raise ValidationError(f"结果超过最大允许行数 {max_rows}")
    return [
        {column: _serialize_value(value) for column, value in zip(columns, row)}
        for row in rows
    ]


def _run_query(cursor, sql: str, params: dict) -> list[dict]:
    validate_readonly_query(sql)
    bound_sql, values = _bound_sql(sql, params)
    cursor.execute(bound_sql, values)
    return _fetch_rows(cursor)


def _find_value(row: dict, name: str) -> Any:
    for key, value in row.items():
        if key.lower() == name.lower():
            return value
    raise ValidationError(f"结果缺少比较列: {name}")


def _values_equal(actual: Any, expected: Any, tolerance: Any = 0) -> bool:
    if actual is None or expected is None:
        return actual is expected
    try:
        left = Decimal(str(actual))
        right = Decimal(str(expected))
        allowed = Decimal(str(tolerance or 0))
        return abs(left - right) <= allowed
    except (InvalidOperation, ValueError, TypeError):
        return str(actual).strip().lower() == str(expected).strip().lower()


def _contract_failure(message: str) -> dict:
    return {
        "match": False,
        "configuration_error": message,
        "summary": f"校验配置错误: {message}",
    }


def _column_pairs(spec: dict, field: str) -> list[tuple[str, str]]:
    items = spec.get(field) or []
    if not isinstance(items, list) or not items:
        raise ValidationError(f"{spec.get('mode', '校验')} 未指定 {field}")
    mapping = spec.get("column_mapping") or {}
    if not isinstance(mapping, dict):
        raise ValidationError("column_mapping 必须是对象")

    pairs = []
    for item in items:
        if isinstance(item, dict):
            actual_name = item.get("actual")
            expected_name = item.get("expected")
        elif isinstance(item, str):
            actual_name = item
            expected_name = next(
                (
                    value for key, value in mapping.items()
                    if str(key).lower() == item.lower()
                ),
                None,
            )
            if expected_name is None:
                inverse = next(
                    (
                        key for key, value in mapping.items()
                        if str(value).lower() == item.lower()
                    ),
                    None,
                )
                actual_name = inverse or item
                expected_name = item
        else:
            raise ValidationError(f"{field} 的元素必须是列名或 actual/expected 对象")
        if not isinstance(actual_name, str) or not isinstance(expected_name, str):
            raise ValidationError(f"{field} 的 actual 和 expected 必须是列名")
        pairs.append((actual_name, expected_name))
    return pairs


def _column_tolerance(tolerances: dict, actual_name: str, expected_name: str) -> Any:
    return tolerances.get(expected_name, tolerances.get(actual_name, 0))


def _aggregate_value(actual: list[dict], spec: dict) -> tuple[str, Any]:
    aggregate = spec.get("actual") or {}
    if not isinstance(aggregate, dict):
        raise ValidationError("aggregate.actual 必须是对象")
    operation = aggregate.get("operation")
    allowed = {"sum", "count_rows", "count_distinct", "min", "max", "avg"}
    if operation not in allowed:
        raise ValidationError(
            "aggregate.actual.operation 必须是 "
            "sum/count_rows/count_distinct/min/max/avg"
        )

    compare_pairs = _column_pairs(spec, "compare_columns")
    output_column = aggregate.get("output_column") or compare_pairs[0][0]
    if not isinstance(output_column, str):
        raise ValidationError("aggregate.actual.output_column 必须是列名")
    if operation == "count_rows":
        return output_column, len(actual)

    source_column = aggregate.get("column")
    if not isinstance(source_column, str) or not source_column:
        raise ValidationError(f"aggregate {operation} 必须指定 actual.column")
    values = [_find_value(row, source_column) for row in actual]
    non_null = [value for value in values if value is not None]

    if operation == "count_distinct":
        markers = set()
        for value in non_null:
            try:
                marker = ("number", Decimal(str(value)).normalize())
            except (InvalidOperation, ValueError, TypeError):
                marker = ("text", str(value).strip().lower())
            markers.add(marker)
        return output_column, len(markers)
    if not non_null:
        return output_column, None
    if operation in {"sum", "avg"}:
        try:
            numbers = [Decimal(str(value)) for value in non_null]
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValidationError(
                f"aggregate {operation} 的源列 {source_column} 包含非数值"
            ) from exc
        value = sum(numbers)
        if operation == "avg":
            value /= len(numbers)
        return output_column, _serialize_value(value)
    try:
        return output_column, min(non_null) if operation == "min" else max(non_null)
    except TypeError as exc:
        raise ValidationError(
            f"aggregate {operation} 的源列 {source_column} 类型不一致"
        ) from exc


def _compare_aggregate(actual: list[dict], expected: list[dict], spec: dict) -> dict:
    if len(expected) != 1:
        return _contract_failure("aggregate 校验要求 Expected 返回一行")
    try:
        output_column, value = _aggregate_value(actual, spec)
    except ValidationError as exc:
        return _contract_failure(str(exc))
    comparison_spec = dict(spec)
    comparison_spec["compare_columns"] = [output_column]
    comparison_spec.pop("column_mapping", None)
    comparison = _compare_scalar([{output_column: value}], expected, comparison_spec)
    comparison["actual_aggregate"] = {
        "operation": (spec.get("actual") or {}).get("operation"),
        "column": (spec.get("actual") or {}).get("column"),
        "value": value,
    }
    return comparison


def _compare_keyed_mapped(actual: list[dict], expected: list[dict], spec: dict) -> dict:
    try:
        key_pairs = _column_pairs(spec, "key_columns")
        column_pairs = _column_pairs(spec, "compare_columns")
        actual_map = {
            tuple(_find_value(row, actual_name) for actual_name, _ in key_pairs): row
            for row in actual
        }
        expected_map = {
            tuple(_find_value(row, expected_name) for _, expected_name in key_pairs): row
            for row in expected
        }
    except ValidationError as exc:
        return _contract_failure(str(exc))
    if len(actual_map) != len(actual) or len(expected_map) != len(expected):
        return {"match": False, "summary": "结果中存在重复业务键"}

    missing = sorted(set(expected_map) - set(actual_map), key=str)
    extra = sorted(set(actual_map) - set(expected_map), key=str)
    tolerances = spec.get("tolerance") or {}
    differences = []
    try:
        for key in set(actual_map) & set(expected_map):
            for actual_name, expected_name in column_pairs:
                left = _find_value(actual_map[key], actual_name)
                right = _find_value(expected_map[key], expected_name)
                if not _values_equal(
                    left,
                    right,
                    _column_tolerance(tolerances, actual_name, expected_name),
                ):
                    differences.append({
                        "key": key,
                        "column": expected_name,
                        "actual_column": actual_name,
                        "sp_value": left,
                        "verify_value": right,
                    })
                    if len(differences) >= 100:
                        break
    except ValidationError as exc:
        return _contract_failure(str(exc))

    match = not missing and not extra and not differences
    return {
        "match": match,
        "missing_keys": missing[:100],
        "extra_keys": extra[:100],
        "differences": differences[:100],
        "summary": "数据一致" if match else
                   f"缺失 {len(missing)}，多余 {len(extra)}，字段差异 {len(differences)}",
    }


def _compare_scalar(actual: list[dict], expected: list[dict], spec: dict) -> dict:
    if len(actual) != 1 or len(expected) != 1:
        return _contract_failure("scalar 校验要求 Actual 和 Expected 各返回一行")
    columns = spec.get("compare_columns") or []
    if not columns:
        return _contract_failure("scalar 校验未指定 compare_columns")
    tolerances = spec.get("tolerance") or {}
    details = []
    for column in columns:
        try:
            left = _find_value(actual[0], column)
            right = _find_value(expected[0], column)
            match = _values_equal(left, right, tolerances.get(column, 0))
            details.append({"column": column, "sp_value": left,
                            "verify_value": right, "match": match})
        except ValidationError as exc:
            return _contract_failure(str(exc))
    match = all(item["match"] for item in details)
    return {"match": match, "details": details,
            "summary": "数据一致" if match else "指标不一致"}


def _row_key(row: dict, columns: list[str]) -> tuple:
    return tuple(_find_value(row, column) for column in columns)


def _compare_keyed(actual: list[dict], expected: list[dict], spec: dict) -> dict:
    has_explicit_mapping = bool(spec.get("column_mapping")) or any(
        isinstance(item, dict)
        for field in ("key_columns", "compare_columns")
        for item in (spec.get(field) or [])
    )
    if has_explicit_mapping:
        return _compare_keyed_mapped(actual, expected, spec)
    keys = spec.get("key_columns") or []
    columns = spec.get("compare_columns") or []
    if not keys or not columns:
        return _contract_failure("keyed_rows 缺少 key_columns 或 compare_columns")
    try:
        for row in actual[:1]:
            for column in keys + columns:
                _find_value(row, column)
        for row in expected[:1]:
            for column in keys + columns:
                _find_value(row, column)
    except ValidationError as exc:
        return _contract_failure(str(exc))
    try:
        actual_map = {_row_key(row, keys): row for row in actual}
        expected_map = {_row_key(row, keys): row for row in expected}
    except ValidationError as exc:
        return _contract_failure(str(exc))
    if len(actual_map) != len(actual) or len(expected_map) != len(expected):
        return {"match": False, "summary": "结果中存在重复业务键"}
    missing = sorted(set(expected_map) - set(actual_map), key=str)
    extra = sorted(set(actual_map) - set(expected_map), key=str)
    tolerances = spec.get("tolerance") or {}
    differences = []
    for key in set(actual_map) & set(expected_map):
        for column in columns:
            left = _find_value(actual_map[key], column)
            right = _find_value(expected_map[key], column)
            if not _values_equal(left, right, tolerances.get(column, 0)):
                differences.append({"key": key, "column": column,
                                    "sp_value": left, "verify_value": right})
                if len(differences) >= 100:
                    break
    match = not missing and not extra and not differences
    return {
        "match": match,
        "missing_keys": missing[:100],
        "extra_keys": extra[:100],
        "differences": differences[:100],
        "summary": "数据一致" if match else
                   f"缺失 {len(missing)}，多余 {len(extra)}，字段差异 {len(differences)}",
    }


def _compare_rows(actual: list[dict], expected: list[dict], spec: dict) -> dict:
    mode = spec.get("mode")
    if mode == "scalar":
        return _compare_scalar(actual, expected, spec)
    if mode == "aggregate":
        return _compare_aggregate(actual, expected, spec)
    if mode in {"keyed_rows", "change_set"}:
        return _compare_keyed(actual, expected, spec)
    if mode == "zero_rows":
        match = len(expected) == 0
        return {"match": match, "unexpected_rows": expected[:20],
                "summary": "未发现异常" if match else f"发现 {len(expected)} 条异常"}
    return _contract_failure(f"不支持的比较模式: {mode}")


def _rows_by_key(rows: list[dict], keys: list[str], label: str) -> dict:
    result = {_row_key(row, keys): row for row in rows}
    if len(result) != len(rows):
        raise ValidationError(f"{label} 快照存在重复业务键")
    return result


def _change_set(before: list[dict], after: list[dict], target: dict) -> list[dict]:
    keys = target["key_columns"]
    columns = target["compare_columns"]
    operation = target["operation"]
    before_map = _rows_by_key(before, keys, "Before")
    after_map = _rows_by_key(after, keys, "After")
    if operation == "insert":
        changed_keys = set(after_map) - set(before_map)
    elif operation == "delete":
        changed_keys = set(before_map) - set(after_map)
    else:
        changed_keys = {
            key for key in set(before_map) & set(after_map)
            if any(
                not _values_equal(
                    _find_value(before_map[key], column),
                    _find_value(after_map[key], column),
                )
                for column in columns
            )
        }

    rows = []
    for key in sorted(changed_keys, key=str):
        old = before_map.get(key)
        new = after_map.get(key)
        item = {column: value for column, value in zip(keys, key)}
        item["ChangeType"] = operation
        for column in columns:
            item[f"Before_{column}"] = _find_value(old, column) if old else None
            item[f"After_{column}"] = _find_value(new, column) if new else None
        rows.append(item)
    return rows


def _compare_change_set(actual: list[dict], expected: list[dict], target: dict,
                        spec: dict) -> dict:
    compare_columns = [
        name for column in target["compare_columns"]
        for name in (f"Before_{column}", f"After_{column}")
    ]
    source_tolerances = spec.get("tolerance") or {}
    tolerances = {}
    for column in target["compare_columns"]:
        tolerance = source_tolerances.get(column, 0)
        tolerances[f"Before_{column}"] = tolerance
        tolerances[f"After_{column}"] = tolerance
    comparison_spec = {
        "key_columns": ["ChangeType"] + target["key_columns"],
        "compare_columns": compare_columns or target["key_columns"],
        "tolerance": tolerances,
    }
    return _compare_keyed(actual, expected, comparison_spec)


def _snapshot_matches(before: list[dict], restored: list[dict],
                      keys: list[str]) -> bool:
    before_map = _rows_by_key(before, keys, "Before")
    restored_map = _rows_by_key(restored, keys, "Restored")
    return before_map == restored_map


def validate_sp_bundle(sp: dict, verify_queries: list[dict], params: dict | None = None) -> dict:
    """完整校验一个 SP；写入型 SP 的所有变化都会在 finally 中回滚。"""
    params = params or {}
    queries = normalize_verify_queries(verify_queries)
    operation_type = sp.get("operation_type") or "query"
    bundle_hash = compute_bundle_hash(sp, queries)
    result = {
        "sp_id": sp.get("id"),
        "sp_name": sp.get("name", ""),
        "syntax_ok": False,
        "business_ok": False,
        "operation_type": operation_type,
        "bundle_hash": bundle_hash,
        "rolled_back": False,
        "restore_confirmed": operation_type == "query",
        "details": [],
    }

    if not queries:
        result["details"].append({"type": "configuration", "pass": False,
                                  "error": "没有校验 SQL，不能判定业务通过"})
        return result
    try:
        safety = validate_reporting_procedure(sp.get("code", ""), operation_type, queries)
        for query in queries:
            validate_readonly_query(query.get("sql_code", ""))
            spec = query["validation_spec"]
            if spec.get("mode") == "change_set":
                validate_readonly_query(spec.get("snapshot_sql", ""))
    except ValidationError as exc:
        result["details"].append({"type": "safety", "pass": False, "error": str(exc)})
        return result

    syntax_ok, syntax_error = check_syntax(safety["code"])
    result["syntax_ok"] = syntax_ok
    if not syntax_ok:
        result["details"].append({"type": "syntax", "pass": False, "error": syntax_error})
        return result

    required_modes = {
        query["validation_spec"].get("mode")
        for query in queries if query["validation_spec"].get("required", True)
    }
    required_direct = DIRECT_COMPARE_MODES if operation_type == "query" else {WRITE_COMPARE_MODE}
    if not required_modes.intersection(required_direct):
        label = "scalar/aggregate/keyed_rows" if operation_type == "query" else "change_set"
        result["details"].append({"type": "configuration", "pass": False,
                                  "error": f"缺少必选的 {label} 直接对账规则"})
        return result

    cfg = get_db_config()
    if operation_type != "query" and not is_explicit_test_database(cfg):
        result["details"].append({"type": "safety", "pass": False,
                                  "error": "写入型校验只允许在已明确配置的 environment=test 数据库执行"})
        return result

    conn = None
    change_contexts = {}
    try:
        conn = get_connection(autocommit=False)
        conn.timeout = QUERY_TIMEOUT_SECONDS
        cursor = conn.cursor()
        cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        cursor.execute("SET XACT_ABORT ON")
        cursor.execute("BEGIN TRANSACTION")

        cursor.execute("SELECT DB_NAME()")
        current_database = cursor.fetchone()[0]
        if cfg.get("database") and current_database.lower() != cfg["database"].lower():
            raise ValidationError("当前连接数据库与配置的测试数据库不一致")

        temp_name, temp_code = _temporary_procedure(safety["code"])
        cursor.execute(temp_code)

        if operation_type != "query":
            for query in queries:
                spec = query["validation_spec"]
                if spec.get("mode") != "change_set":
                    continue
                target = _change_target(spec)
                expected = _run_query(cursor, query["sql_code"], params)
                before = _run_query(cursor, spec["snapshot_sql"], params)
                change_contexts[query.get("id") or query["name"]] = (
                    expected, before, target, spec,
                )

        exec_sql, exec_values = _exec_statement(temp_name, sp, params)
        cursor.execute(exec_sql, exec_values)
        actual_rows = _fetch_rows(cursor) if operation_type == "query" else []

        all_required_pass = True
        for query in queries:
            spec = query["validation_spec"]
            mode = spec.get("mode")
            if mode == "change_set":
                key = query.get("id") or query["name"]
                expected, before, target, _ = change_contexts[key]
                after = _run_query(cursor, spec["snapshot_sql"], params)
                actual_change = _change_set(before, after, target)
                affected = len(actual_change)
                maximum = int(target["max_affected_rows"])
                comparison = _compare_change_set(
                    actual_change, expected, target, spec
                )
                if affected > maximum:
                    comparison = {"match": False,
                                  "summary": f"实际影响 {affected} 行，超过上限 {maximum}"}
                comparison["affected_rows"] = affected
            else:
                expected = _run_query(cursor, query["sql_code"], params)
                comparison = _compare_rows(actual_rows, expected, spec)
            passed = bool(comparison.get("match"))
            configuration_error = comparison.get("configuration_error")
            if spec.get("required", True) and not passed:
                all_required_pass = False
            result["details"].append({
                "type": "configuration" if configuration_error else "business",
                "query_id": query.get("id"),
                "query": query.get("name", "未命名校验"),
                "pass": passed,
                "comparison": comparison,
                "data": expected[:10],
            })
            if configuration_error:
                result["details"][-1]["error"] = configuration_error

        result["business_ok"] = all_required_pass
    except Exception as exc:
        result["business_ok"] = False
        result["details"].append({"type": "execution", "pass": False, "error": str(exc)})
    finally:
        if conn is not None:
            try:
                conn.rollback()
                result["rolled_back"] = True
                if operation_type != "query":
                    restored = bool(change_contexts)
                    try:
                        restore_cursor = conn.cursor()
                        for _, before, target, spec in change_contexts.values():
                            rows = _run_query(
                                restore_cursor, spec["snapshot_sql"], params
                            )
                            if not _snapshot_matches(
                                before, rows, target["key_columns"]
                            ):
                                restored = False
                                break
                    except Exception as exc:
                        restored = False
                        result["details"].append({
                            "type": "rollback", "pass": False,
                            "error": f"回滚后数据恢复检查失败: {exc}",
                        })
                    finally:
                        try:
                            conn.rollback()
                        except Exception:
                            restored = False
                    result["restore_confirmed"] = restored
                    if not restored:
                        result["business_ok"] = False
            except Exception as exc:
                result["rolled_back"] = False
                result["restore_confirmed"] = False
                result["business_ok"] = False
                result["details"].append({
                    "type": "rollback", "pass": False,
                    "error": f"回滚失败: {exc}",
                })
            finally:
                conn.close()
    return result
