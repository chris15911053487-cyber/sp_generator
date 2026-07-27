"""LLM-facing source requirements without writable policy ownership."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from app.contracts.base import StrictContract
from app.contracts.semantic import LogicalType
from app.contracts.semantic_design import Symbol


FilterOperator = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte", "like",
    "is_null", "is_not_null", "between", "full_day_range",
]


class OrdinaryFilterRequirement(StrictContract):
    symbol: Symbol
    meaning: str = Field(min_length=1)
    source_symbol: Symbol
    parameter_symbols: list[Symbol] = Field(default_factory=list)
    operator: FilterOperator
    literal_values: list[Any] = Field(default_factory=list)
    fact_symbols: list[Symbol] = Field(min_length=1)
    skip_when_parameter_null: bool = False

    @model_validator(mode="after")
    def validate_operator_arguments(self):
        _validate_filter_arguments(
            self.operator,
            self.parameter_symbols,
            self.literal_values,
        )
        _validate_optional_bypass(
            self.parameter_symbols,
            self.literal_values,
            self.skip_when_parameter_null,
        )
        return self


class SourceInputImplementation(StrictContract):
    entity_symbol: Symbol
    meaning: str = Field(min_length=1)
    nullable: bool


class PolicyFilterImplementation(StrictContract):
    entity_symbol: Symbol
    source_meaning: str = Field(min_length=1)
    source_logical_type: LogicalType
    nullable: bool
    operator: FilterOperator
    parameter_symbols: list[Symbol] = Field(default_factory=list)
    literal_values: list[Any] = Field(default_factory=list)
    meaning: str = Field(min_length=1)
    skip_when_parameter_null: bool = False

    @model_validator(mode="after")
    def validate_operator_arguments(self):
        _validate_filter_arguments(
            self.operator,
            self.parameter_symbols,
            self.literal_values,
        )
        _validate_optional_bypass(
            self.parameter_symbols,
            self.literal_values,
            self.skip_when_parameter_null,
        )
        return self


def _validate_filter_arguments(operator, parameters, literals) -> None:
    if operator in {"is_null", "is_not_null"}:
        if parameters or literals:
            raise ValueError("SOURCE_FILTER_ARGUMENT_COUNT_INVALID")
        return
    if operator in {"between", "full_day_range"}:
        if len(parameters) != 2 or literals:
            raise ValueError("SOURCE_FILTER_ARGUMENT_COUNT_INVALID")
        return
    if len(parameters) + len(literals) != 1:
        raise ValueError("SOURCE_FILTER_ARGUMENT_COUNT_INVALID")


def _validate_optional_bypass(parameters, literals, enabled) -> None:
    if enabled and (len(parameters) != 1 or literals):
        raise ValueError("SOURCE_OPTIONAL_FILTER_SHAPE_INVALID")
