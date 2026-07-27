"""Program-owned source-input obligations derived from frozen computations."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.contracts.base import StrictContract
from app.contracts.semantic import LogicalType
from app.contracts.semantic_design import Symbol


class SemanticInputObligation(StrictContract):
    obligation_id: str = Field(min_length=64, max_length=64)
    slot_name: Symbol
    fact_symbol: Symbol
    value_symbols: list[Symbol] = Field(min_length=1)
    input_symbol: Symbol
    meaning: str = Field(min_length=1)
    logical_type: LogicalType
    nullable: bool
    usage_paths: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_paths_and_targets(self):
        if len(self.value_symbols) != len(set(self.value_symbols)):
            raise ValueError("COMPUTATION_INPUT_TARGET_DUPLICATE")
        if len(self.usage_paths) != len(set(self.usage_paths)):
            raise ValueError("COMPUTATION_INPUT_USAGE_DUPLICATE")
        return self


class SemanticInputObligationSet(StrictContract):
    version: Literal[1] = 1
    result_contract_hash: str = Field(min_length=64, max_length=64)
    fact_blueprint_hash: str = Field(min_length=64, max_length=64)
    computation_blueprint_hash: str = Field(min_length=64, max_length=64)
    inputs: list[SemanticInputObligation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_owners(self):
        owners = [
            (item.fact_symbol, item.input_symbol) for item in self.inputs
        ]
        if len(owners) != len(set(owners)):
            raise ValueError("COMPUTATION_INPUT_DUPLICATE")
        ids = [item.obligation_id for item in self.inputs]
        slots = [item.slot_name for item in self.inputs]
        if len(ids) != len(set(ids)) or len(slots) != len(set(slots)):
            raise ValueError("COMPUTATION_INPUT_DUPLICATE")
        return self
