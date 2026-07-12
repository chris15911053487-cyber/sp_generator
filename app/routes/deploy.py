"""部署管理 API — 预检 + 一键部署。"""
import datetime
from fastapi import APIRouter
from app.db.sqlite import get_sps, update_sp
from app.db.sqlserver import check_syntax, deploy_procedure

router = APIRouter(prefix="/api/deploy", tags=["deploy"])


@router.post("/precheck/{session_id}")
def api_precheck(session_id: str):
    """部署前预检 — 对所有 SP 执行语法校验。"""
    sps = get_sps(session_id)
    if not sps:
        return {"ok": False, "message": "没有可部署的存储过程"}

    results = []
    all_pass = True
    for sp in sps:
        ok, err = check_syntax(sp["code"])
        update_sp(sp["id"], syntax_valid=1 if ok else 0)
        if not ok:
            all_pass = False
        results.append({"sp_id": sp["id"], "name": sp["name"], "syntax_ok": ok, "error": err})

    return {"ok": all_pass, "results": results}


@router.post("/{session_id}")
def api_deploy(session_id: str):
    """一键部署 — 预检通过后执行 CREATE PROCEDURE。"""
    sps = get_sps(session_id)
    if not sps:
        return {"ok": False, "message": "没有可部署的存储过程"}

    # 先预检
    all_syntax_ok = True
    for sp in sps:
        ok, err = check_syntax(sp["code"])
        if not ok:
            all_syntax_ok = False

    if not all_syntax_ok:
        return {"ok": False, "message": "预检未通过，请先解决语法错误后再部署"}

    # 逐个部署
    results = []
    for sp in sps:
        ok, err = deploy_procedure(sp["name"], sp["code"])
        if ok:
            update_sp(sp["id"], status="deployed", deployed_at=datetime.datetime.now().isoformat())
        results.append({"sp_id": sp["id"], "name": sp["name"], "success": ok, "error": err})

    all_ok = all(r["success"] for r in results)
    return {"ok": all_ok, "results": results}
