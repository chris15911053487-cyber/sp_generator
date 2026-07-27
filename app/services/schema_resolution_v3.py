"""Deterministic routing for Schema resolution issues."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.contracts.schema import CatalogSnapshot, SchemaBindingDraft
from app.contracts.schema_resolution import (
    SchemaBindingCandidate,
    SchemaResolutionIssue,
)
from app.contracts.semantic import SemanticContract


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def make_binding_candidate(
    *,
    semantic_id: str,
    business_label: str,
    physical_binding_fragment: dict,
    evidence: dict | None = None,
    consequences: list[str] | None = None,
) -> SchemaBindingCandidate:
    candidate_id = _hash_payload({
        "semantic_id": semantic_id,
        "physical_binding_fragment": physical_binding_fragment,
    })
    return SchemaBindingCandidate(
        candidate_id=candidate_id,
        semantic_id=semantic_id,
        business_label=business_label,
        physical_binding_fragment=physical_binding_fragment,
        evidence=evidence or {},
        consequences=consequences or [],
    )


def make_issue(
    *,
    code: str,
    category: str,
    semantic_id: str | None,
    business_meaning: str,
    reason: str,
    required_semantic_shape: str,
    catalog_evidence: dict | None = None,
    physical_candidates: list[SchemaBindingCandidate] | None = None,
) -> SchemaResolutionIssue:
    action = {
        "binding_repairable": "auto_repair",
        "physical_ambiguity": "user_select",
        "semantic_capability_gap": "revise_semantic_shape",
        "environment": "retry_environment",
        "internal_generation": "stop",
    }[category]
    identity = {
        "code": code,
        "category": category,
        "semantic_id": semantic_id,
        "required_semantic_shape": required_semantic_shape,
        "catalog_evidence": catalog_evidence or {},
        "candidate_ids": [
            item.candidate_id for item in (physical_candidates or [])
        ],
    }
    return SchemaResolutionIssue(
        issue_id=_hash_payload(identity),
        code=code,
        category=category,
        semantic_id=semantic_id,
        business_meaning=business_meaning,
        reason=reason,
        catalog_evidence=catalog_evidence or {},
        required_semantic_shape=required_semantic_shape,
        physical_candidates=physical_candidates or [],
        allowed_action=action,
    )


def _semantic_meaning(
    contract: SemanticContract,
    semantic_id: str | None,
) -> str:
    if semantic_id:
        for item in contract.source_fields:
            if item.id == semantic_id:
                return item.meaning
        for item in contract.outputs:
            if item.id == semantic_id:
                return item.meaning
    return contract.purpose


def issues_from_draft(
    contract: SemanticContract,
    catalog: CatalogSnapshot,
    draft: SchemaBindingDraft,
) -> list[SchemaResolutionIssue]:
    """Convert unresolved draft items without guessing a physical choice."""
    issues = []
    object_by_name = {
        f"{item.schema}.{item.name}".casefold(): item
        for item in catalog.objects
    }
    proposed_entities = {
        item.entity_id: item
        for item in (draft.proposal.entities if draft.proposal else [])
    }
    source_entities = {
        item.id: item.entity_id for item in contract.source_fields
    }
    for ambiguity in draft.ambiguities:
        if ambiguity.required_semantic_shape != "user_choice_required":
            issues.append(make_issue(
                code="SCHEMA_SEMANTIC_SHAPE_UNBINDABLE",
                category="semantic_capability_gap",
                semantic_id=ambiguity.semantic_id,
                business_meaning=_semantic_meaning(
                    contract, ambiguity.semantic_id,
                ),
                reason=ambiguity.reason,
                required_semantic_shape=ambiguity.required_semantic_shape,
                catalog_evidence={
                    "catalog_fingerprint": catalog.content_hash,
                    "related_physical_fields": ambiguity.candidates,
                },
            ))
            continue
        candidates = []
        for raw in ambiguity.candidates:
            parts = str(raw).split(".")
            column_name = parts[-1]
            if len(parts) >= 2:
                object_name = parts[-2]
                matched = [
                    item for key, item in object_by_name.items()
                    if key.endswith(f".{object_name}".casefold())
                ]
            else:
                entity_id = source_entities.get(ambiguity.semantic_id)
                if entity_id is None and len(contract.entities) == 1:
                    entity_id = contract.entities[0].id
                proposed = proposed_entities.get(entity_id)
                matched = []
                if proposed is not None:
                    physical = object_by_name.get(
                        f"{proposed.schema_name}.{proposed.object_name}".casefold()
                    )
                    if physical is not None:
                        matched = [physical]
            for physical in matched:
                column = next(
                    (
                        item for item in physical.columns
                        if item.name.casefold() == column_name.casefold()
                    ),
                    None,
                )
                if column is None:
                    continue
                candidates.append(make_binding_candidate(
                    semantic_id=ambiguity.semantic_id,
                    business_label=(
                        f"{physical.schema}.{physical.name}.{column.name}"
                    ),
                    physical_binding_fragment={
                        "schema": physical.schema,
                        "object": physical.name,
                        "object_id": physical.object_id,
                        "column": column.name,
                        "column_id": column.column_id,
                    },
                    evidence={
                        "sql_type": column.sql_type,
                        "nullable": column.nullable,
                    },
                ))
        if len(candidates) >= 2:
            issues.append(make_issue(
                code="SCHEMA_PHYSICAL_BINDING_AMBIGUOUS",
                category="physical_ambiguity",
                semantic_id=ambiguity.semantic_id,
                business_meaning=_semantic_meaning(
                    contract, ambiguity.semantic_id,
                ),
                reason=ambiguity.reason,
                required_semantic_shape="user_choice_required",
                catalog_evidence={
                    "catalog_fingerprint": catalog.content_hash,
                    "raw_candidates": ambiguity.candidates,
                },
                physical_candidates=candidates,
            ))
        else:
            issues.append(make_issue(
                code="SCHEMA_SEMANTIC_SHAPE_UNBINDABLE",
                category="semantic_capability_gap",
                semantic_id=ambiguity.semantic_id,
                business_meaning=_semantic_meaning(
                    contract, ambiguity.semantic_id,
                ),
                reason=(
                    "Catalog 证据不能形成两个可直接选择且完整表达业务含义"
                    f"的物理绑定：{ambiguity.reason}"
                ),
                required_semantic_shape="derived_expression",
                catalog_evidence={
                    "catalog_fingerprint": catalog.content_hash,
                    "raw_candidates": ambiguity.candidates,
                    "verified_candidate_count": len(candidates),
                },
            ))
    return issues


def issue_from_exception(
    contract: SemanticContract,
    exc: Exception,
) -> SchemaResolutionIssue:
    code = str(getattr(exc, "code", "SCHEMA_INTERNAL_GENERATION_FAILED"))
    evidence = dict(getattr(exc, "evidence", {}) or {})
    environment_codes = {
        "CATALOG_CAPTURE_FAILED",
        "CATALOG_PERMISSION_DENIED",
        "SCHEMA_DATABASE_IDENTITY_CHANGED",
    }
    capability_codes = {
        "SCHEMA_SEMANTIC_SHAPE_UNBINDABLE",
        "SCHEMA_REQUIRED_ENTITY_MISSING",
        "SCHEMA_REQUIRED_JOIN_MISSING",
    }
    internal_codes = {
        "SCHEMA_CANDIDATE_RETRIEVAL_FAILED",
        "SCHEMA_BINDING_DRAFT_INVALID",
    }
    if code in environment_codes:
        category = "environment"
        shape = "direct_field"
    elif code in capability_codes:
        category = "semantic_capability_gap"
        shape = (
            "missing_join"
            if "JOIN" in code or "GRAPH" in code
            else "multi_entity_fact"
        )
    elif code in internal_codes:
        category = "internal_generation"
        shape = "direct_field"
    else:
        category = "binding_repairable"
        shape = (
            "literal_mapping" if "LITERAL" in code else "direct_field"
        )
    semantic_id = evidence.get("semantic_id")
    return make_issue(
        code=code,
        category=category,
        semantic_id=semantic_id,
        business_meaning=_semantic_meaning(contract, semantic_id),
        reason=str(exc),
        required_semantic_shape=shape,
        catalog_evidence=evidence,
    )
