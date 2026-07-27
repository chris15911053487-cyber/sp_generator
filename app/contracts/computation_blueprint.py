"""Frozen business computations, declared before source fields are designed."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import Field, model_validator
from typing_extensions import TypeAliasType

from app.contracts.base import StrictContract
from app.contracts.semantic import Aggregation, LogicalType
from app.contracts.semantic_design import Symbol


BinaryOperator = Literal[
    "=", "<>", ">", ">=", "<", "<=", "AND", "OR", "+", "-", "*", "/",
]
UnaryOperator = Literal["NOT", "IS NULL", "IS NOT NULL", "NEGATE"]
FunctionOperator = Literal[
    "ABS", "COALESCE", "NULLIF", "CONCAT", "YEAR", "MONTH",
    "DATEFROMPARTS", "EOMONTH",
]


class ComputationInputSpec(StrictContract):
    symbol: Symbol
    meaning: str = Field(min_length=1)
    logical_type: LogicalType
    nullable: bool


class FactInputExpression(StrictContract):
    kind: Literal["input"]
    symbol: Symbol


class FactLiteralExpression(StrictContract):
    kind: Literal["literal"]
    value: Any | None = None


class FactBinaryExpression(StrictContract):
    kind: Literal["binary"]
    operator: BinaryOperator
    args: list["FactComputationExpression"] = Field(min_length=2, max_length=2)


class FactUnaryExpression(StrictContract):
    kind: Literal["unary"]
    operator: UnaryOperator
    args: list["FactComputationExpression"] = Field(min_length=1, max_length=1)


class FactFunctionExpression(StrictContract):
    kind: Literal["function"]
    operator: FunctionOperator
    args: list["FactComputationExpression"] = Field(min_length=1)


class FactWhenThen(StrictContract):
    when: "FactComputationExpression"
    then: "FactComputationExpression"


class FactCaseExpression(StrictContract):
    kind: Literal["case"]
    cases: list[FactWhenThen] = Field(min_length=1)
    else_expr: "FactComputationExpression | None" = None


FactComputationExpression = TypeAliasType(
    "FactComputationExpression",
    Annotated[
        Union[
            FactInputExpression,
            FactLiteralExpression,
            FactBinaryExpression,
            FactUnaryExpression,
            FactFunctionExpression,
            FactCaseExpression,
        ],
        Field(discriminator="kind"),
    ],
)


class ResultFactValueExpression(StrictContract):
    kind: Literal["fact_value"]
    fact_symbol: Symbol
    value_symbol: Symbol


class ResultOutputExpression(StrictContract):
    kind: Literal["output"]
    symbol: Symbol


class ResultParameterExpression(StrictContract):
    kind: Literal["parameter"]
    symbol: Symbol


class ResultLiteralExpression(StrictContract):
    kind: Literal["literal"]
    value: Any | None = None


class ResultBinaryExpression(StrictContract):
    kind: Literal["binary"]
    operator: BinaryOperator
    args: list["ResultComputationExpression"] = Field(
        min_length=2, max_length=2,
    )


class ResultUnaryExpression(StrictContract):
    kind: Literal["unary"]
    operator: UnaryOperator
    args: list["ResultComputationExpression"] = Field(
        min_length=1, max_length=1,
    )


class ResultFunctionExpression(StrictContract):
    kind: Literal["function"]
    operator: FunctionOperator
    args: list["ResultComputationExpression"] = Field(min_length=1)


class ResultWhenThen(StrictContract):
    when: "ResultComputationExpression"
    then: "ResultComputationExpression"


class ResultCaseExpression(StrictContract):
    kind: Literal["case"]
    cases: list[ResultWhenThen] = Field(min_length=1)
    else_expr: "ResultComputationExpression | None" = None


ResultComputationExpression = TypeAliasType(
    "ResultComputationExpression",
    Annotated[
        Union[
            ResultFactValueExpression,
            ResultOutputExpression,
            ResultParameterExpression,
            ResultLiteralExpression,
            ResultBinaryExpression,
            ResultUnaryExpression,
            ResultFunctionExpression,
            ResultCaseExpression,
        ],
        Field(discriminator="kind"),
    ],
)


class FilterOutputExpression(StrictContract):
    kind: Literal["output"]
    symbol: Symbol


class FilterParameterExpression(StrictContract):
    kind: Literal["parameter"]
    symbol: Symbol


class FilterLiteralExpression(StrictContract):
    kind: Literal["literal"]
    value: Any | None = None


class FilterBinaryExpression(StrictContract):
    kind: Literal["binary"]
    operator: BinaryOperator
    args: list["FilterComputationExpression"] = Field(
        min_length=2, max_length=2,
    )


class FilterUnaryExpression(StrictContract):
    kind: Literal["unary"]
    operator: UnaryOperator
    args: list["FilterComputationExpression"] = Field(
        min_length=1, max_length=1,
    )


class FilterFunctionExpression(StrictContract):
    kind: Literal["function"]
    operator: FunctionOperator
    args: list["FilterComputationExpression"] = Field(min_length=1)


class FilterWhenThen(StrictContract):
    when: "FilterComputationExpression"
    then: "FilterComputationExpression"


class FilterCaseExpression(StrictContract):
    kind: Literal["case"]
    cases: list[FilterWhenThen] = Field(min_length=1)
    else_expr: "FilterComputationExpression | None" = None


FilterComputationExpression = TypeAliasType(
    "FilterComputationExpression",
    Annotated[
        Union[
            FilterOutputExpression,
            FilterParameterExpression,
            FilterLiteralExpression,
            FilterBinaryExpression,
            FilterUnaryExpression,
            FilterFunctionExpression,
            FilterCaseExpression,
        ],
        Field(discriminator="kind"),
    ],
)


for _model in (
    FactBinaryExpression,
    FactUnaryExpression,
    FactFunctionExpression,
    FactWhenThen,
    FactCaseExpression,
    ResultBinaryExpression,
    ResultUnaryExpression,
    ResultFunctionExpression,
    ResultWhenThen,
    ResultCaseExpression,
    FilterBinaryExpression,
    FilterUnaryExpression,
    FilterFunctionExpression,
    FilterWhenThen,
    FilterCaseExpression,
):
    _model.model_rebuild(_types_namespace=globals())


def _referenced_symbols(expression, kind: str) -> list[str]:
    if expression.kind == kind:
        return [expression.symbol]
    if expression.kind in {"binary", "unary", "function"}:
        return [
            symbol
            for arg in expression.args
            for symbol in _referenced_symbols(arg, kind)
        ]
    if expression.kind == "case":
        symbols = [
            symbol
            for branch in expression.cases
            for node in (branch.when, branch.then)
            for symbol in _referenced_symbols(node, kind)
        ]
        if expression.else_expr is not None:
            symbols.extend(_referenced_symbols(expression.else_expr, kind))
        return symbols
    return []


class FactValueComputation(StrictContract):
    fact_symbol: Symbol
    value_symbol: Symbol
    inputs: list[ComputationInputSpec] = Field(default_factory=list)
    expression: FactComputationExpression | None
    aggregation: Aggregation
    logical_type: LogicalType

    @model_validator(mode="after")
    def validate_inputs(self):
        if self.aggregation == "count_rows":
            if self.inputs or self.expression is not None:
                raise ValueError("COUNT_ROWS_INPUT_FORBIDDEN")
            return self
        if self.expression is None:
            raise ValueError("COMPUTATION_EXPRESSION_MISSING")
        declared = [item.symbol for item in self.inputs]
        if len(declared) != len(set(declared)):
            raise ValueError("COMPUTATION_INPUT_DUPLICATE")
        used = _referenced_symbols(self.expression, "input")
        unknown = sorted(set(used) - set(declared))
        if unknown:
            raise ValueError("COMPUTATION_INPUT_UNKNOWN: " + ", ".join(unknown))
        unused = sorted(set(declared) - set(used))
        if unused:
            raise ValueError("COMPUTATION_INPUT_UNUSED: " + ", ".join(unused))
        return self


class ResultValueComputation(StrictContract):
    output_symbol: Symbol
    expression: ResultComputationExpression


class ComputationBlueprint(StrictContract):
    version: Literal[1] = 1
    result_contract_hash: str = Field(min_length=64, max_length=64)
    fact_blueprint_hash: str = Field(min_length=64, max_length=64)
    fact_values: list[FactValueComputation] = Field(min_length=1)
    results: list[ResultValueComputation] = Field(min_length=1)
    result_filter: FilterComputationExpression | None = None

    @model_validator(mode="after")
    def validate_unique_targets_and_dependencies(self):
        fact_targets = [
            (item.fact_symbol, item.value_symbol) for item in self.fact_values
        ]
        if len(fact_targets) != len(set(fact_targets)):
            raise ValueError("COMPUTATION_TARGET_DUPLICATE")
        outputs = [item.output_symbol for item in self.results]
        if len(outputs) != len(set(outputs)):
            raise ValueError("COMPUTATION_TARGET_DUPLICATE")
        output_set = set(outputs)
        dependencies = {
            item.output_symbol: set(
                _referenced_symbols(item.expression, "output")
            )
            for item in self.results
        }
        unknown = sorted(
            {symbol for values in dependencies.values() for symbol in values}
            - output_set
        )
        if unknown:
            raise ValueError(
                "COMPUTATION_OUTPUT_UNKNOWN: " + ", ".join(unknown)
            )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(symbol: str):
            if symbol in visiting:
                raise ValueError("COMPUTATION_DEPENDENCY_CYCLE")
            if symbol in visited:
                return
            visiting.add(symbol)
            for dependency in dependencies[symbol]:
                visit(dependency)
            visiting.remove(symbol)
            visited.add(symbol)

        for output in outputs:
            visit(output)
        return self
