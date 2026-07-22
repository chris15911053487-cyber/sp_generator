"""部署管理 API：资格检查与唯一持久化部署入口。"""
import datetime
import json

from fastapi import APIRouter

from app.db.sqlite import get_sps, get_verify_queries, update_sp
from app.db.sqlserver import _deployable_code, check_syntax, deploy_procedures_atomically
from app.services.generation_harness import QuerySpec
from app.services.schema_evidence import capture_schema_evidence
from app.services.validation import compute_bundle_hash, validate_reporting_procedure
from config import get_db_config, is_explicit_test_database

router = APIRouter(prefix="/api/deploy", tags=["deploy"])


def _readiness(session_id: str) -> tuple[bool, list[dict], list[dict]]:
    procedures = get_sps(session_id)
    test_database_confirmed = is_explicit_test_database(get_db_config())
    results = []
    all_ready = bool(procedures)
    evidence_cache = {}
    for sp in procedures:
        queries = get_verify_queries(sp["id"])
        reasons = []
        revalidation_required = False
        if not test_database_confirmed:
            reasons.append("部署只允许在已明确配置的测试数据库执行")
        syntax_ok, syntax_error = check_syntax(sp["code"])
        current_hash = compute_bundle_hash(sp, queries)
        if not syntax_ok:
            reasons.append(syntax_error or "语法检查失败")
        if not sp.get("business_valid"):
            reasons.append("业务校验未通过")
        if not sp.get("validated_hash") or sp.get("validated_hash") != current_hash:
            reasons.append("当前内容与最近校验通过的版本不一致")
        if not sp.get("bundle_hash") or sp.get("bundle_hash") != current_hash:
            reasons.append("当前 bundle 与已审计哈希不一致")
        query_spec_json = sp.get("query_spec_json")
        schema_fingerprint = sp.get("schema_fingerprint")
        if not query_spec_json or not schema_fingerprint:
            reasons.append("缺少 QuerySpec 或 Schema 指纹，必须重新生成并校验")
            revalidation_required = True
        else:
            try:
                cache_key = json.dumps(
                    json.loads(query_spec_json),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if cache_key not in evidence_cache:
                    evidence_cache[cache_key] = capture_schema_evidence(
                        QuerySpec.model_validate_json(cache_key),
                    )
                current_evidence = evidence_cache[cache_key]
                if current_evidence.unresolved:
                    reasons.append("目标 Schema 已无法绑定 QuerySpec 中的全部标识符")
                    revalidation_required = True
                elif current_evidence.fingerprint != schema_fingerprint:
                    reasons.append("目标 Schema 已变化，必须重新校验")
                    revalidation_required = True
            except Exception as exc:
                reasons.append(f"无法刷新目标 Schema 指纹: {exc}")
                revalidation_required = True
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
            "revalidation_required": revalidation_required,
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
