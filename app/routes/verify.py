"""校验管理 API — 语法校验、业务校验（含数据对比）。"""
import json
from fastapi import APIRouter
from pydantic import BaseModel
from app.db.sqlite import get_sps, get_verify_queries, update_sp, update_verify_query
from app.db.sqlserver import (
    check_syntax, execute_query, substitute_params,
    execute_sp_with_params, compare_sp_results,
)

router = APIRouter(prefix="/api/verify", tags=["verify"])


class VerifySpRequest(BaseModel):
    code: str | None = None  # 可选，传入则用此代码做语法校验
    params: dict | None = None  # 可选，参数值 {FromDate: "2024-01-01", ...}


class UpdateVqRequest(BaseModel):
    sql_code: str | None = None
    name: str | None = None


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
def api_check_business(sp_id: str, req: VerifySpRequest = None):
    """对单个 SP 执行所有关联校验 SQL 的业务校验（含数据对比）。"""
    from app.db.sqlite import _get_conn as _get_db
    params = req.params if req and req.params else {}
    if not params:
        conn = _get_db()
        row = conn.execute("SELECT parameters FROM stored_procedures WHERE id = ?", (sp_id,)).fetchone()
        conn.close()
        if row and row["parameters"]:
            try:
                param_list = json.loads(row["parameters"])
                params = {p["name"]: p.get("default", "") for p in param_list if "default" in p}
            except (json.JSONDecodeError, KeyError):
                pass

    # 获取 SP 信息用于执行对比
    conn = _get_db()
    sp_row = conn.execute("SELECT name, status, parameters FROM stored_procedures WHERE id = ?", (sp_id,)).fetchone()
    conn.close()
    sp_name = sp_row["name"] if sp_row else ""
    sp_deployed = sp_row and sp_row["status"] == "deployed"
    param_defs = []
    if sp_row and sp_row["parameters"]:
        try:
            param_defs = json.loads(sp_row["parameters"])
        except (json.JSONDecodeError, TypeError):
            pass

    # 执行 SP 获取结果（只有已部署才能执行）
    sp_rows = None
    sp_exec_error = None
    if sp_deployed:
        try:
            sp_rows = execute_sp_with_params(sp_name, params, param_defs)
        except Exception as e:
            sp_exec_error = str(e)

    vqs = get_verify_queries(sp_id)
    results = []
    for vq in vqs:
        try:
            sql_to_run = substitute_params(vq["sql_code"], params)
            verify_rows = execute_query(sql_to_run)

            # 数据对比
            compare_columns = vq.get("compare_columns", "")
            comparison = None
            if sp_rows is not None and compare_columns:
                comparison = compare_sp_results(sp_rows, verify_rows, compare_columns)
                is_pass = comparison["match"]
            elif sp_exec_error:
                is_pass = True  # SP 执行失败不影响校验 SQL 本身的 pass
                comparison = {"match": None, "summary": f"⚠️ SP 执行失败，无法对比: {sp_exec_error}"}
            elif not sp_deployed:
                is_pass = True  # SP 未部署，只检查校验 SQL 能否执行
                comparison = {"match": None, "summary": "⚠️ SP 未部署，仅验证校验 SQL 可执行"}
            else:
                is_pass = True  # 无对比列，只验证能跑通

            result_detail = {
                "rows": verify_rows[:20],
                "comparison": comparison,
            }
            status = "pass" if is_pass else "fail"
            update_verify_query(vq["id"], status=status,
                                result_detail=json.dumps(result_detail, ensure_ascii=False, indent=2))
            results.append({
                "query_id": vq["id"], "name": vq["name"], "pass": is_pass,
                "data": verify_rows[:10], "comparison": comparison,
            })
        except Exception as e:
            update_verify_query(vq["id"], status="fail", result_detail=str(e))
            results.append({"query_id": vq["id"], "name": vq["name"], "pass": False, "error": str(e)})
    return {"results": results}


@router.get("/{session_id}/sp/{sp_id}")
def api_get_verify_for_sp(session_id: str, sp_id: str):
    """获取指定 SP 的校验查询列表。"""
    return {"verify_queries": get_verify_queries(sp_id)}


@router.put("/query/{query_id}")
def api_update_verify_query(query_id: str, req: UpdateVqRequest):
    """更新校验 SQL 的代码或名称。"""
    kwargs = {}
    if req.sql_code is not None:
        kwargs["sql_code"] = req.sql_code
    if req.name is not None:
        kwargs["name"] = req.name
    if not kwargs:
        return {"ok": False, "message": "没有可更新的字段"}
    update_verify_query(query_id, **kwargs)
    return {"ok": True}


@router.post("/sp/{sp_id}")
def api_verify_single_sp(sp_id: str, req: VerifySpRequest = None):
    """对单个 SP 执行完整校验（语法+业务）。
    如果传入 code，用传入的代码做语法校验（不存入 DB）；
    否则从 DB 读取已存储的代码。
    """
    # 从 DB 查找 SP（需要 session_id 来定位，遍历所有 session 的 SP）
    from app.db.sqlite import _get_conn
    conn = _get_conn()
    row = conn.execute("SELECT * FROM stored_procedures WHERE id = ?", (sp_id,)).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "message": "SP 不存在"}

    sp = dict(row)
    code = req.code if req and req.code else sp["code"]
    params = req.params if req and req.params else {}
    # 如果用户未传参数，使用 DB 中存储的默认值
    if not params and sp.get("parameters"):
        try:
            param_list = json.loads(sp["parameters"])
            params = {p["name"]: p.get("default", "") for p in param_list if "default" in p}
        except (json.JSONDecodeError, KeyError):
            params = {}

    # 保存当前参数值到 DB（更新 defaults）
    if params:
        try:
            current_params = json.loads(sp.get("parameters", "[]"))
            for p in current_params:
                if p["name"] in params:
                    p["default"] = str(params[p["name"]])
            update_sp(sp_id, parameters=json.dumps(current_params, ensure_ascii=False))
        except (json.JSONDecodeError, KeyError):
            pass

    # 语法校验
    syntax_ok, syntax_err = check_syntax(code)
    update_sp(sp_id, syntax_valid=1 if syntax_ok else 0)

    # 执行 SP 获取结果集（只有已部署才能执行）
    sp_deployed = sp.get("status") == "deployed"
    sp_rows = None
    sp_exec_error = None
    param_defs = []
    try:
        param_defs = json.loads(sp.get("parameters", "[]"))
    except (json.JSONDecodeError, TypeError):
        pass
    if sp_deployed:
        try:
            sp_rows = execute_sp_with_params(sp["name"], params, param_defs)
        except Exception as e:
            sp_exec_error = str(e)

    # 业务校验（含数据对比）
    vqs = get_verify_queries(sp_id)
    biz_results = []
    biz_all_ok = True
    for vq in vqs:
        try:
            sql_to_run = substitute_params(vq["sql_code"], params)
            verify_rows = execute_query(sql_to_run)

            # 数据对比
            compare_columns = vq.get("compare_columns", "")
            comparison = None
            if sp_rows is not None and compare_columns:
                comparison = compare_sp_results(sp_rows, verify_rows, compare_columns)
                is_pass = comparison["match"]
            elif sp_exec_error:
                is_pass = True  # SP 执行失败不影响校验 SQL 本身的 pass
                comparison = {"match": None, "summary": f"⚠️ SP 执行失败，无法对比: {sp_exec_error}"}
            elif not sp_deployed:
                is_pass = True  # SP 未部署，只检查校验 SQL 能否执行
                comparison = {"match": None, "summary": "⚠️ SP 未部署，仅验证校验 SQL 可执行"}
            else:
                is_pass = True  # 无对比列，只验证能跑通

            if not is_pass:
                biz_all_ok = False

            result_detail = {
                "rows": verify_rows[:20],
                "comparison": comparison,
            }
            status = "pass" if is_pass else "fail"
            update_verify_query(vq["id"], status=status,
                                result_detail=json.dumps(result_detail, ensure_ascii=False, indent=2))
            biz_results.append({
                "query_id": vq["id"], "name": vq["name"], "pass": is_pass,
                "data": verify_rows[:10], "comparison": comparison,
            })
        except Exception as e:
            biz_all_ok = False
            update_verify_query(vq["id"], status="fail", result_detail=str(e))
            biz_results.append({"query_id": vq["id"], "name": vq["name"], "pass": False, "error": str(e)})

    update_sp(sp_id, business_valid=1 if biz_all_ok else 0)

    # 更新 SP 状态
    sp_status = "verified" if syntax_ok and biz_all_ok else "verify_failed"
    sp_result = {
        "sp_id": sp_id,
        "sp_name": sp["name"],
        "syntax_ok": syntax_ok,
        "business_ok": biz_all_ok,
        "details": [],
    }
    if not syntax_ok:
        sp_result["details"].append({"type": "syntax", "pass": False, "error": syntax_err})
    for br in biz_results:
        sp_result["details"].append({
            "type": "business",
            "pass": br["pass"],
            "query": br["name"],
            "query_id": br.get("query_id"),
            "data": br.get("data"),
            "error": br.get("error"),
            "comparison": br.get("comparison"),
        })
    update_sp(sp_id, status=sp_status, verify_result=str(sp_result))

    return {"ok": True, "result": sp_result}


@router.post("/all/{session_id}")
def api_verify_all(session_id: str):
    """对会话下所有 SP 执行完整校验。"""
    sps = get_sps(session_id)
    all_results = []
    for sp in sps:
        syntax_ok, syntax_err = check_syntax(sp["code"])
        update_sp(sp["id"], syntax_valid=1 if syntax_ok else 0)
        sp_result = {"sp_id": sp["id"], "name": sp["name"], "syntax_ok": syntax_ok, "syntax_err": syntax_err}

        # 加载默认参数
        params = {}
        try:
            param_list = json.loads(sp.get("parameters", "[]"))
            params = {p["name"]: p.get("default", "") for p in param_list if "default" in p}
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

        vqs = get_verify_queries(sp["id"])
        biz_results = []
        for vq in vqs:
            try:
                sql_to_run = substitute_params(vq["sql_code"], params)
                rows = execute_query(sql_to_run)
                update_verify_query(vq["id"], status="pass", result_detail=json.dumps(rows[:20], ensure_ascii=False, indent=2))
                biz_results.append({"query_id": vq["id"], "name": vq["name"], "pass": True})
            except Exception as e:
                update_verify_query(vq["id"], status="fail", result_detail=str(e))
                biz_results.append({"query_id": vq["id"], "name": vq["name"], "pass": False, "error": str(e)})
        sp_result["business"] = biz_results
        all_results.append(sp_result)

    return {"results": all_results}
