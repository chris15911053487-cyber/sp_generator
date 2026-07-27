from app.contracts.schema import (
    EntityBindingProposal,
    FieldBindingProposal,
    SchemaBindingAmbiguity,
    SchemaBindingDraft,
    SchemaBindingProposal,
)
from app.services.schema_resolution_v3 import issues_from_draft
from app.services.schema_resolution_v3 import issue_from_exception
from app.services.schema_binding_v3 import SchemaBindingError
from v3_test_helpers import catalog, contract


def _partial_proposal():
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
                binding_id="invoice_date",
                semantic_id="invoice_date",
                entity_id="invoice",
                column="DocDate",
            ),
        ],
    )


def test_two_complete_physical_candidates_require_user_choice():
    schema_catalog = catalog()
    schema_catalog.objects[0].columns[2].name = "AmountA"
    second = schema_catalog.objects[0].columns[2].model_copy(
        update={"column_id": 5, "name": "AmountB"},
    )
    schema_catalog.objects[0].columns.append(second)
    draft = SchemaBindingDraft(
        proposal=_partial_proposal(),
        ambiguities=[
            SchemaBindingAmbiguity(
                semantic_id="amount",
                candidates=["AmountA", "AmountB"],
                reason="两个字段都完整表达同一业务口径",
                required_semantic_shape="user_choice_required",
            )
        ],
    )

    issues = issues_from_draft(contract(), schema_catalog, draft)

    assert len(issues) == 1
    assert issues[0].category == "physical_ambiguity"
    assert len(issues[0].physical_candidates) == 2


def test_multiple_component_columns_are_a_capability_gap_not_a_choice():
    draft = SchemaBindingDraft(
        proposal=_partial_proposal(),
        ambiguities=[
            SchemaBindingAmbiguity(
                semantic_id="amount",
                candidates=["DocTotal", "DocDate"],
                reason="没有单列能表达业务金额，需要组合计算",
                required_semantic_shape="derived_expression",
            )
        ],
    )

    issues = issues_from_draft(contract(), catalog(), draft)

    assert len(issues) == 1
    assert issues[0].category == "semantic_capability_gap"
    assert issues[0].required_semantic_shape == "derived_expression"
    assert issues[0].physical_candidates == []


def test_physical_proposal_missing_an_existing_graph_edge_is_repairable():
    issue = issue_from_exception(
        contract(),
        SchemaBindingError(
            "SCHEMA_FACT_ENTITY_GRAPH_DISCONNECTED",
            "事实所需实体尚未被物理关联",
            evidence={"fact_id": "invoice_income"},
        ),
    )

    assert issue.category == "binding_repairable"
    assert issue.allowed_action == "auto_repair"
