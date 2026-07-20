"""LangGraph StateGraph 组装 — 定义节点和条件边的完整流程。"""
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from app.agent.nodes import (
    AgentState, clarify_node, assumptions_node, design_node, generate_node,
    verify_node,
)

# 模块级单例 MemorySaver — 确保状态在跨请求间持久
_memory = MemorySaver()


def _after_clarify(state: AgentState) -> str:
    if state.get("mode") == "assumptions":
        return "assumptions"
    if state.get("mode") == "design":
        return "assumptions"
    if state.get("mode") == "generate":
        return "generate"
    return "clarify"


def _after_assumptions(state: AgentState) -> str:
    """关键项确认后路由：进入设计阶段。"""
    if state.get("mode") == "design":
        return "plan"
    if state.get("mode") == "generate":
        return "generate"
    return "assumptions"


def _after_design(state: AgentState) -> str:
    """设计阶段后续路由：用户确认 → 进入生成；用户反馈 → 回到设计。"""
    if state.get("mode") == "design":
        return "plan"
    return "generate"


def _compile_graph() -> StateGraph:
    builder = StateGraph(AgentState)

    builder.add_node("clarify", clarify_node)
    builder.add_node("assumptions", assumptions_node)
    builder.add_node("plan", design_node)
    builder.add_node("generate", generate_node)
    builder.add_node("verify", verify_node)

    builder.set_entry_point("clarify")

    builder.add_conditional_edges("clarify", _after_clarify, {
        "clarify": "clarify",
        "assumptions": "assumptions",
        "generate": "generate",
    })
    builder.add_conditional_edges("assumptions", _after_assumptions, {
        "assumptions": "assumptions",
        "plan": "plan",
        "generate": "generate",
    })
    builder.add_conditional_edges("plan", _after_design, {
        "plan": "plan",
        "generate": "generate",
    })
    builder.add_edge("generate", END)
    builder.add_edge("verify", END)

    return builder.compile(checkpointer=_memory)


# 图结构和 checkpointer 都是进程级资源。编译结果是线程安全的，且会话隔离由
# configurable.thread_id 保证；不要在每个 SSE 请求里重复构建整张图。
_graph = _compile_graph()


def create_graph() -> StateGraph:
    """返回进程级已编译图（保留原函数名，避免影响现有调用方）。"""
    return _graph
