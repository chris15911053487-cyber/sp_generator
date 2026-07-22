"""内存候选、确定性闸门与受约束修复流水线。"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.generation_harness import GateError, ProcedureSpec, QuerySpec
from app.services.schema_evidence import SchemaEvidence


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class VerifyQueryCandidate(_StrictModel):
    name: str = Field(min_length=1)
    sql_code: str = Field(min_length=1)
    compare_columns: str
    validation_spec: dict


class GateResult(_StrictModel):
    gate: Literal["query_spec", "schema", "safety", "compile", "contract", "business"]
    passed: bool
    errors: list[GateError] = Field(default_factory=list)
    details: dict = Field(default_factory=dict)


class CandidateBundle(_StrictModel):
    query_spec: QuerySpec
    procedure_spec: ProcedureSpec
    procedure_sql: str = Field(min_length=1)
    verify_queries: list[VerifyQueryCandidate] = Field(min_length=1)
    schema_evidence: SchemaEvidence
    gate_results: list[GateResult] = Field(default_factory=list)
    repair_count: int = 0
    bundle_hash: str = ""
    status: Literal[
        "candidate_generated", "validated", "needs_review", "failed",
    ] = "candidate_generated"

    def sp_dict(self) -> dict:
        return {
            "name": self.procedure_spec.name,
            "code": self.procedure_sql,
            "parameters": json.dumps(
                [
                    {
                        "name": item.name,
                        "type": item.sql_type,
                        "required": item.required,
                        "default": item.default,
                        "meaning": item.meaning,
                    }
                    for item in self.procedure_spec.parameters
                ],
                ensure_ascii=False,
            ),
            "operation_type": operation_type_for_spec(self.procedure_spec),
            "query_spec_json": self.query_spec.canonical_json(),
            "schema_fingerprint": self.schema_evidence.fingerprint,
        }

    def query_dicts(self) -> list[dict]:
        return [
            {
                "name": item.name,
                "sql_code": item.sql_code,
                "compare_columns": item.compare_columns,
                "validation_spec": item.validation_spec,
            }
            for item in self.verify_queries
        ]


Compiler = Callable[[str, str, str, list[dict]], dict]
BusinessValidator = Callable[[dict, list[dict], dict], dict]
Repairer = Callable[[CandidateBundle, list[GateError]], CandidateBundle]
SchemaRefresher = Callable[[QuerySpec], SchemaEvidence]


def operation_type_for_spec(spec: ProcedureSpec) -> str:
    if spec.operation_type == "reporting":
        return "query"
    operations = {item.operation for item in spec.writes}
    return next(iter(operations)) if len(operations) == 1 else "mixed"


def _error(
    artifact: str,
    category: str,
    code: str,
    message: str,
    repairable: bool,
    schema_subset: dict | list | None = None,
) -> GateError:
    return GateError(
        artifact=artifact,
        category=category,
        code=code,
        message=message,
        schema_subset=schema_subset,
        repairable=repairable,
    )


def _gate(name: str, errors: list[GateError] | None = None, **details) -> GateResult:
    errors = errors or []
    return GateResult(
        gate=name,
        passed=not errors,
        errors=errors,
        details=details,
    )


def _normalized_identifier(value: str) -> str:
    return ".".join(
        part.strip().strip("[]")
        for part in value.strip().split(".")
        if part.strip()
    )


def _procedure_header(sql: str) -> tuple[str, str]:
    match = re.match(
        r"(?is)^\s*(?:CREATE\s+OR\s+ALTER|CREATE|ALTER)\s+"
        r"PROC(?:EDURE)?\s+"
        r"((?:\[[^\]]+\]|[A-Za-z_][\w$#]*)(?:\.(?:\[[^\]]+\]|[A-Za-z_][\w$#]*))?)"
        r"(.*?)\bAS\b",
        sql,
    )
    if not match:
        raise ValueError("无法解析存储过程名称或参数")
    return _normalized_identifier(match.group(1)), match.group(2)


def _split_csv(value: str) -> list[str]:
    items = []
    start = 0
    depth = 0
    for index, character in enumerate(value):
        if character == "(":
            depth += 1
        elif character == ")":
            depth = max(0, depth - 1)
        elif character == "," and depth == 0:
            items.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail:
        items.append(tail)
    return items


def _parameter_signature(header: str) -> list[tuple[str, str, bool, str | None]]:
    signature = []
    for item in _split_csv(header):
        if not item:
            continue
        match = re.match(
            r"(?is)^(@[A-Za-z_][A-Za-z0-9_]*)\s+"
            r"([A-Za-z][A-Za-z0-9_]*(?:\s*\([^)]*\))?)"
            r"(?:\s*=\s*(.*?))?(?:\s+OUTPUT)?$",
            item,
        )
        if not match:
            raise ValueError(f"无法解析参数定义: {item}")
        default = match.group(3).strip() if match.group(3) is not None else None
        signature.append((
            match.group(1),
            re.sub(r"\s+", "", match.group(2)).upper(),
            default is None,
            default,
        ))
    return signature


def _normalized_default(value: Any | None) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", "", str(value)).strip()
    if text.upper() == "NULL":
        return "NULL"
    match = re.fullmatch(r"N?'(.*)'", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).replace("''", "'")
    return text


def _table_references(sql: str) -> set[str]:
    inspected = re.sub(r"/\*.*?\*/|--[^\r\n]*", " ", sql, flags=re.DOTALL)
    ctes = {
        match.group(1)
        for match in re.finditer(
            r"(?is)(?:\bWITH\b|,)\s*([A-Za-z_][\w$]*)\s+AS\s*\(",
            inspected,
        )
    }
    references = set()
    patterns = (
        r"\bFROM\s+([^\s,;()]+)",
        r"\bJOIN\s+([^\s,;()]+)",
        r"\bUPDATE\s+([^\s,;()]+)",
        r"\bINSERT\s+(?:INTO\s+)?([^\s,;()]+)",
        r"\bDELETE\s+FROM\s+([^\s,;()]+)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, inspected, flags=re.IGNORECASE):
            raw_identifier = match.group(1)
            identifier = _normalized_identifier(raw_identifier)
            is_bracketed_user_table = raw_identifier.lstrip().startswith("[@")
            if not identifier or identifier.startswith("#"):
                continue
            if identifier.startswith("@") and not is_bracketed_user_table:
                continue
            if identifier in ctes:
                continue
            references.add(identifier)
    return references


def _select_columns(sql: str) -> list[str]:
    matches = list(re.finditer(
        r"(?is)\bSELECT\s+(.*?)\s+FROM\b",
        sql,
    ))
    if not matches:
        return []
    projection = matches[-1].group(1)
    names = []
    for expression in _split_csv(projection):
        alias = re.search(
            r"(?is)\bAS\s+(\[[^\]]+\]|[A-Za-z_][\w$]*)\s*$",
            expression,
        )
        if alias:
            names.append(alias.group(1).strip("[]"))
            continue
        trailing = re.search(
            r"(\[[^\]]+\]|[A-Za-z_][\w$]*)\s*$",
            expression,
        )
        if trailing and "(" not in expression:
            names.append(trailing.group(1).strip("[]"))
    return names


def _sql_type_family(value: str) -> str:
    base = re.split(r"[\s(]", str(value).strip().upper(), maxsplit=1)[0]
    aliases = {
        "NUMERIC": "DECIMAL",
        "INTEGER": "INT",
        "ROWVERSION": "TIMESTAMP",
    }
    return aliases.get(base, base)


def _contract_errors(
    bundle: CandidateBundle, compiled: dict | None = None,
) -> list[GateError]:
    compiled = compiled or {}
    errors = []
    try:
        actual_name, header = _procedure_header(bundle.procedure_sql)
    except ValueError as exc:
        return [_error("procedure", "contract", "procedure_header", str(exc), True)]

    expected_name = bundle.procedure_spec.name
    if "." in expected_name:
        name_matches = actual_name == expected_name
    else:
        name_matches = actual_name.split(".")[-1] == expected_name
    if not name_matches:
        errors.append(_error(
            "procedure", "contract", "procedure_name",
            f"过程名 {actual_name} 与契约 {expected_name} 不一致", True,
        ))

    try:
        actual_parameters = _parameter_signature(header)
    except ValueError as exc:
        errors.append(_error(
            "procedure", "contract", "parameter_parse", str(exc), True,
        ))
        actual_parameters = []
    expected_parameters = [
        (
            item.name,
            re.sub(r"\s+", "", item.sql_type).upper(),
            item.required,
            (
                None if item.required
                else _normalized_default("NULL" if item.default is None else item.default)
            ),
        )
        for item in bundle.procedure_spec.parameters
    ]
    actual_parameters = [
        (*item[:3], _normalized_default(item[3])) for item in actual_parameters
    ]
    if len(actual_parameters) != len(expected_parameters):
        errors.append(_error(
            "procedure", "contract", "parameter_count",
            "参数数量或顺序与 QuerySpec 不一致", True,
        ))
    else:
        for actual, expected in zip(actual_parameters, expected_parameters):
            if actual != expected:
                errors.append(_error(
                    "procedure", "contract", "parameter_signature",
                    f"参数签名漂移: {actual[0]}", True,
                ))
                break

    allowed_tables = {
        f"{item.schema}.{item.table}"
        for item in bundle.procedure_spec.sources
    } | {
        f"{item.schema}.{item.table}"
        for item in bundle.procedure_spec.writes
    }
    for artifact, sql in [
        ("procedure", bundle.procedure_sql),
        *[("oracle", item.sql_code) for item in bundle.verify_queries],
    ]:
        unexpected = sorted(_table_references(sql) - allowed_tables)
        if unexpected:
            errors.append(_error(
                artifact, "contract", "unexpected_table",
                f"引用了 QuerySpec 未允许的表: {', '.join(unexpected)}", True,
            ))

    expected_outputs = [item.name for item in bundle.procedure_spec.outputs]
    procedure_columns = compiled.get(
        f"procedure:{bundle.procedure_spec.name}", {},
    ).get("result_columns") or []
    actual_outputs = (
        [item.get("name") for item in procedure_columns]
        if procedure_columns else _select_columns(bundle.procedure_sql)
    )
    if actual_outputs != expected_outputs:
        errors.append(_error(
            "procedure", "contract", "output_columns",
            f"输出列 {actual_outputs} 与契约 {expected_outputs} 不一致", True,
        ))
    elif procedure_columns:
        incompatible = [
            expected.name
            for expected, actual in zip(bundle.procedure_spec.outputs, procedure_columns)
            if _sql_type_family(expected.sql_type)
            != _sql_type_family(actual.get("sql_type", ""))
        ]
        if incompatible:
            errors.append(_error(
                "procedure", "contract", "output_types",
                f"输出类型与 QuerySpec 不兼容: {', '.join(incompatible)}", True,
            ))

    rules = {item.name: item for item in bundle.procedure_spec.verification_rules}
    if {item.name for item in bundle.verify_queries} != set(rules):
        errors.append(_error(
            "oracle", "contract", "verification_rules",
            "Oracle 规则集合与 QuerySpec 不一致", True,
        ))
    for query in bundle.verify_queries:
        rule = rules.get(query.name)
        if rule is None:
            continue
        if query.validation_spec.get("mode") != rule.mode:
            errors.append(_error(
                "oracle", "contract", "verification_mode",
                f"{query.name} 的 mode 与 QuerySpec 不一致", True,
            ))
        required = list(rule.required_columns)
        configured = query.validation_spec.get("compare_columns") or []
        if required and configured != required:
            errors.append(_error(
                "oracle", "contract", "verification_columns",
                f"{query.name} 的所需列与 QuerySpec 不一致", True,
            ))
        oracle_columns = compiled.get(f"oracle:{query.name}", {}).get(
            "result_columns",
        ) or []
        oracle_outputs = (
            [item.get("name") for item in oracle_columns]
            if oracle_columns else _select_columns(query.sql_code)
        )
        missing = [name for name in required if name not in oracle_outputs]
        if missing:
            errors.append(_error(
                "oracle", "contract", "oracle_output",
                f"{query.name} 缺少列: {', '.join(missing)}", True,
            ))
        if rule.mode == "change_set":
            expected_targets = sorted(
                (
                    f"{item.schema}.{item.table}",
                    item.operation,
                    tuple(item.key_columns),
                    item.max_affected_rows,
                )
                for item in bundle.procedure_spec.writes
            )
            actual_targets = []
            for target in query.validation_spec.get("affected_tables") or []:
                if not isinstance(target, dict):
                    continue
                actual_targets.append((
                    _normalized_identifier(str(target.get("table") or "")),
                    target.get("operation"),
                    tuple(target.get("key_columns") or []),
                    target.get("max_affected_rows"),
                ))
            if sorted(actual_targets) != expected_targets:
                errors.append(_error(
                    "oracle",
                    "contract",
                    "write_scope",
                    f"{query.name} 的写入变化集范围与 QuerySpec 不一致",
                    True,
                ))
    return errors


def _bundle_hash(bundle: CandidateBundle) -> str:
    from app.services.validation import compute_bundle_hash

    return compute_bundle_hash(bundle.sp_dict(), bundle.query_dicts())


def _defaults(bundle: CandidateBundle) -> dict:
    return {
        item.name.lstrip("@"): item.default
        for item in bundle.procedure_spec.parameters
        if item.default is not None
    }


def run_candidate_gates(
    bundle: CandidateBundle,
    *,
    compiler: Compiler | None = None,
    business_validator: BusinessValidator | None = None,
) -> CandidateBundle:
    """按固定顺序执行闸门；前一道失败后立即停止。"""
    from app.db.sqlserver import compile_candidate
    from app.services.validation import (
        ValidationError,
        validate_readonly_query,
        validate_reporting_procedure,
        validate_sp_bundle,
    )

    compiler = compiler or compile_candidate
    business_validator = business_validator or validate_sp_bundle
    bundle.gate_results = []
    bundle.status = "candidate_generated"

    bundle.gate_results.append(_gate("query_spec"))

    schema_errors = [
        _error(
            "query_spec",
            "schema",
            "unresolved_identifier",
            item.reason + ": " + item.identifier,
            False,
            item.model_dump(mode="json"),
        )
        for item in bundle.schema_evidence.unresolved
    ]
    bundle.gate_results.append(_gate("schema", schema_errors))
    if schema_errors:
        bundle.status = "failed"
        return bundle

    try:
        validate_reporting_procedure(
            bundle.procedure_sql,
            operation_type_for_spec(bundle.procedure_spec),
            bundle.query_dicts(),
        )
        for query in bundle.verify_queries:
            validate_readonly_query(query.sql_code)
            snapshot = query.validation_spec.get("snapshot_sql")
            if snapshot:
                validate_readonly_query(snapshot)
        safety_errors = []
    except ValidationError as exc:
        safety_errors = [_error(
            "bundle", "safety", "unsafe_sql", str(exc), False,
        )]
    bundle.gate_results.append(_gate("safety", safety_errors))
    if safety_errors:
        bundle.status = "failed"
        return bundle

    compile_errors = []
    parameter_defs = json.loads(bundle.sp_dict()["parameters"])
    artifacts = [
        ("procedure", bundle.procedure_spec.name, bundle.procedure_sql),
        *[
            ("oracle", query.name, query.sql_code)
            for query in bundle.verify_queries
        ],
    ]
    compiled = {}
    for artifact, name, sql in artifacts:
        result = compiler(artifact, name, sql, parameter_defs)
        compiled[f"{artifact}:{name}"] = result
        if not result.get("ok"):
            compile_errors.append(_error(
                artifact,
                "compile",
                str(result.get("code") or "compile_failed"),
                str(result.get("error") or "编译失败"),
                True,
            ))
    bundle.gate_results.append(_gate(
        "compile", compile_errors, artifacts=compiled,
    ))
    if compile_errors:
        bundle.status = "failed"
        return bundle

    contract_errors = _contract_errors(bundle, compiled)
    bundle.gate_results.append(_gate("contract", contract_errors))
    if contract_errors:
        bundle.status = "failed"
        return bundle

    bundle.bundle_hash = _bundle_hash(bundle)
    business = business_validator(
        bundle.sp_dict(),
        bundle.query_dicts(),
        _defaults(bundle),
    )
    if business.get("syntax_ok") and business.get("business_ok"):
        bundle.gate_results.append(_gate("business", result=business))
        bundle.status = "validated"
        return bundle

    details = business.get("details") or []
    unattributed = bool(details) and all(
        item.get("type") == "business" for item in details
    )
    error = _error(
        "bundle",
        "business",
        "result_mismatch" if unattributed else "business_validation_failed",
        "SP 与独立 Oracle 结果不一致" if unattributed else "业务校验失败",
        False,
    )
    bundle.gate_results.append(_gate("business", [error], result=business))
    bundle.status = "needs_review" if unattributed else "failed"
    return bundle


def _invariant_snapshot(bundle: CandidateBundle) -> str:
    payload = {
        "query_spec": bundle.query_spec.model_dump(mode="json", by_alias=True),
        "procedure_spec": bundle.procedure_spec.model_dump(mode="json", by_alias=True),
        "verification_contracts": [
            {
                "name": item.name,
                "compare_columns": item.compare_columns,
                "validation_spec": item.validation_spec,
            }
            for item in bundle.verify_queries
        ],
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def validate_candidate_with_repairs(
    bundle: CandidateBundle,
    repairer: Repairer | None,
    *,
    compiler: Compiler | None = None,
    business_validator: BusinessValidator | None = None,
    schema_refresher: SchemaRefresher | None = None,
    max_repairs: int = 2,
) -> CandidateBundle:
    """最多修复两轮；每轮保持不变量并从第一道闸门重新执行。"""
    invariant = _invariant_snapshot(bundle)
    current = bundle
    repair_count = 0
    schema_refreshed = False
    while True:
        current.repair_count = repair_count
        current = run_candidate_gates(
            current,
            compiler=compiler,
            business_validator=business_validator,
        )
        if current.status in {"validated", "needs_review"}:
            return current
        errors = [
            error
            for gate in current.gate_results
            for error in gate.errors
        ]
        if (
            not schema_refreshed
            and schema_refresher is not None
            and any(error.code in {"207", "208"} for error in errors)
        ):
            current.schema_evidence = schema_refresher(current.query_spec)
            schema_refreshed = True
            continue
        if (
            repair_count == max_repairs
            or repairer is None
            or not errors
            or any(not error.repairable for error in errors)
        ):
            return current
        repaired = repairer(current, errors)
        if _invariant_snapshot(repaired) != invariant:
            repaired.status = "failed"
            repaired.gate_results.append(_gate("contract", [_error(
                "bundle",
                "contract",
                "invariant_changed",
                "自动修复改变了 QuerySpec 或不可变契约",
                False,
            )]))
            return repaired
        current = repaired
        repair_count += 1
