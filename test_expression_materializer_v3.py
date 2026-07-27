import pytest

from app.services.computation_blueprint_schema import (
    create_computation_blueprint_response_model,
    materialize_computation_blueprint,
)
from app.services.expression_materializer import (
    ExpressionMaterializationError,
    materialize_expression_design,
)
from app.services.semantic_input_compiler import (
    compile_semantic_input_obligations,
)
from app.services.semantic_obligation_compiler import (
    compile_semantic_obligations,
)
from app.services.semantic_compiler_v3 import compile_semantic_contract
from app.services.source_obligation_schema import (
    create_source_requirements_response_model,
    materialize_source_requirements,
)
from test_computation_blueprint_v3 import _contracts, _draft_payload


def _case():
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
    inputs = compile_semantic_input_obligations(
        result,
        facts,
        computations,
    )
    policies = compile_semantic_obligations(result, facts)
    source_model = create_source_requirements_response_model(
        policies,
        inputs,
    )
    source_draft = source_model.model_validate({
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
            for item in inputs.inputs
        },
        "ordinary_filters": [],
        "policy_filters": {},
    })
    sources = materialize_source_requirements(
        source_draft,
        policies,
        inputs,
        facts,
    )
    return result, facts, computations, inputs, policies, sources


def test_materializer_preserves_frozen_formula_and_maps_only_inputs():
    _, facts, computations, inputs, _, sources = _case()
    expressions = materialize_expression_design(
        facts,
        computations,
        inputs,
        sources,
    )
    formula = expressions.measures[0].expression
    assert formula.kind == "binary"
    assert formula.operator == "*"
    assert [item.kind for item in formula.args] == ["source", "source"]
    assert [item.symbol for item in formula.args] == [
        "input_inventory_quantity",
        "input_inventory_unit_cost",
    ]
    result = expressions.results[0]
    assert result.expression.kind == "fact_value"
    assert result.expression.fact_symbol == "inventory"


def test_materializer_stops_if_a_required_source_input_is_missing():
    _, facts, computations, inputs, _, sources = _case()
    incomplete = sources.model_copy(update={
        "fields": sources.fields[1:],
    })
    with pytest.raises(ExpressionMaterializationError) as exc:
        materialize_expression_design(
            facts,
            computations,
            inputs,
            incomplete,
        )
    assert exc.value.code == "SOURCE_INPUT_IMPLEMENTATION_MISSING"


def test_semantic_compiler_accepts_only_materialized_frozen_formula():
    result, facts, computations, inputs, policies, sources = _case()
    expressions = materialize_expression_design(
        facts,
        computations,
        inputs,
        sources,
    )
    contract, symbols = compile_semantic_contract(
        result,
        facts,
        sources,
        expressions,
        policies,
        computations=computations,
        input_obligations=inputs,
        decision_hash="decision",
        confirmed_decision_keys=set(),
    )
    assert contract.procedure_name == "usp_inventory_amount"
    assert symbols["policy_coverage"] == []
