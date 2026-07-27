"""Pure business semantics, deliberately free of physical table/column names."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Literal

from pydantic import Field, model_validator

from app.contracts.base import StrictContract
from app.services.semantic_guard import (
    assert_business_output_name,
    assert_semantic_text,
)


LogicalType = Literal[
    "string", "integer", "decimal", "money", "date", "datetime", "boolean",
]
Boundary = Literal[
    "none", "inclusive", "exclusive", "inclusive_full_day",
]
ResultMode = Literal["full_rows", "exception_rows", "scalar_summary"]
Aggregation = Literal[
    "none", "sum", "count_rows", "count_distinct", "min", "max", "avg",
]


class SemanticParameter(StrictContract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(pattern=r"^@[A-Za-z_][A-Za-z0-9_]*$")
    logical_type: LogicalType
    required: bool
    default: Any | None
    meaning: str = Field(min_length=1)
    boundary: Boundary = "none"


class SemanticEntity(StrictContract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    meaning: str = Field(min_length=1)


class SemanticOutput(StrictContract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    meaning: str = Field(min_length=1)
    logical_type: LogicalType
    nullable: bool = True


class SemanticFilter(StrictContract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    meaning: str = Field(min_length=1)
    field_ids: list[str] = Field(default_factory=list)
    parameter_ids: list[str] = Field(default_factory=list)
    operator: Literal[
        "eq", "ne", "gt", "gte", "lt", "lte", "like",
        "is_null", "is_not_null", "between", "full_day_range",
    ]
    literal_values: list[Any] = Field(default_factory=list)
    skip_when_parameter_null: bool = False


class SemanticWhenThen(StrictContract):
    when: "SemanticExpression"
    then: "SemanticExpression"


class SemanticExpression(StrictContract):
    kind: Literal["output", "literal", "binary", "function", "case"]
    output_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$"
    )
    value: Any | None = None
    operator: str | None = None
    args: list["SemanticExpression"] = Field(default_factory=list)
    cases: list[SemanticWhenThen] = Field(default_factory=list)
    else_expr: "SemanticExpression | None" = None

    @model_validator(mode="after")
    def validate_shape(self):
        if self.kind == "output" and not self.output_id:
            raise ValueError("派生表达式 output 缺少 output_id")
        if self.kind in {"binary", "unary", "function"} and not self.operator:
            raise ValueError(f"派生表达式 {self.kind} 缺少 operator")
        if self.kind == "binary" and len(self.args) != 2:
            raise ValueError("派生 binary 表达式必须有两个参数")
        if self.kind == "function" and not self.args:
            raise ValueError("派生 function 表达式至少有一个参数")
        if self.kind == "case" and not self.cases:
            raise ValueError("派生 case 表达式至少有一个分支")
        return self


class DerivedField(StrictContract):
    output_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    expression: SemanticExpression


class SemanticSourceField(StrictContract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    entity_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    meaning: str = Field(min_length=1)
    logical_type: LogicalType
    nullable: bool = True


class SemanticFactDimension(StrictContract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    field_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$",
    )
    expression: "SemanticSourceExpression | None" = None
    meaning: str = Field(min_length=1)
    logical_type: LogicalType | None = None

    @model_validator(mode="after")
    def validate_dimension(self):
        if (self.field_id is None) == (self.expression is None):
            raise ValueError(
                f"维度 {self.id} 必须且只能声明 field_id 或 expression"
            )
        if self.expression is not None and self.logical_type is None:
            raise ValueError(f"派生维度 {self.id} 必须声明 logical_type")
        return self


class SemanticSourceWhenThen(StrictContract):
    when: "SemanticSourceExpression"
    then: "SemanticSourceExpression"


class SemanticSourceExpression(StrictContract):
    kind: Literal[
        "field", "literal", "binary", "unary", "function", "case",
    ]
    field_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$"
    )
    value: Any | None = None
    operator: str | None = None
    args: list["SemanticSourceExpression"] = Field(default_factory=list)
    cases: list[SemanticSourceWhenThen] = Field(default_factory=list)
    else_expr: "SemanticSourceExpression | None" = None

    @model_validator(mode="after")
    def validate_shape(self):
        if self.kind == "field" and not self.field_id:
            raise ValueError("field 表达式缺少 field_id")
        if self.kind in {"binary", "unary", "function"} and not self.operator:
            raise ValueError(f"{self.kind} 表达式缺少 operator")
        if self.kind == "binary" and len(self.args) != 2:
            raise ValueError("binary 表达式必须有两个参数")
        if self.kind == "unary" and len(self.args) != 1:
            raise ValueError("unary 表达式必须有一个参数")
        if self.kind == "function" and not self.args:
            raise ValueError("function 表达式至少需要一个参数")
        if self.kind == "case" and not self.cases:
            raise ValueError("case 表达式至少需要一个分支")
        if self.kind == "binary" and str(self.operator).upper() not in {
            "=", "<>", ">", ">=", "<", "<=", "AND", "OR",
            "+", "-", "*", "/",
        }:
            raise ValueError(f"不支持源字段二元运算符 {self.operator}")
        if self.kind == "function" and str(self.operator).upper() not in {
            "ABS", "COALESCE", "NULLIF", "CONCAT", "YEAR", "MONTH",
            "DATEFROMPARTS", "EOMONTH",
        }:
            raise ValueError(f"不支持源字段函数 {self.operator}")
        if self.kind == "unary" and str(self.operator).upper() not in {
            "NOT", "IS NULL", "IS NOT NULL", "NEGATE",
        }:
            raise ValueError(f"不支持源字段一元运算符 {self.operator}")
        return self


class SemanticFactMeasure(StrictContract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    field_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$"
    )
    expression: SemanticSourceExpression | None = None
    meaning: str = Field(min_length=1)
    aggregation: Aggregation
    logical_type: LogicalType

    @model_validator(mode="after")
    def validate_measure(self):
        if (
            self.aggregation != "count_rows"
            and not self.field_id
            and self.expression is None
        ):
            raise ValueError(f"指标 {self.id} 缺少 field_id")
        if self.aggregation == "count_rows" and (
            self.field_id is not None or self.expression is not None
        ):
            raise ValueError("count_rows 不允许 field_id")
        if self.field_id is not None and self.expression is not None:
            raise ValueError(
                f"指标 {self.id} 不能同时声明 field_id 和 expression"
            )
        return self


class SemanticFact(StrictContract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    meaning: str = Field(min_length=1)
    entity_ids: list[str] = Field(min_length=1)
    dimensions: list[SemanticFactDimension] = Field(default_factory=list)
    measures: list[SemanticFactMeasure] = Field(default_factory=list)
    filter_ids: list[str] = Field(default_factory=list)
    grain: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_fact(self):
        values = [item.id for item in self.dimensions + self.measures]
        if len(values) != len({item.casefold() for item in values}):
            raise ValueError(f"事实 {self.id} 存在重复 value id")
        dimension_ids = {item.id.casefold() for item in self.dimensions}
        missing = [
            item for item in self.grain
            if item.casefold() not in dimension_ids
        ]
        if missing:
            raise ValueError(
                f"事实 {self.id} 的 grain 引用未知维度: "
                + ", ".join(missing)
            )
        if not self.dimensions and not self.measures:
            raise ValueError(f"事实 {self.id} 没有维度或指标")
        aggregate_flags = {
            item.aggregation != "none" for item in self.measures
        }
        if len(aggregate_flags) > 1:
            raise ValueError(
                f"事实 {self.id} 不能混用聚合与非聚合指标"
            )
        return self


class SemanticFactValueRef(StrictContract):
    fact_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    value_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class SemanticResultWhenThen(StrictContract):
    when: "SemanticResultExpression"
    then: "SemanticResultExpression"


class SemanticResultExpression(StrictContract):
    kind: Literal[
        "fact_value", "output", "parameter", "literal",
        "binary", "unary", "function", "case",
    ]
    fact_value: SemanticFactValueRef | None = None
    output_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$"
    )
    parameter_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$"
    )
    value: Any | None = None
    operator: str | None = None
    args: list["SemanticResultExpression"] = Field(default_factory=list)
    cases: list[SemanticResultWhenThen] = Field(default_factory=list)
    else_expr: "SemanticResultExpression | None" = None

    @model_validator(mode="after")
    def validate_shape(self):
        if self.kind == "output" and not self.output_id:
            raise ValueError("output 表达式缺少 output_id")
        if self.kind == "parameter" and not self.parameter_id:
            raise ValueError("parameter 表达式缺少 parameter_id")
        if self.kind == "fact_value" and self.fact_value is None:
            raise ValueError("fact_value 表达式缺少事实值引用")
        if self.kind in {"binary", "unary", "function"} and not self.operator:
            raise ValueError(f"{self.kind} 表达式缺少 operator")
        if self.kind == "binary" and len(self.args) != 2:
            raise ValueError("binary 表达式必须恰好有两个参数")
        if self.kind == "unary" and len(self.args) != 1:
            raise ValueError("unary 表达式必须有一个参数")
        if self.kind == "function" and not self.args:
            raise ValueError("function 表达式至少有一个参数")
        if self.kind == "case" and not self.cases:
            raise ValueError("case 表达式至少有一个分支")
        if self.kind == "binary" and str(self.operator).upper() not in {
            "=", "<>", ">", ">=", "<", "<=", "AND", "OR",
            "+", "-", "*", "/",
        }:
            raise ValueError(f"不支持结果二元运算符 {self.operator}")
        if self.kind == "function" and str(self.operator).upper() not in {
            "ABS", "COALESCE", "NULLIF",
        }:
            raise ValueError(f"不支持结果函数 {self.operator}")
        if self.kind == "unary" and str(self.operator).upper() not in {
            "NOT", "IS NULL", "IS NOT NULL", "NEGATE",
        }:
            raise ValueError(f"不支持结果一元运算符 {self.operator}")
        return self


class SemanticResultBinding(StrictContract):
    output_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    expression: SemanticResultExpression


class SemanticFactJoinKey(StrictContract):
    left: SemanticFactValueRef
    right: SemanticFactValueRef


class SemanticFactJoin(StrictContract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    keys: list[SemanticFactJoinKey] = Field(min_length=1)
    join_type: Literal["inner", "left", "full"]
    meaning: str = Field(min_length=1)


class SemanticContract(StrictContract):
    version: Literal[3] = 3
    contract_id: str = Field(min_length=1)
    procedure_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    purpose: str = Field(min_length=1)
    result_mode: ResultMode
    parameters: list[SemanticParameter] = Field(default_factory=list)
    entities: list[SemanticEntity] = Field(min_length=1)
    grain: list[str] = Field(default_factory=list)
    outputs: list[SemanticOutput] = Field(min_length=1)
    filters: list[SemanticFilter] = Field(default_factory=list)
    derived_fields: list[DerivedField] = Field(default_factory=list)
    source_fields: list[SemanticSourceField] = Field(default_factory=list)
    facts: list[SemanticFact] = Field(default_factory=list)
    fact_joins: list[SemanticFactJoin] = Field(default_factory=list)
    result_bindings: list[SemanticResultBinding] = Field(default_factory=list)
    result_filter: SemanticResultExpression | None = None
    allow_empty: bool = True
    money_tolerance: float = Field(default=0.01, ge=0)

    @model_validator(mode="after")
    def validate_references(self):
        assert_semantic_text(self.purpose, "业务目的")
        for item in self.parameters:
            assert_semantic_text(item.meaning, f"参数 {item.id} 的含义")
        for item in self.entities:
            assert_semantic_text(item.meaning, f"实体 {item.id} 的含义")
        for item in self.outputs:
            assert_business_output_name(item.name)
            assert_semantic_text(item.meaning, f"输出 {item.id} 的含义")
        for item in self.filters:
            assert_semantic_text(item.meaning, f"过滤 {item.id} 的含义")
        for item in self.source_fields:
            assert_semantic_text(item.meaning, f"源字段 {item.id} 的含义")
        for item in self.facts:
            assert_semantic_text(item.meaning, f"事实 {item.id} 的含义")
            for value in item.dimensions + item.measures:
                assert_semantic_text(
                    value.meaning, f"事实值 {item.id}.{value.id} 的含义"
                )
        for item in self.fact_joins:
            assert_semantic_text(item.meaning, f"事实关联 {item.id} 的含义")

        def duplicates(values: list[str]) -> list[str]:
            seen: set[str] = set()
            result: list[str] = []
            for value in values:
                key = value.casefold()
                if key in seen:
                    result.append(value)
                seen.add(key)
            return result

        parameter_ids = [item.id for item in self.parameters]
        entity_ids = [item.id for item in self.entities]
        output_ids = [item.id for item in self.outputs]
        output_names = [item.name for item in self.outputs]
        filter_ids = [item.id for item in self.filters]
        derived_output_ids = [item.output_id for item in self.derived_fields]
        source_field_ids = [item.id for item in self.source_fields]
        fact_ids = [item.id for item in self.facts]
        fact_join_ids = [item.id for item in self.fact_joins]
        result_output_ids = [item.output_id for item in self.result_bindings]
        for label, values in (
            ("参数 ID", parameter_ids),
            ("实体 ID", entity_ids),
            ("输出 ID", output_ids),
            ("输出名称", output_names),
            ("过滤条件 ID", filter_ids),
            ("派生输出 ID", derived_output_ids),
            ("粒度", self.grain),
            ("源字段 ID", source_field_ids),
            ("事实 ID", fact_ids),
            ("事实关联 ID", fact_join_ids),
            ("结果绑定输出 ID", result_output_ids),
        ):
            repeated = duplicates(values)
            if repeated:
                raise ValueError(f"{label} 重复: {', '.join(repeated)}")

        def normalized_meaning(value: str) -> str:
            text = re.split(r"[，,（(]", str(value), maxsplit=1)[0].lower()
            for source, target in (
                ("标识", "编号"),
                ("代码", "编号"),
                ("应收", ""),
                ("系统自动生成的", ""),
                ("唯一", ""),
            ):
                text = text.replace(source, target)
            return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)

        for index, left in enumerate(self.outputs):
            left_meaning = normalized_meaning(left.meaning)
            for right in self.outputs[index + 1:]:
                if left.logical_type != right.logical_type:
                    continue
                right_meaning = normalized_meaning(right.meaning)
                if SequenceMatcher(
                    None, left_meaning, right_meaning,
                ).ratio() >= 0.9:
                    raise ValueError(
                        "输出存在语义重复: "
                        f"{left.name}, {right.name}"
                    )

        output_set = {item.casefold() for item in output_ids}
        missing_grain = [
            item for item in self.grain if item.casefold() not in output_set
        ]
        if missing_grain:
            raise ValueError(
                "粒度引用未声明输出: " + ", ".join(missing_grain)
            )
        if self.result_mode != "scalar_summary" and not self.grain:
            raise ValueError("明细或异常结果必须声明稳定粒度")

        parameter_set = {item.casefold() for item in parameter_ids}
        for item in self.filters:
            if len(item.field_ids) != 1:
                raise ValueError(f"过滤条件 {item.id} 必须声明逻辑字段")
            missing = [
                value for value in item.parameter_ids
                if value.casefold() not in parameter_set
            ]
            if missing:
                raise ValueError(
                    f"过滤条件 {item.id} 引用未声明参数: {', '.join(missing)}"
                )
            expected_parameters = {
                "is_null": 0,
                "is_not_null": 0,
                "between": 2,
                "full_day_range": 2,
            }.get(item.operator)
            if (
                expected_parameters is not None
                and len(item.parameter_ids) != expected_parameters
            ):
                raise ValueError(
                    f"过滤条件 {item.id} 的参数数量不符合 {item.operator}"
                )
            if (
                not item.parameter_ids
                and not item.literal_values
                and item.operator not in {"is_null", "is_not_null"}
            ):
                raise ValueError(f"过滤条件 {item.id} 缺少参数或常量")
        parameter_by_id = {item.id: item for item in self.parameters}
        for semantic_filter in self.filters:
            unsafe_optional = [
                parameter_id
                for parameter_id in semantic_filter.parameter_ids
                if (
                    not parameter_by_id[parameter_id].required
                    and parameter_by_id[parameter_id].default is None
                    and not semantic_filter.skip_when_parameter_null
                )
            ]
            if unsafe_optional:
                raise ValueError(
                    f"过滤条件 {semantic_filter.id} 使用了无默认值的可选参数: "
                    + ", ".join(unsafe_optional)
                    + "；当前过滤语义会把 NULL 当作真实比较值"
                )
            if semantic_filter.skip_when_parameter_null:
                if (
                    len(semantic_filter.parameter_ids) != 1
                    or semantic_filter.literal_values
                ):
                    raise ValueError(
                        f"过滤条件 {semantic_filter.id} 的可选参数绕过形状无效"
                    )
                parameter = parameter_by_id[
                    semantic_filter.parameter_ids[0]
                ]
                if parameter.required or parameter.default is not None:
                    raise ValueError(
                        f"过滤条件 {semantic_filter.id} 只能为无默认值可选参数启用 NULL 绕过"
                    )
        full_day_parameters = {
            item.id for item in self.parameters
            if item.boundary == "inclusive_full_day"
        }
        full_day_ends = {
            item.parameter_ids[1]
            for item in self.filters
            if item.operator == "full_day_range"
            and len(item.parameter_ids) == 2
        }
        if full_day_parameters != full_day_ends:
            raise ValueError(
                "inclusive_full_day 参数必须且只能作为 full_day_range 终点"
            )
        for item in self.filters:
            if item.operator != "full_day_range":
                continue
            start = parameter_by_id[item.parameter_ids[0]]
            end = parameter_by_id[item.parameter_ids[1]]
            if start.boundary != "inclusive" or end.boundary != "inclusive_full_day":
                raise ValueError(
                    f"过滤条件 {item.id} 的自然日范围边界声明不正确"
                )

        for item in self.derived_fields:
            if item.output_id.casefold() not in output_set:
                raise ValueError(
                    f"派生字段引用未声明输出: {item.output_id}"
                )
            referenced: set[str] = set()

            def collect(expression: SemanticExpression):
                if expression.output_id:
                    referenced.add(expression.output_id.casefold())
                for child in expression.args:
                    collect(child)
                for case in expression.cases:
                    collect(case.when)
                    collect(case.then)
                if expression.else_expr is not None:
                    collect(expression.else_expr)

            collect(item.expression)
            unknown = referenced - output_set
            if unknown:
                raise ValueError(
                    "派生表达式引用未声明输出: " + ", ".join(sorted(unknown))
                )
            if item.output_id.casefold() in referenced:
                raise ValueError("派生字段不能循环引用自身")

        entity_set = {item.casefold() for item in entity_ids}
        source_by_id = {
            item.id.casefold(): item for item in self.source_fields
        }
        filter_set = {item.casefold() for item in filter_ids}
        if (
            len(self.entities) > 1 or self.result_mode == "scalar_summary"
        ) and not self.facts:
            raise ValueError(
                "多实体或汇总合同必须声明结构化 facts，"
                "禁止在关系计划阶段猜测"
            )
        for field in self.source_fields:
            if field.entity_id.casefold() not in entity_set:
                raise ValueError(
                    f"源字段 {field.id} 引用未知实体 {field.entity_id}"
                )
        for fact in self.facts:
            fact_entities = {item.casefold() for item in fact.entity_ids}
            unknown_entities = fact_entities - entity_set
            if unknown_entities:
                raise ValueError(
                    f"事实 {fact.id} 引用未知实体: "
                    + ", ".join(sorted(unknown_entities))
                )
            unknown_filters = {
                item.casefold() for item in fact.filter_ids
            } - filter_set
            if unknown_filters:
                raise ValueError(
                    f"事实 {fact.id} 引用未知过滤: "
                    + ", ".join(sorted(unknown_filters))
                )
            for filter_id in fact.filter_ids:
                semantic_filter = next(
                    item for item in self.filters
                    if item.id.casefold() == filter_id.casefold()
                )
                for filter_field_id in semantic_filter.field_ids:
                    source = source_by_id.get(filter_field_id.casefold())
                    if source is None:
                        raise ValueError(
                            f"事实 {fact.id} 的过滤条件引用未知源字段 "
                            f"{filter_field_id}"
                        )
                    if source.entity_id.casefold() not in fact_entities:
                        raise ValueError(
                            f"事实 {fact.id} 的过滤字段 {filter_field_id} "
                            "不属于该事实实体"
                        )
            field_ids = [
                item.field_id for item in fact.dimensions
                if item.field_id is not None
            ] + [
                item.field_id for item in fact.measures
                if item.field_id is not None
            ]

            def collect_source_fields(
                expression: SemanticSourceExpression | None,
            ) -> list[str]:
                if expression is None:
                    return []
                result = (
                    [expression.field_id]
                    if expression.field_id is not None else []
                )
                for child in expression.args:
                    result.extend(collect_source_fields(child))
                for case in expression.cases:
                    result.extend(collect_source_fields(case.when))
                    result.extend(collect_source_fields(case.then))
                result.extend(
                    collect_source_fields(expression.else_expr)
                )
                return result

            for measure in fact.measures:
                field_ids.extend(
                    collect_source_fields(measure.expression)
                )
            for dimension in fact.dimensions:
                field_ids.extend(
                    collect_source_fields(dimension.expression)
                )
            for field_id in field_ids:
                source = source_by_id.get(field_id.casefold())
                if source is None:
                    raise ValueError(
                        f"事实 {fact.id} 引用未知源字段 {field_id}"
                    )
                if source.entity_id.casefold() not in fact_entities:
                    raise ValueError(
                        f"事实 {fact.id} 的字段 {field_id} 不属于该事实实体"
                    )

        fact_values = {
            fact.id.casefold(): {
                item.id.casefold()
                for item in fact.dimensions + fact.measures
            }
            for fact in self.facts
        }

        def validate_fact_ref(ref: SemanticFactValueRef):
            values = fact_values.get(ref.fact_id.casefold())
            if values is None or ref.value_id.casefold() not in values:
                raise ValueError(
                    f"未知事实值引用 {ref.fact_id}.{ref.value_id}"
                )

        for join in self.fact_joins:
            fact_pairs = set()
            for key in join.keys:
                validate_fact_ref(key.left)
                validate_fact_ref(key.right)
                if (
                    key.left.fact_id.casefold()
                    == key.right.fact_id.casefold()
                ):
                    raise ValueError(f"事实关联 {join.id} 不能自关联")
                fact_pairs.add(frozenset({
                    key.left.fact_id.casefold(),
                    key.right.fact_id.casefold(),
                }))
            if len(fact_pairs) != 1:
                raise ValueError(
                    f"事实关联 {join.id} 的所有键必须连接同一对事实"
                )
        if self.facts:
            if self.derived_fields:
                raise ValueError(
                    "facts 合同的最终公式必须只在 result_bindings 中声明"
                )
            if {
                item.casefold() for item in result_output_ids
            } != output_set:
                raise ValueError(
                    "facts 合同必须为每个输出声明唯一 result_binding"
                )

            def validate_result_expression(
                expression: SemanticResultExpression,
            ):
                if expression.fact_value is not None:
                    validate_fact_ref(expression.fact_value)
                if (
                    expression.output_id is not None
                    and expression.output_id.casefold() not in output_set
                ):
                    raise ValueError(
                        f"结果公式引用未知输出 {expression.output_id}"
                    )
                if (
                    expression.parameter_id is not None
                    and expression.parameter_id.casefold()
                    not in parameter_set
                ):
                    raise ValueError(
                        f"结果公式引用未知参数 "
                        f"{expression.parameter_id}"
                    )
                for child in expression.args:
                    validate_result_expression(child)
                for case in expression.cases:
                    validate_result_expression(case.when)
                    validate_result_expression(case.then)
                if expression.else_expr is not None:
                    validate_result_expression(expression.else_expr)

            for result in self.result_bindings:
                validate_result_expression(result.expression)
            if self.result_filter is not None:
                validate_result_expression(self.result_filter)
            if (
                self.result_mode == "exception_rows"
                and self.result_filter is None
            ):
                raise ValueError(
                    "exception_rows facts 合同必须声明 result_filter"
                )
            result_by_id = {
                item.output_id.casefold(): item.expression
                for item in self.result_bindings
            }

            def output_dependencies(
                expression: SemanticResultExpression,
            ) -> set[str]:
                result = (
                    {expression.output_id.casefold()}
                    if expression.output_id is not None else set()
                )
                for child in expression.args:
                    result.update(output_dependencies(child))
                for case in expression.cases:
                    result.update(output_dependencies(case.when))
                    result.update(output_dependencies(case.then))
                if expression.else_expr is not None:
                    result.update(
                        output_dependencies(expression.else_expr)
                    )
                return result

            visiting: set[str] = set()
            visited: set[str] = set()

            def visit_output(output_id: str):
                if output_id in visited:
                    return
                if output_id in visiting:
                    raise ValueError(
                        f"结果公式存在循环输出依赖 {output_id}"
                    )
                visiting.add(output_id)
                for dependency in output_dependencies(
                    result_by_id[output_id]
                ):
                    visit_output(dependency)
                visiting.remove(output_id)
                visited.add(output_id)

            for output_id in result_by_id:
                visit_output(output_id)

            source_types = {
                item.id.casefold(): item.logical_type
                for item in self.source_fields
            }
            fact_value_types = {}
            for fact in self.facts:
                values = {}
                for dimension in fact.dimensions:
                    values[dimension.id.casefold()] = (
                        dimension.logical_type
                        if dimension.expression is not None
                        else source_types.get(
                            str(dimension.field_id).casefold()
                        )
                    )
                values.update({
                    item.id.casefold(): item.logical_type
                    for item in fact.measures
                })
                fact_value_types[fact.id.casefold()] = values
            output_types = {
                item.id.casefold(): item.logical_type
                for item in self.outputs
            }

            def infer_result_type(
                expression: SemanticResultExpression,
                resolving: tuple[str, ...] = (),
            ) -> str | None:
                if expression.kind == "fact_value":
                    ref = expression.fact_value
                    return fact_value_types.get(
                        ref.fact_id.casefold(), {},
                    ).get(ref.value_id.casefold())
                if expression.kind == "parameter":
                    return next(
                        (
                            item.logical_type for item in self.parameters
                            if item.id == expression.parameter_id
                        ),
                        None,
                    )
                if expression.kind == "literal":
                    if expression.value is None:
                        return None
                    if isinstance(expression.value, bool):
                        return "boolean"
                    if isinstance(expression.value, int):
                        return "integer"
                    if isinstance(expression.value, float):
                        return "decimal"
                    return "string"
                if expression.kind == "output":
                    output_id = str(expression.output_id).casefold()
                    if output_id in resolving:
                        return output_types.get(output_id)
                    nested = result_by_id.get(output_id)
                    return (
                        infer_result_type(
                            nested, resolving + (output_id,),
                        )
                        if nested is not None
                        else output_types.get(output_id)
                    )
                if expression.kind == "binary":
                    operator = str(expression.operator).upper()
                    if operator in {
                        "=", "<>", ">", ">=", "<", "<=",
                        "AND", "OR",
                    }:
                        return "boolean"
                    values = [
                        infer_result_type(item, resolving)
                        for item in expression.args
                    ]
                    if "money" in values:
                        return "money"
                    if "decimal" in values:
                        return "decimal"
                    return values[0] if values else None
                if expression.kind == "unary":
                    if str(expression.operator).upper() == "NEGATE":
                        return infer_result_type(
                            expression.args[0], resolving,
                        )
                    return "boolean"
                if expression.kind == "function":
                    if str(expression.operator).upper() == "CONCAT":
                        return "string"
                    values = [
                        infer_result_type(item, resolving)
                        for item in expression.args
                    ]
                    return next(
                        (item for item in values if item is not None),
                        None,
                    )
                if expression.kind == "case":
                    values = [
                        infer_result_type(item.then, resolving)
                        for item in expression.cases
                    ]
                    if expression.else_expr is not None:
                        values.append(
                            infer_result_type(
                                expression.else_expr, resolving,
                            )
                        )
                    return next(
                        (item for item in values if item is not None),
                        None,
                    )
                return None

            compatible_types = {
                ("money", "decimal"),
                ("decimal", "money"),
            }
            for result in self.result_bindings:
                actual_type = infer_result_type(result.expression)
                expected_type = output_types[
                    result.output_id.casefold()
                ]
                if (
                    actual_type is not None
                    and actual_type != expected_type
                    and (expected_type, actual_type)
                    not in compatible_types
                ):
                    raise ValueError(
                        f"结果绑定 {result.output_id} 的表达式类型 "
                        f"{actual_type} 与输出类型 {expected_type} 不一致"
                    )

            if len(self.facts) > 1 and not self.fact_joins:
                raise ValueError("多事实合同必须声明 fact_joins")
            if len(self.facts) > 1:
                included = {self.facts[0].id.casefold()}
                remaining = {
                    item.id.casefold() for item in self.facts[1:]
                }
                unused = list(self.fact_joins)
                while remaining:
                    join = next(
                        (
                            item for item in unused
                            if {
                                item.keys[0].left.fact_id.casefold(),
                                item.keys[0].right.fact_id.casefold(),
                            } & included
                            and {
                                item.keys[0].left.fact_id.casefold(),
                                item.keys[0].right.fact_id.casefold(),
                            } & remaining
                        ),
                        None,
                    )
                    if join is None:
                        raise ValueError(
                            "fact_joins 必须形成连通的事实图"
                        )
                    left_id = join.keys[0].left.fact_id.casefold()
                    right_id = join.keys[0].right.fact_id.casefold()
                    if join.join_type == "left" and not (
                        left_id in included and right_id in remaining
                    ):
                        raise ValueError(
                            f"左连接 {join.id} 必须从已包含的左事实"
                            "连接新右事实"
                        )
                    new_fact = next(
                        iter({left_id, right_id} & remaining)
                    )
                    included.add(new_fact)
                    remaining.remove(new_fact)
                    unused.remove(join)
        return self


class SemanticDesign(StrictContract):
    """用户确认的纯业务设计；物理对象只能在确认后由 SchemaBinding 产生。"""

    version: Literal[3] = 3
    design_version: str = Field(min_length=1)
    decision_hash: str = Field(min_length=1)
    contracts: list[SemanticContract] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_contracts(self):
        contract_ids = [item.contract_id.casefold() for item in self.contracts]
        procedure_names = [
            item.procedure_name.casefold() for item in self.contracts
        ]
        if len(contract_ids) != len(set(contract_ids)):
            raise ValueError("SemanticDesign 存在重复 contract_id")
        if len(procedure_names) != len(set(procedure_names)):
            raise ValueError("SemanticDesign 存在重复存储过程名称")
        return self
