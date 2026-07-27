"""对话路由 — SSE 流式对话，驱动 Agent 状态图。"""
import asyncio
import json
import logging
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from app.db.sqlite import save_message, get_messages
from app.agent.graph import create_graph
from langgraph.types import Command

_graph = create_graph()
router = APIRouter(prefix="/api/chat", tags=["chat"])
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    session_id: str
    message: str
    action: str = "send"


def _parse_assumption_response(
    message: str,
    expected_keys: set[str] | None = None,
) -> dict:
    """解析前端关键项确认；禁止把任意文本误当成“全部确认”."""
    try:
        payload = json.loads(message)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "关键项确认格式无效，请逐项选择“同意”或“修改”后提交"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("关键项确认必须是结构化对象")
    confirmed = payload.get("confirmed", [])
    modified = payload.get("modified", {})
    if (
        not isinstance(confirmed, list)
        or not all(isinstance(item, str) and item.strip() for item in confirmed)
        or not isinstance(modified, dict)
        or not all(
            isinstance(key, str)
            and key.strip()
            and isinstance(value, str)
            and value.strip()
            for key, value in modified.items()
        )
    ):
        raise ValueError("关键项确认中的 confirmed 或 modified 格式无效")
    overlap = set(confirmed) & set(modified)
    if overlap:
        raise ValueError(
            "同一关键项不能同时确认和修改：" + "、".join(sorted(overlap))
        )
    submitted = set(confirmed) | set(modified)
    if expected_keys is not None and submitted != expected_keys:
        missing = sorted(expected_keys - submitted)
        unknown = sorted(submitted - expected_keys)
        details = []
        if missing:
            details.append("缺少：" + "、".join(missing))
        if unknown:
            details.append("未知：" + "、".join(unknown))
        raise ValueError("关键项确认与当前问题不一致；" + "；".join(details))
    return {"confirmed": confirmed, "modified": modified}


def _parse_schema_choice_response(message: str) -> dict:
    try:
        payload = json.loads(message)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Schema 选择格式无效，请在候选卡片中逐项选择") from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("checkpoint_id"), str)
        or not isinstance(payload.get("selections"), dict)
        or not payload["selections"]
        or not all(
            isinstance(key, str)
            and isinstance(value, str)
            and key
            and value
            for key, value in payload["selections"].items()
        )
    ):
        raise ValueError("Schema 选择必须包含 checkpoint_id 和完整 selections")
    return payload


def _verify_result_display_name(result: dict) -> str:
    return (
        result.get("sp_name")
        or str(result.get("sp_id") or "")[:8]
        or "未知候选"
    )


_STAGE_LABELS = {
    "environment": "环境",
    "semantic_contract": "业务合同",
    "schema_binding": "Schema 绑定",
    "reference_plan": "Reference 计划",
    "reference_compile": "Reference 编译",
    "reference_preflight": "Reference 预执行",
    "procedure_plan": "SP 计划",
    "procedure_compile": "SP 编译",
    "result_contract": "结果合同",
    "business_comparison": "业务对账",
    "evidence_integrity": "证据完整性",
    "query_spec": "方案契约",
    "schema": "Schema",
    "safety": "安全",
    "compile": "SQL 编译",
    "contract": "契约一致性",
    "business": "业务",
}


def _stage_summary(result: dict) -> str:
    stages = result.get("stages") or {}
    parts = []
    if isinstance(stages, list):
        by_name = {item.get("stage"): item for item in stages}
        names = (
            "environment", "semantic_contract", "schema_binding",
            "reference_plan", "reference_compile", "reference_preflight",
            "procedure_plan", "procedure_compile", "result_contract",
            "business_comparison", "evidence_integrity",
        )
    else:
        by_name = stages
        names = ("schema", "safety", "compile", "contract", "business")
    for name in names:
        status = (by_name.get(name) or {}).get("status", "not_run")
        icon = {
            "passed": "✅", "failed": "❌", "inconclusive": "⚠️",
            "running": "⏳", "not_run": "⬜",
        }.get(
            status, "⬜",
        )
        parts.append(f"{icon} {_STAGE_LABELS[name]}")
    return "  ".join(parts)


def _wants_to_skip_clarify(message: str) -> bool:
    """识别前端明确的“跳过澄清”快捷操作，不猜测普通自然语言。"""
    normalized = "".join(message.lower().split()).strip("，,。.!！?？")
    return normalized in {
        "不需要再问了，请直接生成设计方案",
        "跳过澄清，直接设计",
        "跳过确认，直接设计",
    }


def _has_interrupt(state) -> bool:
    """检查 StateSnapshot 是否有待处理的中断。"""
    if state is None:
        return False
    if not state.tasks:
        return False
    for task in state.tasks:
        if task.interrupts:
            return True
    return False


def _get_interrupt_value(state):
    """获取第一个中断的值。"""
    for task in (state.tasks or []):
        for iv in (task.interrupts or []):
            return iv.value
    return None


async def _get_graph_state(config):
    """避免同步状态读取阻塞 FastAPI 的事件循环。"""
    async_get_state = getattr(_graph, "aget_state", None)
    if async_get_state is not None:
        return await async_get_state(config)
    return await asyncio.to_thread(_graph.get_state, config)


@router.post("/stream")
async def api_chat_stream(req: ChatRequest):
    """SSE 流式对话端点。"""
    async def event_stream():
        graph = _graph
        config = {"configurable": {"thread_id": req.session_id}}

        try:
            # 立即返回首个事件，让浏览器不再无反馈地等待 LLM/数据库。
            yield f"data: {json.dumps({'type': 'progress', 'stage': 'accepted', 'content': '已收到，正在分析...'})}\n\n"
            await asyncio.to_thread(save_message, req.session_id, "user", req.message)
            state = await _get_graph_state(config)

            # 检测用户消息数（用于强制退出澄清阶段）
            all_msgs = await asyncio.to_thread(get_messages, req.session_id)
            user_count = sum(1 for m in all_msgs if m["role"] == "user")

            # 判断是否处于中断等待状态
            if _has_interrupt(state):
                interrupt_val = _get_interrupt_value(state)
                itype = interrupt_val.get("type", "") if isinstance(interrupt_val, dict) else ""

                if itype == "clarify" and _wants_to_skip_clarify(req.message):
                    raise ValueError(
                        "当前仍有影响结果的阻塞问题，不能跳过；请回答当前问题"
                    )
                if itype == "design" and req.action == "confirm_design":
                    # 快捷确认按钮使用结构化动作，避免让 LLM 猜测固定按钮文案的意图。
                    events = graph.astream(
                        Command(resume={"action": "confirm"}),
                        config,
                        stream_mode=["updates", "custom"],
                    )
                elif itype == "design_revision":
                    response = (
                        {"action": "confirm"}
                        if req.action == "confirm_design_revision"
                        else {"action": "reject", "feedback": req.message}
                    )
                    events = graph.astream(
                        Command(resume=response),
                        config,
                        stream_mode=["updates", "custom"],
                    )
                elif itype == "schema_choice":
                    events = graph.astream(
                        Command(
                            resume=_parse_schema_choice_response(req.message)
                        ),
                        config,
                        stream_mode=["updates", "custom"],
                    )
                elif itype == "assumptions":
                    expected_keys = {
                        str(item.get("key"))
                        for item in interrupt_val.get("assumptions", [])
                        if isinstance(item, dict) and item.get("key")
                    }
                    events = graph.astream(
                        Command(
                            resume=_parse_assumption_response(
                                req.message, expected_keys,
                            )
                        ),
                        config,
                        stream_mode=["updates", "custom"],
                    )
                else:
                    events = graph.astream(Command(resume=req.message), config, stream_mode=["updates", "custom"])
            elif state and state.values:
                # 继续既有会话
                mode = state.values.get("mode", "clarify")
                status = state.values.get("status", "")

                # 校验完成后用户追问 → 先更新并重新确认设计，再生成。
                if status in ("persisted", "verify_failed", "needs_review") and req.message.strip():
                    # 把用户反馈追加到设计方案中作为修改要求
                    input_state = {
                        "session_id": req.session_id,
                        "user_input": req.message,
                        "mode": "clarify",
                        "requirements": (
                            f"{state.values.get('requirements', '')}\n"
                            f"用户追加修改要求：{req.message}"
                        ).strip(),
                        "confirmed_assumptions": state.values.get("confirmed_assumptions", ""),
                        "design": "",
                        "sp_list": state.values.get("sp_list", []),
                        "verify_results": [],
                        "status": "",
                        "error": "",
                        "clarify_count": state.values.get("clarify_count", 0),
                        "design_phase": None,
                        "last_feedback_reply": "已收到修改要求，正在更新方案。",
                        "query_spec": {},
                        "schema_fingerprint": state.values.get("schema_fingerprint", ""),
                        "clarify_decisions": [],
                        "deferred_decisions": [],
                        "pending_clarify": None,
                        "decision_plan": {},
                        "confirmed_decision_set": {},
                        "semantic_design_hash": state.values.get("semantic_design_hash", ""),
                        "schema_catalog": state.values.get("schema_catalog", {}),
                        "schema_artifacts": state.values.get("schema_artifacts", []),
                        "schema_resolution_checkpoints": state.values.get("schema_resolution_checkpoints", []),
                        "schema_resolution_issues": state.values.get("schema_resolution_issues", []),
                        "pending_schema_interaction": state.values.get("pending_schema_interaction"),
                        "schema_resolution_status": state.values.get("schema_resolution_status", ""),
                        "semantic_revision": state.values.get("semantic_revision", {}),
                        "semantic_revision_diff": state.values.get("semantic_revision_diff", {}),
                        "semantic_revision_count": state.values.get("semantic_revision_count", 0),
                        "result_contract": {},
                        "fact_blueprint": {},
                        "computation_blueprint": {},
                        "semantic_obligations": {},
                        "semantic_inputs": {},
                        "source_requirements": {},
                        "expression_design": {},
                        "semantic_compile_result": {},
                        "semantic_design_stage": "result_contract",
                        "semantic_design_diagnostics": [],
                    }
                    events = graph.astream(input_state, config, stream_mode=["updates", "custom"])
                else:
                    input_state = {
                        "session_id": req.session_id,
                        "user_input": req.message,
                        "mode": mode,
                        "requirements": state.values.get("requirements", ""),
                        "confirmed_assumptions": state.values.get("confirmed_assumptions", ""),
                        "design": state.values.get("design", ""),
                        "sp_list": state.values.get("sp_list", []),
                        "verify_results": state.values.get("verify_results", []),
                        "status": state.values.get("status", ""),
                        "error": state.values.get("error", ""),
                        "clarify_count": state.values.get("clarify_count", 0),
                        "design_phase": state.values.get("design_phase"),
                        "last_feedback_reply": state.values.get("last_feedback_reply", ""),
                        "query_spec": state.values.get("query_spec", {}),
                        "schema_fingerprint": state.values.get("schema_fingerprint", ""),
                        "clarify_decisions": state.values.get("clarify_decisions", []),
                        "deferred_decisions": state.values.get("deferred_decisions", []),
                        "pending_clarify": state.values.get("pending_clarify"),
                        "decision_plan": state.values.get("decision_plan", {}),
                        "confirmed_decision_set": state.values.get("confirmed_decision_set", {}),
                        "result_contract": state.values.get("result_contract", {}),
                        "fact_blueprint": state.values.get("fact_blueprint", {}),
                        "computation_blueprint": state.values.get("computation_blueprint", {}),
                        "semantic_obligations": state.values.get("semantic_obligations", {}),
                        "semantic_inputs": state.values.get("semantic_inputs", {}),
                        "source_requirements": state.values.get("source_requirements", {}),
                        "expression_design": state.values.get("expression_design", {}),
                        "semantic_compile_result": state.values.get("semantic_compile_result", {}),
                        "semantic_design_stage": state.values.get("semantic_design_stage", ""),
                        "semantic_design_diagnostics": state.values.get("semantic_design_diagnostics", []),
                    }
                    events = graph.astream(input_state, config, stream_mode=["updates", "custom"])
            else:
                # 全新会话
                input_state = {
                    "session_id": req.session_id,
                    "user_input": req.message,
                    "mode": "clarify",
                    "requirements": "",
                    "confirmed_assumptions": "",
                    "design": "",
                    "sp_list": [],
                    "verify_results": [],
                    "status": "",
                    "error": "",
                    "clarify_count": 0,
                    "design_phase": None,
                    "last_feedback_reply": "",
                    "query_spec": {},
                    "schema_fingerprint": "",
                    "clarify_decisions": [],
                    "deferred_decisions": [],
                    "pending_clarify": None,
                    "decision_plan": {},
                    "confirmed_decision_set": {},
                    "result_contract": {},
                    "fact_blueprint": {},
                    "computation_blueprint": {},
                    "semantic_obligations": {},
                    "semantic_inputs": {},
                    "source_requirements": {},
                    "expression_design": {},
                    "semantic_compile_result": {},
                    "semantic_design_stage": "",
                    "semantic_design_diagnostics": [],
                }
                events = graph.astream(input_state, config, stream_mode=["updates", "custom"])

            assistant_response = ""
            assistant_saved = False
            generate_failed = False  # generate 失败时不再用 verify 结果覆盖，避免"校验全对但右侧旧SP"误导

            async def _handle_event():
                """处理单个事件流，返回是否需要继续处理后续中断（auto_fix 场景）。
                注意：用 nonlocal 修改外层 assistant_response 和 generate_failed。
                """
                nonlocal assistant_response, assistant_saved, generate_failed

                async for mode, data in events:
                    if mode == "custom":
                        # 流式 token 事件：直接转发给前端
                        yield f"data: {json.dumps(data)}\n\n"
                        continue

                    # mode == "updates": 节点完成事件，保持原有逻辑
                    for node_name, node_output in data.items():
                        if isinstance(node_output, dict):
                            if node_output.get("error") and node_output.get("status") in (
                                "semantic_design_failed", "design_failed", "generate_failed",
                                "generation_failed",
                            ):
                                generate_failed = True
                                if node_output.get("status") == "semantic_design_failed":
                                    diagnostics = node_output.get(
                                        "semantic_design_diagnostics", [],
                                    )
                                    diagnostic = (
                                        diagnostics[0]
                                        if diagnostics
                                        else {
                                            "stage": node_output.get(
                                                "semantic_design_stage",
                                                "semantic_compile",
                                            ),
                                            "code": "SEMANTIC_DESIGN_FAILED",
                                            "message": node_output["error"],
                                            "evidence": {},
                                            "system_action": (
                                                "已停止设计，下游 Schema、Reference 和 SP 均未运行"
                                            ),
                                            "user_action": "请补充或修正业务口径",
                                        }
                                    )
                                    payload = {
                                        "type": "semantic_design_error",
                                        **diagnostic,
                                    }
                                    assistant_response = (
                                        f"语义设计停在 {payload.get('stage', '')}："
                                        f"{payload.get('message', node_output['error'])}"
                                    )
                                    yield (
                                        "data: "
                                        + json.dumps(payload)
                                        + "\n\n"
                                    )
                                elif node_output.get("status") == "design_failed":
                                    assistant_response = (
                                        "⚠️ 方案草稿已生成，但业务契约尚未通过。\n\n"
                                        f"{node_output.get('design', '')}\n\n"
                                        f"契约问题：{node_output['error'][:500]}\n\n"
                                        "草稿已保存；修正前不会生成或部署 SQL。"
                                    )
                                    yield f"data: {json.dumps({'type': 'design_draft', 'content': assistant_response})}\n\n"
                                else:
                                    issues = (
                                        node_output.get(
                                            "schema_resolution_issues"
                                        )
                                        or node_output.get("issues")
                                        or []
                                    )
                                    issue_text = ""
                                    if issues:
                                        first = issues[0]
                                        if first.get("category"):
                                            payload = {
                                                "type": "schema_issue",
                                                "stage": "schema_resolution",
                                                "code": first.get("code", ""),
                                                "category": first.get("category", ""),
                                                "title": "Schema 解析未通过",
                                                "business_impact": first.get(
                                                    "business_meaning", ""
                                                ),
                                                "evidence": first.get(
                                                    "catalog_evidence", {}
                                                ),
                                                "system_action": (
                                                    "系统已停止后续 Reference 和 SP 生成"
                                                ),
                                                "user_action": {
                                                    "binding_repairable": "重新生成绑定提案",
                                                    "physical_ambiguity": "选择正确的数据库字段",
                                                    "semantic_capability_gap": "确认数据来源设计修订",
                                                    "environment": "检查测试数据库连接与权限",
                                                    "internal_generation": "检查模型输出或稍后重试",
                                                }.get(
                                                    first.get("category"),
                                                    "查看问题后重试",
                                                ),
                                                "reason": first.get(
                                                    "reason",
                                                    node_output["error"],
                                                ),
                                            }
                                            assistant_response = (
                                                f"Schema 解析未通过："
                                                f"{payload['reason']}"
                                            )
                                            yield (
                                                "data: "
                                                + json.dumps(payload)
                                                + "\n\n"
                                            )
                                            continue
                                        issue_text = (
                                            f"\n错误码：{first.get('code', '')}"
                                            f"\n处理建议：{first.get('user_action', '')}"
                                        )
                                    assistant_response = (
                                        f"❌ 生成失败：{node_output['error'][:300]}\n\n"
                                        f"{issue_text}\n\n"
                                        "存储过程未能生成，右侧仍显示上一次的结果。"
                                    )
                                    yield f"data: {json.dumps({'type': 'error', 'content': assistant_response})}\n\n"

                            if node_output.get("status") == "candidate_generated":
                                sp_list = node_output.get("sp_list", [])
                                assistant_response = f"已生成 {len(sp_list)} 个内存候选。\n"
                                for sp in sp_list:
                                    assistant_response += f"- {sp['name']}\n"
                                assistant_response += "\n正在校验..."

                            elif node_output.get("status") in ("persisted", "verify_failed", "needs_review"):
                                # generate 已失败时跳过 verify 结果
                                if generate_failed:
                                    yield f"data: {json.dumps({'node': node_name, 'data': node_output, 'type': 'update'})}\n\n"
                                    continue
                                v_results = node_output.get("verify_results", [])
                                print(f"[DEBUG verify_result] status={node_output.get('status')}, v_results count={len(v_results)}", flush=True)
                                for i, vr in enumerate(v_results):
                                    print(f"[DEBUG verify_result]   [{i}] sp_id={str(vr.get('sp_id') or '?')[:8]}, syntax_ok={vr.get('syntax_ok')}, biz_ok={vr.get('business_ok')}, details={len(vr.get('details',[]))}", flush=True)
                                lines = ["\n--- 校验结果 ---"]
                                if v_results:
                                    for vr in v_results:
                                        sp_name = _verify_result_display_name(vr)
                                        lines.append(f"📄 {sp_name}")
                                        lines.append(f"   {_stage_summary(vr)}")
                                        for d in vr.get("details", []):
                                            if d.get("type") == "compile" and not d.get("pass"):
                                                lines.append(f"   SQL 编译错误: {d.get('error', '')[:120]}")
                                            elif d.get("type") == "business":
                                                mark = "✅" if d.get("pass") else "❌"
                                                comparison = d.get("comparison") or {}
                                                detail = comparison.get("summary") or d.get("error") or d.get("data", "")
                                                detail_str = str(detail)[:120] if detail else ''
                                                lines.append(f"   {mark} {d.get('query', '')}: {detail_str}")
                                            elif not d.get("pass") and d.get("error"):
                                                label = {
                                                    "safety": "安全检查", "execution": "执行错误",
                                                    "rollback": "回滚检查", "configuration": "校验配置错误",
                                                }.get(d.get("type"), "校验错误")
                                                lines.append(f"   ❌ {label}: {d['error'][:120]}")
                                else:
                                    lines.append("⚠️ 校验结果为空，可能未生成存储过程")
                                if node_output.get("status") == "needs_review":
                                    lines.append("⚠️ 候选已保存为待复核草稿，可在右侧编辑；通过校验前不可部署。")
                                elif node_output.get("status") == "persisted":
                                    lines.append("✅ 整批候选已通过并原子保存。")
                                elif node_output.get("status") == "verify_failed":
                                    lines.append("⚠️ 候选已保存为校验失败草稿，可在右侧编辑；通过校验前不可部署。")
                                elif node_output.get("error"):
                                    lines.append(f"❌ {node_output['error'][:300]}")
                                assistant_response = "\n".join(lines)
                                # 先持久化最终状态再推送 SSE；客户端此时断开也能在刷新后恢复结果。
                                await asyncio.to_thread(
                                    save_message, req.session_id, "assistant", assistant_response,
                                )
                                assistant_saved = True
                                yield f"data: {json.dumps({'type': 'verify_result', 'content': assistant_response, 'data': node_output})}\n\n"

                            yield f"data: {json.dumps({'node': node_name, 'data': node_output, 'type': 'update'})}\n\n"

            # 主事件流
            async for _item in _handle_event():
                yield _item

            # 自动修复 / 中断循环
            while True:
                new_state = await _get_graph_state(config)
                if not new_state or not _has_interrupt(new_state):
                    break

                interrupt_val = _get_interrupt_value(new_state)
                if not isinstance(interrupt_val, dict):
                    break

                itype = interrupt_val.get("type", "")

                if itype == "auto_fix_progress":
                    # 发送修复进度消息，然后自动恢复 graph
                    msg = interrupt_val.get("message", "")
                    yield f"data: {json.dumps({'type': 'auto_fix_progress', 'content': msg})}\n\n"
                    try:
                        events = graph.astream(Command(resume="continue"), config, stream_mode=["updates", "custom"])
                        async for _item in _handle_event():
                            yield _item
                        continue  # 回到 while 开头检查是否有新中断
                    except Exception as e:
                        error_msg = f"自动修复过程出错: {str(e)}"
                        yield f"data: {json.dumps({'type': 'error', 'content': error_msg})}\n\n"
                        break

                elif itype == "clarify":
                    q_num = interrupt_val.get("q_num", "")
                    prefix = f"Q{q_num}：" if q_num else ""
                    assistant_response = prefix + interrupt_val.get("question", "")
                    yield f"data: {json.dumps({'type': 'question', 'content': assistant_response})}\n\n"
                    break  # 等待用户输入

                elif itype == "assumptions":
                    assumptions = interrupt_val.get("assumptions", [])
                    assistant_response = "请确认以下关键项："
                    for a in assumptions:
                        assistant_response += f"\n- {a.get('title', '')}: {a.get('value', '')}"
                    yield f"data: {json.dumps({'type': 'assumptions', 'content': assistant_response, 'assumptions': assumptions})}\n\n"
                    break  # 等待用户确认

                elif itype == "design":
                    content = interrupt_val.get("content", "")
                    reply = interrupt_val.get("reply", "")
                    if reply:
                        assistant_response = reply + "\n\n" + content
                    else:
                        assistant_response = content
                    yield f"data: {json.dumps({'type': 'design', 'content': assistant_response})}\n\n"
                    break  # 等待用户输入

                elif itype == "schema_choice":
                    assistant_response = "数据库中存在多个都合理的物理实现，请选择业务上正确的一项。"
                    yield f"data: {json.dumps({'type': 'schema_choice', 'content': assistant_response, 'checkpoint_id': interrupt_val.get('checkpoint_id'), 'issues': interrupt_val.get('issues', [])})}\n\n"
                    break

                elif itype == "design_revision":
                    assistant_response = "当前业务口径需要调整数据来源的实现形状，请确认建议修订。"
                    yield f"data: {json.dumps({'type': 'design_revision', 'content': assistant_response, 'revision': interrupt_val.get('content', {})})}\n\n"
                    break

                else:
                    break  # 未知中断类型

            if not assistant_response:
                assistant_response = "处理完成"

            if not assistant_saved:
                await asyncio.to_thread(save_message, req.session_id, "assistant", assistant_response)
            yield f"data: {json.dumps({'type': 'done', 'content': assistant_response})}\n\n"

        except Exception as e:
            logger.exception("处理会话流时发生异常")
            error_msg = f"处理出错: {str(e)}"
            await asyncio.to_thread(save_message, req.session_id, "assistant", error_msg)
            yield f"data: {json.dumps({'type': 'error', 'content': error_msg})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/messages/{session_id}")
def api_get_messages(session_id: str):
    from app.db.sqlite import (
        get_semantic_design_checkpoint,
        list_schema_resolution_checkpoints,
    )

    pending = [
        item for item in list_schema_resolution_checkpoints(session_id)
        if item.get("status") in {
            "awaiting_schema_choice",
            "awaiting_design_reconfirmation",
        }
    ]
    semantic_design = get_semantic_design_checkpoint(session_id)
    return {
        "messages": get_messages(session_id),
        "schema_pending": pending,
        "semantic_design_checkpoint": semantic_design,
    }
