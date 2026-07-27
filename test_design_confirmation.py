"""方案确认回归测试。"""
import json

from langchain_core.messages import AIMessage

from app.agent import nodes
from app.contracts.semantic import SemanticDesign
from v3_test_helpers import contract


class _NoInvokeLLM:
    def invoke(self, _messages):
        raise AssertionError("确认快捷动作不应调用 LLM 分类")


def _compiled_design():
    return SemanticDesign(
        design_version="confirmed",
        decision_hash="confirmed",
        contracts=[contract()],
    )


def _state(phase: str | None) -> dict:
    return {
        "session_id": "test-session",
        "user_input": "",
        "mode": "design",
        "requirements": "测试需求",
        "confirmed_assumptions": "已确认",
        "design": "重新设计后的方案",
        "sp_list": [],
        "verify_results": [],
        "status": "designed",
        "error": "",
        "clarify_count": 1,
        "design_phase": phase,
        "last_feedback_reply": "方案已按您的意见修改，请确认。",
        "query_spec": {"design_version": "confirmed"},
        "semantic_compile_result": {"contract_hash": "compiled"},
    }


def test_confirmation_node_rejects_missing_compiler_product(monkeypatch):
    state = _state(None)
    state["query_spec"] = {}
    monkeypatch.setattr(nodes, "_get_llm", lambda: _NoInvokeLLM())
    monkeypatch.setattr(
        nodes, "interrupt",
        lambda _value: (_ for _ in ()).throw(
            AssertionError("固化 QuerySpec 的节点执行不应等待用户确认"),
        ),
    )

    result = nodes.design_node(state)

    assert result["mode"] == "design"
    assert result["status"] == "semantic_design_failed"
    assert result["error"] == "SEMANTIC_COMPILE_RESULT_MISSING"


def test_confirm_action_after_redesign_enters_generate_without_llm(monkeypatch):
    monkeypatch.setattr(nodes, "_get_llm", lambda: _NoInvokeLLM())
    monkeypatch.setattr(nodes, "interrupt", lambda _value: {"action": "confirm"})
    monkeypatch.setattr(
        nodes,
        "_confirm_semantic_design",
        lambda state: {
            "mode": "generate",
            "status": "designed",
            "design_phase": None,
            "design": state["design"],
        },
    )

    result = nodes.design_node(_state("feedback"))

    assert result["mode"] == "generate"
    assert result["status"] == "designed"
    assert result["design_phase"] is None
    assert result["design"] == "重新设计后的方案"


def test_confirm_action_on_initial_design_enters_generate_without_llm(monkeypatch):
    monkeypatch.setattr(nodes, "_get_llm", lambda: _NoInvokeLLM())
    monkeypatch.setattr(nodes, "interrupt", lambda _value: {"action": "confirm"})
    monkeypatch.setattr(
        nodes,
        "_confirm_semantic_design",
        lambda state: {
            "mode": "generate",
            "status": "designed",
            "design_phase": None,
            "design": state["design"],
        },
    )

    result = nodes.design_node(_state("new"))

    assert result["mode"] == "generate"
    assert result["design_phase"] is None


def test_only_unambiguous_confirmation_text_uses_fast_path():
    assert nodes._is_explicit_design_confirmation("确认，请开始生成存储过程")
    assert nodes._is_explicit_design_confirmation("确认方案开始生成")
    assert not nodes._is_explicit_design_confirmation("确认，但请把 INNER JOIN 改成 LEFT JOIN")


def test_design_modification_returns_to_business_decisions(monkeypatch):
    monkeypatch.setattr(
        "app.db.sqlite.invalidate_semantic_design_checkpoint",
        lambda _session_id: 1,
    )
    result = nodes._revise_semantic_design(
        _state("new"), "改为只看未取消单据",
    )

    assert result["mode"] == "clarify"
    assert result["decision_plan"] == {}
    assert result["confirmed_decision_set"] == {}
    assert result["query_spec"] == {}


def test_semantic_design_is_frozen_without_physical_schema(monkeypatch):
    semantic = contract().model_dump(mode="json")
    payload = {
        "summary": "应收发票汇总方案",
        "semantic_design": {
            "version": 3,
            "design_version": "will-be-overwritten",
            "decision_hash": "will-be-overwritten",
            "contracts": [semantic],
        },
    }

    class NoRepairLlm:
        def invoke(self, *_args, **_kwargs):
            raise AssertionError("合法 DesignEnvelope 不应触发第二次模型修复")

    monkeypatch.setattr(
        nodes,
        "_invoke_with_tools",
        lambda *_args, **_kwargs: AIMessage(
            content=json.dumps(payload, ensure_ascii=False),
        ),
    )
    state = {
        "requirements": "查询应收发票",
        "clarify_decisions": [],
        "confirmed_assumptions": "排除取消单据",
        "confirmed_decision_set": {
            "summary": "查询应收发票",
            "decisions": [],
            "decision_hash": "confirmed-hash",
        },
    }

    spec, summary, _draft = nodes._generate_design_query_spec(
        NoRepairLlm(), state,
    )

    assert summary == "应收发票汇总方案"
    assert spec.design_version == "confirmed-hash"
    assert spec.decision_hash == "confirmed-hash"
    assert "OINV" not in spec.canonical_json()
