from app.services.validation_cases import (
    coverage_is_effective,
    discover_validation_cases,
)
from v3_test_helpers import binding, contract


class ProbeExecutor:
    def discover_parameter_values(self, _contract, _binding):
        return (
            {
                "from_date": "2026-01-31",
                "to_date": "2026-01-31",
                "customer": "C001",
            },
            {"source": "schema_bound_data_probe"},
        )


def test_cases_include_data_driven_coverage_and_boundaries():
    semantic = contract()
    payload = semantic.model_dump(mode="json")
    payload["parameters"].append(
        {
            "id": "customer",
            "name": "@Customer",
            "logical_type": "string",
            "required": False,
            "default": None,
            "meaning": "可选客户",
            "boundary": "none",
        }
    )
    semantic = type(semantic).model_validate(payload)
    cases = discover_validation_cases(
        semantic, binding(semantic), ProbeExecutor()
    )
    by_id = {item.case_id: item for item in cases}
    assert by_id["coverage_probe"].parameters["customer"] == "C001"
    assert by_id["boundary_invoice_date_range_same_day"].kind == "boundary"
    assert by_id["boundary_customer_null"].parameters["customer"] is None
    assert by_id["empty_legal_period"].kind == "empty"


def test_composed_result_is_effective_coverage_when_one_join_side_is_empty():
    assert coverage_is_effective(
        [1, 0],
        composed_expected_row_count=1,
    )
    assert not coverage_is_effective(
        [1, 0],
        composed_expected_row_count=0,
    )
