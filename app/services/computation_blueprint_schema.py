"""Dynamic computation slots and deterministic target materialization."""

from __future__ import annotations

from pydantic import Field, create_model

from app.contracts.base import StrictContract
from app.contracts.computation_blueprint import (
    ComputationBlueprint,
    ComputationInputSpec,
    FactComputationExpression,
    FactValueComputation,
    FilterComputationExpression,
    ResultComputationExpression,
    ResultValueComputation,
)
from app.contracts.semantic_design import FactBlueprint, ResultContract


class FactValueComputationDraft(StrictContract):
    inputs: list[ComputationInputSpec] = Field(default_factory=list)
    expression: FactComputationExpression | None


class ResultValueComputationDraft(StrictContract):
    expression: ResultComputationExpression


def fact_value_slot(fact_symbol: str, value_symbol: str) -> str:
    return f"fact_value_{fact_symbol}_{value_symbol}"


def result_value_slot(output_symbol: str) -> str:
    return f"result_{output_symbol}"


def create_computation_blueprint_response_model(
    result: ResultContract,
    facts: FactBlueprint,
):
    fact_values_model = create_model(
        "RequiredFactComputations_" + facts.content_hash[:12],
        __base__=StrictContract,
        **{
            fact_value_slot(fact.symbol, value.symbol): (
                FactValueComputationDraft,
                ...,
            )
            for fact in facts.facts
            for value in fact.dimensions + fact.measures
        },
    )
    results_model = create_model(
        "RequiredResultComputations_" + result.content_hash[:12],
        __base__=StrictContract,
        **{
            result_value_slot(output.symbol): (
                ResultValueComputationDraft,
                ...,
            )
            for output in result.outputs
        },
    )
    filter_field = (
        (FilterComputationExpression, ...)
        if result.result_mode == "exception_rows"
        else (None, None)
    )
    return create_model(
        "ComputationBlueprintDraft_"
        + result.content_hash[:6]
        + facts.content_hash[:6],
        __base__=StrictContract,
        fact_values=(fact_values_model, ...),
        results=(results_model, ...),
        result_filter=filter_field,
    )


def materialize_computation_blueprint(
    draft,
    result: ResultContract,
    facts: FactBlueprint,
) -> ComputationBlueprint:
    fact_values = []
    for fact in facts.facts:
        for value in fact.dimensions + fact.measures:
            implementation = getattr(
                draft.fact_values,
                fact_value_slot(fact.symbol, value.symbol),
            )
            fact_values.append(FactValueComputation(
                fact_symbol=fact.symbol,
                value_symbol=value.symbol,
                inputs=implementation.inputs,
                expression=implementation.expression,
                aggregation=getattr(value, "aggregation", "none"),
                logical_type=value.logical_type,
            ))
    results = [
        ResultValueComputation(
            output_symbol=output.symbol,
            expression=getattr(
                draft.results,
                result_value_slot(output.symbol),
            ).expression,
        )
        for output in result.outputs
    ]
    return ComputationBlueprint(
        result_contract_hash=result.content_hash,
        fact_blueprint_hash=facts.content_hash,
        fact_values=fact_values,
        results=results,
        result_filter=draft.result_filter,
    )
