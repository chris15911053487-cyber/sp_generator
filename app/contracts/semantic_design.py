"""Small, staged contracts compiled deterministically into SemanticContract."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import Field, TypeAdapter, model_validator
from typing_extensions import TypeAliasType

from app.contracts.base import StrictContract
from app.contracts.semantic import Aggregation, Boundary, LogicalType, ResultMode
from app.services.semantic_guard import (
    assert_business_output_name,
    assert_semantic_text,
)


Symbol = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
BusinessPolicyEffect = Literal[
    "source_population",
    "calculation",
    "matching",
    "result_selection",
    "presentation",
]


class BusinessPolicySpec(StrictContract):
    key: Symbol
    value: str = Field(min_length=1)
    effect: BusinessPolicyEffect
    meaning: str = Field(min_length=1)


class ResultParameterSpec(StrictContract):
    symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(pattern=r"^@[A-Za-z_][A-Za-z0-9_]*$")
    logical_type: LogicalType
    required: bool
    default: Any | None
    meaning: str = Field(min_length=1)
    boundary: Boundary = "none"


class ResultOutputSpec(StrictContract):
    symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    meaning: str = Field(min_length=1)
    logical_type: LogicalType
    nullable: bool = True


class ResultContract(StrictContract):
    version: Literal[1] = 1
    procedure_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    purpose: str = Field(min_length=1)
    result_mode: ResultMode
    parameters: list[ResultParameterSpec] = Field(default_factory=list)
    outputs: list[ResultOutputSpec] = Field(min_length=1)
    grain_output_symbols: list[Symbol] = Field(default_factory=list)
    allow_empty: bool = True
    money_tolerance: float = Field(default=0.01, ge=0)
    business_policies: list[BusinessPolicySpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result_shape(self):
        assert_semantic_text(self.purpose, "业务目的")
        for item in self.parameters:
            assert_semantic_text(
                item.meaning, f"参数 {item.symbol} 的含义",
            )
        for item in self.outputs:
            assert_business_output_name(item.name)
            assert_semantic_text(
                item.meaning, f"输出 {item.symbol} 的含义",
            )
        for policy in self.business_policies:
            assert_semantic_text(policy.value, f"业务政策 {policy.key}")
            assert_semantic_text(
                policy.meaning, f"业务政策 {policy.key} 的含义",
            )
        policy_keys = [item.key.casefold() for item in self.business_policies]
        if len(policy_keys) != len(set(policy_keys)):
            raise ValueError("BUSINESS_POLICY_DUPLICATE")
        reserved_policies = sorted(
            set(policy_keys)
            & {"result_mode", "allow_empty", "money_tolerance"}
        )
        if reserved_policies:
            raise ValueError(
                "BUSINESS_POLICY_DUPLICATES_STRUCTURED_FIELD: "
                + ", ".join(reserved_policies)
            )
        parameter_symbols = [item.symbol.casefold() for item in self.parameters]
        parameter_names = [item.name.casefold() for item in self.parameters]
        output_symbols = [item.symbol.casefold() for item in self.outputs]
        output_names = [item.name.casefold() for item in self.outputs]
        for label, values in (
            ("参数 symbol", parameter_symbols),
            ("参数名称", parameter_names),
            ("输出 symbol", output_symbols),
            ("输出名称", output_names),
            ("业务粒度", [item.casefold() for item in self.grain_output_symbols]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} 重复")
        unknown = sorted(
            set(item.casefold() for item in self.grain_output_symbols)
            - set(output_symbols)
        )
        if unknown:
            raise ValueError(
                "RESULT_GRAIN_OUTPUT_MISSING: " + ", ".join(unknown)
            )
        if self.result_mode != "scalar_summary" and not self.grain_output_symbols:
            raise ValueError("RESULT_GRAIN_REQUIRED")
        return self


class FactDimensionNeed(StrictContract):
    symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    meaning: str = Field(min_length=1)
    logical_type: LogicalType
    result_output_symbol: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$",
    )


class FactMeasureNeed(StrictContract):
    symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    meaning: str = Field(min_length=1)
    logical_type: LogicalType
    aggregation: Aggregation
    result_output_symbol: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$",
    )


class FactBlueprintItem(StrictContract):
    symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    meaning: str = Field(min_length=1)
    entity_symbols: list[Symbol] = Field(min_length=1)
    dimensions: list[FactDimensionNeed] = Field(default_factory=list)
    measures: list[FactMeasureNeed] = Field(default_factory=list)
    grain_dimension_symbols: list[Symbol] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_fact_shape(self):
        if self.symbol in {"final_result", "sp_result", "procedure_result"}:
            raise ValueError("FACT_PSEUDO_SOURCE_REJECTED")
        values = [item.symbol.casefold() for item in self.dimensions + self.measures]
        if len(values) != len(set(values)):
            raise ValueError(f"事实 {self.symbol} 的值 symbol 重复")
        dimension_symbols = {
            item.symbol.casefold() for item in self.dimensions
        }
        unknown = sorted(
            set(item.casefold() for item in self.grain_dimension_symbols)
            - dimension_symbols
        )
        if unknown:
            raise ValueError(
                "FACT_GRAIN_DIMENSION_MISSING: " + ", ".join(unknown)
            )
        if not self.dimensions and not self.measures:
            raise ValueError(f"事实 {self.symbol} 没有维度或指标")
        return self


class FactJoinBlueprint(StrictContract):
    symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    left_fact_symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    right_fact_symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    left_dimension_symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    right_dimension_symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    join_type: Literal["inner", "left", "full"]
    meaning: str = Field(min_length=1)


class FactFilterPolicyBinding(StrictContract):
    kind: Literal["fact_filter"]
    policy_key: Symbol
    fact_symbol: Symbol


class FactExpressionPolicyBinding(StrictContract):
    kind: Literal["fact_expression"]
    policy_key: Symbol
    fact_symbol: Symbol
    value_symbol: Symbol


class JoinPolicyBinding(StrictContract):
    kind: Literal["join"]
    policy_key: Symbol
    join_symbol: Symbol
    match_mode: Literal[
        "matched_only", "left_preserved", "include_unmatched",
    ]


class ResultFilterPolicyBinding(StrictContract):
    kind: Literal["result_filter"]
    policy_key: Symbol


class ContractOnlyPolicyBinding(StrictContract):
    kind: Literal["contract_only"]
    policy_key: Symbol


PolicyBinding = TypeAliasType(
    "PolicyBinding",
    Annotated[
        Union[
            FactFilterPolicyBinding,
            FactExpressionPolicyBinding,
            JoinPolicyBinding,
            ResultFilterPolicyBinding,
            ContractOnlyPolicyBinding,
        ],
        Field(discriminator="kind"),
    ],
)


class FactBlueprint(StrictContract):
    version: Literal[1] = 1
    facts: list[FactBlueprintItem] = Field(min_length=1)
    joins: list[FactJoinBlueprint] = Field(default_factory=list)
    derived_output_symbols: list[Symbol] = Field(default_factory=list)
    policy_bindings: list[PolicyBinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_fact_graph(self):
        for fact in self.facts:
            assert_semantic_text(fact.meaning, f"事实 {fact.symbol} 的含义")
            for item in fact.dimensions + fact.measures:
                assert_semantic_text(
                    item.meaning,
                    f"事实值 {fact.symbol}.{item.symbol} 的含义",
                )
        for join in self.joins:
            assert_semantic_text(
                join.meaning, f"事实关联 {join.symbol} 的含义",
            )
        facts = {item.symbol.casefold(): item for item in self.facts}
        if len(facts) != len(self.facts):
            raise ValueError("事实 symbol 重复")
        pairs = []
        for join in self.joins:
            left = facts.get(join.left_fact_symbol.casefold())
            right = facts.get(join.right_fact_symbol.casefold())
            if left is None or right is None:
                raise ValueError("FACT_JOIN_SYMBOL_UNKNOWN")
            left_dimensions = {
                item.symbol.casefold() for item in left.dimensions
            }
            right_dimensions = {
                item.symbol.casefold() for item in right.dimensions
            }
            if (
                join.left_dimension_symbol.casefold() not in left_dimensions
                or join.right_dimension_symbol.casefold() not in right_dimensions
            ):
                raise ValueError("FACT_JOIN_SYMBOL_UNKNOWN")
            left_type = next(
                item.logical_type
                for item in left.dimensions
                if (
                    item.symbol.casefold()
                    == join.left_dimension_symbol.casefold()
                )
            )
            right_type = next(
                item.logical_type
                for item in right.dimensions
                if (
                    item.symbol.casefold()
                    == join.right_dimension_symbol.casefold()
                )
            )
            if (
                left_type != right_type
                and {left_type, right_type} != {"money", "decimal"}
            ):
                raise ValueError("FACT_JOIN_TYPE_MISMATCH")
            pairs.append({
                join.left_fact_symbol.casefold(),
                join.right_fact_symbol.casefold(),
            })
        if len(facts) > 1:
            reachable = {next(iter(facts))}
            changed = True
            while changed:
                changed = False
                for pair in pairs:
                    if pair & reachable and not pair.issubset(reachable):
                        reachable.update(pair)
                        changed = True
            if reachable != set(facts):
                raise ValueError("FACT_JOIN_GRAPH_DISCONNECTED")
        joins = {item.symbol.casefold(): item for item in self.joins}
        seen_policy_targets = set()
        for binding in self.policy_bindings:
            target = [binding.kind, binding.policy_key.casefold()]
            if binding.kind == "fact_filter":
                if binding.fact_symbol.casefold() not in facts:
                    raise ValueError("POLICY_BINDING_TARGET_UNKNOWN")
                target.append(binding.fact_symbol.casefold())
            elif binding.kind == "fact_expression":
                fact = facts.get(binding.fact_symbol.casefold())
                if fact is None or binding.value_symbol.casefold() not in {
                    item.symbol.casefold()
                    for item in fact.dimensions + fact.measures
                }:
                    raise ValueError("POLICY_BINDING_TARGET_UNKNOWN")
                target.extend([
                    binding.fact_symbol.casefold(),
                    binding.value_symbol.casefold(),
                ])
            elif binding.kind == "join":
                join = joins.get(binding.join_symbol.casefold())
                if join is None:
                    raise ValueError("POLICY_BINDING_TARGET_UNKNOWN")
                expected_join_type = {
                    "matched_only": "inner",
                    "left_preserved": "left",
                    "include_unmatched": "full",
                }[binding.match_mode]
                if join.join_type != expected_join_type:
                    raise ValueError("POLICY_JOIN_MODE_MISMATCH")
                target.append(binding.join_symbol.casefold())
            key = tuple(target)
            if key in seen_policy_targets:
                raise ValueError("POLICY_BINDING_DUPLICATE")
            seen_policy_targets.add(key)
        return self


class EntityRequirement(StrictContract):
    symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    meaning: str = Field(min_length=1)
    grain_meaning: str = Field(min_length=1)


class SourceFieldRequirement(StrictContract):
    symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    entity_symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    meaning: str = Field(min_length=1)
    logical_type: LogicalType
    nullable: bool = True


class FilterRequirement(StrictContract):
    symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    meaning: str = Field(min_length=1)
    source_symbol: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    parameter_symbols: list[Symbol] = Field(default_factory=list)
    operator: Literal[
        "eq", "ne", "gt", "gte", "lt", "lte", "like",
        "is_null", "is_not_null", "between", "full_day_range",
    ]
    literal_values: list[Any] = Field(default_factory=list)
    policy_key: str | None = None
    fact_symbols: list[Symbol] = Field(min_length=1)
    skip_when_parameter_null: bool = False


class SourceRequirements(StrictContract):
    version: Literal[1] = 1
    entities: list[EntityRequirement] = Field(min_length=1)
    fields: list[SourceFieldRequirement] = Field(min_length=1)
    filters: list[FilterRequirement] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_sources(self):
        for entity in self.entities:
            assert_semantic_text(
                entity.meaning, f"实体 {entity.symbol} 的含义",
            )
            assert_semantic_text(
                entity.grain_meaning, f"实体 {entity.symbol} 的粒度",
            )
        for field in self.fields:
            assert_semantic_text(
                field.meaning, f"源字段 {field.symbol} 的含义",
            )
        for semantic_filter in self.filters:
            assert_semantic_text(
                semantic_filter.meaning,
                f"过滤 {semantic_filter.symbol} 的含义",
            )
        entities = {item.symbol.casefold() for item in self.entities}
        if len(entities) != len(self.entities):
            raise ValueError("实体 symbol 重复")
        fields = {item.symbol.casefold(): item for item in self.fields}
        if len(fields) != len(self.fields):
            raise ValueError("源字段 symbol 重复")
        for item in self.fields:
            if item.entity_symbol.casefold() not in entities:
                raise ValueError(
                    f"SOURCE_FIELD_OWNER_UNKNOWN: {item.symbol}"
                )
        for item in self.filters:
            if item.source_symbol.casefold() not in fields:
                raise ValueError(
                    f"EXPRESSION_SYMBOL_UNKNOWN: {item.source_symbol}"
                )
        return self


class SymbolSourceExpression(StrictContract):
    kind: Literal["source"]
    symbol: Symbol


class SymbolFactValueExpression(StrictContract):
    kind: Literal["fact_value"]
    fact_symbol: Symbol
    value_symbol: Symbol


class SymbolOutputExpression(StrictContract):
    kind: Literal["output"]
    symbol: Symbol


class SymbolParameterExpression(StrictContract):
    kind: Literal["parameter"]
    symbol: Symbol


class SymbolLiteralExpression(StrictContract):
    kind: Literal["literal"]
    value: Any | None = None


class SymbolBinaryExpression(StrictContract):
    kind: Literal["binary"]
    operator: Literal[
        "=", "<>", ">", ">=", "<", "<=", "AND", "OR", "+", "-", "*", "/",
    ]
    args: list["SymbolExpression"] = Field(min_length=2, max_length=2)


class SymbolUnaryExpression(StrictContract):
    kind: Literal["unary"]
    operator: Literal["NOT", "IS NULL", "IS NOT NULL", "NEGATE"]
    args: list["SymbolExpression"] = Field(min_length=1, max_length=1)


class SymbolFunctionExpression(StrictContract):
    kind: Literal["function"]
    operator: Literal[
        "ABS", "COALESCE", "NULLIF", "CONCAT", "YEAR", "MONTH",
        "DATEFROMPARTS", "EOMONTH",
    ]
    args: list["SymbolExpression"] = Field(min_length=1)


class SymbolWhenThen(StrictContract):
    when: "SymbolExpression"
    then: "SymbolExpression"


class SymbolCaseExpression(StrictContract):
    kind: Literal["case"]
    cases: list[SymbolWhenThen] = Field(min_length=1)
    else_expr: "SymbolExpression | None" = None


SymbolExpression = TypeAliasType(
    "SymbolExpression",
    Annotated[
        Union[
            SymbolSourceExpression,
            SymbolFactValueExpression,
            SymbolOutputExpression,
            SymbolParameterExpression,
            SymbolLiteralExpression,
            SymbolBinaryExpression,
            SymbolUnaryExpression,
            SymbolFunctionExpression,
            SymbolCaseExpression,
        ],
        Field(discriminator="kind"),
    ],
)

for _expression_model in (
    SymbolBinaryExpression,
    SymbolUnaryExpression,
    SymbolFunctionExpression,
    SymbolWhenThen,
    SymbolCaseExpression,
):
    _expression_model.model_rebuild(_types_namespace=globals())

_symbol_expression_adapter = TypeAdapter(SymbolExpression)


def make_symbol_expression(**data: Any):
    """Build a strict discriminated expression node for programmatic callers."""
    return _symbol_expression_adapter.validate_python(data)


class FactDimensionExpression(StrictContract):
    fact_symbol: Symbol
    dimension_symbol: Symbol
    expression: SymbolExpression
    logical_type: LogicalType


class FactMeasureExpression(StrictContract):
    fact_symbol: Symbol
    measure_symbol: Symbol
    expression: SymbolExpression | None = None
    aggregation: Aggregation
    logical_type: LogicalType


class ResultBindingExpression(StrictContract):
    output_symbol: Symbol
    expression: SymbolExpression


class ExpressionDesign(StrictContract):
    version: Literal[1] = 1
    dimensions: list[FactDimensionExpression] = Field(default_factory=list)
    measures: list[FactMeasureExpression] = Field(default_factory=list)
    results: list[ResultBindingExpression] = Field(min_length=1)
    result_filter: SymbolExpression | None = None
