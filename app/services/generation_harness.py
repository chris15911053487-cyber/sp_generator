"""生成 harness 的结构化业务契约。"""
import json
import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import (
    BaseModel, ConfigDict, Field, ValidationError, field_validator,
    model_validator,
)

from app.agent.prompts import QUERY_SPEC_PROMPT, QUERY_SPEC_REPAIR_PROMPT


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


def _normalize_enum(value: Any) -> Any:
    """只规范化无歧义的枚举大小写和首尾空格。"""
    return value.strip().lower() if isinstance(value, str) else value


class ColumnRef(_StrictModel):
    source_alias: str = Field(min_length=1)
    column: str = Field(min_length=1)


class ParameterSpec(_StrictModel):
    name: str = Field(pattern=r"^@[A-Za-z_][A-Za-z0-9_]*$")
    sql_type: str = Field(min_length=1)
    required: bool
    default: Any | None
    meaning: str = Field(min_length=1)


class SourceSpec(_StrictModel):
    schema_name: str = Field(alias="schema", min_length=1)

    @property
    def schema(self) -> str:
        return self.schema_name
    table: str = Field(min_length=1)
    alias: str = Field(min_length=1)
    role: str = Field(min_length=1)


class JoinSpec(_StrictModel):
    join_type: Literal["inner", "left", "right", "full", "cross"]
    left: ColumnRef
    right: ColumnRef
    reason: str = Field(min_length=1)

    _normalize_join_type = field_validator(
        "join_type", mode="before",
    )(_normalize_enum)


class FilterSpec(_StrictModel):
    description: str = Field(min_length=1)
    column_refs: list[ColumnRef]
    parameter_refs: list[str]


class OutputSpec(_StrictModel):
    name: str = Field(min_length=1)
    meaning: str = Field(min_length=1)
    source_columns: list[ColumnRef]
    aggregation: str | None
    sql_type: str = Field(min_length=1)


class WriteSpec(_StrictModel):
    schema_name: str = Field(alias="schema", min_length=1)

    @property
    def schema(self) -> str:
        return self.schema_name
    table: str = Field(min_length=1)
    operation: Literal["insert", "update", "delete"]
    key_columns: list[str] = Field(min_length=1)
    max_affected_rows: int = Field(gt=0)

    _normalize_operation = field_validator(
        "operation", mode="before",
    )(_normalize_enum)


class VerificationRuleSpec(_StrictModel):
    name: str = Field(min_length=1)
    mode: Literal["scalar", "keyed_rows", "zero_rows", "change_set"]
    required_columns: list[str]
    description: str = Field(min_length=1)

    _normalize_mode = field_validator(
        "mode", mode="before",
    )(_normalize_enum)


def _duplicates(values: list[str]) -> list[str]:
    seen = set()
    duplicates = []
    for value in values:
        normalized = value.casefold()
        if normalized in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(normalized)
    return duplicates


class ProcedureSpec(_StrictModel):
    name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    operation_type: Literal["reporting", "controlled_write"]
    parameters: list[ParameterSpec]
    sources: list[SourceSpec] = Field(min_length=1)
    joins: list[JoinSpec]
    filters: list[FilterSpec]
    grain: list[ColumnRef]
    outputs: list[OutputSpec]
    writes: list[WriteSpec]
    verification_rules: list[VerificationRuleSpec] = Field(min_length=1)

    _normalize_operation_type = field_validator(
        "operation_type", mode="before",
    )(_normalize_enum)

    @model_validator(mode="after")
    def validate_contract(self):
        duplicate_parameters = _duplicates([item.name for item in self.parameters])
        if duplicate_parameters:
            raise ValueError(f"重复参数: {', '.join(duplicate_parameters)}")

        duplicate_sources = _duplicates([item.alias for item in self.sources])
        if duplicate_sources:
            raise ValueError(f"重复来源 alias: {', '.join(duplicate_sources)}")

        duplicate_outputs = _duplicates([item.name for item in self.outputs])
        if duplicate_outputs:
            raise ValueError(f"重复输出: {', '.join(duplicate_outputs)}")

        duplicate_rules = _duplicates([item.name for item in self.verification_rules])
        if duplicate_rules:
            raise ValueError(f"重复校验规则: {', '.join(duplicate_rules)}")

        aliases = {item.alias for item in self.sources}
        column_refs = list(self.grain)
        for join in self.joins:
            column_refs.extend((join.left, join.right))
        for item in self.filters:
            column_refs.extend(item.column_refs)
        for item in self.outputs:
            column_refs.extend(item.source_columns)
        missing_aliases = sorted({
            item.source_alias for item in column_refs
            if item.source_alias not in aliases
        })
        if missing_aliases:
            raise ValueError(f"未声明来源别名: {', '.join(missing_aliases)}")

        parameters = {item.name.casefold() for item in self.parameters}
        missing_parameters = sorted({
            name for item in self.filters for name in item.parameter_refs
            if name.casefold() not in parameters
        })
        if missing_parameters:
            raise ValueError(f"未声明参数: {', '.join(missing_parameters)}")

        outputs = {item.name.casefold() for item in self.outputs}
        missing_outputs = sorted({
            name for rule in self.verification_rules for name in rule.required_columns
            if name.casefold() not in outputs
        })
        if missing_outputs:
            raise ValueError(f"校验规则引用未声明输出: {', '.join(missing_outputs)}")

        if self.operation_type == "reporting" and self.writes:
            raise ValueError("reporting 过程不得声明 writes")
        if self.operation_type == "controlled_write" and not self.writes:
            raise ValueError("controlled_write 过程必须声明 writes")
        return self


class QuerySpec(_StrictModel):
    design_version: str = Field(min_length=1)
    procedures: list[ProcedureSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_procedures(self):
        duplicate_names = _duplicates([item.name for item in self.procedures])
        if duplicate_names:
            raise ValueError(f"重复存储过程: {', '.join(duplicate_names)}")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


class GateError(_StrictModel):
    artifact: Literal["query_spec", "procedure", "oracle", "bundle"]
    category: Literal["safety", "schema", "compile", "contract", "business"]
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    schema_subset: dict | list | None
    repairable: bool


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else text


def compile_query_spec(
    design: str,
    compiler: Callable[[str], str | dict | Any],
) -> QuerySpec:
    """把已确认设计编译为严格 QuerySpec，结构错误时定向纠正一次。"""
    if not design or not design.strip():
        raise ValueError("已确认设计不能为空")

    normalized_design = design.strip()
    schema = json.dumps(
        QuerySpec.model_json_schema(), ensure_ascii=False, separators=(",", ":"),
    )
    response = compiler(QUERY_SPEC_PROMPT.format(
        design=normalized_design,
        schema=schema,
    ))

    def validate(value: Any) -> QuerySpec:
        if hasattr(value, "content"):
            value = value.content
        if isinstance(value, str):
            return QuerySpec.model_validate_json(_strip_json_fence(value))
        return QuerySpec.model_validate(value)

    try:
        return validate(response)
    except ValidationError as exc:
        raw_response = response.content if hasattr(response, "content") else response
        if not isinstance(raw_response, str):
            raw_response = json.dumps(raw_response, ensure_ascii=False, default=str)
        errors = json.dumps(
            exc.errors(include_url=False),
            ensure_ascii=False,
            default=str,
            indent=2,
        )
        repaired = compiler(QUERY_SPEC_REPAIR_PROMPT.format(
            design=normalized_design,
            schema=schema,
            response=raw_response,
            errors=errors,
        ))
        return validate(repaired)
