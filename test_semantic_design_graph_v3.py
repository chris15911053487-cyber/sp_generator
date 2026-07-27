from langchain_core.messages import AIMessage
import pytest

from app.agent import graph, nodes
from app.agent.prompts import RESULT_CONTRACT_PROMPT
from app.contracts.semantic_design import (
    ContractOnlyPolicyBinding,
    FilterRequirement,
    ResultContract,
)
from app.db import sqlite as sqlite_db
from app.services.semantic_input_compiler import compile_semantic_input_obligations
from test_semantic_compiler_v3 import (
    staged_computation_artifacts,
    staged_reconciliation,
)


class _QueuedLlm:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    def invoke(self, _messages):
        if not self.payloads:
            raise AssertionError("LLM 调用次数超出预期")
        return AIMessage(content=self.payloads.pop(0))


def _state(session_id):
    return {
        "session_id": session_id,
        "user_input": "",
        "mode": "design",
        "requirements": "按月份对比销售收入与财务凭证收入",
        "confirmed_assumptions": "已确认",
        "design": "",
        "sp_list": [],
        "verify_results": [],
        "status": "assumptions_confirmed",
        "error": "",
        "clarify_count": 1,
        "design_phase": None,
        "last_feedback_reply": "",
        "confirmed_decision_set": {
            "summary": "按月份对账",
            "decisions": [{
                "key": "currency_basis",
                "value": "本位币",
                "contract_relevant": True,
                "source": "user",
            }],
            "decision_hash": "d" * 64,
        },
    }


def test_main_design_path_freezes_computations_before_sources(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(sqlite_db, "DB_PATH", str(tmp_path / "graph.db"))
    sqlite_db.init_db()
    session = sqlite_db.create_session("staged graph")
    result, blueprint, sources, expressions, _obligations = staged_reconciliation()
    computations = staged_computation_artifacts((
        result,
        blueprint,
        sources,
        expressions,
        _obligations,
    ))["computations"]
    fact_draft = {
        "facts": [
            item.model_dump(mode="json") for item in blueprint.facts
        ],
        "joins": [
            item.model_dump(mode="json") for item in blueprint.joins
        ],
        "derived_output_symbols": blueprint.derived_output_symbols,
        "policy_targets": {
            "policy_target_currency_basis": [
                {"fact_symbol": "sales", "value_symbol": "amount"},
                {"fact_symbol": "journal", "value_symbol": "amount"},
            ],
        },
    }
    computation_draft = {
        "fact_values": {
            f"fact_value_{item.fact_symbol}_{item.value_symbol}": {
                "inputs": [
                    value.model_dump(mode="json") for value in item.inputs
                ],
                "expression": (
                    item.expression.model_dump(mode="json")
                    if item.expression is not None else None
                ),
            }
            for item in computations.fact_values
        },
        "results": {
            f"result_{item.output_symbol}": {
                "expression": item.expression.model_dump(mode="json"),
            }
            for item in computations.results
        },
        "result_filter": None,
    }
    input_obligations = compile_semantic_input_obligations(
        result,
        blueprint,
        computations,
    )
    source_by_symbol = {item.symbol: item for item in sources.fields}
    source_draft = {
        "entities": [
            item.model_dump(mode="json") for item in sources.entities
        ],
        "required_inputs": {
            item.slot_name: {
                "entity_symbol": source_by_symbol[
                    item.input_symbol
                ].entity_symbol,
                "meaning": item.meaning,
                "nullable": item.nullable,
            }
            for item in input_obligations.inputs
        },
        "ordinary_filters": [],
        "policy_filters": {},
    }
    llm = _QueuedLlm([
        result.canonical_json(),
        __import__("json").dumps(fact_draft, ensure_ascii=False),
        __import__("json").dumps(computation_draft, ensure_ascii=False),
        __import__("json").dumps(source_draft, ensure_ascii=False),
    ])
    monkeypatch.setattr(nodes, "_get_llm", lambda: llm)
    monkeypatch.setattr(
        nodes,
        "_generate_design_query_spec",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("主链不得调用整份 SemanticDesign 生成器"),
        ),
    )

    state = _state(session["id"])
    for node in (
        nodes.result_contract_node,
        nodes.fact_blueprint_node,
        nodes.computation_blueprint_node,
        nodes.semantic_obligations_node,
        nodes.semantic_inputs_node,
        nodes.source_requirements_node,
        nodes.expression_materialize_node,
        nodes.semantic_compile_node,
    ):
        state.update(node(state))

    assert llm.payloads == []
    assert state["status"] == "designed"
    assert state["semantic_compile_result"]["contract_hash"]
    assert len(state["query_spec"]["contracts"][0]["facts"]) == 2
    assert graph._after_semantic_stage(state, "plan") == "plan"

    checkpoint = sqlite_db.get_semantic_design_checkpoint(session["id"])
    assert checkpoint["status"] == "ready_for_confirmation"
    assert checkpoint["compile_result"]["contract_hash"]


def test_invalid_result_contract_stops_before_fact_stage(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(sqlite_db, "DB_PATH", str(tmp_path / "stop.db"))
    sqlite_db.init_db()
    session = sqlite_db.create_session("invalid result")
    llm = _QueuedLlm(["{}", "{}"])
    monkeypatch.setattr(nodes, "_get_llm", lambda: llm)

    state = _state(session["id"])
    state.update(nodes.result_contract_node(state))

    assert state["status"] == "semantic_design_failed"
    assert state["semantic_design_stage"] == "result_contract"
    assert graph._after_semantic_stage(state, "fact_blueprint") == "end"
    checkpoint = sqlite_db.get_semantic_design_checkpoint(session["id"])
    assert checkpoint["fact_blueprint"] is None


def test_result_contract_cannot_rewrite_confirmed_decision_value():
    result, _, _, _, _ = staged_reconciliation()
    changed = result.model_copy(deep=True)
    changed.business_policies[0].value = "改写后的本位币口径"

    with pytest.raises(ValueError, match="changed_values"):
        nodes._validate_result_contract_stage(_state("session"), changed)


def test_result_selection_policy_requires_exception_rows_at_result_stage():
    result, _, _, _, _ = staged_reconciliation()
    payload = result.model_dump(mode="json")
    payload["business_policies"][0].update({
        "key": "output_content",
        "value": "仅输出有差异的记录",
        "effect": "result_selection",
        "meaning": "最终结果仅保留业务与财务余额不一致的记录",
    })
    invalid = ResultContract.model_validate(payload)
    state = _state("session")
    state["confirmed_decision_set"]["decisions"][0].update({
        "key": "output_content",
        "value": "仅输出有差异的记录",
    })
    with pytest.raises(ValueError) as exc:
        nodes._validate_result_contract_stage(state, invalid)
    assert exc.value.code == "POLICY_RESULT_MODE_MISMATCH"


def test_single_cutoff_date_cannot_be_full_day_range_endpoint():
    result, _, _, _, _ = staged_reconciliation()
    payload = result.model_dump(mode="json")
    payload["parameters"] = [{
        "symbol": "as_of_date",
        "name": "@AsOfDate",
        "logical_type": "date",
        "required": True,
        "default": None,
        "meaning": "库存余额截止日期",
        "boundary": "inclusive_full_day",
    }]
    invalid = ResultContract.model_validate(payload)
    with pytest.raises(ValueError) as exc:
        nodes._validate_result_contract_stage(_state("session"), invalid)
    assert exc.value.code == "PARAMETER_BOUNDARY_INVALID"


def test_confirmed_assumptions_enter_staged_design_not_legacy_plan():
    assert graph._after_assumptions({
        "mode": "design",
        "error": "",
    }) == "result_contract"


def test_policy_targets_are_frozen_by_upstream_contract():
    result, blueprint, sources, _expressions, obligations = staged_reconciliation()
    invalid_blueprint = blueprint.model_copy(deep=True)
    invalid_blueprint.policy_bindings.append(ContractOnlyPolicyBinding(
        kind="contract_only",
        policy_key="currency_basis",
    ))
    with pytest.raises(ValueError) as exc:
        nodes._validate_fact_blueprint_stage(result, invalid_blueprint)
    assert exc.value.code == "POLICY_EFFECT_BINDING_MISMATCH"

    invalid_sources = sources.model_copy(deep=True)
    invalid_sources.filters.append(FilterRequirement(
        symbol="date_filter",
        meaning="按日期参数过滤",
        source_symbol="sales_date",
        parameter_symbols=[],
        operator="is_not_null",
        policy_key="date_range",
        fact_symbols=["sales"],
    ))
    with pytest.raises(ValueError, match="OBLIGATION_IMPLEMENTATION_MISSING"):
        nodes._validate_source_requirements_stage(
            result, blueprint, obligations, invalid_sources,
        )


def test_fact_dimension_type_is_frozen_before_expression_design():
    result, blueprint, _sources, _expressions, _ = staged_reconciliation()
    invalid = blueprint.model_copy(deep=True)
    invalid.facts[0].dimensions[0].result_output_symbol = "period"
    invalid.facts[0].dimensions[0].logical_type = "integer"

    with pytest.raises(ValueError, match="FACT_RESULT_TYPE_MISMATCH"):
        nodes._validate_fact_blueprint_stage(result, invalid)


def test_pydantic_error_context_is_json_safe_for_persistence():
    invalid = """{
      "procedure_name": "usp_Invalid",
      "purpose": "测试错误序列化",
      "result_mode": "full_rows",
      "parameters": [],
      "outputs": [{
        "symbol": "document_number",
        "name": "DocumentNumber",
        "meaning": "单据编号",
        "logical_type": "string",
        "nullable": false
      }],
      "grain_output_symbols": ["missing_output"],
      "allow_empty": true,
      "money_tolerance": 0.01,
      "business_policies": []
    }"""
    with pytest.raises(nodes.SemanticStageError) as exc:
        nodes._generate_semantic_stage(
            _QueuedLlm([invalid, invalid]),
            stage="result_contract",
            contract_type=ResultContract,
            instruction=RESULT_CONTRACT_PROMPT,
            state={"requirements": "测试"},
            upstream={},
        )

    import json
    json.dumps(exc.value.evidence, ensure_ascii=False)


def test_dynamic_computation_missing_target_keeps_stable_error_code():
    from app.services.computation_blueprint_schema import (
        create_computation_blueprint_response_model,
    )

    result, blueprint, _, _, _ = staged_reconciliation()
    response_model = create_computation_blueprint_response_model(
        result,
        blueprint,
    )
    with pytest.raises(nodes.SemanticStageError) as exc:
        nodes._generate_semantic_stage(
            _QueuedLlm(["{}", "{}"]),
            stage="computation_blueprint",
            contract_type=response_model,
            instruction="test",
            state={"requirements": "测试"},
            upstream={},
        )
    assert exc.value.code == "COMPUTATION_TARGET_MISSING"
    assert exc.value.evidence["errors"][0]["code"] == (
        "COMPUTATION_TARGET_MISSING"
    )
