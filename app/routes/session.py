"""会话管理 API — 新建、列表、删除。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db.sqlite import create_session, get_sessions, delete_session, get_messages

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    name: str = "新会话"


@router.post("")
def api_create_session(req: CreateSessionRequest):
    session = create_session(req.name)
    return {"ok": True, "session": session}


@router.get("")
def api_get_sessions():
    return {"sessions": get_sessions()}


@router.delete("/{session_id}")
def api_delete_session(session_id: str):
    delete_session(session_id)
    return {"ok": True}


@router.get("/{session_id}/messages")
def api_get_messages(session_id: str):
    return {"messages": get_messages(session_id)}
