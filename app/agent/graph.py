"""LangGraph StateGraph 组装 — 定义节点和条件边的完整流程。"""
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from app.agent.nodes import (
    AgentState, clarify_node, clarify_answer_node, assumptions_node, design_node,
    result_contract_node, fact_blueprint_node, computation_blueprint_node,
    semantic_obligations_node, semantic_inputs_node, source_requirements_node,
    expression_materialize_node, semantic_compile_node,
    schema_capture_node, schema_resolve_node, semantic_revise_node,
    design_reconfirm_node, generate_node, verify_node,
)

# 模块级单例 MemorySaver — 确保状态在跨请求间持久
_memory = MemorySaver()


def _after_clarify(state: AgentState) -> str:
    if state.get("mode") == "clarify_answer":
        return "clarify_answer"
    if state.get("mode") == "assumptions":
        return "assumptions"
    if state.get("mode") == "design":
        return "assumptions"
    if state.get("mode") == "generate":
        return "schema_capture"
    return "clarify"


def _after_clarify_answer(state: AgentState) -> str:
    if state.get("mode") == "assumptions":
        return "assumptions"
    return "clarify"


def _after_assumptions(state: AgentState) -> str:
    """关键项确认后路由：进入设计阶段。"""
    if state.get("mode") == "design":
        return "result_contract"
    if state.get("mode") == "generate":
        return "schema_capture"
    return "assumptions"


def _after_design(state: AgentState) -> str:
    """设计阶段后续路由：用户确认 → 进入 Schema；用户反馈 → 回到设计。"""
    if state.get("error"):
        return "end"
    if state.get("mode") == "clarify":
        return "clarify"
    if state.get("mode") == "assumptions":
        return "assumptions"
    if state.get("mode") == "design":
        return "plan"
    return "schema_capture"


def _after_semantic_stage(state: AgentState, next_node: str) -> str:
    if state.get("error") or state.get("status") == "semantic_design_failed":
        return "end"
    return next_node


def _after_schema_capture(state: AgentState) -> str:
    if (
        state.get("status") == "schema_resolving"
        and not state.get("error")
    ):
        return "schema_resolve"
    return "end"


def _after_schema_resolve(state: AgentState) -> str:
    status = state.get("status")
    if status == "schema_resolved" and not state.get("error"):
        return "generate"
    if status == "semantic_revision_required":
        return "semantic_revise"
    return "end"


def _after_semantic_revise(state: AgentState) -> str:
    if (
        state.get("status") == "awaiting_design_reconfirmation"
        and not state.get("error")
    ):
        return "design_reconfirm"
    return "end"


def _after_design_reconfirm(state: AgentState) -> str:
    if state.get("status") == "design_reconfirmed":
        return "schema_capture"
    if state.get("mode") == "design":
        return "plan"
    return "end"


def _after_generate(state: AgentState) -> str:
    """仅在本次生成完整成功后进入数据库校验。"""
    if state.get("status") == "candidate_generated" and not state.get("error"):
        return "verify"
    return "end"


def _compile_graph() -> StateGraph:
    builder = StateGraph(AgentState)

    builder.add_node("clarify", clarify_node)
    builder.add_node("clarify_answer", clarify_answer_node)
    builder.add_node("assumptions", assumptions_node)
    builder.add_node("result_contract", result_contract_node)
    builder.add_node("fact_blueprint", fact_blueprint_node)
    builder.add_node("computation_blueprint", computation_blueprint_node)
    builder.add_node("semantic_obligations", semantic_obligations_node)
    builder.add_node("semantic_inputs", semantic_inputs_node)
    builder.add_node("source_requirements", source_requirements_node)
    builder.add_node("expression_materialize", expression_materialize_node)
    builder.add_node("semantic_compile", semantic_compile_node)
    builder.add_node("plan", design_node)
    builder.add_node("schema_capture", schema_capture_node)
    builder.add_node("schema_resolve", schema_resolve_node)
    builder.add_node("semantic_revise", semantic_revise_node)
    builder.add_node("design_reconfirm", design_reconfirm_node)
    builder.add_node("generate", generate_node)
    builder.add_node("verify", verify_node)

    builder.set_entry_point("clarify")

    builder.add_conditional_edges("clarify", _after_clarify, {
        "clarify": "clarify",
        "clarify_answer": "clarify_answer",
        "assumptions": "assumptions",
        "schema_capture": "schema_capture",
    })
    builder.add_conditional_edges("clarify_answer", _after_clarify_answer, {
        "clarify": "clarify",
        "assumptions": "assumptions",
    })
    builder.add_conditional_edges("assumptions", _after_assumptions, {
        "assumptions": "assumptions",
        "result_contract": "result_contract",
        "schema_capture": "schema_capture",
    })
    builder.add_conditional_edges(
        "result_contract",
        lambda state: _after_semantic_stage(state, "fact_blueprint"),
        {"fact_blueprint": "fact_blueprint", "end": END},
    )
    builder.add_conditional_edges(
        "fact_blueprint",
        lambda state: _after_semantic_stage(
            state,
            "computation_blueprint",
        ),
        {"computation_blueprint": "computation_blueprint", "end": END},
    )
    builder.add_conditional_edges(
        "computation_blueprint",
        lambda state: _after_semantic_stage(state, "semantic_obligations"),
        {"semantic_obligations": "semantic_obligations", "end": END},
    )
    builder.add_conditional_edges(
        "semantic_obligations",
        lambda state: _after_semantic_stage(state, "semantic_inputs"),
        {"semantic_inputs": "semantic_inputs", "end": END},
    )
    builder.add_conditional_edges(
        "semantic_inputs",
        lambda state: _after_semantic_stage(state, "source_requirements"),
        {"source_requirements": "source_requirements", "end": END},
    )
    builder.add_conditional_edges(
        "source_requirements",
        lambda state: _after_semantic_stage(
            state,
            "expression_materialize",
        ),
        {"expression_materialize": "expression_materialize", "end": END},
    )
    builder.add_conditional_edges(
        "expression_materialize",
        lambda state: _after_semantic_stage(state, "semantic_compile"),
        {"semantic_compile": "semantic_compile", "end": END},
    )
    builder.add_conditional_edges(
        "semantic_compile",
        lambda state: _after_semantic_stage(state, "plan"),
        {"plan": "plan", "end": END},
    )
    builder.add_conditional_edges("plan", _after_design, {
        "plan": "plan",
        "clarify": "clarify",
        "assumptions": "assumptions",
        "schema_capture": "schema_capture",
        "end": END,
    })
    builder.add_conditional_edges("schema_capture", _after_schema_capture, {
        "schema_resolve": "schema_resolve",
        "end": END,
    })
    builder.add_conditional_edges("schema_resolve", _after_schema_resolve, {
        "generate": "generate",
        "semantic_revise": "semantic_revise",
        "end": END,
    })
    builder.add_conditional_edges("semantic_revise", _after_semantic_revise, {
        "design_reconfirm": "design_reconfirm",
        "end": END,
    })
    builder.add_conditional_edges("design_reconfirm", _after_design_reconfirm, {
        "schema_capture": "schema_capture",
        "plan": "plan",
        "end": END,
    })
    builder.add_conditional_edges("generate", _after_generate, {
        "verify": "verify",
        "end": END,
    })
    builder.add_edge("verify", END)

    return builder.compile(checkpointer=_memory)


# 图结构和 checkpointer 都是进程级资源。编译结果是线程安全的，且会话隔离由
# configurable.thread_id 保证；不要在每个 SSE 请求里重复构建整张图。
_graph = _compile_graph()


def create_graph() -> StateGraph:
    """返回进程级已编译图（保留原函数名，避免影响现有调用方）。"""
    return _graph
