"""存储过程管理 API — 列表、更新、删除、执行。"""
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db.sqlite import get_sps, update_sp, delete_sp
from app.db.sqlserver import get_connection, _serialize_value

router = APIRouter(prefix="/api/sp", tags=["stored_procedures"])


class UpdateSpRequest(BaseModel):
    name: str | None = None
    code: str | None = None


class ExecuteSpRequest(BaseModel):
    params: dict = {}


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


@router.post("/execute/{sp_id}")
def api_execute_sp(sp_id: str, req: ExecuteSpRequest):
    """执行存储过程并返回结果集。"""
    from app.db.sqlite import _get_conn as get_sqlite_conn

    # 从 SQLite 获取 SP 信息
    conn_lite = get_sqlite_conn()
    row = conn_lite.execute(
        "SELECT name, parameters, status, deployed_at FROM stored_procedures WHERE id = ?", (sp_id,)
    ).fetchone()
    conn_lite.close()

    if not row:
        raise HTTPException(404, "存储过程不存在")

    # 检查是否已部署
    if row["status"] != "deployed" and not row["deployed_at"]:
        return {
            "ok": False,
            "error": f"存储过程 [{row['name']}] 尚未部署到 SQL Server，请先点击「🚀 一键部署」后再执行。",
            "not_deployed": True,
        }

    sp_name = row["name"]
    param_defs = []
    try:
        param_defs = json.loads(row["parameters"] or "[]")
    except (json.JSONDecodeError, TypeError):
        pass

    # 构建 EXEC 语句
    param_parts = []
    for p in param_defs:
        pname = p.get("name", "")
        if not pname.startswith("@"):
            pname = "@" + pname
        # 从请求参数中获取值
        key = pname.lstrip("@")
        if key in req.params:
            val = req.params[key]
            if val is None or val == "":
                param_parts.append(f"{pname} = NULL")
            elif isinstance(val, (int, float)):
                param_parts.append(f"{pname} = {val}")
            else:
                escaped = str(val).replace("'", "''")
                param_parts.append(f"{pname} = '{escaped}'")
        elif p.get("default"):
            # 使用默认值
            pass
        else:
            param_parts.append(f"{pname} = NULL")

    exec_sql = f"EXEC [{sp_name}]"
    if param_parts:
        exec_sql += " " + ", ".join(param_parts)

    # 执行并获取结果
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(exec_sql)
        columns = [col[0] for col in cursor.description] if cursor.description else []
        rows = []
        if columns:
            for r in cursor.fetchall():
                rows.append({col: _serialize_value(val) for col, val in zip(columns, r)})
        conn.close()
        return {"ok": True, "columns": columns, "rows": rows, "sql": exec_sql}
    except Exception as e:
        return {"ok": False, "error": str(e), "sql": exec_sql}
