"""LangGraph StateGraph 组装 — 定义节点和条件边的完整流程。"""
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from app.agent.nodes import (
    AgentState, clarify_node, design_node, generate_node,
    verify_node, deploy_check_node, deploy_node,
)

# 模块级单例 MemorySaver — 确保状态在跨请求间持久
_memory = MemorySaver()


def _after_clarify(state: AgentState) -> str:
    if state.get("mode") == "design":
        return "plan"
    return "clarify"


def _after_design(state: AgentState) -> str:
    """设计阶段后续路由：用户确认 → 进入生成；用户反馈 → 回到设计。"""
    if state.get("mode") == "design":
        return "plan"
    return "generate"


def create_graph() -> StateGraph:
    builder = StateGraph(AgentState)

    builder.add_node("clarify", clarify_node)
    builder.add_node("plan", design_node)
    builder.add_node("generate", generate_node)
    builder.add_node("verify", verify_node)
    builder.add_node("deploy_check", deploy_check_node)
    builder.add_node("deploy", deploy_node)

    builder.set_entry_point("clarify")

    builder.add_conditional_edges("clarify", _after_clarify, {
        "clarify": "clarify",
        "plan": "plan",
    })
    builder.add_conditional_edges("plan", _after_design, {
        "plan": "plan",
        "generate": "generate",
    })
    builder.add_edge("generate", "verify")
    builder.add_edge("verify", END)
    builder.add_edge("deploy_check", "deploy")
    builder.add_edge("deploy", END)

    return builder.compile(checkpointer=_memory)
