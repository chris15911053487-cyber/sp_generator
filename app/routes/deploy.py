"""V3 部署入口：只认可冻结制品链和 ValidationEvidence。"""

import datetime
import json

from fastapi import APIRouter

from app.contracts.reference import ReferenceBundle
from app.contracts.schema import SchemaBinding
from app.contracts.semantic import SemanticContract
from app.contracts.validation import ProcedureCandidateV3, ValidationEvidence
from app.db.sqlite import get_sps, get_v3_deployment_chain, update_sp
from app.db.sqlserver import (
    _deployable_code,
    compile_candidate,
    deploy_procedures_atomically,
)
from app.services.catalog_v3 import capture_catalog_snapshot
from app.services.schema_binding_v3 import validate_binding_against_catalog
from app.services.sql_renderer_v3 import SqlRendererV3
from config import get_db_config, is_explicit_test_database


router = APIRouter(prefix="/api/deploy", tags=["deploy"])


def _readiness(session_id: str) -> tuple[bool, list[dict], list[dict]]:
    procedures = get_sps(session_id)
    results = []
    all_ready = bool(procedures)
    test_database = is_explicit_test_database(get_db_config())

    for sp in procedures:
        reasons = []
        if not test_database:
            reasons.append("部署只允许在已明确配置的测试数据库执行")
        chain = (
            get_v3_deployment_chain(session_id, sp.get("validated_hash"))
            if sp.get("validated_hash") else None
        )
        if chain is None:
            reasons.append("缺少完整 V3 冻结制品链或 validated 证据")
        else:
            try:
                semantic = SemanticContract.model_validate(
                    chain["semantic_contract"]
                )
                binding = SchemaBinding.model_validate(chain["schema_binding"])
                reference = ReferenceBundle.model_validate(
                    chain["reference_bundle"]
                )
                candidate = ProcedureCandidateV3.model_validate(
                    chain["procedure_candidate"]
                )
                evidence = ValidationEvidence.model_validate_json(
                    json.dumps(
                        chain["validation_evidence"], ensure_ascii=False
                    )
                )
                catalog = capture_catalog_snapshot()
                validate_binding_against_catalog(semantic, catalog, binding)
                rendered = SqlRendererV3(
                    semantic, binding
                ).render_procedure(candidate.procedure_plan)
                if rendered != candidate.procedure_sql or rendered != sp["code"]:
                    reasons.append("待部署 SQL 与冻结 ProcedurePlan 不一致")
                if candidate.content_hash != sp.get("validated_hash"):
                    reasons.append("候选 hash 与 validated 版本不一致")
                if (
                    evidence.status != "validated"
                    or evidence.candidate_hash != candidate.content_hash
                    or evidence.reference_bundle_hash != reference.content_hash
                    or not evidence.coverage
                    or not evidence.coverage.effective
                ):
                    reasons.append("ValidationEvidence 未证明有效覆盖和完整通过")
                parameters = json.loads(sp.get("parameters") or "[]")
                compiled = compile_candidate(
                    "procedure", sp["name"], sp["code"], parameters
                )
                if not compiled.get("ok"):
                    reasons.append(
                        str(compiled.get("error") or "SQL 编译检查失败")
                    )
                _deployable_code(sp["name"], sp["code"])
            except Exception as exc:
                reasons.append(f"V3 部署证据检查失败: {exc}")

        ready = not reasons
        all_ready = all_ready and ready
        results.append(
            {
                "sp_id": sp["id"],
                "name": sp["name"],
                "ready": ready,
                "reasons": reasons,
                "error": "；".join(reasons),
                "revalidation_required": not ready,
            }
        )
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
                sp["id"],
                status="deployed",
                deployed_at=deployed_at,
                deployed_hash=sp["validated_hash"],
            )
    return {"ok": all_ok, "results": results}
