"""V3 Schema-first 制品与验证 API。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.contracts.reference import ComparatorSpec, ReferenceBundle, ValidationCase
from app.contracts.relational_plan import RelationalPlan
from app.contracts.schema import SchemaBinding, SchemaBindingProposal
from app.contracts.semantic import SemanticContract
from app.contracts.validation import ProcedureCandidateV3
from app.db.sqlite import (
    get_sp,
    get_v3_deployment_chain,
    get_latest_v3_validation_run,
    save_v3_artifacts,
    save_v3_validation_run,
    update_sp,
)
from app.services.catalog_v3 import capture_catalog_snapshot
from app.services.procedure_generator_v3 import generate_procedure_candidate
from app.services.reference_planner import (
    ReferenceFactDraft,
    freeze_reference_bundle,
)
from app.services.schema_binding_v3 import (
    build_schema_binding,
    validate_binding_against_catalog,
)
from app.services.validation_runner_v3 import (
    SqlServerValidationExecutor,
    validate_candidate_v3,
)


router = APIRouter(prefix="/api/v3", tags=["v3"])


class BindRequest(BaseModel):
    semantic_contract: SemanticContract
    proposal: SchemaBindingProposal


class ReferenceFactDraftPayload(BaseModel):
    fact_id: str
    meaning: str
    actual_projection: list[str]
    plan: RelationalPlan
    comparator: ComparatorSpec


class FreezeReferenceRequest(BaseModel):
    semantic_contract: SemanticContract
    schema_binding: SchemaBinding
    facts: list[ReferenceFactDraftPayload]
    validation_cases: list[ValidationCase]


class BuildProcedureRequest(BaseModel):
    semantic_contract: SemanticContract
    schema_binding: SchemaBinding
    reference_bundle: ReferenceBundle
    procedure_plan: RelationalPlan


class ValidateV3Request(BaseModel):
    semantic_contract: SemanticContract
    schema_binding: SchemaBinding
    reference_bundle: ReferenceBundle
    procedure_candidate: ProcedureCandidateV3
    case_id: str | None = None


def _error(exc: Exception) -> dict:
    return {
        "ok": False,
        "error": {
            "code": getattr(exc, "code", exc.__class__.__name__),
            "title": "V3 流水线未通过",
            "summary": str(exc),
            "evidence": getattr(exc, "evidence", {}),
            "technical_detail": getattr(exc, "detail", ""),
        },
    }


@router.post("/schema/bind")
def bind_schema(request: BindRequest):
    try:
        catalog = capture_catalog_snapshot()
        binding = build_schema_binding(
            request.semantic_contract,
            catalog,
            request.proposal,
        )
        return {
            "ok": True,
            "catalog_snapshot": catalog.model_dump(mode="json", by_alias=True),
            "schema_binding": binding.model_dump(mode="json", by_alias=True),
        }
    except Exception as exc:
        return _error(exc)


@router.post("/reference/freeze")
def freeze_reference(request: FreezeReferenceRequest):
    try:
        catalog = capture_catalog_snapshot()
        validate_binding_against_catalog(
            request.semantic_contract,
            catalog,
            request.schema_binding,
        )
        executor = SqlServerValidationExecutor()
        bundle = freeze_reference_bundle(
            request.semantic_contract,
            request.schema_binding,
            [
                ReferenceFactDraft(
                    fact_id=item.fact_id,
                    meaning=item.meaning,
                    actual_projection=item.actual_projection,
                    plan=item.plan,
                    comparator=item.comparator,
                )
                for item in request.facts
            ],
            request.validation_cases,
            preflight_executor=lambda sql, parameters: executor.preflight_reference(
                request.semantic_contract,
                sql,
                parameters,
            ),
        )
        return {
            "ok": True,
            "reference_bundle": bundle.model_dump(mode="json", by_alias=True),
        }
    except Exception as exc:
        return _error(exc)


@router.post("/procedure/build")
def build_procedure(request: BuildProcedureRequest):
    try:
        catalog = capture_catalog_snapshot()
        validate_binding_against_catalog(
            request.semantic_contract,
            catalog,
            request.schema_binding,
        )
        candidate = generate_procedure_candidate(
            request.semantic_contract,
            request.schema_binding,
            request.reference_bundle,
            lambda _contract, _binding: request.procedure_plan,
        )
        return {
            "ok": True,
            "procedure_candidate": candidate.model_dump(
                mode="json",
                by_alias=True,
            ),
        }
    except Exception as exc:
        return _error(exc)


@router.post("/validate/{session_id}")
def validate_v3(session_id: str, request: ValidateV3Request):
    try:
        catalog = capture_catalog_snapshot()
        artifact_ids = save_v3_artifacts(
            session_id,
            semantic_contract=request.semantic_contract,
            catalog_snapshot=catalog,
            schema_binding=request.schema_binding,
            reference_bundle=request.reference_bundle,
            procedure_candidate=request.procedure_candidate,
        )
        evidence = validate_candidate_v3(
            request.semantic_contract,
            catalog,
            request.schema_binding,
            request.reference_bundle,
            request.procedure_candidate,
            executor=SqlServerValidationExecutor(),
            case_id=request.case_id,
        )
        run = save_v3_validation_run(session_id, evidence)
        return {
            "ok": evidence.status == "validated",
            "status": evidence.status,
            "artifact_ids": artifact_ids,
            "run_id": run["id"],
            "evidence": evidence.model_dump(mode="json"),
        }
    except Exception as exc:
        return _error(exc)


@router.get("/validation/{session_id}/latest")
def latest_validation_v3(session_id: str):
    result = get_latest_v3_validation_run(session_id)
    if result is None:
        return {"ok": False, "message": "该会话还没有 V3 验证记录"}
    return {"ok": True, "evidence": result}


@router.post("/revalidate/sp/{sp_id}")
def revalidate_stored_v3(sp_id: str):
    """从冻结制品链重新校验，禁止接收前端提交的 SQL 或旧校验规则。"""
    try:
        stored = get_sp(sp_id)
        if not stored:
            return {"ok": False, "message": "SP 不存在"}
        chain = get_v3_deployment_chain(
            stored["session_id"],
            stored.get("bundle_hash") or stored.get("validated_hash"),
        )
        if chain is None:
            return {"ok": False, "message": "V3 冻结制品链不存在，请重新生成"}
        contract = SemanticContract.model_validate(chain["semantic_contract"])
        binding = SchemaBinding.model_validate(chain["schema_binding"])
        reference = ReferenceBundle.model_validate(chain["reference_bundle"])
        candidate = ProcedureCandidateV3.model_validate(
            chain["procedure_candidate"]
        )
        catalog = capture_catalog_snapshot()
        executor = SqlServerValidationExecutor()
        case_evidence = [
            validate_candidate_v3(
                contract,
                catalog,
                binding,
                reference,
                candidate,
                executor=executor,
                case_id=case.case_id,
            )
            for case in sorted(
                reference.validation_cases,
                key=lambda item: item.kind == "coverage",
            )
        ]
        evidence = next(
            (
                item for item in case_evidence
                if item.status != "validated"
            ),
            next(
                item for item in case_evidence
                if item.validation_case.get("kind") == "coverage"
            ),
        )
        runs = [
            save_v3_validation_run(stored["session_id"], item)
            for item in case_evidence
        ]
        run = runs[case_evidence.index(evidence)]
        validated = evidence.status == "validated"
        update_sp(
            sp_id,
            status="verified" if validated else (
                "needs_review"
                if evidence.status in {"inconclusive", "needs_review"}
                else "verify_failed"
            ),
            verify_result=evidence.model_dump_json(),
            validated_hash=candidate.content_hash if validated else None,
            bundle_hash=candidate.content_hash,
            schema_fingerprint=evidence.catalog_fingerprint,
        )
        return {
            "ok": True,
            "result": {
                **evidence.model_dump(mode="json"),
                "sp_id": sp_id,
                "run_id": run["id"],
                "deployment_eligible": validated,
            },
        }
    except Exception as exc:
        return _error(exc)
