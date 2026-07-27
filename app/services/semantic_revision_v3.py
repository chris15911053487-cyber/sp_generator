"""Deterministic guard for restricted semantic implementation revisions."""

from __future__ import annotations

from typing import Any

from app.contracts.schema_resolution import (
    SchemaResolutionIssue,
    SemanticRevisionDiff,
    SemanticRevisionProposal,
)
from app.contracts.semantic import SemanticContract


_FROZEN_FIELDS = (
    "contract_id",
    "procedure_name",
    "purpose",
    "result_mode",
    "parameters",
    "grain",
    "outputs",
    "filters",
    "derived_fields",
    "result_bindings",
    "result_filter",
    "allow_empty",
    "money_tolerance",
)
_ALLOWED_FIELDS = (
    "entities",
    "source_fields",
    "facts",
    "fact_joins",
)


def _change(path: str, before: Any, after: Any) -> dict[str, Any]:
    return {"path": path, "before": before, "after": after}


def _source_expression_field_ids(expression) -> set[str]:
    if expression is None:
        return set()
    result = {expression.field_id} if expression.field_id else set()
    for item in expression.args:
        result.update(_source_expression_field_ids(item))
    for item in expression.cases:
        result.update(_source_expression_field_ids(item.when))
        result.update(_source_expression_field_ids(item.then))
    result.update(_source_expression_field_ids(expression.else_expr))
    return result


def evaluate_semantic_revision(
    base: SemanticContract,
    proposal: SemanticRevisionProposal,
    issues: list[SchemaResolutionIssue],
) -> SemanticRevisionDiff:
    if proposal.base_contract_hash != base.content_hash:
        return SemanticRevisionDiff(
            allowed=False,
            forbidden_changes=[{
                "path": "base_contract_hash",
                "before": base.content_hash,
                "after": proposal.base_contract_hash,
            }],
            unresolved_issue_ids=[item.issue_id for item in issues],
        )
    revised = proposal.revised_contract
    before = base.model_dump(mode="json", by_alias=True)
    after = revised.model_dump(mode="json", by_alias=True)
    forbidden = [
        _change(name, before[name], after[name])
        for name in _FROZEN_FIELDS
        if before[name] != after[name]
    ]
    allowed = [
        _change(name, before[name], after[name])
        for name in _ALLOWED_FIELDS
        if before[name] != after[name]
    ]
    known_issue_ids = {
        item.issue_id for item in issues
        if item.category == "semantic_capability_gap"
    }
    addressed = set(proposal.addressed_issue_ids)
    unresolved = sorted(known_issue_ids - addressed)
    unknown = sorted(addressed - known_issue_ids)
    if unknown:
        forbidden.append({
            "path": "addressed_issue_ids",
            "before": sorted(known_issue_ids),
            "after": sorted(addressed),
            "reason": "修订引用未知或非语义能力缺口问题",
        })

    source_ids = {item.id for item in revised.source_fields}
    used_source_ids = {
        field_id
        for item in revised.filters
        for field_id in item.field_ids
    }
    for fact in revised.facts:
        used_source_ids.update(
            item.field_id for item in fact.dimensions
            if item.field_id is not None
        )
        for dimension in fact.dimensions:
            used_source_ids.update(
                _source_expression_field_ids(dimension.expression)
            )
        for measure in fact.measures:
            if measure.field_id:
                used_source_ids.add(measure.field_id)
            used_source_ids.update(
                _source_expression_field_ids(measure.expression)
            )
    unused_added = sorted(
        (source_ids - {item.id for item in base.source_fields})
        - used_source_ids
    )
    if unused_added:
        forbidden.append({
            "path": "source_fields",
            "before": [],
            "after": unused_added,
            "reason": "新增源字段未被过滤或事实引用",
        })

    return SemanticRevisionDiff(
        allowed=not forbidden and not unresolved and bool(allowed),
        allowed_changes=allowed,
        forbidden_changes=forbidden,
        unresolved_issue_ids=unresolved,
    )
