"""确定性验证 _invoke_with_tools 在工具循环耗尽时的行为。
模拟 LLM 一直请求调 tool（模拟 run 1 的场景），确认耗尽后用 plain llm.invoke 收尾并返回其 content。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unittest.mock import MagicMock
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from app.agent.nodes import _invoke_with_tools
from app.agent import nodes


def make_ai(tool_calls=None, content=""):
    msg = AIMessage(content=content)
    if tool_calls:
        msg.tool_calls = tool_calls
    return msg


# 准备 mock：llm_with_tools.invoke 一直返回带 tool_calls 的响应（模拟 LLM 死循环调工具）
# 第 max_rounds 轮后耗尽，应调用 plain llm.invoke 返回最终 JSON
FINAL_JSON = '```json\n{"procedures": [{"name": "SP_X", "code": "CREATE PROCEDURE SP_X AS BEGIN SET NOCOUNT ON END"}]}\n```'

call_log = []


def make_llm():
    llm = MagicMock()

    def bind_tools(tools):
        bt = MagicMock()

        def bt_invoke(messages):
            call_log.append("with_tools")
            # 一直请求调 check_syntax_tool，模拟 run 1 的死循环
            return make_ai(tool_calls=[{"name": "check_syntax_tool", "args": {"sql": "SELECT 1"}, "id": "tc1"}], content="让我再检查语法。")

        bt.invoke = MagicMock(side_effect=bt_invoke)
        return bt

    llm.bind_tools = MagicMock(side_effect=bind_tools)

    def llm_invoke(messages):
        call_log.append("plain")
        return AIMessage(content=FINAL_JSON)  # 不带 tool_calls，返回 JSON

    llm.invoke = MagicMock(side_effect=llm_invoke)
    return llm


def test_exhaustion_uses_plain_invoke():
    global call_log
    call_log = []
    llm = make_llm()
    messages = [SystemMessage(content="sys"), HumanMessage(content="gen")]
    resp = _invoke_with_tools(llm, messages, max_rounds=3)

    # 断言1：耗尽后调用了 plain llm.invoke（修复点）
    assert "plain" in call_log, f"未调用 plain llm.invoke，call_log={call_log}"
    # 断言2：plain invoke 是最后一次调用
    assert call_log[-1] == "plain", f"最后一次调用不是 plain，call_log={call_log}"
    # 断言3：返回的 content 是最终 JSON
    assert "procedures" in resp.content, f"返回 content 非 JSON: {resp.content!r}"
    # 断言4：返回的 response 无 tool_calls
    assert not getattr(resp, "tool_calls", None), "返回的 response 仍有 tool_calls"
    # 断言5：with_tools 被调用了 max_rounds 次
    assert call_log.count("with_tools") == 3, f"with_tools 调用次数不符: {call_log}"
    print("PASS: 工具循环耗尽 → plain llm.invoke 收尾 → 返回 JSON，无 tool_calls")
    print(f"  call_log = {call_log}")


if __name__ == "__main__":
    test_exhaustion_uses_plain_invoke()
    print("DONE")
