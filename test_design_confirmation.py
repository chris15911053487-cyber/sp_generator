"""方案确认回归测试。"""
from app.agent import nodes


class _NoInvokeLLM:
    def invoke(self, _messages):
        raise AssertionError("确认快捷动作不应调用 LLM 分类")


class _CompiledSpec:
    def model_dump(self, **_kwargs):
        return {"design_version": "confirmed", "procedures": [{"name": "sp_Test"}]}


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
    }


def test_design_is_compiled_and_persisted_before_confirmation(monkeypatch):
    state = _state(None)
    state["query_spec"] = {}
    monkeypatch.setattr(nodes, "_get_llm", lambda: _NoInvokeLLM())
    monkeypatch.setattr(
        nodes, "_compile_design_query_spec", lambda _llm, _design: _CompiledSpec(),
    )
    monkeypatch.setattr(
        nodes, "_render_query_spec", lambda _spec: "结构化确认方案",
    )
    monkeypatch.setattr(
        nodes, "interrupt",
        lambda _value: (_ for _ in ()).throw(
            AssertionError("固化 QuerySpec 的节点执行不应等待用户确认"),
        ),
    )

    result = nodes.design_node(state)

    assert result["mode"] == "design"
    assert result["design_phase"] == "new"
    assert result["query_spec"]["design_version"] == "confirmed"
    assert result["design"] == "结构化确认方案"


def test_confirm_action_after_redesign_enters_generate_without_llm(monkeypatch):
    monkeypatch.setattr(nodes, "_get_llm", lambda: _NoInvokeLLM())
    monkeypatch.setattr(nodes, "interrupt", lambda _value: {"action": "confirm"})

    result = nodes.design_node(_state("feedback"))

    assert result["mode"] == "generate"
    assert result["status"] == "designed"
    assert result["design_phase"] is None
    assert result["design"] == "重新设计后的方案"


def test_confirm_action_on_initial_design_enters_generate_without_llm(monkeypatch):
    monkeypatch.setattr(nodes, "_get_llm", lambda: _NoInvokeLLM())
    monkeypatch.setattr(nodes, "interrupt", lambda _value: {"action": "confirm"})

    result = nodes.design_node(_state("new"))

    assert result["mode"] == "generate"
    assert result["design_phase"] is None


def test_only_unambiguous_confirmation_text_uses_fast_path():
    assert nodes._is_explicit_design_confirmation("确认，请开始生成存储过程")
    assert nodes._is_explicit_design_confirmation("确认方案开始生成")
    assert not nodes._is_explicit_design_confirmation("确认，但请把 INNER JOIN 改成 LEFT JOIN")
