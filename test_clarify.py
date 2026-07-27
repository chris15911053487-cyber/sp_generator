"""clarify 相关纯函数与节点回归测试。"""
import json
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from langchain_core.messages import AIMessage
from app.agent.nodes import (
    _extract_first_question,
    _is_duplicate_clarify_question,
    _parse_clarify_decision,
    _resolve_clarify_answer,
)


def test_single_question_unchanged():
    content = "统计维度是按客户还是按月份？\nA. 按客户\nB. 按月份"
    assert _extract_first_question(content) == content


def test_multiple_questions_truncated():
    content = "### 问题1：统计维度？\nA. 按客户\nB. 按月份\n\n### 问题2：时间范围？\nA. 本月\nB. 本年"
    result = _extract_first_question(content)
    assert "问题1" not in result
    assert "统计维度" in result
    assert "问题2" not in result


def test_numbered_prefix_truncated():
    content = "问题1：维度？\nA. x\nB. y\n\n问题2：范围？\nA. a\nB. b"
    result = _extract_first_question(content)
    assert "维度" in result
    assert "范围" not in result


def test_single_wrong_internal_number_removed():
    content = (
        "基于已确认的信息，您需要输出基础发票信息且只包含未收清发票。"
        "为确定存储过程的具体应用场景和格式要求，请确认：\n\n"
        "Q3：这个存储过程的主要用途是什么？\n\n"
        "A. 用于SAP B1报表模块展示\n"
        "B. 用于外部系统接口调用\n"
        "C. 用于内部临时查询"
    )
    result = _extract_first_question(content)
    assert "Q3" not in result
    assert "这个存储过程的主要用途是什么" in result
    assert "A. 用于SAP B1报表模块展示" in result


def test_empty_string():
    assert _extract_first_question("") == ""


def _question(
    key="outstanding_amount_basis",
    question="未收款金额按哪个口径计算？",
    decision_type="blocking",
):
    return {
        "action": "ask",
        "decision_key": key,
        "decision_type": decision_type,
        "question": question,
        "options": [
            {"id": "A", "value": "按发票含税总额计算"},
            {"id": "B", "value": "按发票含税总额减已收款金额计算"},
            {"id": "C", "value": "按发票未清余额字段计算"},
        ],
        "reason": "该选择影响最终金额",
    }


def _plan(*decisions):
    normalized = []
    for decision in decisions:
        item = dict(decision)
        item.pop("action", None)
        item.setdefault(
            "recommended_option_id",
            item["options"][0]["id"] if item["decision_type"] == "defaultable" else None,
        )
        item.setdefault("contract_relevant", True)
        normalized.append(item)
    return {
        "action": "plan",
        "requirements_summary": "查询应收发票",
        "decisions": normalized,
    }


def test_parse_structured_clarify_decision():
    decision = _parse_clarify_decision(
        "```json\n" + json.dumps(_question(), ensure_ascii=False) + "\n```"
    )
    assert decision["decision_key"] == "outstanding_amount_basis"
    assert decision["options"][1]["value"] == "按发票含税总额减已收款金额计算"


def test_parse_rejects_invalid_clarify_decision():
    invalid = _question()
    invalid["options"][1]["id"] = "A"
    assert _parse_clarify_decision(json.dumps(invalid, ensure_ascii=False)) is None
    assert _parse_clarify_decision('{"action":"unknown"}') is None


def test_parse_sufficient_decision():
    assert _parse_clarify_decision(
        '{"action":"sufficient","summary":"查询未结清应收发票"}'
    ) == {
        "action": "sufficient",
        "summary": "查询未结清应收发票",
    }


def test_resolve_option_letter_to_semantic_answer():
    resolved = _resolve_clarify_answer(_question(), " 选b。 ")
    assert resolved == {
        "selected_option_id": "B",
        "answer": "按发票含税总额减已收款金额计算",
        "raw_answer": "选b。",
    }


def test_resolve_free_text_without_guessing():
    resolved = _resolve_clarify_answer(_question(), "按未核销明细逐行计算")
    assert resolved["selected_option_id"] is None
    assert resolved["answer"] == "按未核销明细逐行计算"


def test_duplicate_decision_uses_key_then_semantic_fallback():
    answered = [{
        "decision_key": "outstanding_amount_basis",
        "question": "未收款金额按哪个口径计算？",
        "options": _question()["options"],
        "answer": "按发票含税总额减已收款金额计算",
    }]
    assert _is_duplicate_clarify_question(_question(), answered)

    renamed = _question(
        key="remaining_receivable_calculation",
        question="未收款金额具体采用哪一种计算口径？",
    )
    assert _is_duplicate_clarify_question(renamed, answered)

    different = {
        **_question(key="output_grain", question="结果需要按什么粒度输出？"),
        "options": [
            {"id": "A", "value": "发票明细"},
            {"id": "B", "value": "客户汇总"},
            {"id": "C", "value": "月份汇总"},
        ],
    }
    assert not _is_duplicate_clarify_question(different, answered)


def test_clarify_disables_thinking(monkeypatch):
    from app.agent import nodes

    calls = []

    class FakeLlm:
        def invoke(self, messages, **kwargs):
                calls.append(kwargs)
                return AIMessage(content=json.dumps(
                    _plan(_question(
                        key="invoice_scope",
                        question="应收发票需要包含哪些状态？",
                    )),
                    ensure_ascii=False,
                ))

    monkeypatch.setattr(nodes, "_get_llm", lambda: FakeLlm())
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    monkeypatch.setattr(nodes, "_build_chat_history", lambda _session_id: "")
    monkeypatch.setattr(nodes, "get_messages", lambda _session_id: [])
    monkeypatch.setattr(nodes, "interrupt", lambda _value: "A")

    nodes.clarify_node({
        "session_id": "session-1",
        "user_input": "查询应收发票",
        "mode": "clarify",
        "requirements": "",
        "clarify_count": 0,
        "clarify_decisions": [],
        "deferred_decisions": [],
    })

    assert calls == [{
        "extra_body": {"thinking": {"type": "disabled"}},
    }]


def test_clarify_saves_semantic_answer(monkeypatch):
    from app.agent import nodes

    class FakeLlm:
        def invoke(self, _messages, **_kwargs):
            return AIMessage(content=json.dumps(_plan(_question()), ensure_ascii=False))

    monkeypatch.setattr(nodes, "_get_llm", lambda: FakeLlm())
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    monkeypatch.setattr(nodes, "_build_chat_history", lambda _session_id: "")
    monkeypatch.setattr(nodes, "get_messages", lambda _session_id: [])
    state = {
        "session_id": "session-17",
        "user_input": "查询应收发票未收款金额",
        "mode": "clarify",
        "requirements": "",
        "clarify_count": 0,
        "clarify_decisions": [],
        "deferred_decisions": [],
    }
    prepared = nodes.clarify_node(state)
    monkeypatch.setattr(nodes, "interrupt", lambda _value: "b")
    result = nodes.clarify_answer_node({**state, **prepared})

    assert "A: 按发票含税总额减已收款金额计算（选项 B）" in result["requirements"]
    assert result["clarify_decisions"][0]["answer"] == "按发票含税总额减已收款金额计算"
    assert result["clarify_decisions"][0]["selected_option_id"] == "B"
    assert result["pending_clarify"] is None


def test_repeated_question_retries_once_then_enters_assumptions(monkeypatch):
    from app.agent import nodes

    responses = [_plan(_question())]
    calls = []

    class FakeLlm:
        def invoke(self, _messages, **_kwargs):
            calls.append(1)
            return AIMessage(content=json.dumps(responses.pop(0), ensure_ascii=False))

    monkeypatch.setattr(nodes, "_get_llm", lambda: FakeLlm())
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    monkeypatch.setattr(nodes, "_build_chat_history", lambda _session_id: "")
    monkeypatch.setattr(nodes, "get_messages", lambda _session_id: [])
    monkeypatch.setattr(
        nodes,
        "interrupt",
        lambda _value: (_ for _ in ()).throw(
            AssertionError("重复问题不应再次展示给用户"),
        ),
    )

    result = nodes.clarify_node({
        "session_id": "session-17",
        "user_input": "b",
        "mode": "clarify",
        "requirements": (
            "Q2 [outstanding_amount_basis]: 未收款金额按哪个口径计算？\n"
            "A: 按发票含税总额减已收款金额计算（选项 B）\n"
        ),
        "clarify_count": 1,
        "clarify_decisions": [{
            "decision_key": "outstanding_amount_basis",
            "question": "未收款金额按哪个口径计算？",
            "options": _question()["options"],
            "selected_option_id": "B",
            "answer": "按发票含税总额减已收款金额计算",
        }],
        "deferred_decisions": [],
    })

    assert len(calls) == 1
    assert result["mode"] == "assumptions"
    assert result["clarify_count"] == 1


def test_defaultable_question_is_deferred(monkeypatch):
    from app.agent import nodes

    class FakeLlm:
        def invoke(self, _messages, **_kwargs):
            return AIMessage(content=json.dumps(
                _plan(_question(decision_type="defaultable")),
                ensure_ascii=False,
            ))

    monkeypatch.setattr(nodes, "_get_llm", lambda: FakeLlm())
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    monkeypatch.setattr(nodes, "_build_chat_history", lambda _session_id: "")
    monkeypatch.setattr(nodes, "get_messages", lambda _session_id: [])
    monkeypatch.setattr(
        nodes,
        "interrupt",
        lambda _value: (_ for _ in ()).throw(
            AssertionError("可默认项不应在澄清阶段逐条询问"),
        ),
    )

    result = nodes.clarify_node({
        "session_id": "session-1",
        "user_input": "查询应收发票",
        "mode": "clarify",
        "requirements": "",
        "clarify_count": 0,
        "clarify_decisions": [],
        "deferred_decisions": [],
    })

    assert result["mode"] == "assumptions"
    assert result["deferred_decisions"][0]["decision_key"] == "outstanding_amount_basis"


def test_invalid_model_output_retries_once_then_enters_assumptions(monkeypatch):
    from app.agent import nodes

    calls = []

    class FakeLlm:
        def invoke(self, _messages, **_kwargs):
            calls.append(1)
            return AIMessage(content="这不是约定的 JSON")

    monkeypatch.setattr(nodes, "_get_llm", lambda: FakeLlm())
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    monkeypatch.setattr(nodes, "_build_chat_history", lambda _session_id: "")
    monkeypatch.setattr(nodes, "get_messages", lambda _session_id: [])

    result = nodes.clarify_node({
        "session_id": "session-1",
        "user_input": "查询应收发票",
        "mode": "clarify",
        "requirements": "",
        "clarify_count": 0,
        "clarify_decisions": [],
        "deferred_decisions": [],
    })

    assert len(calls) == 2
    assert result["mode"] == "assumptions"
    assert result["clarify_count"] == 0


def test_decision_plan_is_reused_without_second_model_call(monkeypatch):
    from app.agent import nodes

    class NoInvokeLlm:
        def invoke(self, *_args, **_kwargs):
            raise AssertionError("已有 DecisionPlan 时不得再次调用模型")

    plan = _plan(
        _question(key="invoice_scope", question="查询哪些发票？"),
        {
            **_question(key="output_grain", question="结果按什么粒度输出？"),
            "options": [
                {"id": "A", "value": "发票明细"},
                {"id": "B", "value": "客户汇总"},
            ],
        },
    )
    plan.pop("action")
    monkeypatch.setattr(nodes, "_get_llm", lambda: NoInvokeLlm())
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    monkeypatch.setattr(nodes, "_build_chat_history", lambda _session_id: "")
    monkeypatch.setattr(nodes, "get_messages", lambda _session_id: [])

    result = nodes.clarify_node({
        "session_id": "session-1",
        "user_input": "查询发票",
        "mode": "clarify",
        "requirements": "",
        "clarify_count": 0,
        "clarify_decisions": [],
        "decision_plan": plan,
    })

    assert result["pending_clarify"]["decision_key"] == "invoice_scope"


def test_assumptions_merge_deferred_and_excludes_answered(monkeypatch):
    from app.agent import nodes

    generated = {
        "assumptions": [
            {
                "key": "invoice_scope",
                "title": "发票范围",
                "value": "仅未结清",
                "reason": "重复项",
            },
            {
                "key": "exclude_cancelled",
                "title": "排除作废",
                "value": "是",
                "reason": "作废单据通常不统计",
            },
        ]
    }
    captured = {}

    class FakeLlm:
        def invoke(self, messages, **_kwargs):
            captured["prompt"] = messages[-1].content
            return AIMessage(content=json.dumps(generated, ensure_ascii=False))

    monkeypatch.setattr(nodes, "_get_llm", lambda: FakeLlm())
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)

    def fake_interrupt(payload):
        captured["assumptions"] = payload["assumptions"]
        return {
            "confirmed": [item["key"] for item in payload["assumptions"]],
            "modified": {},
        }

    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    result = nodes.assumptions_node({
        "session_id": "session-1",
        "mode": "assumptions",
        "requirements": "查询未结清发票",
        "clarify_decisions": [{
            "decision_key": "invoice_scope",
            "question": "查询哪些发票？",
            "answer": "仅未结清",
        }],
        "deferred_decisions": [{
            **_question(decision_type="defaultable"),
            "decision_key": "amount_basis",
        }],
    })

    keys = [item["key"] for item in captured["assumptions"]]
    assert keys == ["exclude_cancelled", "amount_basis"]
    assert "amount_basis" in captured["prompt"]
    assert result["mode"] == "design"


def test_assumptions_do_not_reopen_semantically_confirmed_decision():
    from app.agent import nodes

    clarified = [{
        "decision_key": "cancelled_invoice_scope",
        "question": "是否排除已取消发票？",
        "options": [
            {"id": "A", "value": "排除已取消发票"},
            {"id": "B", "value": "包含已取消发票并增加标志"},
        ],
        "answer": "排除已取消发票",
    }]
    generated = [{
        "key": "cancelled_flag_field",
        "title": "取消发票处理",
        "value": "返回所有状态并增加取消标志",
        "reason": "确认取消状态展示方式",
    }]

    assert nodes._merge_assumptions(generated, [], clarified) == []


if __name__ == "__main__":
    test_single_question_unchanged()
    test_multiple_questions_truncated()
    test_numbered_prefix_truncated()
    test_single_wrong_internal_number_removed()
    test_empty_string()
    print("PASS")
