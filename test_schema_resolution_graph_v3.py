from app.agent import nodes
from app.contracts.schema import (
    EntityBindingProposal,
    FieldBindingProposal,
    SchemaBindingAmbiguity,
    SchemaBindingDraft,
    SchemaBindingProposal,
)
from app.contracts.semantic import SemanticDesign
from app.contracts.schema_resolution import SchemaResolutionCheckpoint
from app.db import sqlite as sqlite_db
from app.services.catalog_v3 import catalog_fingerprint
from v3_test_helpers import catalog, contract


def _design():
    return SemanticDesign(
        design_version="schema-state-machine",
        decision_hash="confirmed-decisions",
        contracts=[contract()],
    )


def _proposal():
    return SchemaBindingProposal(
        entities=[
            EntityBindingProposal(
                entity_id="invoice",
                database="TEST_DB",
                schema="dbo",
                object="OINV",
                alias="i",
            )
        ],
        fields=[
            FieldBindingProposal(
                binding_id="invoice_id",
                semantic_id="invoice_id",
                entity_id="invoice",
                column="DocEntry",
            ),
            FieldBindingProposal(
                binding_id="amount",
                semantic_id="amount",
                entity_id="invoice",
                column="DocTotal",
            ),
            FieldBindingProposal(
                binding_id="invoice_date",
                semantic_id="invoice_date",
                entity_id="invoice",
                column="DocDate",
            ),
        ],
    )


def _state(session_id):
    design = _design()
    schema_catalog = catalog()
    return {
        "session_id": session_id,
        "query_spec": design.model_dump(mode="json", by_alias=True),
        "semantic_design_hash": design.content_hash,
        "schema_catalog": schema_catalog.model_dump(
            mode="json", by_alias=True,
        ),
        "schema_fingerprint": catalog_fingerprint(schema_catalog),
    }


def test_schema_resolve_freezes_binding_before_downstream(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(sqlite_db, "DB_PATH", str(tmp_path / "resolved.db"))
    sqlite_db.init_db()
    session = sqlite_db.create_session("resolved")
    monkeypatch.setattr(nodes, "_get_llm", lambda: object())
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    monkeypatch.setattr(
        nodes,
        "_generate_schema_binding_proposal_v3",
        lambda *_args, **_kwargs: SchemaBindingDraft(
            proposal=_proposal(),
        ),
    )

    result = nodes.schema_resolve_node(_state(session["id"]))

    assert result["status"] == "schema_resolved"
    assert result["mode"] == "generate"
    assert len(result["schema_artifacts"]) == 1
    checkpoint = sqlite_db.get_schema_resolution_checkpoint(
        session["id"], "invoice_income",
    )
    assert checkpoint["status"] == "resolved"


def test_capability_gap_routes_to_semantic_revision_not_binding_retry(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(sqlite_db, "DB_PATH", str(tmp_path / "gap.db"))
    sqlite_db.init_db()
    session = sqlite_db.create_session("gap")
    calls = []
    monkeypatch.setattr(nodes, "_get_llm", lambda: object())
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)

    def draft(*_args, **_kwargs):
        calls.append("propose")
        return SchemaBindingDraft(
            proposal=_proposal(),
            ambiguities=[
                SchemaBindingAmbiguity(
                    semantic_id="amount",
                    candidates=["DocTotal", "CardCode"],
                    reason="业务金额需要多个底层组成项计算",
                    required_semantic_shape="derived_expression",
                )
            ],
        )

    monkeypatch.setattr(
        nodes, "_generate_schema_binding_proposal_v3", draft,
    )

    result = nodes.schema_resolve_node(_state(session["id"]))

    assert result["status"] == "semantic_revision_required"
    assert result["schema_resolution_issues"][0]["category"] == (
        "semantic_capability_gap"
    )
    restored = SchemaResolutionCheckpoint.model_validate(
        result["schema_resolution_checkpoints"][0]
    )
    assert restored.partial_proposal == _proposal()
    assert calls == ["propose"]
