from pathlib import Path

import pytest
from pydantic import ValidationError

from app.contracts.semantic_design import (
    BusinessPolicySpec,
    FactBlueprint,
    FactFilterPolicyBinding,
    ResultContract,
)
from app.services.semantic_compiler_v3 import (
    SemanticCompileError,
    compile_semantic_contract,
)
from app.services.fact_policy_schema import (
    create_fact_blueprint_response_model,
    materialize_fact_blueprint,
)
from app.services.semantic_obligation_compiler import (
    SemanticObligationError,
    compile_semantic_obligations,
)
from app.services.source_obligation_schema import (
    create_source_requirements_response_model,
    materialize_source_requirements,
)
from test_semantic_compiler_v3 import (
    staged_computation_artifacts,
    staged_reconciliation,
)


def _source_filter_case():
    result, blueprint, sources, expressions, _ = staged_reconciliation()
    result_payload = result.model_dump(mode="json")
    result_payload["business_policies"] = [
        BusinessPolicySpec(
            key="posted_only",
            value="仅已过账",
            effect="source_population",
            meaning="销售收入事实只包含已过账单据",
        ).model_dump(mode="json"),
    ]
    result = ResultContract.model_validate(result_payload)
    blueprint_payload = blueprint.model_dump(mode="json")
    blueprint_payload["policy_bindings"] = [
        FactFilterPolicyBinding(
            kind="fact_filter",
            policy_key="posted_only",
            fact_symbol="sales",
        ).model_dump(mode="json"),
    ]
    blueprint = FactBlueprint.model_validate(blueprint_payload)
    obligations = compile_semantic_obligations(result, blueprint)
    return result, blueprint, sources, expressions, obligations


def test_obligation_compiler_is_deterministic_and_rejects_missing_binding():
    result, blueprint, _, _, obligations = _source_filter_case()
    assert (
        compile_semantic_obligations(result, blueprint).content_hash
        == obligations.content_hash
    )

    invalid = blueprint.model_copy(update={"policy_bindings": []})
    with pytest.raises(SemanticObligationError) as exc:
        compile_semantic_obligations(result, invalid)
    assert exc.value.code == "POLICY_BINDING_MISSING"


def test_fact_schema_requires_typed_targets_and_program_owns_binding_kind():
    result, blueprint, _, _, _ = _source_filter_case()
    response_model = create_fact_blueprint_response_model(result)
    payload = {
        "facts": [
            item.model_dump(mode="json") for item in blueprint.facts
        ],
        "joins": [
            item.model_dump(mode="json") for item in blueprint.joins
        ],
        "derived_output_symbols": blueprint.derived_output_symbols,
        "policy_targets": {},
    }
    with pytest.raises(ValidationError):
        response_model.model_validate(payload)

    payload["policy_targets"]["policy_target_posted_only"] = [
        {"fact_symbol": "sales"},
    ]
    canonical = materialize_fact_blueprint(
        response_model.model_validate(payload),
        result,
    )
    binding = canonical.policy_bindings[0]
    assert binding.kind == "fact_filter"
    assert binding.policy_key == "posted_only"
    assert binding.fact_symbol == "sales"


def test_source_schema_requires_every_compiled_policy_slot():
    result, blueprint, sources, _, obligations = _source_filter_case()
    staged = _source_filter_case()
    frozen = staged_computation_artifacts(staged)
    input_obligations = frozen["input_obligations"]
    response_model = create_source_requirements_response_model(
        obligations,
        input_obligations,
    )
    source_by_symbol = {item.symbol: item for item in sources.fields}
    base = {
        "entities": [
            item.model_dump(mode="json") for item in sources.entities
        ],
        "required_inputs": {
            item.slot_name: {
                "entity_symbol": source_by_symbol[item.slot_name].entity_symbol,
                "meaning": item.meaning,
                "nullable": item.nullable,
            }
            for item in input_obligations.inputs
        },
        "ordinary_filters": [],
        "policy_filters": {},
    }
    with pytest.raises(ValidationError):
        response_model.model_validate(base)

    slot = obligations.obligations[0].slot_name
    base["policy_filters"][slot] = {
        "entity_symbol": "sales_invoice",
        "source_meaning": "销售记录的过账状态",
        "source_logical_type": "boolean",
        "nullable": False,
        "operator": "eq",
        "parameter_symbols": [],
        "literal_values": [True],
        "meaning": "仅保留已过账销售收入记录",
    }
    draft = response_model.model_validate(base)
    materialized = materialize_source_requirements(
        draft,
        obligations,
        input_obligations,
        blueprint,
    )

    assert materialized.filters[0].policy_key == "posted_only"
    assert materialized.filters[0].fact_symbols == ["sales"]
    assert materialized.filters[0].symbol == slot


def test_compiler_emits_exact_policy_coverage_and_rejects_stale_obligations():
    result, blueprint, sources, expressions, obligations = staged_reconciliation()
    frozen = staged_computation_artifacts(
        (result, blueprint, sources, expressions, obligations),
    )
    _, symbols = compile_semantic_contract(
        result,
        blueprint,
        sources,
        expressions,
        obligations,
        **frozen,
        decision_hash="decisions",
        confirmed_decision_keys={"currency_basis"},
    )
    assert {
        item["obligation_id"] for item in symbols["policy_coverage"]
    } == {
        item.obligation_id for item in obligations.obligations
    }

    changed_result = result.model_copy(update={"purpose": "变更后的对账目的"})
    with pytest.raises(SemanticCompileError) as exc:
        compile_semantic_contract(
            changed_result,
            blueprint,
            sources,
            expressions,
            obligations,
            **frozen,
            decision_hash="decisions",
            confirmed_decision_keys={"currency_basis"},
        )
    assert exc.value.code == "OBLIGATION_TARGET_CHANGED"


def test_semantic_error_ui_displays_policy_obligation_evidence():
    template = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "semantic_obligations: '业务政策义务编译'" in template
    assert "computation_blueprint: '业务计算蓝图'" in template
    assert "semantic_inputs: '来源输入义务编译'" in template
    assert "expression_materialize: '确定性公式物化'" in template
    for label in (
        "确认值：",
        "影响类型：",
        "义务类型：",
        "目标：",
        "冻结公式：",
        "必需输入：",
        "实际输入：",
        "缺失输入：",
        "多余或未消费输入：",
        "已阻止后续阶段：",
        "证据：",
    ):
        assert label in template
