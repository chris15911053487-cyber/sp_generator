"""存储过程管理 API — 列表、更新、删除。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db.sqlite import get_sps, update_sp, delete_sp

router = APIRouter(prefix="/api/sp", tags=["stored_procedures"])


class UpdateSpRequest(BaseModel):
    name: str | None = None
    code: str | None = None


@router.get("/{session_id}")
def api_get_sps(session_id: str):
    return {"procedures": get_sps(session_id)}


@router.put("/{sp_id}")
def api_update_sp(sp_id: str, req: UpdateSpRequest):
    kwargs = {}
    if req.name is not None:
        kwargs["name"] = req.name
    if req.code is not None:
        kwargs["code"] = req.code
    if not kwargs:
        raise HTTPException(400, "没有可更新的字段")
    update_sp(sp_id, **kwargs)
    return {"ok": True}


@router.delete("/{sp_id}")
def api_delete_sp(sp_id: str):
    delete_sp(sp_id)
    return {"ok": True}
