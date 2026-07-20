"""统一的 SP 语法和业务校验 API。"""
import json

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.db.sqlite import (
    _get_conn, get_sp, get_sps, get_verify_queries, save_sp_bundle,
    update_sp, update_verify_query,
)
from app.db.sqlserver import check_syntax
from app.services.validation import compute_bundle_hash, validate_sp_bundle

router = APIRouter(prefix="/api/verify", tags=["verify"])


class VerifyQueryPayload(BaseModel):
    id: str | None = None
    name: str = "未命名校验"
    sql_code: str = ""
    compare_columns: str = ""
    validation_spec: dict | str = Field(default_factory=dict)


class VerifySpRequest(BaseModel):
    code: str | None = None
    params: dict | None = None
    operation_type: str | None = None
    verify_queries: list[VerifyQueryPayload] | None = None
    save: bool = False


class UpdateVqRequest(BaseModel):
    sql_code: str | None = None
    name: str | None = None
    validation_spec: dict | str | None = None


def _params_with_defaults(sp: dict, values: dict) -> str:
    try:
        definitions = json.loads(sp.get("parameters") or "[]")
    except (TypeError, json.JSONDecodeError):
        definitions = []
    for definition in definitions:
        name = str(definition.get("name", "")).lstrip("@")
        if name in values:
            definition["default"] = str(values[name])
    return json.dumps(definitions, ensure_ascii=False)


def _payload_queries(req: VerifySpRequest, sp_id: str) -> list[dict]:
    if req.verify_queries is None:
        return get_verify_queries(sp_id)
    return [query.model_dump() for query in req.verify_queries]


def _persist_result(sp: dict, queries: list[dict], result: dict) -> None:
    all_pass = result["syntax_ok"] and result["business_ok"]
    status = "verify_failed"
    if all_pass:
        status = (
            "deployed"
            if sp.get("deployed_hash") == result["bundle_hash"]
            else "verified"
        )
    result["status"] = status
    update_sp(
        sp["id"],
        syntax_valid=1 if result["syntax_ok"] else 0,
        business_valid=1 if result["business_ok"] else 0,
        status=status,
        verify_result=json.dumps(result, ensure_ascii=False),
        validated_hash=result["bundle_hash"] if all_pass else None,
    )
    detail_by_id = {
        detail.get("query_id"): detail
        for detail in result.get("details", []) if detail.get("query_id")
    }
    global_failure = next(
        (
            detail for detail in result.get("details", [])
            if not detail.get("pass", False) and not detail.get("query_id")
        ),
        None,
    )
    for query in queries:
        query_id = query.get("id")
        detail = detail_by_id.get(query_id) or global_failure
        if not query_id or detail is None:
            continue
        update_verify_query(
            query_id,
            status="pass" if detail.get("pass") else "fail",
            result_detail=json.dumps(detail, ensure_ascii=False, indent=2),
        )


def _verify(sp_id: str, req: VerifySpRequest) -> dict:
    stored = get_sp(sp_id)
    if not stored:
        return {"ok": False, "message": "SP 不存在"}

    params = req.params or {}
    queries = _payload_queries(req, sp_id)
    candidate = dict(stored)
    if req.code is not None:
        candidate["code"] = req.code
    if req.operation_type is not None:
        candidate["operation_type"] = req.operation_type
    candidate["parameters"] = _params_with_defaults(candidate, params)

    if req.save:
        save_sp_bundle(
            sp_id,
            candidate["code"],
            candidate["parameters"],
            candidate.get("operation_type") or "query",
            queries,
        )
        candidate = get_sp(sp_id)
        queries = get_verify_queries(sp_id)

    result = validate_sp_bundle(candidate, queries, params)

    saved = get_sp(sp_id)
    saved_queries = get_verify_queries(sp_id)
    matches_saved = result["bundle_hash"] == compute_bundle_hash(saved, saved_queries)
    if matches_saved:
        _persist_result(saved, saved_queries, result)
    else:
        result["unsaved"] = True
        result["deployment_eligible"] = False
        result["status"] = "draft"

    return {"ok": True, "result": result}


@router.post("/syntax/{session_id}/{sp_id}")
def api_check_syntax(session_id: str, sp_id: str):
    target = next((sp for sp in get_sps(session_id) if sp["id"] == sp_id), None)
    if not target:
        return {"ok": False, "message": "SP 不存在"}
    ok, error = check_syntax(target["code"])
    update_sp(sp_id, syntax_valid=1 if ok else 0)
    return {"ok": ok, "error": error}


@router.post("/business/{sp_id}")
def api_check_business(sp_id: str, req: VerifySpRequest | None = None):
    return _verify(sp_id, req or VerifySpRequest())


@router.get("/{session_id}/sp/{sp_id}")
def api_get_verify_for_sp(session_id: str, sp_id: str):
    return {"verify_queries": get_verify_queries(sp_id)}


@router.put("/query/{query_id}")
def api_update_verify_query(query_id: str, req: UpdateVqRequest):
    changes = {key: value for key, value in req.model_dump().items() if value is not None}
    if not changes:
        return {"ok": False, "message": "没有可更新的字段"}
    if isinstance(changes.get("validation_spec"), dict):
        changes["validation_spec"] = json.dumps(changes["validation_spec"], ensure_ascii=False)
    conn = _get_conn()
    row = conn.execute("SELECT sp_id FROM verify_queries WHERE id = ?", (query_id,)).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "message": "校验 SQL 不存在"}
    update_verify_query(query_id, **changes, status="pending", result_detail=None)
    update_sp(row["sp_id"], status="draft", syntax_valid=0, business_valid=0,
              validated_hash=None, verify_result=None)
    return {"ok": True}


@router.post("/sp/{sp_id}")
def api_verify_single_sp(sp_id: str, req: VerifySpRequest | None = None):
    return _verify(sp_id, req or VerifySpRequest())


@router.post("/all/{session_id}")
def api_verify_all(session_id: str):
    results = []
    for sp in get_sps(session_id):
        response = _verify(sp["id"], VerifySpRequest())
        if response.get("result"):
            results.append(response["result"])
    return {"results": results}
