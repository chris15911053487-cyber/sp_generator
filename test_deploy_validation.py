"""部署资格必须绑定最近一次业务校验通过的完整版本。"""
import json

import pytest

from app.routes import deploy
from app.services.validation import compute_bundle_hash


def _fixture():
    queries = [{
        "id": "vq-1", "name": "对账", "sql_code": "SELECT 1 AS X",
        "compare_columns": "X",
        "validation_spec": {"mode": "scalar", "compare_columns": ["X"]},
    }]
    sp = {
        "id": "sp-1", "name": "sp_Test",
        "code": "CREATE PROCEDURE sp_Test AS SELECT 1 AS X",
        "parameters": "[]", "operation_type": "query", "business_valid": 1,
        "query_spec_json": json.dumps({
            "design_version": "v1",
            "procedures": [{
                "name": "sp_Test", "purpose": "返回测试值",
                "operation_type": "reporting", "parameters": [],
                "sources": [{
                    "schema": "dbo", "table": "TestSource",
                    "alias": "source", "role": "测试来源",
                }],
                "joins": [], "filters": [], "grain": [],
                "outputs": [{
                    "name": "X", "meaning": "测试值",
                    "source_columns": [{"source_alias": "source", "column": "X"}],
                    "aggregation": None, "sql_type": "INT",
                }],
                "writes": [],
                "verification_rules": [{
                    "name": "对账", "mode": "scalar",
                    "required_columns": ["X"], "description": "直接对账",
                }],
            }],
        }, ensure_ascii=False),
        "schema_fingerprint": "a" * 64,
    }
    sp["validated_hash"] = compute_bundle_hash(sp, queries)
    sp["bundle_hash"] = sp["validated_hash"]
    return sp, queries


def _test_database(monkeypatch):
    monkeypatch.setattr(
        deploy, "get_db_config",
        lambda: {"database": "TestDB", "environment": "test"},
    )
    monkeypatch.setattr(
        deploy,
        "capture_schema_evidence",
        lambda _spec: type("Evidence", (), {
            "fingerprint": "a" * 64,
            "unresolved": [],
        })(),
    )

def test_deploy_check_accepts_exact_validated_version(monkeypatch):
    _test_database(monkeypatch)
    sp, queries = _fixture()
    monkeypatch.setattr(deploy, "get_sps", lambda _session_id: [sp])
    monkeypatch.setattr(deploy, "get_verify_queries", lambda _sp_id: queries)
    monkeypatch.setattr(deploy, "check_syntax", lambda _code: (True, ""))

    ready, results, _ = deploy._readiness("session-1")

    assert ready is True
    assert results[0]["ready"] is True


def test_deploy_check_blocks_changed_sp_without_running_deploy(monkeypatch):
    _test_database(monkeypatch)
    sp, queries = _fixture()
    sp["code"] = "CREATE PROCEDURE sp_Test AS SELECT 2 AS X"
    called = []
    monkeypatch.setattr(deploy, "get_sps", lambda _session_id: [sp])
    monkeypatch.setattr(deploy, "get_verify_queries", lambda _sp_id: queries)
    monkeypatch.setattr(deploy, "check_syntax", lambda _code: (True, ""))
    monkeypatch.setattr(
        deploy, "deploy_procedures_atomically", lambda _items: called.append(True),
    )

    response = deploy.api_deploy("session-1")

    assert response["ok"] is False
    assert "版本不一致" in response["results"][0]["error"]
    assert called == []

def test_deploy_check_rejects_record_and_code_name_mismatch(monkeypatch):
    _test_database(monkeypatch)
    sp, queries = _fixture()
    sp["name"] = "sp_Other"
    sp["validated_hash"] = compute_bundle_hash(sp, queries)
    monkeypatch.setattr(deploy, "get_sps", lambda _session_id: [sp])
    monkeypatch.setattr(deploy, "get_verify_queries", lambda _sp_id: queries)
    monkeypatch.setattr(deploy, "check_syntax", lambda _code: (True, ""))

    ready, results, _ = deploy._readiness("session-1")

    assert ready is False
    assert "过程名" in results[0]["error"]


def test_schema_qualified_execution_uses_two_identifiers():
    from app.routes.sp import _execution

    sql, values = _execution(
        {"name": "dbo.sp_Test", "parameters": "[]"}, {},
    )

    assert sql == "EXEC [dbo].[sp_Test]"
    assert values == []


def test_revalidated_deployed_version_keeps_deployed_status(monkeypatch):
    from app.routes import verify

    sp, queries = _fixture()
    sp["deployed_hash"] = sp["validated_hash"]
    changes = []
    monkeypatch.setattr(
        verify, "update_sp", lambda _sp_id, **kwargs: changes.append(kwargs),
    )
    monkeypatch.setattr(verify, "update_verify_query", lambda *_args, **_kwargs: None)
    result = {
        "syntax_ok": True, "business_ok": True,
        "bundle_hash": sp["deployed_hash"], "details": [],
    }

    verify._persist_result(sp, queries, result)

    assert changes[0]["status"] == "deployed"
    assert result["status"] == "deployed"


def test_global_failure_marks_pending_queries_failed(monkeypatch):
    from app.routes import verify

    sp, queries = _fixture()
    query_updates = []
    monkeypatch.setattr(verify, "update_sp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        verify, "update_verify_query",
        lambda query_id, **kwargs: query_updates.append((query_id, kwargs)),
    )
    result = {
        "syntax_ok": True,
        "business_ok": False,
        "bundle_hash": sp["validated_hash"],
        "details": [{"type": "execution", "pass": False, "error": "boom"}],
    }

    verify._persist_result(sp, queries, result)

    assert query_updates[0][0] == "vq-1"
    assert query_updates[0][1]["status"] == "fail"
    assert "boom" in query_updates[0][1]["result_detail"]


def test_atomic_deployment_rolls_back_all_when_second_sp_fails(monkeypatch):
    from app.db import sqlserver

    class Cursor:
        def execute(self, sql):
            if "sp_Two" in sql:
                raise RuntimeError("second failed")

    class Connection:
        def __init__(self):
            self.committed = False
            self.rolled_back = False

        def cursor(self):
            return Cursor()

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(
        sqlserver, "get_connection", lambda *, autocommit: connection,
    )
    procedures = [
        {"id": "1", "name": "sp_One",
         "code": "CREATE PROCEDURE sp_One AS SELECT 1"},
        {"id": "2", "name": "sp_Two",
         "code": "CREATE PROCEDURE sp_Two AS SELECT 2"},
    ]

    results = sqlserver.deploy_procedures_atomically(procedures)

    assert connection.rolled_back is True
    assert connection.committed is False
    assert all(item["success"] is False for item in results)


def test_execute_accepts_revalidated_current_deployed_version(monkeypatch):
    from app.routes import sp as sp_route

    stored, queries = _fixture()
    stored.update(
        status="verified",
        deployed_hash=stored["validated_hash"],
        deployed_at="2026-07-20T12:00:00",
    )

    class Cursor:
        description = None
        timeout = None

        def execute(self, _sql, _values):
            pass

        def nextset(self):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(sp_route, "get_sp", lambda _sp_id: stored)
    monkeypatch.setattr(sp_route, "get_verify_queries", lambda _sp_id: queries)
    monkeypatch.setattr(sp_route, "get_connection", lambda: Connection())

    response = sp_route.api_execute_sp(
        stored["id"], sp_route.ExecuteSpRequest(),
    )

    assert response["ok"] is True


def test_unsaved_verified_content_remains_draft(monkeypatch):
    from app.routes import verify

    stored, queries = _fixture()
    monkeypatch.setattr(verify, "get_sp", lambda _sp_id: dict(stored))
    monkeypatch.setattr(
        verify, "get_verify_queries", lambda _sp_id: [dict(item) for item in queries],
    )
    monkeypatch.setattr(
        verify, "validate_sp_bundle",
        lambda *_args, **_kwargs: {
            "syntax_ok": True, "business_ok": True,
            "bundle_hash": "different-hash", "details": [],
        },
    )
    monkeypatch.setattr(
        verify, "update_sp",
        lambda *_args, **_kwargs: pytest.fail("未保存内容不应写入校验状态"),
    )

    response = verify._verify(
        stored["id"],
        verify.VerifySpRequest(code="CREATE PROCEDURE sp_Test AS SELECT 2 AS X"),
    )

    assert response["result"]["unsaved"] is True
    assert response["result"]["status"] == "draft"
    assert response["result"]["deployment_eligible"] is False


def test_deploy_requires_explicit_test_environment(monkeypatch):
    sp, queries = _fixture()
    monkeypatch.setattr(deploy, "get_sps", lambda _session_id: [sp])
    monkeypatch.setattr(deploy, "get_verify_queries", lambda _sp_id: queries)
    monkeypatch.setattr(deploy, "check_syntax", lambda _code: (True, ""))
    monkeypatch.setattr(
        deploy, "get_db_config",
        lambda: {"database": "ProductionDB", "environment": ""},
    )

    ready, results, _ = deploy._readiness("session-1")

    assert ready is False
    assert "测试数据库" in results[0]["error"]


def test_write_execution_requires_explicit_test_environment(monkeypatch):
    from app.routes import sp as sp_route

    stored, queries = _fixture()
    stored.update(operation_type="delete", deployed_at="2026-07-20T12:00:00")
    stored["deployed_hash"] = compute_bundle_hash(stored, queries)
    monkeypatch.setattr(sp_route, "get_sp", lambda _sp_id: stored)
    monkeypatch.setattr(sp_route, "get_verify_queries", lambda _sp_id: queries)
    monkeypatch.setattr(
        sp_route, "get_db_config",
        lambda: {"database": "ProductionDB", "environment": ""},
    )
    monkeypatch.setattr(
        sp_route, "get_connection",
        lambda: pytest.fail("环境门禁失败时不应连接数据库"),
    )

    response = sp_route.api_execute_sp(
        stored["id"], sp_route.ExecuteSpRequest(confirm_write=True),
    )

    assert response["ok"] is False
    assert response["environment_required"] is True


def test_deploy_check_requires_revalidation_after_schema_change(monkeypatch):
    _test_database(monkeypatch)
    sp, queries = _fixture()
    monkeypatch.setattr(deploy, "get_sps", lambda _session_id: [sp])
    monkeypatch.setattr(deploy, "get_verify_queries", lambda _sp_id: queries)
    monkeypatch.setattr(deploy, "check_syntax", lambda _code: (True, ""))
    monkeypatch.setattr(
        deploy,
        "capture_schema_evidence",
        lambda _spec: type("Evidence", (), {
            "fingerprint": "b" * 64,
            "unresolved": [],
        })(),
    )

    ready, results, _ = deploy._readiness("session-1")

    assert ready is False
    assert results[0]["revalidation_required"] is True
    assert "Schema 已变化" in results[0]["error"]


def test_deploy_check_rejects_changed_audit_bundle_hash(monkeypatch):
    _test_database(monkeypatch)
    sp, queries = _fixture()
    sp["bundle_hash"] = "tampered"
    monkeypatch.setattr(deploy, "get_sps", lambda _session_id: [sp])
    monkeypatch.setattr(deploy, "get_verify_queries", lambda _sp_id: queries)
    monkeypatch.setattr(deploy, "check_syntax", lambda _code: (True, ""))

    ready, results, _ = deploy._readiness("session-1")

    assert ready is False
    assert "审计哈希不一致" in results[0]["error"]


def test_invalid_manual_save_does_not_overwrite_current_version(monkeypatch):
    from app.routes import verify

    stored, queries = _fixture()
    before = json.dumps({"sp": stored, "queries": queries}, sort_keys=True)
    monkeypatch.setattr(verify, "get_sp", lambda _sp_id: dict(stored))
    monkeypatch.setattr(
        verify,
        "get_verify_queries",
        lambda _sp_id: [dict(item) for item in queries],
    )
    monkeypatch.setattr(
        verify,
        "validate_sp_bundle",
        lambda *_args, **_kwargs: {
            "syntax_ok": False,
            "business_ok": False,
            "bundle_hash": "invalid",
            "details": [{"type": "syntax", "pass": False, "error": "bad sql"}],
        },
    )
    monkeypatch.setattr(
        verify,
        "save_sp_bundle",
        lambda *_args, **_kwargs: pytest.fail("无效候选不得保存"),
    )
    monkeypatch.setattr(
        verify,
        "update_sp",
        lambda *_args, **_kwargs: pytest.fail("无效候选不得覆盖状态"),
    )

    response = verify._verify(
        stored["id"],
        verify.VerifySpRequest(code="INVALID SQL", save=True),
    )

    assert response["result"]["unsaved"] is True
    assert response["result"]["status"] == "verify_failed"
    assert json.dumps({"sp": stored, "queries": queries}, sort_keys=True) == before


def test_validated_manual_bundle_is_saved_with_status_in_one_transaction(
    tmp_path, monkeypatch,
):
    from app.db import sqlite as sqlite_db

    db_path = tmp_path / "manual.db"
    monkeypatch.setattr(sqlite_db, "DB_PATH", str(db_path))
    sqlite_db.init_db()
    session = sqlite_db.create_session("manual")
    template, _ = _fixture()
    stored = sqlite_db.save_sp(
        session["id"],
        template["name"],
        "CREATE PROCEDURE sp_Test AS SELECT 0 AS X",
    )
    query = sqlite_db.save_verify_query(
        stored["id"],
        "对账",
        "SELECT 1 AS X",
        "X",
        json.dumps({"mode": "scalar", "compare_columns": ["X"]}),
    )
    sqlite_db.update_sp(
        stored["id"],
        query_spec_json=template["query_spec_json"],
        schema_fingerprint=template["schema_fingerprint"],
    )
    candidate = sqlite_db.get_sp(stored["id"])
    candidate["code"] = "CREATE PROCEDURE sp_Test AS SELECT 1 AS X"
    queries = [dict(query)]
    bundle_hash = compute_bundle_hash(candidate, queries)
    result = {
        "syntax_ok": True,
        "business_ok": True,
        "bundle_hash": bundle_hash,
        "status": "verified",
        "details": [{
            "query_id": query["id"], "query": "对账", "pass": True,
        }],
    }

    saved = sqlite_db.save_sp_bundle(
        stored["id"],
        candidate["code"],
        candidate["parameters"],
        candidate["operation_type"],
        queries,
        validation_result=result,
    )
    saved_query = sqlite_db.get_verify_queries(stored["id"])[0]

    assert saved["code"] == candidate["code"]
    assert saved["status"] == "verified"
    assert saved["validated_hash"] == bundle_hash
    assert saved["bundle_hash"] == bundle_hash
    assert saved_query["status"] == "pass"
