"""对话路由 — SSE 流式对话，驱动 Agent 状态图。"""
import json
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from app.db.sqlite import save_message, get_messages
from app.agent.graph import create_graph

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    session_id: str
    message: str
    action: str = "send"


@router.post("/stream")
async def api_chat_stream(req: ChatRequest):
    """SSE 流式对话端点。"""
    save_message(req.session_id, "user", req.message)

    async def event_stream():
        graph = create_graph()
        thread_id = req.session_id
        config = {"configurable": {"thread_id": thread_id}}

        try:
            state = graph.get_state(config)
            if state and state.values:
                current_mode = state.values.get("mode", "clarify")
            else:
                current_mode = "clarify"

            input_state = {
                "session_id": req.session_id,
                "user_input": req.message,
                "mode": current_mode,
                "requirements": state.values.get("requirements", "") if state and state.values else "",
                "sp_list": state.values.get("sp_list", []) if state and state.values else [],
            }

            # 如果有 interrupt，resume
            if state and state.interrupts:
                graph.update_state(config, {"user_input": req.message})
                events = graph.stream(None, config)
            else:
                events = graph.stream(input_state, config)

            assistant_response = ""

            for event in events:
                for node_name, node_output in event.items():
                    if isinstance(node_output, dict):
                        if node_output.get("status") == "generated":
                            sp_list = node_output.get("sp_list", [])
                            assistant_response = f"已生成 {len(sp_list)} 个存储过程，校验完毕。请查看右侧面板。\n"
                            for sp in sp_list:
                                assistant_response += f"- {sp['name']}\n"

                        yield f"data: {json.dumps({'node': node_name, 'data': node_output, 'type': 'update'})}\n\n"

            if not assistant_response:
                assistant_response = "处理完成"

            save_message(req.session_id, "assistant", assistant_response)

            yield f"data: {json.dumps({'type': 'done', 'content': assistant_response})}\n\n"

        except Exception as e:
            error_msg = f"处理出错: {str(e)}"
            save_message(req.session_id, "assistant", error_msg)
            yield f"data: {json.dumps({'type': 'error', 'content': error_msg})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/messages/{session_id}")
def api_get_messages(session_id: str):
    return {"messages": get_messages(session_id)}
