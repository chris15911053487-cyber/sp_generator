"""部署管理 API：资格检查与唯一持久化部署入口。"""
import datetime

from fastapi import APIRouter

from app.db.sqlite import get_sps, get_verify_queries, update_sp
from app.db.sqlserver import _deployable_code, check_syntax, deploy_procedures_atomically
from app.services.validation import compute_bundle_hash, validate_reporting_procedure

router = APIRouter(prefix="/api/deploy", tags=["deploy"])


def _readiness(session_id: str) -> tuple[bool, list[dict], list[dict]]:
    procedures = get_sps(session_id)
    results = []
    all_ready = bool(procedures)
    for sp in procedures:
        queries = get_verify_queries(sp["id"])
        reasons = []
        syntax_ok, syntax_error = check_syntax(sp["code"])
        current_hash = compute_bundle_hash(sp, queries)
        if not syntax_ok:
            reasons.append(syntax_error or "语法检查失败")
        if not sp.get("business_valid"):
            reasons.append("业务校验未通过")
        if not sp.get("validated_hash") or sp.get("validated_hash") != current_hash:
            reasons.append("当前内容与最近校验通过的版本不一致")
        try:
            validate_reporting_procedure(
                sp["code"], sp.get("operation_type") or "query", queries
            )
            _deployable_code(sp["name"], sp["code"])
        except Exception as exc:
            reasons.append(str(exc))
        ready = not reasons
        all_ready = all_ready and ready
        results.append({
            "sp_id": sp["id"], "name": sp["name"], "ready": ready,
            "syntax_ok": syntax_ok, "reasons": reasons,
            "error": "；".join(reasons),
        })
    return all_ready, results, procedures


@router.post("/precheck/{session_id}")
def api_precheck(session_id: str):
    ready, results, procedures = _readiness(session_id)
    if not procedures:
        return {"ok": False, "message": "没有可部署的存储过程", "results": []}
    return {"ok": ready, "results": results}


@router.post("/{session_id}")
def api_deploy(session_id: str):
    ready, checks, procedures = _readiness(session_id)
    if not procedures:
        return {"ok": False, "message": "没有可部署的存储过程", "results": []}
    if not ready:
        return {"ok": False, "message": "部署检查未通过", "results": checks}

    results = deploy_procedures_atomically(procedures)
    all_ok = bool(results) and all(item["success"] for item in results)
    if all_ok:
        deployed_at = datetime.datetime.now().isoformat()
        for sp in procedures:
            update_sp(
                sp["id"], status="deployed", deployed_at=deployed_at,
                deployed_hash=sp["validated_hash"],
            )
    return {"ok": all_ok, "results": results}
