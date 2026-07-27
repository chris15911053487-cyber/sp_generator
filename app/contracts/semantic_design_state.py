"""Persistent state for the staged semantic-design compiler."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from app.contracts.base import StrictContract
from app.contracts.computation_blueprint import ComputationBlueprint
from app.contracts.semantic_design import (
    ExpressionDesign,
    FactBlueprint,
    ResultContract,
    SourceRequirements,
)
from app.contracts.semantic_obligations import SemanticObligationSet
from app.contracts.semantic_input_obligations import SemanticInputObligationSet


SemanticDesignStage = Literal[
    "result_contract",
    "fact_blueprint",
    "computation_blueprint",
    "semantic_obligations",
    "semantic_inputs",
    "source_requirements",
    "expression_materialize",
    "semantic_compile",
]

SemanticDesignStatus = Literal[
    "building_result",
    "building_facts",
    "building_computations",
    "building_obligations",
    "building_inputs",
    "building_sources",
    "materializing_expressions",
    "compiling",
    "ready_for_confirmation",
    "confirmed",
    "failed",
    "invalidated",
]


class SemanticDesignDiagnostic(StrictContract):
    stage: SemanticDesignStage
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    business_element: str | None = None
    message: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)
    system_action: str = Field(min_length=1)
    user_action: str | None = None


class SemanticDesignCheckpoint(StrictContract):
    version: Literal[3] = 3
    checkpoint_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    decision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: SemanticDesignStage
    stage_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_contract: ResultContract | None = None
    fact_blueprint: FactBlueprint | None = None
    computation_blueprint: ComputationBlueprint | None = None
    semantic_obligations: SemanticObligationSet | None = None
    semantic_inputs: SemanticInputObligationSet | None = None
    source_requirements: SourceRequirements | None = None
    expression_design: ExpressionDesign | None = None
    compile_result: dict[str, Any] | None = None
    diagnostics: list[SemanticDesignDiagnostic] = Field(default_factory=list)
    repair_counts: dict[SemanticDesignStage, int] = Field(default_factory=dict)
    status: SemanticDesignStatus

    @model_validator(mode="after")
    def validate_state(self):
        if any(value < 0 or value > 1 for value in self.repair_counts.values()):
            raise ValueError("SEMANTIC_DESIGN_REPAIR_LIMIT_EXCEEDED")

        required = {
            "building_facts": ("result_contract",),
            "building_computations": (
                "result_contract", "fact_blueprint",
            ),
            "building_obligations": (
                "result_contract", "fact_blueprint",
                "computation_blueprint",
            ),
            "building_inputs": (
                "result_contract", "fact_blueprint",
                "computation_blueprint", "semantic_obligations",
            ),
            "building_sources": (
                "result_contract", "fact_blueprint",
                "computation_blueprint", "semantic_obligations",
                "semantic_inputs",
            ),
            "materializing_expressions": (
                "result_contract",
                "fact_blueprint",
                "computation_blueprint",
                "semantic_obligations",
                "semantic_inputs",
                "source_requirements",
            ),
            "compiling": (
                "result_contract",
                "fact_blueprint",
                "computation_blueprint",
                "semantic_obligations",
                "semantic_inputs",
                "source_requirements",
                "expression_design",
            ),
            "ready_for_confirmation": (
                "result_contract",
                "fact_blueprint",
                "computation_blueprint",
                "semantic_obligations",
                "semantic_inputs",
                "source_requirements",
                "expression_design",
                "compile_result",
            ),
            "confirmed": (
                "result_contract",
                "fact_blueprint",
                "computation_blueprint",
                "semantic_obligations",
                "semantic_inputs",
                "source_requirements",
                "expression_design",
                "compile_result",
            ),
        }
        missing = [
            field
            for field in required.get(self.status, ())
            if getattr(self, field) is None
        ]
        if missing:
            raise ValueError(
                "SEMANTIC_DESIGN_CHECKPOINT_INCOMPLETE: "
                + ", ".join(missing)
            )
        return self
