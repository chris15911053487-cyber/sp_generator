"""存储过程管理 API — 列表、更新、删除、执行。"""
import json
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.sqlite import (
    delete_sp, get_sp, get_sps, get_verify_queries, update_sp,
)
from app.db.sqlserver import _serialize_value, get_connection
from app.services.validation import compute_bundle_hash

router = APIRouter(prefix="/api/sp", tags=["stored_procedures"])
MAX_EXECUTE_ROWS = 50000


class UpdateSpRequest(BaseModel):
    name: str | None = None
    code: str | None = None


class ExecuteSpRequest(BaseModel):
    params: dict = Field(default_factory=dict)
    confirm_write: bool = False


@router.get("/{session_id}")
def api_get_sps(session_id: str):
    return {"procedures": get_sps(session_id)}


@router.put("/{sp_id}")
def api_update_sp(sp_id: str, req: UpdateSpRequest):
    changes = {key: value for key, value in req.model_dump().items() if value is not None}
    if not changes:
        raise HTTPException(400, "没有可更新的字段")
    changes.update(
        status="draft", syntax_valid=0, business_valid=0,
        validated_hash=None, verify_result=None,
    )
    update_sp(sp_id, **changes)
    return {"ok": True}


@router.delete("/{sp_id}")
def api_delete_sp(sp_id: str):
    delete_sp(sp_id)
    return {"ok": True}


def _execution(sp: dict, params: dict) -> tuple[str, list]:
    name = sp["name"]
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", name):
        raise ValueError("非法存储过程名称")
    try:
        definitions = json.loads(sp.get("parameters") or "[]")
    except (TypeError, json.JSONDecodeError):
        definitions = []
    assignments = []
    values = []
    for definition in definitions:
        parameter = str(definition.get("name", "")).lstrip("@")
        if not parameter:
            continue
        if parameter in params:
            value = params[parameter]
        elif definition.get("default") not in (None, ""):
            value = definition["default"]
        else:
            continue
        assignments.append(f"@{parameter} = ?")
        values.append(value)
    safe_name = ".".join(f"[{part}]" for part in name.split("."))
    sql = f"EXEC {safe_name}"
    if assignments:
        sql += " " + ", ".join(assignments)
    return sql, values


@router.post("/execute/{sp_id}")
def api_execute_sp(sp_id: str, req: ExecuteSpRequest):
    """执行已经由本系统部署的版本；写入型执行必须明确确认。"""
    sp = get_sp(sp_id)
    if not sp:
        raise HTTPException(404, "存储过程不存在")
    current_hash = compute_bundle_hash(sp, get_verify_queries(sp_id))
    if (not sp.get("deployed_hash") or not sp.get("deployed_at")
            or sp["deployed_hash"] != current_hash):
        return {
            "ok": False,
            "error": f"存储过程 [{sp['name']}] 尚未由本系统部署，请先点击一键部署。",
            "not_deployed": True,
        }
    operation_type = sp.get("operation_type") or "query"
    if operation_type != "query" and not req.confirm_write:
        return {
            "ok": False,
            "error": f"该过程包含 {operation_type.upper()} 操作，必须确认后才能永久修改测试数据库。",
            "confirmation_required": True,
        }

    try:
        exec_sql, values = _execution(sp, req.params)
        conn = get_connection()
        cursor = conn.cursor()
        cursor.timeout = 60
        cursor.execute(exec_sql, values)
        while cursor.description is None and cursor.nextset():
            pass
        columns = [column[0] for column in cursor.description] if cursor.description else []
        fetched = cursor.fetchmany(MAX_EXECUTE_ROWS + 1) if columns else []
        if len(fetched) > MAX_EXECUTE_ROWS:
            conn.close()
            return {"ok": False, "error": f"结果超过 {MAX_EXECUTE_ROWS} 行限制"}
        rows = [
            {column: _serialize_value(value) for column, value in zip(columns, row)}
            for row in fetched
        ]
        conn.close()
        return {
            "ok": True, "columns": columns, "rows": rows,
            "operation_type": operation_type,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
