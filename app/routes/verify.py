"""校验管理 API — 语法校验、业务校验。"""
from fastapi import APIRouter
from app.db.sqlite import get_sps, get_verify_queries, update_sp, update_verify_query
from app.db.sqlserver import check_syntax, execute_query

router = APIRouter(prefix="/api/verify", tags=["verify"])


@router.post("/syntax/{session_id}/{sp_id}")
def api_check_syntax(session_id: str, sp_id: str):
    """对单个 SP 执行语法校验。"""
    sps = get_sps(session_id)
    target = next((s for s in sps if s["id"] == sp_id), None)
    if not target:
        return {"ok": False, "message": "SP 不存在"}
    ok, err = check_syntax(target["code"])
    update_sp(sp_id, syntax_valid=1 if ok else 0)
    return {"ok": ok, "error": err}


@router.post("/business/{sp_id}")
def api_check_business(sp_id: str):
    """对单个 SP 执行所有关联校验 SQL 的业务校验。"""
    vqs = get_verify_queries(sp_id)
    results = []
    for vq in vqs:
        try:
            rows = execute_query(vq["sql_code"])
            update_verify_query(vq["id"], status="pass", result_detail=str(rows[:20]))
            results.append({"query_id": vq["id"], "name": vq["name"], "pass": True, "data": rows[:10]})
        except Exception as e:
            update_verify_query(vq["id"], status="fail", result_detail=str(e))
            results.append({"query_id": vq["id"], "name": vq["name"], "pass": False, "error": str(e)})
    return {"results": results}


@router.get("/{session_id}/sp/{sp_id}")
def api_get_verify_for_sp(session_id: str, sp_id: str):
    """获取指定 SP 的校验查询列表。"""
    return {"verify_queries": get_verify_queries(sp_id)}


@router.post("/all/{session_id}")
def api_verify_all(session_id: str):
    """对会话下所有 SP 执行完整校验。"""
    sps = get_sps(session_id)
    all_results = []
    for sp in sps:
        syntax_ok, syntax_err = check_syntax(sp["code"])
        update_sp(sp["id"], syntax_valid=1 if syntax_ok else 0)
        sp_result = {"sp_id": sp["id"], "name": sp["name"], "syntax_ok": syntax_ok, "syntax_err": syntax_err}

        vqs = get_verify_queries(sp["id"])
        biz_results = []
        for vq in vqs:
            try:
                rows = execute_query(vq["sql_code"])
                update_verify_query(vq["id"], status="pass", result_detail=str(rows[:20]))
                biz_results.append({"query_id": vq["id"], "name": vq["name"], "pass": True})
            except Exception as e:
                update_verify_query(vq["id"], status="fail", result_detail=str(e))
                biz_results.append({"query_id": vq["id"], "name": vq["name"], "pass": False, "error": str(e)})
        sp_result["business"] = biz_results
        all_results.append(sp_result)

    return {"results": all_results}
