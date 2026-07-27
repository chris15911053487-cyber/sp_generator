import pytest
from pydantic import ValidationError

from app.contracts.computation_blueprint import FactValueComputation
from app.contracts.semantic_design import (
    FactBlueprint,
    FactBlueprintItem,
    FactMeasureNeed,
    ResultContract,
    ResultOutputSpec,
)
from app.services.computation_blueprint_schema import (
    create_computation_blueprint_response_model,
    materialize_computation_blueprint,
)
from app.services.computation_blueprint_validator import (
    ComputationBlueprintError,
    validate_computation_blueprint,
)


def _contracts():
    result = ResultContract(
        procedure_name="usp_inventory_amount",
        purpose="汇总业务库存金额",
        result_mode="scalar_summary",
        outputs=[
            ResultOutputSpec(
                symbol="inventory_amount",
                name="InventoryAmount",
                meaning="业务库存金额",
                logical_type="money",
                nullable=False,
            ),
        ],
    )
    facts = FactBlueprint(facts=[
        FactBlueprintItem(
            symbol="inventory",
            meaning="业务库存余额",
            entity_symbols=["inventory"],
            measures=[
                FactMeasureNeed(
                    symbol="inventory_amount",
                    meaning="数量乘以单位成本后的库存金额",
                    logical_type="money",
                    aggregation="sum",
                    result_output_symbol="inventory_amount",
                ),
            ],
        ),
    ])
    return result, facts


def _draft_payload():
    return {
        "fact_values": {
            "fact_value_inventory_inventory_amount": {
                "inputs": [
                    {
                        "symbol": "quantity",
                        "meaning": "在手数量",
                        "logical_type": "decimal",
                        "nullable": False,
                    },
                    {
                        "symbol": "unit_cost",
                        "meaning": "单位成本",
                        "logical_type": "money",
                        "nullable": False,
                    },
                ],
                "expression": {
                    "kind": "binary",
                    "operator": "*",
                    "args": [
                        {"kind": "input", "symbol": "quantity"},
                        {"kind": "input", "symbol": "unit_cost"},
                    ],
                },
            },
        },
        "results": {
            "result_inventory_amount": {
                "expression": {
                    "kind": "fact_value",
                    "fact_symbol": "inventory",
                    "value_symbol": "inventory_amount",
                },
            },
        },
        "result_filter": None,
    }


def test_dynamic_schema_freezes_targets_and_materializes_formula():
    result, facts = _contracts()
    model = create_computation_blueprint_response_model(result, facts)
    draft = model.model_validate(_draft_payload())
    computations = materialize_computation_blueprint(draft, result, facts)
    validate_computation_blueprint(result, facts, computations)
    assert computations.fact_values[0].fact_symbol == "inventory"
    assert computations.fact_values[0].aggregation == "sum"
    assert computations.results[0].output_symbol == "inventory_amount"


def test_fact_context_rejects_parameter_reference():
    with pytest.raises(ValidationError):
        FactValueComputation.model_validate({
            "fact_symbol": "inventory",
            "value_symbol": "inventory_amount",
            "inputs": [],
            "expression": {"kind": "parameter", "symbol": "as_of_date"},
            "aggregation": "sum",
            "logical_type": "money",
        })


def test_declared_but_unused_input_is_rejected():
    payload = _draft_payload()
    payload["fact_values"]["fact_value_inventory_inventory_amount"][
        "expression"
    ] = {"kind": "input", "symbol": "quantity"}
    result, facts = _contracts()
    model = create_computation_blueprint_response_model(result, facts)
    with pytest.raises(ValidationError, match="COMPUTATION_INPUT_UNUSED"):
        materialize_computation_blueprint(
            model.model_validate(payload),
            result,
            facts,
        )


def test_changed_upstream_hash_is_rejected():
    result, facts = _contracts()
    model = create_computation_blueprint_response_model(result, facts)
    computations = materialize_computation_blueprint(
        model.model_validate(_draft_payload()),
        result,
        facts,
    )
    changed = result.model_copy(update={"purpose": "另一个业务目的"})
    with pytest.raises(
        ComputationBlueprintError,
        match="结果合同已经变化",
    ):
        validate_computation_blueprint(changed, facts, computations)


def test_formula_input_types_are_checked_before_source_design():
    result, facts = _contracts()
    payload = _draft_payload()
    payload["fact_values"]["fact_value_inventory_inventory_amount"][
        "inputs"
    ][0]["logical_type"] = "date"
    model = create_computation_blueprint_response_model(result, facts)
    computations = materialize_computation_blueprint(
        model.model_validate(payload),
        result,
        facts,
    )
    with pytest.raises(
        ComputationBlueprintError,
        match="算术运算只能使用数值输入",
    ):
        validate_computation_blueprint(result, facts, computations)


def test_result_formula_type_must_match_frozen_output():
    result, facts = _contracts()
    payload = _draft_payload()
    payload["results"]["result_inventory_amount"]["expression"] = {
        "kind": "literal",
        "value": "不是金额",
    }
    model = create_computation_blueprint_response_model(result, facts)
    computations = materialize_computation_blueprint(
        model.model_validate(payload),
        result,
        facts,
    )
    with pytest.raises(ComputationBlueprintError) as exc:
        validate_computation_blueprint(result, facts, computations)
    assert exc.value.code == "COMPUTATION_RESULT_TYPE_MISMATCH"
