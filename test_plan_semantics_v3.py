import pytest

from app.contracts.relational_plan import RelationalPlan
from app.contracts.semantic import SemanticContract
from app.services.plan_semantics_v3 import PlanSemanticError, validate_plan_semantics
from v3_test_helpers import binding, contract, plan


def full_day_contract():
    payload = contract().model_dump()
    payload["filters"] = [
        {
            "id": "invoice_date_range",
            "meaning": "发票日期覆盖起止自然日",
            "field_ids": ["invoice_date"],
            "parameter_ids": ["from_date", "to_date"],
            "operator": "full_day_range",
            "literal_values": [],
        }
    ]
    return SemanticContract.model_validate(payload)


def test_full_day_filter_is_machine_verified():
    semantic = full_day_contract()
    schema_binding = binding(semantic)
    validate_plan_semantics(plan(), semantic, schema_binding)


def test_missing_full_day_upper_bound_is_rejected():
    semantic = full_day_contract()
    schema_binding = binding(semantic)
    payload = plan().model_dump()
    upper = payload["root"]["input"]["predicate"]["args"][1]
    upper["operator"] = "<="
    upper["args"][1] = {
        "kind": "parameter",
        "parameter_id": "to_date",
    }
    invalid = RelationalPlan.model_validate(payload)
    with pytest.raises(PlanSemanticError) as error:
        validate_plan_semantics(invalid, semantic, schema_binding)
    assert error.value.code == "PLAN_FILTER_COVERAGE_MISSING"


def test_filter_cannot_be_omitted_from_both_reference_and_procedure():
    semantic = full_day_contract()
    schema_binding = binding(semantic)
    payload = plan().model_dump()
    payload["root"]["input"] = payload["root"]["input"]["input"]
    omitted = RelationalPlan.model_validate(payload)
    with pytest.raises(PlanSemanticError) as error:
        validate_plan_semantics(omitted, semantic, schema_binding)
    assert error.value.evidence["missing_filters"] == ["invoice_date_range"]


def test_date_boundary_on_wrong_field_is_rejected():
    semantic = full_day_contract()
    schema_binding = binding(semantic)
    payload = plan().model_dump()
    predicate = payload["root"]["input"]["predicate"]
    predicate["args"][0]["args"][0]["field_binding_id"] = "invoice_id"
    predicate["args"][1]["args"][0]["field_binding_id"] = "invoice_id"
    invalid = RelationalPlan.model_validate(payload)
    with pytest.raises(PlanSemanticError) as error:
        validate_plan_semantics(invalid, semantic, schema_binding)
    assert error.value.code == "PLAN_FILTER_COVERAGE_MISSING"


def test_output_alias_cannot_hide_wrong_business_field():
    semantic = full_day_contract()
    schema_binding = binding(semantic)
    payload = plan().model_dump()
    payload["root"]["projections"][1]["expression"]["field_binding_id"] = "invoice_id"
    invalid = RelationalPlan.model_validate(payload)
    with pytest.raises(PlanSemanticError) as error:
        validate_plan_semantics(invalid, semantic, schema_binding)
    assert error.value.code == "PLAN_OUTPUT_SOURCE_MISMATCH"


def _nested_output_plan(*, wrong_invoice_source=False):
    payload = plan().model_dump(mode="json")
    inner = payload["root"]
    inner["node_id"] = "project_base"
    if wrong_invoice_source:
        inner["projections"][0]["expression"]["field_binding_id"] = (
            "invoice_amount"
        )
    payload["root"] = {
        "node_id": "project_outer",
        "kind": "project",
        "input": inner,
        "projections": [
            {
                "name": item["name"],
                "expression": {
                    "kind": "output",
                    "output_name": item["name"],
                },
            }
            for item in inner["projections"]
        ],
    }
    return RelationalPlan.model_validate(payload)


def test_nested_project_output_lineage_reaches_bound_columns():
    semantic = full_day_contract()
    validate_plan_semantics(
        _nested_output_plan(),
        semantic,
        binding(semantic),
    )


def test_nested_project_alias_cannot_hide_wrong_bound_column():
    semantic = full_day_contract()
    with pytest.raises(PlanSemanticError) as error:
        validate_plan_semantics(
            _nested_output_plan(wrong_invoice_source=True),
            semantic,
            binding(semantic),
        )
    assert error.value.code == "PLAN_OUTPUT_SOURCE_MISMATCH"


def test_derived_formula_cannot_be_changed_by_plan():
    payload = full_day_contract().model_dump(mode="json")
    payload["outputs"].append(
        {
            "id": "net_amount",
            "name": "NetAmount",
            "meaning": "按确认公式计算的净额",
            "logical_type": "money",
            "nullable": False,
        }
    )
    payload["derived_fields"] = [
        {
            "output_id": "net_amount",
            "expression": {
                "kind": "binary",
                "operator": "-",
                "args": [
                    {"kind": "output", "output_id": "amount"},
                    {"kind": "literal", "value": 0},
                ],
            },
        }
    ]
    semantic = SemanticContract.model_validate(payload)
    schema_binding = binding(semantic)
    plan_payload = plan().model_dump(mode="json")
    plan_payload["root"]["projections"].append(
        {
            "name": "NetAmount",
            "expression": {
                "kind": "binary",
                "operator": "+",
                "args": [
                    {
                        "kind": "column",
                        "field_binding_id": "invoice_amount",
                    },
                    {"kind": "literal", "value": 0, "value_type": "integer"},
                ],
            },
        }
    )
    plan_payload["result_schema"].append(
        {
            "name": "NetAmount",
            "logical_type": "money",
            "nullable": False,
        }
    )
    invalid = RelationalPlan.model_validate(plan_payload)
    with pytest.raises(PlanSemanticError) as error:
        validate_plan_semantics(invalid, semantic, schema_binding)
    assert error.value.code == "PLAN_DERIVED_EXPRESSION_MISMATCH"
