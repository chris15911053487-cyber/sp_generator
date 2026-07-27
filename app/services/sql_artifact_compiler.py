"""Deterministic parameter handling shared by SQL compile and execution."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable


_PARAMETER_NAME = re.compile(r"^@[A-Za-z_][A-Za-z0-9_]*$")
_SQL_TYPE = re.compile(
    r"^[A-Z][A-Z0-9_]*"
    r"(?:\((?:MAX|\d+)(?:\s*,\s*\d+)?\))?$",
)


class SqlArtifactError(ValueError):
    """A deterministic SQL artifact contract failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParameterReference:
    name: str
    start: int
    end: int


@dataclass(frozen=True)
class ParameterDefinition:
    name: str
    sql_type: str
    required: bool
    default: Any

    @property
    def key(self) -> str:
        return self.name[1:].casefold()


def _normal_sql_ranges(sql: str) -> Iterable[tuple[int, int]]:
    """Yield ranges outside strings, comments, and quoted identifiers."""
    index = 0
    start = 0
    length = len(sql)
    while index < length:
        marker = sql[index:index + 2]
        char = sql[index]
        if marker == "--":
            if start < index:
                yield start, index
            newline = sql.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            start = index
            continue
        if marker == "/*":
            if start < index:
                yield start, index
            end = sql.find("*/", index + 2)
            index = length if end < 0 else end + 2
            start = index
            continue
        if char in {"'", '"', "["}:
            if start < index:
                yield start, index
            closing = "]" if char == "[" else char
            index += 1
            while index < length:
                if sql[index] == closing:
                    if index + 1 < length and sql[index + 1] == closing:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            start = index
            continue
        index += 1
    if start < length:
        yield start, length


def _legacy_references(sql: str) -> list[ParameterReference]:
    references = []
    pattern = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
    for start, end in _normal_sql_ranges(sql):
        for match in pattern.finditer(sql, start, end):
            references.append(ParameterReference(
                name=f"@{match.group(1)}",
                start=match.start(),
                end=match.end(),
            ))
    return references


def canonicalize_parameter_syntax(sql: str) -> str:
    """Convert legacy ``{Name}`` tokens to canonical ``@Name`` safely."""
    references = _legacy_references(sql)
    if not references:
        return sql
    parts = []
    cursor = 0
    for reference in references:
        parts.extend((sql[cursor:reference.start], reference.name))
        cursor = reference.end
    parts.append(sql[cursor:])
    return "".join(parts)


def scan_parameter_references(sql: str) -> list[ParameterReference]:
    """Return canonical parameter references in lexical order."""
    canonical = canonicalize_parameter_syntax(sql)
    references = []
    pattern = re.compile(r"(?<!@)@[A-Za-z_][A-Za-z0-9_]*")
    for start, end in _normal_sql_ranges(canonical):
        for match in pattern.finditer(canonical, start, end):
            references.append(ParameterReference(
                name=match.group(0),
                start=match.start(),
                end=match.end(),
            ))
    return references


def normalize_parameter_definitions(
    parameter_defs: list[dict] | None,
) -> dict[str, ParameterDefinition]:
    """Validate QuerySpec parameter definitions and index them by name."""
    normalized: dict[str, ParameterDefinition] = {}
    for raw in parameter_defs or []:
        if not isinstance(raw, dict):
            raise SqlArtifactError(
                "invalid_parameter_definition",
                "参数定义必须是对象",
            )
        name = str(raw.get("name") or "").strip()
        if not _PARAMETER_NAME.fullmatch(name):
            raise SqlArtifactError("invalid_parameter", f"非法参数名: {name}")
        sql_type = str(
            raw.get("type") or raw.get("sql_type") or "",
        ).strip().upper()
        if not _SQL_TYPE.fullmatch(sql_type):
            raise SqlArtifactError(
                "invalid_parameter_type",
                f"非法参数类型: {name} {sql_type}",
            )
        definition = ParameterDefinition(
            name=name,
            sql_type=sql_type,
            required=bool(raw.get("required", False)),
            default=raw.get("default"),
        )
        if definition.key in normalized:
            raise SqlArtifactError(
                "duplicate_parameter",
                f"重复参数: {name}",
            )
        normalized[definition.key] = definition
    return normalized


def parameter_manifest(
    sql: str,
    parameter_defs: list[dict] | None,
    *,
    allow_undeclared: bool = False,
) -> tuple[str, list[dict]]:
    """Return canonical SQL and its validated parameter manifest."""
    canonical = canonicalize_parameter_syntax(sql)
    definitions = normalize_parameter_definitions(parameter_defs)
    references = scan_parameter_references(canonical)
    counts: dict[str, int] = {}
    ordered_keys = []
    for reference in references:
        key = reference.name[1:].casefold()
        if key not in definitions:
            if allow_undeclared:
                continue
            raise SqlArtifactError(
                "undeclared_parameter",
                f"SQL 引用了未声明参数: {reference.name}",
            )
        if key not in counts:
            ordered_keys.append(key)
            counts[key] = 0
        counts[key] += 1
    return canonical, [
        {
            "name": definitions[key].name,
            "type": definitions[key].sql_type,
            "required": definitions[key].required,
            "default": definitions[key].default,
            "occurrences": counts[key],
        }
        for key in ordered_keys
    ]


def describe_parameter_declaration(
    sql: str,
    parameter_defs: list[dict] | None,
    *,
    allow_undeclared: bool = False,
) -> tuple[str, str | None, list[dict]]:
    """Build the exact declaration consumed by SQL Server metadata APIs."""
    canonical, manifest = parameter_manifest(
        sql,
        parameter_defs,
        allow_undeclared=allow_undeclared,
    )
    declaration = ", ".join(
        f"{item['name']} {item['type']}" for item in manifest
    )
    return canonical, declaration or None, manifest


def find_sql_keyword(sql: str, keyword: str) -> int | None:
    """Find a keyword outside quoted and commented regions."""
    pattern = re.compile(rf"(?i)\b{re.escape(keyword)}\b")
    for start, end in _normal_sql_ranges(sql):
        match = pattern.search(sql, start, end)
        if match:
            return match.start()
    return None


def extract_procedure_body(sql: str) -> str:
    """Extract a canonical procedure batch body without changing DB context."""
    canonical = canonicalize_parameter_syntax(sql)
    as_index = find_sql_keyword(canonical, "AS")
    if as_index is None:
        raise SqlArtifactError(
            "invalid_definition",
            "存储过程定义缺少 AS 主体",
        )
    body = canonical[as_index + 2:].strip()
    if not body:
        raise SqlArtifactError(
            "invalid_definition",
            "存储过程主体为空",
        )
    return body


def normalize_collation_clauses(
    sql: str,
    target_collation: str,
) -> tuple[str, list[dict]]:
    """Normalize model-authored COLLATE clauses to captured DB policy."""
    if not re.fullmatch(r"[A-Za-z0-9_]+", target_collation or ""):
        raise SqlArtifactError(
            "collation_evidence_missing",
            f"非法或缺失的目标数据库排序规则: {target_collation}",
        )
    pattern = re.compile(
        r"(?i)\bCOLLATE\s+(DATABASE_DEFAULT|[A-Za-z0-9_]+)",
    )
    matches = []
    for start, end in _normal_sql_ranges(sql):
        matches.extend(pattern.finditer(sql, start, end))
    if not matches:
        return sql, []
    parts = []
    decisions = []
    cursor = 0
    for match in matches:
        source = match.group(1)
        replacement = f"COLLATE {target_collation}"
        parts.extend((sql[cursor:match.start()], replacement))
        cursor = match.end()
        if source.casefold() != target_collation.casefold():
            decisions.append({
                "operation": "normalize_explicit_collation",
                "source_collation": source,
                "target_collation": target_collation,
                "start": match.start(),
            })
    parts.append(sql[cursor:])
    return "".join(parts), decisions


def normalize_qualified_column_collations(
    sql: str,
    column_policies: list[dict],
    target_collation: str,
) -> tuple[str, list[dict]]:
    """Collate known qualified text columns using captured schema evidence."""
    if not re.fullmatch(r"[A-Za-z0-9_]+", target_collation or ""):
        raise SqlArtifactError(
            "collation_evidence_missing",
            f"非法或缺失的目标数据库排序规则: {target_collation}",
        )
    matches = []
    for policy in column_policies:
        source = str(policy.get("source_collation") or "")
        if not source or source.casefold() == target_collation.casefold():
            continue
        qualifier = str(policy.get("qualifier") or "")
        column = str(policy.get("column") or "")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$#@]*", qualifier):
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$#@]*", column):
            continue
        pattern = re.compile(
            rf"(?i)(?:\[{re.escape(qualifier)}\]|"
            rf"\b{re.escape(qualifier)}\b)\s*\.\s*"
            rf"(?:\[{re.escape(column)}\]|"
            rf"\b{re.escape(column)}\b)",
        )
        for start, end in _normal_sql_ranges(sql):
            for match in pattern.finditer(sql, start, end):
                following = sql[match.end():]
                if re.match(r"(?is)^\s+COLLATE\b", following):
                    continue
                matches.append((match.start(), match.end(), policy))
    if not matches:
        return sql, []
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    selected = []
    last_end = -1
    for item in matches:
        if item[0] < last_end:
            continue
        selected.append(item)
        last_end = item[1]
    parts = []
    decisions = []
    cursor = 0
    for start, end, policy in selected:
        parts.extend((
            sql[cursor:end],
            f" COLLATE {target_collation}",
        ))
        cursor = end
        decisions.append({
            "operation": "normalize_source_column_collation",
            "qualifier": policy["qualifier"],
            "column": policy["column"],
            "source_collation": policy["source_collation"],
            "target_collation": target_collation,
            "start": start,
        })
    parts.append(sql[cursor:])
    return "".join(parts), decisions


def compile_odbc_binding(
    sql: str,
    values: dict[str, Any],
) -> tuple[str, list[Any]]:
    """Compile canonical named parameters to positional ODBC bindings."""
    canonical = canonicalize_parameter_syntax(sql)
    references = scan_parameter_references(canonical)
    if not references:
        return canonical, []
    indexed_values = {
        str(key).lstrip("@").casefold(): value
        for key, value in (values or {}).items()
    }
    parts = []
    bound_values = []
    cursor = 0
    for reference in references:
        key = reference.name[1:].casefold()
        if key not in indexed_values:
            raise SqlArtifactError(
                "missing_parameter_value",
                f"缺少校验参数: {reference.name[1:]}",
            )
        parts.extend((canonical[cursor:reference.start], "?"))
        bound_values.append(indexed_values[key])
        cursor = reference.end
    parts.append(canonical[cursor:])
    return "".join(parts), bound_values
