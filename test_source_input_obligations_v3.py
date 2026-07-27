import pytest
from pydantic import ValidationError

from app.contracts.semantic_design import EntityRequirement
from app.services.computation_blueprint_schema import (
    create_computation_blueprint_response_model,
    materialize_computation_blueprint,
)
from app.services.semantic_input_compiler import (
    compile_semantic_input_obligations,
)
from app.services.semantic_obligation_compiler import (
    compile_semantic_obligations,
)
from app.services.source_obligation_schema import (
    SourceObligationError,
    create_source_requirements_response_model,
    materialize_source_requirements,
)
from test_computation_blueprint_v3 import _contracts, _draft_payload


def _source_case():
    result, facts = _contracts()
    computation_model = create_computation_blueprint_response_model(
        result,
        facts,
    )
    computations = materialize_computation_blueprint(
        computation_model.model_validate(_draft_payload()),
        result,
        facts,
    )
    policy_obligations = compile_semantic_obligations(result, facts)
    input_obligations = compile_semantic_input_obligations(
        result,
        facts,
        computations,
    )
    response_model = create_source_requirements_response_model(
        policy_obligations,
        input_obligations,
    )
    payload = {
        "entities": [{
            "symbol": "inventory",
            "meaning": "业务库存记录",
            "grain_meaning": "每个库存项目一条记录",
        }],
        "required_inputs": {
            item.slot_name: {
                "entity_symbol": "inventory",
                "meaning": item.meaning,
                "nullable": item.nullable,
            }
            for item in input_obligations.inputs
        },
        "ordinary_filters": [],
        "policy_filters": {},
    }
    return (
        policy_obligations,
        input_obligations,
        response_model,
        payload,
        facts,
    )


def test_every_input_slot_is_required_and_extra_fields_are_impossible():
    _, _, response_model, payload, _ = _source_case()
    removed = payload["required_inputs"].pop("input_inventory_quantity")
    with pytest.raises(ValidationError):
        response_model.model_validate(payload)
    payload["required_inputs"]["input_inventory_quantity"] = removed
    payload["fields"] = [{
        "symbol": "business_amount",
        "entity_symbol": "inventory",
        "meaning": "绕过公式的派生库存金额",
        "logical_type": "money",
        "nullable": False,
    }]
    with pytest.raises(ValidationError):
        response_model.model_validate(payload)


def test_materializer_owns_source_symbol_and_logical_type():
    policies, inputs, response_model, payload, facts = _source_case()
    sources = materialize_source_requirements(
        response_model.model_validate(payload),
        policies,
        inputs,
        facts,
    )
    assert {item.symbol for item in sources.fields} == {
        "input_inventory_quantity",
        "input_inventory_unit_cost",
    }
    assert {
        item.symbol: item.logical_type for item in sources.fields
    } == {
        "input_inventory_quantity": "decimal",
        "input_inventory_unit_cost": "money",
    }


def test_source_cannot_change_frozen_nullability():
    policies, inputs, response_model, payload, facts = _source_case()
    payload["required_inputs"]["input_inventory_quantity"]["nullable"] = True
    draft = response_model.model_validate(payload)
    with pytest.raises(SourceObligationError) as exc:
        materialize_source_requirements(draft, policies, inputs, facts)
    assert exc.value.code == "COMPUTATION_INPUT_TYPE_MISMATCH"


def test_source_owner_must_be_a_declared_entity():
    policies, inputs, response_model, payload, facts = _source_case()
    payload["entities"].append({
        "symbol": "journal",
        "meaning": "财务凭证明细",
        "grain_meaning": "每条凭证分录一条记录",
    })
    payload["required_inputs"]["input_inventory_quantity"][
        "entity_symbol"
    ] = "journal"
    with pytest.raises(SourceObligationError) as exc:
        materialize_source_requirements(
            response_model.model_validate(payload),
            policies,
            inputs,
            facts,
        )
    assert exc.value.code == "SOURCE_INPUT_OWNER_UNKNOWN"


def test_optional_filter_bypass_requires_exactly_one_parameter():
    from app.contracts.source_requirements_draft import (
        OrdinaryFilterRequirement,
    )

    with pytest.raises(
        ValidationError,
        match="SOURCE_OPTIONAL_FILTER_SHAPE_INVALID",
    ):
        OrdinaryFilterRequirement(
            symbol="customer_filter",
            meaning="可选客户筛选",
            source_symbol="customer_code",
            parameter_symbols=["customer_code", "customer_group"],
            operator="between",
            literal_values=[],
            fact_symbols=["invoice_fact"],
            skip_when_parameter_null=True,
        )


def test_full_day_range_requires_two_parameters_in_source_schema():
    from app.contracts.source_requirements_draft import (
        PolicyFilterImplementation,
    )

    with pytest.raises(ValidationError, match="SOURCE_FILTER_ARGUMENT_COUNT_INVALID"):
        PolicyFilterImplementation(
            entity_symbol="inventory",
            source_meaning="库存交易日期",
            source_logical_type="date",
            nullable=False,
            operator="full_day_range",
            parameter_symbols=["as_of_date"],
            literal_values=[],
            meaning="统计截止日期前的库存交易",
        )
