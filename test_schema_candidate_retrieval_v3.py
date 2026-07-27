import pytest

from app.contracts.schema import SchemaBindingDraft
from app.agent.nodes import (
    _compact_catalog_candidates_payload,
    _generate_schema_binding_proposal_v3,
    _resolve_deterministic_schema_ambiguities_v3,
    _resolve_catalog_candidates,
)
from app.contracts.schema import CatalogSnapshot
from app.contracts.semantic import SemanticContract
from v3_test_helpers import catalog, contract


def test_compact_catalog_omits_full_column_payload():
    payload = _compact_catalog_candidates_payload(catalog())

    assert payload
    assert "columns" not in payload[0]
    assert len(payload[0]["column_sample"]) <= 8


def test_resolve_catalog_candidates_returns_only_verified_objects():
    selected = _resolve_catalog_candidates(
        catalog(),
        {"objects": ["dbo.OINV"]},
    )

    assert [item.name for item in selected] == ["OINV"]


def test_resolve_catalog_candidates_rejects_unknown_or_unbounded_objects():
    with pytest.raises(ValueError, match="不在 Catalog"):
        _resolve_catalog_candidates(
            catalog(),
            {"objects": ["dbo.NOT_REAL"]},
        )

    with pytest.raises(ValueError, match="1~16"):
        _resolve_catalog_candidates(
            catalog(),
            {"objects": [f"dbo.T{i}" for i in range(17)]},
        )


def test_schema_draft_repairs_singleton_ambiguity_into_complete_proposal(
    monkeypatch,
):
    responses = iter([
        {"objects": ["dbo.OINV"]},
        {
            "proposal": None,
            "ambiguities": [{
                "semantic_id": "amount",
                "candidates": ["OINV.DocTotal"],
                "reason": "唯一候选但错误放入歧义",
            }],
        },
        {
            "proposal": {
                "entities": [{
                    "entity_id": "invoice",
                    "database": "TEST_DB",
                    "schema": "dbo",
                    "object": "OINV",
                    "alias": "i",
                }],
                "fields": [
                    {
                        "binding_id": "invoice_id",
                        "semantic_id": "invoice_id",
                        "entity_id": "invoice",
                        "column": "DocEntry",
                    },
                    {
                        "binding_id": "invoice_amount",
                        "semantic_id": "amount",
                        "entity_id": "invoice",
                        "column": "DocTotal",
                    },
                    {
                        "binding_id": "invoice_date",
                        "semantic_id": "invoice_date",
                        "entity_id": "invoice",
                        "column": "DocDate",
                    },
                ],
                "joins": [],
            },
            "ambiguities": [],
        },
    ])
    monkeypatch.setattr(
        "app.agent.nodes._candidate_json",
        lambda *_args, **_kwargs: next(responses),
    )

    proposal = _generate_schema_binding_proposal_v3(
        object(), contract(), catalog(),
    )

    assert proposal.fields[1].column_name == "DocTotal"


def test_schema_draft_can_represent_resolved_and_unresolved_parts_together():
    draft = SchemaBindingDraft.model_validate({
        "proposal": {
            "entities": [{
                "entity_id": "invoice",
                "database": "TEST_DB",
                "schema": "dbo",
                "object": "OINV",
                "alias": "i",
            }],
            "fields": [{
                "binding_id": "invoice_id",
                "semantic_id": "invoice_id",
                "entity_id": "invoice",
                "column": "DocEntry",
            }],
        },
        "ambiguities": [{
            "semantic_id": "amount",
            "candidates": ["OINV.DocTotal", "OINV.DocTotalSy"],
            "reason": "金额币种口径尚未确定",
        }],
    })

    assert draft.proposal is not None
    assert len(draft.ambiguities) == 1


def test_currency_ambiguity_is_resolved_only_when_one_candidate_matches():
    catalog_payload = catalog().model_dump(mode="python", by_alias=True)
    catalog_payload["objects"][0]["columns"].extend([
        {
            "column_id": 5,
            "name": "DocTotalFC",
            "sql_type": "decimal",
            "max_length": 9,
            "precision": 19,
            "scale": 6,
            "nullable": True,
            "collation": None,
        },
        {
            "column_id": 6,
            "name": "DocTotalSy",
            "sql_type": "decimal",
            "max_length": 9,
            "precision": 19,
            "scale": 6,
            "nullable": True,
            "collation": None,
        },
    ])
    semantic_payload = contract().model_dump(mode="json")
    semantic_payload["outputs"][1]["meaning"] = "账套本位币收入金额"
    semantic = SemanticContract.model_validate(semantic_payload)
    draft = SchemaBindingDraft.model_validate({
        "proposal": {
            "entities": [{
                "entity_id": "invoice",
                "database": "TEST_DB",
                "schema": "dbo",
                "object": "OINV",
                "alias": "i",
            }],
            "fields": [
                {
                    "binding_id": "invoice_id",
                    "semantic_id": "invoice_id",
                    "entity_id": "invoice",
                    "column": "DocEntry",
                },
                {
                    "binding_id": "invoice_date",
                    "semantic_id": "invoice_date",
                    "entity_id": "invoice",
                    "column": "DocDate",
                },
            ],
        },
        "ambiguities": [{
            "semantic_id": "amount",
            "candidates": [
                "OINV.DocTotal",
                "OINV.DocTotalFC",
                "OINV.DocTotalSy",
            ],
            "reason": "币种候选",
        }],
    })

    resolved = _resolve_deterministic_schema_ambiguities_v3(
        draft,
        semantic,
        CatalogSnapshot.model_validate(catalog_payload),
    )

    assert resolved.ambiguities == []
    amount = next(
        item for item in resolved.proposal.fields
        if item.semantic_id == "amount"
    )
    assert amount.column_name == "DocTotal"

    selected_wrong = SchemaBindingDraft.model_validate({
        "proposal": {
            **draft.proposal.model_dump(mode="python", by_alias=True),
            "fields": [
                *draft.proposal.model_dump(
                    mode="python", by_alias=True,
                )["fields"],
                {
                    "binding_id": "amount",
                    "semantic_id": "amount",
                    "entity_id": "invoice",
                    "column": "DocTotalSy",
                },
            ],
        },
        "ambiguities": [],
    })
    normalized = _resolve_deterministic_schema_ambiguities_v3(
        selected_wrong,
        semantic,
        CatalogSnapshot.model_validate(catalog_payload),
    )
    amount = next(
        item for item in normalized.proposal.fields
        if item.semantic_id == "amount"
    )
    assert amount.column_name == "DocTotal"
