import pytest

from app.contracts.semantic_design import FactMeasureNeed
from app.services.computation_blueprint_schema import (
    create_computation_blueprint_response_model,
    materialize_computation_blueprint,
)
from app.services.semantic_input_compiler import (
    SemanticInputCompilerError,
    compile_semantic_input_obligations,
)
from test_computation_blueprint_v3 import _contracts, _draft_payload


def _compile():
    result, facts = _contracts()
    model = create_computation_blueprint_response_model(result, facts)
    computations = materialize_computation_blueprint(
        model.model_validate(_draft_payload()),
        result,
        facts,
    )
    return compile_semantic_input_obligations(
        result,
        facts,
        computations,
    )


def test_input_ids_and_slots_are_deterministic():
    first = _compile()
    second = _compile()
    assert first == second
    assert [item.slot_name for item in first.inputs] == [
        "input_inventory_quantity",
        "input_inventory_unit_cost",
    ]


def test_same_fact_input_is_reused_across_values():
    result, facts = _contracts()
    extra = FactMeasureNeed(
        symbol="quantity_total",
        meaning="库存数量合计",
        logical_type="decimal",
        aggregation="sum",
    )
    facts = facts.model_copy(update={
        "facts": [
            facts.facts[0].model_copy(update={
                "measures": [*facts.facts[0].measures, extra],
            }),
        ],
    })
    model = create_computation_blueprint_response_model(result, facts)
    payload = _draft_payload()
    payload["fact_values"]["fact_value_inventory_quantity_total"] = {
        "inputs": [{
            "symbol": "quantity",
            "meaning": "在手数量",
            "logical_type": "decimal",
            "nullable": False,
        }],
        "expression": {"kind": "input", "symbol": "quantity"},
    }
    computations = materialize_computation_blueprint(
        model.model_validate(payload),
        result,
        facts,
    )
    obligations = compile_semantic_input_obligations(
        result,
        facts,
        computations,
    )
    quantity = next(
        item for item in obligations.inputs
        if item.input_symbol == "quantity"
    )
    assert quantity.value_symbols == ["inventory_amount", "quantity_total"]


def test_reused_input_must_keep_same_contract():
    result, facts = _contracts()
    extra = FactMeasureNeed(
        symbol="quantity_total",
        meaning="库存数量合计",
        logical_type="decimal",
        aggregation="sum",
    )
    facts = facts.model_copy(update={
        "facts": [
            facts.facts[0].model_copy(update={
                "measures": [*facts.facts[0].measures, extra],
            }),
        ],
    })
    model = create_computation_blueprint_response_model(result, facts)
    payload = _draft_payload()
    payload["fact_values"]["fact_value_inventory_quantity_total"] = {
        "inputs": [{
            "symbol": "quantity",
            "meaning": "另一种数量定义",
            "logical_type": "integer",
            "nullable": False,
        }],
        "expression": {"kind": "input", "symbol": "quantity"},
    }
    computations = materialize_computation_blueprint(
        model.model_validate(payload),
        result,
        facts,
    )
    with pytest.raises(
        SemanticInputCompilerError,
        match="定义不一致",
    ):
        compile_semantic_input_obligations(result, facts, computations)
