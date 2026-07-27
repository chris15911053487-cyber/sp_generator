from app.contracts.relational_plan import Expression
from app.contracts.schema import (
    CatalogSnapshot,
    EntityBindingProposal,
    FieldBindingProposal,
    SchemaBindingProposal,
)
from app.contracts.semantic import SemanticContract
from app.services.plan_semantics_v3 import validate_plan_semantics
from app.services.schema_binding_v3 import build_schema_binding
from v3_test_helpers import catalog, contract, plan


def _contract_with_cancel_filter():
    payload = contract().model_dump(mode="json", by_alias=True)
    payload["filters"].append({
        "id": "exclude_cancelled",
        "meaning": "仅返回未取消发票",
        "field_ids": ["cancellation_status"],
        "parameter_ids": [],
        "operator": "eq",
        "literal_values": ["not_cancelled"],
    })
    return SemanticContract.model_validate(payload)


def _catalog_with_cancel_column():
    payload = catalog().model_dump(mode="python", by_alias=True)
    payload["objects"][0]["columns"].append({
        "column_id": 5,
        "name": "CANCELED",
        "sql_type": "char",
        "max_length": 1,
        "precision": 0,
        "scale": 0,
        "nullable": False,
        "collation": "Latin1_General_CI_AS",
    })
    return CatalogSnapshot.model_validate(payload)


def _binding_with_cancel_literal():
    semantic = _contract_with_cancel_filter()
    physical_catalog = _catalog_with_cancel_column()
    proposal = SchemaBindingProposal(
        entities=[EntityBindingProposal(
            entity_id="invoice",
            database="TEST_DB",
            schema="dbo",
            object="OINV",
            alias="i",
        )],
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
            FieldBindingProposal(
                binding_id="invoice_amount",
                semantic_id="amount",
                entity_id="invoice",
                column="DocTotal",
            ),
            FieldBindingProposal(
                binding_id="cancel_status",
                semantic_id="cancellation_status",
                entity_id="invoice",
                column="CANCELED",
                literal_map={"not_cancelled": "N"},
            ),
        ],
    )
    return (
        semantic,
        build_schema_binding(semantic, physical_catalog, proposal),
    )


def test_schema_binding_freezes_semantic_to_physical_literal_map():
    _semantic, binding = _binding_with_cancel_literal()

    assert binding.field("cancel_status").literal_map == {
        "not_cancelled": "N",
    }


def test_plan_filter_coverage_uses_physical_literal():
    semantic, binding = _binding_with_cancel_literal()
    relational_plan = plan()
    date_filter = relational_plan.root.input
    date_filter.predicate = Expression(
        kind="binary",
        operator="AND",
        args=[
            date_filter.predicate,
            Expression(
                kind="binary",
                operator="=",
                args=[
                    Expression(
                        kind="column",
                        field_binding_id="cancel_status",
                    ),
                    Expression(
                        kind="literal",
                        value="N",
                        value_type="string",
                    ),
                ],
            ),
        ],
    )

    validate_plan_semantics(relational_plan, semantic, binding)
