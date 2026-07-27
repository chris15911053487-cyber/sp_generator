import pytest
from pydantic import ValidationError

from app.contracts.semantic import SemanticContract
from v3_test_helpers import contract


def test_contract_is_physical_schema_free_and_hash_stable():
    value = contract()
    serialized = value.canonical_json()
    assert "OINV" not in serialized
    assert "DocEntry" not in serialized
    assert value.content_hash == SemanticContract.model_validate_json(
        serialized
    ).content_hash


def test_non_scalar_contract_requires_stable_grain():
    payload = contract().model_dump()
    payload["grain"] = []
    with pytest.raises(ValidationError, match="稳定粒度"):
        SemanticContract.model_validate(payload)


def test_unknown_parameter_reference_is_rejected():
    payload = contract().model_dump()
    payload["filters"] = [
        {
            "id": "bad_filter",
            "meaning": "错误参数",
            "field_ids": ["invoice_id"],
            "parameter_ids": ["missing"],
            "operator": "eq",
            "literal_values": [],
        }
    ]
    with pytest.raises(ValidationError, match="未声明参数"):
        SemanticContract.model_validate(payload)
