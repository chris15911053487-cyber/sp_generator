from types import SimpleNamespace

import pytest

from app.contracts.schema import CatalogSnapshot, SchemaBindingProposal
from app.contracts.semantic import SemanticContract
from app.services.schema_binding_v3 import (
    SchemaBindingError,
    _validate_fact_entity_connectivity,
    build_schema_binding,
    validate_binding_against_catalog,
)
from v3_test_helpers import binding, catalog, contract


def test_binding_freezes_real_object_and_column_ids():
    value = binding()
    assert value.entities[0].object_id == 101
    assert value.field("invoice_id").column_id == 1
    assert value.field("invoice_amount").column == "DocTotal"


def test_virtual_sp_result_object_is_rejected():
    proposal = SchemaBindingProposal.model_validate(
        {
            "entities": [
                {
                    "entity_id": "invoice",
                    "database": "TEST_DB",
                    "schema": "dbo",
                    "object": "SP_RESULT",
                    "alias": "r",
                }
            ],
            "fields": [
                {
                    "binding_id": "invoice_id",
                    "semantic_id": "invoice_id",
                    "entity_id": "invoice",
                    "column": "InvoiceId",
                }
            ],
        }
    )
    with pytest.raises(SchemaBindingError) as error:
        build_schema_binding(contract(), catalog(), proposal)
    assert error.value.code == "SCHEMA_OBJECT_NOT_FOUND"


def test_database_identity_must_match_snapshot():
    payload = {
        "entities": [
            {
                "entity_id": "invoice",
                "database": "OTHER_DB",
                "schema": "dbo",
                "object": "OINV",
                "alias": "i",
            }
        ],
        "fields": [
            {
                "binding_id": "invoice_id",
                "semantic_id": "invoice_id",
                "entity_id": "invoice",
                "column": "DocEntry",
            }
        ],
    }
    with pytest.raises(SchemaBindingError) as error:
        build_schema_binding(
            contract(),
            catalog(),
            SchemaBindingProposal.model_validate(payload),
        )
    assert error.value.code == "ENV_DATABASE_IDENTITY_MISMATCH"


def test_fact_entities_must_be_connected_before_binding_is_frozen():
    semantic = SimpleNamespace(
        facts=[
            SimpleNamespace(
                id="invoice_revenue",
                entity_ids=["invoice_header", "invoice_line"],
            )
        ]
    )

    with pytest.raises(SchemaBindingError) as error:
        _validate_fact_entity_connectivity(semantic, [])

    assert error.value.code == "SCHEMA_FACT_ENTITY_GRAPH_DISCONNECTED"
    assert error.value.evidence["fact_id"] == "invoice_revenue"


def test_fact_entity_connectivity_accepts_a_complete_join_graph():
    semantic = SimpleNamespace(
        facts=[
            SimpleNamespace(
                id="invoice_revenue",
                entity_ids=["invoice_header", "invoice_line", "tax_detail"],
            )
        ]
    )
    joins = [
        SimpleNamespace(
            left_entity="invoice_header",
            right_entity="invoice_line",
        ),
        SimpleNamespace(
            left_entity="invoice_line",
            right_entity="tax_detail",
        ),
    ]

    _validate_fact_entity_connectivity(semantic, joins)


def _currency_catalog():
    payload = catalog().model_dump(mode="python", by_alias=True)
    payload["objects"][0]["columns"].extend([
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
    return CatalogSnapshot.model_validate(payload)


def _currency_contract(meaning):
    payload = contract().model_dump(mode="json")
    payload["outputs"][1]["meaning"] = meaning
    return SemanticContract.model_validate(payload)


def _currency_proposal(column):
    return SchemaBindingProposal.model_validate({
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
            {
                "binding_id": "invoice_amount",
                "semantic_id": "amount",
                "entity_id": "invoice",
                "column": column,
            },
        ],
    })


def test_local_currency_semantics_reject_system_currency_column():
    semantic = _currency_contract("按账套本位币统计收入金额")
    with pytest.raises(SchemaBindingError) as error:
        build_schema_binding(
            semantic,
            _currency_catalog(),
            _currency_proposal("DocTotalSy"),
        )
    assert error.value.code == "SCHEMA_CURRENCY_SCOPE_MISMATCH"


def test_system_currency_semantics_accept_system_currency_column():
    semantic = _currency_contract("按账套系统币统计收入金额")
    value = build_schema_binding(
        semantic,
        _currency_catalog(),
        _currency_proposal("DocTotalSy"),
    )
    assert value.field("invoice_amount").column == "DocTotalSy"


def test_document_currency_semantics_reject_single_amount_column():
    semantic = _currency_contract("按单据原始币种统计收入金额")
    with pytest.raises(SchemaBindingError) as error:
        build_schema_binding(
            semantic,
            _currency_catalog(),
            _currency_proposal("DocTotalFC"),
        )
    assert error.value.code == "SCHEMA_CURRENCY_SCOPE_AMBIGUOUS"


def test_frozen_binding_revalidation_does_not_require_derived_output_column():
    payload = contract().model_dump(mode="json")
    payload["outputs"].append({
        "id": "net_amount",
        "name": "NetAmount",
        "meaning": "按冻结公式计算净额",
        "logical_type": "money",
        "nullable": False,
    })
    payload["derived_fields"] = [{
        "output_id": "net_amount",
        "expression": {
            "kind": "binary",
            "operator": "-",
            "args": [
                {"kind": "output", "output_id": "amount"},
                {"kind": "literal", "value": 0},
            ],
        },
    }]
    semantic = SemanticContract.model_validate(payload)
    frozen = binding(semantic, catalog())

    validate_binding_against_catalog(semantic, catalog(), frozen)
