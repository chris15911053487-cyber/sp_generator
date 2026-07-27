import json

import pytest

from app.contracts.schema import (
    CatalogColumn,
    CatalogObject,
    EntityBindingProposal,
    FieldBindingProposal,
    SchemaBindingProposal,
)
from app.services.schema_resolution_v3 import make_issue
from app.services.semantic_revision_context import (
    MAX_REVISION_EVIDENCE_CHARS,
    MAX_REVISION_PROMPT_CHARS,
    SemanticRevisionContextError,
    build_semantic_revision_evidence,
    validate_semantic_revision_prompt,
)
from v3_test_helpers import catalog, contract


def _column(column_id: int, name: str) -> CatalogColumn:
    return CatalogColumn(
        column_id=column_id,
        name=name,
        sql_type="nvarchar",
        max_length=100,
        precision=None,
        scale=None,
        nullable=True,
        collation="Chinese_PRC_CI_AS",
    )


def _proposal() -> SchemaBindingProposal:
    return SchemaBindingProposal(
        entities=[
            EntityBindingProposal(
                entity_id="invoice",
                database="TEST_DB",
                schema="dbo",
                object="OINV",
                alias="inv",
            )
        ],
        fields=[
            FieldBindingProposal(
                binding_id="amount",
                semantic_id="amount",
                entity_id="invoice",
                column="DocTotal",
            )
        ],
    )


def _issue():
    return make_issue(
        code="SCHEMA_SEMANTIC_SHAPE_UNBINDABLE",
        category="semantic_capability_gap",
        semantic_id="amount",
        business_meaning="收入金额",
        reason="金额需要由两个物理字段计算",
        required_semantic_shape="derived_expression",
        catalog_evidence={
            "related_physical_fields": ["Debit", "Credit"],
        },
    )


def test_revision_evidence_excludes_large_unrelated_catalog():
    base = catalog()
    unrelated = [
        CatalogObject(
            schema="archive",
            name=f"UNRELATED_MARKER_{object_index}",
            object_id=1000 + object_index,
            object_type="table",
            columns=[
                _column(column_index + 1, f"unused_{column_index}")
                for column_index in range(80)
            ],
        )
        for object_index in range(40)
    ]
    huge = base.model_copy(update={
        "objects": [*base.objects, *unrelated],
    })

    evidence = build_semantic_revision_evidence(
        contract=contract(),
        catalog=huge,
        issues=[_issue()],
        partial_proposal=_proposal(),
    )
    encoded = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))

    assert len(encoded) <= MAX_REVISION_EVIDENCE_CHARS
    assert [item["object"] for item in evidence["relevant_objects"]] == [
        "OINV"
    ]
    assert "UNRELATED_MARKER" not in encoded


def test_revision_evidence_fails_closed_when_scope_is_too_broad():
    entities = [
        EntityBindingProposal(
            entity_id=f"entity_{index}",
            database="TEST_DB",
            schema="dbo",
            object=f"OBJECT_{index}",
            alias=f"e{index}",
        )
        for index in range(9)
    ]
    proposal = SchemaBindingProposal(
        entities=entities,
        fields=[
            FieldBindingProposal(
                binding_id="amount",
                semantic_id="amount",
                entity_id="entity_0",
                column="Amount",
            )
        ],
    )

    with pytest.raises(
        SemanticRevisionContextError,
        match="SEMANTIC_REVISION_EVIDENCE_TOO_BROAD",
    ):
        build_semantic_revision_evidence(
            contract=contract(),
            catalog=catalog(),
            issues=[_issue()],
            partial_proposal=proposal,
        )


def test_revision_prompt_has_a_hard_preflight_limit():
    validate_semantic_revision_prompt("x" * MAX_REVISION_PROMPT_CHARS)
    with pytest.raises(
        SemanticRevisionContextError,
        match="SEMANTIC_REVISION_PROMPT_TOO_LARGE",
    ):
        validate_semantic_revision_prompt(
            "x" * (MAX_REVISION_PROMPT_CHARS + 1)
        )
