"""存储过程管理 API — 列表、更新、删除、执行。"""
import json
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.sqlite import (
    delete_sp, get_sp, get_sps, get_v3_deployment_chain,
    update_sp,
)
from app.db.sqlserver import _serialize_value, get_connection
from config import get_db_config, is_explicit_test_database

router = APIRouter(prefix="/api/sp", tags=["stored_procedures"])
MAX_EXECUTE_ROWS = 50000


class UpdateSpRequest(BaseModel):
    name: str | None = None
    code: str | None = None


class ExecuteSpRequest(BaseModel):
    params: dict = Field(default_factory=dict)
    confirm_write: bool = False


def _is_v3_sp(sp: dict) -> bool:
    try:
        return json.loads(sp.get("verification_plan_json") or "{}").get(
            "version"
        ) == 3
    except (TypeError, json.JSONDecodeError):
        return False


@router.get("/{session_id}")
def api_get_sps(session_id: str):
    return {"procedures": get_sps(session_id)}


@router.put("/{sp_id}")
def api_update_sp(sp_id: str, req: UpdateSpRequest):
    stored = get_sp(sp_id)
    if not stored:
        raise HTTPException(404, "存储过程不存在")
    raise HTTPException(
        409,
        "V3 SQL 由冻结关系计划确定性生成；修改业务逻辑请返回方案重新生成。",
    )


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
    if _is_v3_sp(sp):
        chain = (
            get_v3_deployment_chain(
                sp["session_id"],
                sp.get("deployed_hash"),
            )
            if sp.get("deployed_hash") else None
        )
        deployed = bool(
            chain
            and sp.get("deployed_at")
            and chain["procedure_candidate"].get("procedure_sql") == sp["code"]
        )
    else:
        return {
            "ok": False,
            "error": "旧协议记录不可执行，请从 SemanticDesign V3 重新生成。",
        }
    if not deployed:
        return {
            "ok": False,
            "error": f"存储过程 [{sp['name']}] 尚未由本系统部署，请先点击一键部署。",
            "not_deployed": True,
        }
    operation_type = sp.get("operation_type") or "query"
    if operation_type != "query" and not is_explicit_test_database(get_db_config()):
        return {
            "ok": False,
            "error": "写入型存储过程只允许在已明确配置的测试数据库执行。",
            "environment_required": True,
        }
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
