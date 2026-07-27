"""Deterministic transitions and downstream invalidation for design stages."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from app.contracts.semantic_design_state import (
    SemanticDesignCheckpoint,
    SemanticDesignDiagnostic,
    SemanticDesignStage,
)


_STAGE_FIELDS = {
    "result_contract": "result_contract",
    "fact_blueprint": "fact_blueprint",
    "computation_blueprint": "computation_blueprint",
    "semantic_obligations": "semantic_obligations",
    "semantic_inputs": "semantic_inputs",
    "source_requirements": "source_requirements",
    "expression_materialize": "expression_design",
    "semantic_compile": "compile_result",
}
_STAGE_ORDER = tuple(_STAGE_FIELDS)
_NEXT_STAGE = {
    "result_contract": ("fact_blueprint", "building_facts"),
    "fact_blueprint": (
        "computation_blueprint", "building_computations",
    ),
    "computation_blueprint": (
        "semantic_obligations", "building_obligations",
    ),
    "semantic_obligations": ("semantic_inputs", "building_inputs"),
    "semantic_inputs": ("source_requirements", "building_sources"),
    "source_requirements": (
        "expression_materialize", "materializing_expressions",
    ),
    "expression_materialize": ("semantic_compile", "compiling"),
    "semantic_compile": ("semantic_compile", "ready_for_confirmation"),
}


def _canonical(value: Any) -> str:
    if value is None:
        return "null"
    if hasattr(value, "canonical_json"):
        return value.canonical_json()
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stage_input_hash(
    checkpoint: SemanticDesignCheckpoint,
    stage: SemanticDesignStage,
) -> str:
    index = _STAGE_ORDER.index(stage)
    inputs = [checkpoint.decision_hash]
    for prior_stage in _STAGE_ORDER[:index]:
        inputs.append(_canonical(getattr(
            checkpoint, _STAGE_FIELDS[prior_stage],
        )))
    return hashlib.sha256("\n".join(inputs).encode("utf-8")).hexdigest()


def new_semantic_design_checkpoint(
    session_id: str,
    decision_hash: str,
) -> SemanticDesignCheckpoint:
    seed = SemanticDesignCheckpoint(
        checkpoint_id=str(uuid.uuid4()),
        session_id=session_id,
        decision_hash=decision_hash,
        stage="result_contract",
        stage_input_hash="0" * 64,
        status="building_result",
    )
    return seed.model_copy(update={
        "stage_input_hash": stage_input_hash(seed, "result_contract"),
    })


def advance_semantic_design_checkpoint(
    checkpoint: SemanticDesignCheckpoint,
    completed_stage: SemanticDesignStage,
    artifact: Any,
) -> SemanticDesignCheckpoint:
    field = _STAGE_FIELDS[completed_stage]
    current = getattr(checkpoint, field)
    changed = _canonical(current) != _canonical(artifact)
    update: dict[str, Any] = {
        field: artifact,
        "diagnostics": [],
    }
    if changed:
        index = _STAGE_ORDER.index(completed_stage)
        for downstream in _STAGE_ORDER[index + 1:]:
            update[_STAGE_FIELDS[downstream]] = None

    next_stage, status = _NEXT_STAGE[completed_stage]
    update.update({"stage": next_stage, "status": status})
    candidate = checkpoint.model_copy(update=update)
    candidate = candidate.model_copy(update={
        "stage_input_hash": stage_input_hash(candidate, next_stage),
    })
    return SemanticDesignCheckpoint.model_validate(
        candidate.model_dump(mode="json"),
    )


def record_semantic_design_failure(
    checkpoint: SemanticDesignCheckpoint,
    diagnostic: SemanticDesignDiagnostic,
) -> SemanticDesignCheckpoint:
    counts = dict(checkpoint.repair_counts)
    attempts = counts.get(diagnostic.stage, 0) + 1
    counts[diagnostic.stage] = attempts
    status = "failed" if attempts > 1 else checkpoint.status
    candidate = checkpoint.model_copy(update={
        "repair_counts": counts,
        "diagnostics": [diagnostic],
        "status": status,
    })
    if attempts > 1:
        # Build the failed state without weakening the public checkpoint model's
        # repair-count invariant. The caller must stop before persisting a retry.
        raise ValueError(
            f"SEMANTIC_DESIGN_REPAIR_LIMIT_EXCEEDED: {diagnostic.stage}"
        )
    return SemanticDesignCheckpoint.model_validate(
        candidate.model_dump(mode="json"),
    )
