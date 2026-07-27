"""Build a bounded, issue-local catalog evidence pack for semantic revision."""

from __future__ import annotations

import json

from app.contracts.schema import CatalogSnapshot, SchemaBindingProposal
from app.contracts.schema_resolution import SchemaResolutionIssue
from app.contracts.semantic import SemanticContract


MAX_REVISION_OBJECTS = 8
MAX_REVISION_COLUMNS_PER_OBJECT = 128
MAX_REVISION_EVIDENCE_CHARS = 160_000
MAX_REVISION_PROMPT_CHARS = 240_000


class SemanticRevisionContextError(ValueError):
    pass


def validate_semantic_revision_prompt(prompt: str) -> None:
    if len(prompt) > MAX_REVISION_PROMPT_CHARS:
        raise SemanticRevisionContextError(
            "SEMANTIC_REVISION_PROMPT_TOO_LARGE: "
            f"{len(prompt)} > {MAX_REVISION_PROMPT_CHARS}"
        )


def _issue_column_names(
    issues: list[SchemaResolutionIssue],
) -> set[str]:
    names = set()
    for issue in issues:
        evidence = issue.catalog_evidence
        for key in ("related_physical_fields", "raw_candidates"):
            values = evidence.get(key) or []
            if not isinstance(values, list):
                continue
            for value in values:
                name = str(value).rsplit(".", 1)[-1].strip()
                if name:
                    names.add(name.casefold())
    return names


def _evidence_payload(
    *,
    contract: SemanticContract,
    catalog: CatalogSnapshot,
    issues: list[SchemaResolutionIssue],
    partial_proposal: SchemaBindingProposal | None,
    column_limit: int,
) -> dict:
    proposal_entities = (
        partial_proposal.entities if partial_proposal is not None else []
    )
    proposal_fields = (
        partial_proposal.fields if partial_proposal is not None else []
    )
    source_entity_by_field = {
        item.id.casefold(): item.entity_id.casefold()
        for item in contract.source_fields
    }
    issue_entity_ids = {
        source_entity_by_field[item.semantic_id.casefold()]
        for item in issues
        if (
            item.semantic_id
            and item.semantic_id.casefold() in source_entity_by_field
        )
    }
    ordered_entities = sorted(
        proposal_entities,
        key=lambda item: (
            item.entity_id.casefold() not in issue_entity_ids,
            item.entity_id.casefold(),
        ),
    )
    if len(ordered_entities) > MAX_REVISION_OBJECTS:
        raise SemanticRevisionContextError(
            "SEMANTIC_REVISION_EVIDENCE_TOO_BROAD: "
            f"相关实体 {len(ordered_entities)} 个，超过上限 "
            f"{MAX_REVISION_OBJECTS}"
        )

    catalog_objects = {
        (item.schema.casefold(), item.name.casefold()): item
        for item in catalog.objects
    }
    issue_columns = _issue_column_names(issues)
    selected_objects = []
    selected_object_ids = set()
    for entity in ordered_entities:
        physical = catalog_objects.get((
            entity.schema_name.casefold(),
            entity.object_name.casefold(),
        ))
        if physical is None:
            continue
        selected_object_ids.add(physical.object_id)
        bound_columns = {
            item.column_name.casefold()
            for item in proposal_fields
            if item.entity_id.casefold() == entity.entity_id.casefold()
        }
        key_column_ids = set(physical.primary_key)
        for key in physical.unique_keys:
            key_column_ids.update(key)
        physical_column_names = {
            item.name.casefold() for item in physical.columns
        }
        preferred = bound_columns | (
            issue_columns & physical_column_names
        )
        ordered_columns = sorted(
            physical.columns,
            key=lambda item: (
                item.name.casefold() not in preferred,
                item.column_id not in key_column_ids,
                item.column_id,
            ),
        )
        selected_columns = ordered_columns[:column_limit]
        missing_preferred = sorted(
            preferred
            - {item.name.casefold() for item in selected_columns}
        )
        if missing_preferred:
            raise SemanticRevisionContextError(
                "SEMANTIC_REVISION_EVIDENCE_COLUMN_OVERFLOW: "
                + ", ".join(missing_preferred)
            )
        selected_objects.append({
            "entity_id": entity.entity_id,
            "schema": physical.schema,
            "object": physical.name,
            "object_type": physical.object_type,
            "columns": [
                {
                    "name": item.name,
                    "sql_type": item.sql_type,
                    "nullable": item.nullable,
                    "precision": item.precision,
                    "scale": item.scale,
                }
                for item in selected_columns
            ],
            "primary_key_columns": [
                item.name for item in physical.columns
                if item.column_id in set(physical.primary_key)
            ],
        })

    foreign_keys = [
        {
            "name": item.name,
            "parent_object_id": item.parent_object_id,
            "parent_column_id": item.parent_column_id,
            "referenced_object_id": item.referenced_object_id,
            "referenced_column_id": item.referenced_column_id,
        }
        for item in catalog.foreign_keys
        if (
            item.parent_object_id in selected_object_ids
            and item.referenced_object_id in selected_object_ids
        )
    ]
    return {
        "catalog_fingerprint": catalog.content_hash,
        "database_name": catalog.database_name,
        "compatibility_level": catalog.compatibility_level,
        "issues": [
            item.model_dump(mode="json") for item in issues
        ],
        "partial_binding": (
            partial_proposal.model_dump(mode="json", by_alias=True)
            if partial_proposal is not None else None
        ),
        "relevant_objects": selected_objects,
        "relevant_foreign_keys": foreign_keys,
    }


def build_semantic_revision_evidence(
    *,
    contract: SemanticContract,
    catalog: CatalogSnapshot,
    issues: list[SchemaResolutionIssue],
    partial_proposal: SchemaBindingProposal | None,
) -> dict:
    """Return deterministic local evidence and never fall back to full Catalog."""
    if not issues:
        raise SemanticRevisionContextError(
            "SEMANTIC_REVISION_EVIDENCE_EMPTY"
        )
    column_limit = MAX_REVISION_COLUMNS_PER_OBJECT
    while column_limit >= 8:
        payload = _evidence_payload(
            contract=contract,
            catalog=catalog,
            issues=issues,
            partial_proposal=partial_proposal,
            column_limit=column_limit,
        )
        size = len(json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ))
        if size <= MAX_REVISION_EVIDENCE_CHARS:
            return payload
        column_limit //= 2
    raise SemanticRevisionContextError(
        "SEMANTIC_REVISION_EVIDENCE_TOO_LARGE: "
        f"最小证据包仍超过 {MAX_REVISION_EVIDENCE_CHARS} 字符"
    )
