"""Materialize the canonical expression design without an LLM call."""

from __future__ import annotations

from app.contracts.computation_blueprint import ComputationBlueprint
from app.contracts.semantic_design import (
    ExpressionDesign,
    FactBlueprint,
    FactDimensionExpression,
    FactMeasureExpression,
    ResultBindingExpression,
    SourceRequirements,
    make_symbol_expression,
)
from app.contracts.semantic_input_obligations import (
    SemanticInputObligationSet,
)


class ExpressionMaterializationError(ValueError):
    def __init__(self, code: str, message: str, *, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def _materialize_node(expression, input_symbols=None):
    if expression.kind == "input":
        source_symbol = input_symbols.get(expression.symbol)
        if source_symbol is None:
            raise ExpressionMaterializationError(
                "COMPUTATION_INPUT_UNKNOWN",
                "公式引用了没有输入义务的业务输入",
                evidence={"input": expression.symbol},
            )
        return make_symbol_expression(
            kind="source",
            symbol=source_symbol,
        )
    if expression.kind == "fact_value":
        return make_symbol_expression(
            kind="fact_value",
            fact_symbol=expression.fact_symbol,
            value_symbol=expression.value_symbol,
        )
    if expression.kind in {"output", "parameter"}:
        return make_symbol_expression(
            kind=expression.kind,
            symbol=expression.symbol,
        )
    if expression.kind == "literal":
        return make_symbol_expression(kind="literal", value=expression.value)
    if expression.kind in {"binary", "unary", "function"}:
        return make_symbol_expression(
            kind=expression.kind,
            operator=expression.operator,
            args=[
                _materialize_node(arg, input_symbols)
                for arg in expression.args
            ],
        )
    if expression.kind == "case":
        return make_symbol_expression(
            kind="case",
            cases=[
                {
                    "when": _materialize_node(branch.when, input_symbols),
                    "then": _materialize_node(branch.then, input_symbols),
                }
                for branch in expression.cases
            ],
            else_expr=(
                _materialize_node(expression.else_expr, input_symbols)
                if expression.else_expr is not None
                else None
            ),
        )
    raise ExpressionMaterializationError(
        "COMPUTATION_EXPRESSION_UNKNOWN",
        "存在不能物化的计算表达式节点",
        evidence={"kind": expression.kind},
    )


def materialize_expression_design(
    facts: FactBlueprint,
    computations: ComputationBlueprint,
    input_obligations: SemanticInputObligationSet,
    sources: SourceRequirements,
) -> ExpressionDesign:
    if input_obligations.fact_blueprint_hash != facts.content_hash:
        raise ExpressionMaterializationError(
            "COMPUTATION_TARGET_CHANGED",
            "输入义务引用的事实蓝图已经变化",
        )
    if (
        input_obligations.computation_blueprint_hash
        != computations.content_hash
    ):
        raise ExpressionMaterializationError(
            "COMPUTATION_TARGET_CHANGED",
            "输入义务引用的计算蓝图已经变化",
        )
    source_symbols = {item.symbol for item in sources.fields}
    required_symbols = {
        item.slot_name for item in input_obligations.inputs
    }
    missing = sorted(required_symbols - source_symbols)
    if missing:
        raise ExpressionMaterializationError(
            "SOURCE_INPUT_IMPLEMENTATION_MISSING",
            "部分计算输入没有来源实现",
            evidence={"missing": missing},
        )
    obligation_by_owner = {
        (item.fact_symbol, item.input_symbol): item
        for item in input_obligations.inputs
    }
    computation_by_target = {
        (item.fact_symbol, item.value_symbol): item
        for item in computations.fact_values
    }
    dimensions = []
    measures = []
    for fact in facts.facts:
        for dimension in fact.dimensions:
            computation = computation_by_target[
                (fact.symbol, dimension.symbol)
            ]
            inputs = {
                item.symbol: obligation_by_owner[
                    (fact.symbol, item.symbol)
                ].slot_name
                for item in computation.inputs
            }
            dimensions.append(FactDimensionExpression(
                fact_symbol=fact.symbol,
                dimension_symbol=dimension.symbol,
                expression=_materialize_node(computation.expression, inputs),
                logical_type=dimension.logical_type,
            ))
        for measure in fact.measures:
            computation = computation_by_target[
                (fact.symbol, measure.symbol)
            ]
            inputs = {
                item.symbol: obligation_by_owner[
                    (fact.symbol, item.symbol)
                ].slot_name
                for item in computation.inputs
            }
            measures.append(FactMeasureExpression(
                fact_symbol=fact.symbol,
                measure_symbol=measure.symbol,
                expression=(
                    _materialize_node(computation.expression, inputs)
                    if computation.expression is not None
                    else None
                ),
                aggregation=measure.aggregation,
                logical_type=measure.logical_type,
            ))
    results = [
        ResultBindingExpression(
            output_symbol=item.output_symbol,
            expression=_materialize_node(item.expression),
        )
        for item in computations.results
    ]
    return ExpressionDesign(
        dimensions=dimensions,
        measures=measures,
        results=results,
        result_filter=(
            _materialize_node(computations.result_filter)
            if computations.result_filter is not None
            else None
        ),
    )
