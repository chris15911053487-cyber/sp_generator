"""部署资格必须绑定最近一次业务校验通过的完整版本。"""
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
    }
    sp["validated_hash"] = compute_bundle_hash(sp, queries)
    return sp, queries


def test_deploy_check_accepts_exact_validated_version(monkeypatch):
    sp, queries = _fixture()
    monkeypatch.setattr(deploy, "get_sps", lambda _session_id: [sp])
    monkeypatch.setattr(deploy, "get_verify_queries", lambda _sp_id: queries)
    monkeypatch.setattr(deploy, "check_syntax", lambda _code: (True, ""))

    ready, results, _ = deploy._readiness("session-1")

    assert ready is True
    assert results[0]["ready"] is True


def test_deploy_check_blocks_changed_sp_without_running_deploy(monkeypatch):
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
