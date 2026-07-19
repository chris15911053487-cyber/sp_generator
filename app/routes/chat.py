"""对话路由 — SSE 流式对话，驱动 Agent 状态图。"""
import asyncio
import json
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from app.db.sqlite import save_message, get_messages
from app.agent.graph import create_graph
from langgraph.types import Command

_graph = create_graph()
router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    session_id: str
    message: str
    action: str = "send"

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

                # 如果澄清阶段超过 4 条用户消息，强制进入关键项确认阶段
                if itype == "clarify" and (user_count >= 6 or _wants_to_skip_clarify(req.message)):
                    # 跳过需求确认，直接用 mode=assumptions 重启 graph
                    requirements = state.values.get("requirements", "") if state.values else ""
                    new_input = {
                        "session_id": req.session_id,
                        "user_input": req.message,
                        "mode": "assumptions",
                        "requirements": requirements,
                        "confirmed_assumptions": state.values.get("confirmed_assumptions", "") if state.values else "",
                        "design": "",
                        "sp_list": [],
                        "verify_results": [],
                        "status": "",
                        "error": "",
                        "clarify_count": state.values.get("clarify_count", 0) if state.values else 0,
                        "design_phase": state.values.get("design_phase") if state.values else None,
                        "last_feedback_reply": state.values.get("last_feedback_reply", "") if state.values else "",
                    }
                    events = graph.astream(new_input, config, stream_mode=["updates", "custom"])
                elif itype == "design" and req.action == "confirm_design":
                    # 快捷确认按钮使用结构化动作，避免让 LLM 猜测固定按钮文案的意图。
                    events = graph.astream(
                        Command(resume={"action": "confirm"}),
                        config,
                        stream_mode=["updates", "custom"],
                    )
                else:
                    events = graph.astream(Command(resume=req.message), config, stream_mode=["updates", "custom"])
            elif state and state.values:
                # 继续既有会话
                mode = state.values.get("mode", "clarify")
                status = state.values.get("status", "")

                # 校验完成后用户追问 → 将用户反馈作为修改需求，重新生成
                if status in ("verified", "verify_failed") and req.message.strip():
                    # 把用户反馈追加到设计方案中作为修改要求
                    design = state.values.get("design", "")
                    modified_design = design + f"\n\n## 用户修改要求\n{req.message}"
                    input_state = {
                        "session_id": req.session_id,
                        "user_input": req.message,
                        "mode": "generate",
                        "requirements": state.values.get("requirements", ""),
                        "confirmed_assumptions": state.values.get("confirmed_assumptions", ""),
                        "design": modified_design,
                        "sp_list": state.values.get("sp_list", []),
                        "verify_results": [],
                        "status": "",
                        "error": "",
                        "clarify_count": state.values.get("clarify_count", 0),
                        "design_phase": None,
                        "last_feedback_reply": "",
                    }
                    events = graph.astream(input_state, config, stream_mode=["updates", "custom"])
                else:
                    # 如果已有足够用户消息，强制设为 design 模式
                    if user_count >= 6:
                        mode = "design"
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
                            if node_output.get("error"):
                                # generate 等节点出错（如 LLM 响应解析失败）
                                generate_failed = True
                                assistant_response = (
                                    f"❌ 生成失败：{node_output['error'][:300]}\n\n"
                                    "存储过程未能生成，右侧仍显示上一次的结果。请重新确认设计方案后重试。"
                                )
                                yield f"data: {json.dumps({'type': 'error', 'content': assistant_response})}\n\n"

                            if node_output.get("status") == "generated":
                                sp_list = node_output.get("sp_list", [])
                                assistant_response = f"已生成 {len(sp_list)} 个存储过程。\n"
                                for sp in sp_list:
                                    assistant_response += f"- {sp['name']}\n"
                                assistant_response += "\n正在校验..."

                            elif node_output.get("status") in ("verified", "verify_failed"):
                                # generate 已失败时跳过 verify 结果
                                if generate_failed:
                                    yield f"data: {json.dumps({'node': node_name, 'data': node_output, 'type': 'update'})}\n\n"
                                    continue
                                v_results = node_output.get("verify_results", [])
                                print(f"[DEBUG verify_result] status={node_output.get('status')}, v_results count={len(v_results)}", flush=True)
                                for i, vr in enumerate(v_results):
                                    print(f"[DEBUG verify_result]   [{i}] sp_id={vr.get('sp_id','?')[:8]}, syntax_ok={vr.get('syntax_ok')}, biz_ok={vr.get('business_ok')}, details={len(vr.get('details',[]))}", flush=True)
                                lines = ["\n--- 校验结果 ---"]
                                if v_results:
                                    for vr in v_results:
                                        sp_name = vr.get("sp_name", vr.get("sp_id", "")[:8])
                                        syn = "✅" if vr.get("syntax_ok") else "❌"
                                        biz = "✅" if vr.get("business_ok") else "❌"
                                        lines.append(f"📄 {sp_name}")
                                        lines.append(f"   {syn} 语法  {biz} 业务")
                                        for d in vr.get("details", []):
                                            if d.get("type") == "syntax" and not d.get("pass"):
                                                lines.append(f"   语法错误: {d.get('error', '')[:120]}")
                                            elif d.get("type") == "business":
                                                mark = "✅" if d.get("pass") else "❌"
                                                detail = d.get('data', d.get('error', ''))
                                                detail_str = str(detail)[:120] if detail else ''
                                                lines.append(f"   {mark} {d.get('query', '')}: {detail_str}")
                                else:
                                    lines.append("⚠️ 校验结果为空，可能未生成存储过程")
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

                else:
                    break  # 未知中断类型

            if not assistant_response:
                assistant_response = "处理完成"

            if not assistant_saved:
                await asyncio.to_thread(save_message, req.session_id, "assistant", assistant_response)
            yield f"data: {json.dumps({'type': 'done', 'content': assistant_response})}\n\n"

        except Exception as e:
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
    return {"messages": get_messages(session_id)}
