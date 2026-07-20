"""显式校验复用统一服务；生成失败不得替换旧制品。"""
from langchain_core.messages import AIMessage

from app.agent import nodes
from app.db import sqlite as sqlite_db
from app.services import validation


def test_verify_node_uses_unified_validation_without_deployment(monkeypatch):
    sp = {
        "id": "sp-1", "name": "sp_Test", "code": "CREATE PROCEDURE sp_Test AS SELECT 1",
        "parameters": "[]", "operation_type": "query",
    }
    queries = [{"id": "vq-1", "name": "校验", "sql_code": "SELECT 1"}]
    updates = []
    calls = []

    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    monkeypatch.setattr(sqlite_db, "get_sps", lambda _session_id: [dict(sp)])
    monkeypatch.setattr(sqlite_db, "get_verify_queries", lambda _sp_id: queries)
    monkeypatch.setattr(
        sqlite_db, "update_sp", lambda _sp_id, **changes: updates.append(changes),
    )

    def validate(candidate, candidate_queries, params):
        calls.append((candidate, candidate_queries, params))
        return {
            "sp_id": candidate["id"], "sp_name": candidate["name"],
            "syntax_ok": True, "business_ok": True, "bundle_hash": "hash-1",
            "details": [],
        }

    monkeypatch.setattr(validation, "validate_sp_bundle", validate)

    result = nodes.verify_node({"session_id": "session-1", "sp_list": [sp]})

    assert result["status"] == "verified"
    assert len(calls) == 1
    assert updates[-1]["validated_hash"] == "hash-1"
    assert updates[-1]["status"] == "verified"
    assert "code" not in updates[-1]


def test_generate_keeps_old_artifacts_when_verify_sql_generation_fails(monkeypatch):
    old_replaced = []
    discarded = []
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    monkeypatch.setattr(nodes, "_get_llm", lambda: type(
        "Llm", (), {
            "invoke": lambda self, _messages: AIMessage(content=(
                '{"procedures":[{"name":"sp_New","operation_type":"query",'
                '"code":"CREATE PROCEDURE sp_New AS SELECT 1 AS X"}]}'
            ))
        },
    )())
    monkeypatch.setattr(nodes, "save_sp", lambda *_args, **_kwargs: {
        "id": "new-1", "name": "sp_New", "operation_type": "query",
        "code": "CREATE PROCEDURE sp_New AS SELECT 1 AS X",
        "parameters": "[]",
    })
    monkeypatch.setattr(
        nodes, "_generate_verify_sql_for_sp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("LLM failed")),
    )
    monkeypatch.setattr(sqlite_db, "get_verify_queries", lambda _sp_id: [])
    monkeypatch.setattr(
        sqlite_db, "delete_sps_except",
        lambda *_args: old_replaced.append(True),
    )
    monkeypatch.setattr(
        sqlite_db, "delete_sp", lambda sp_id: discarded.append(sp_id),
    )

    result = nodes.generate_node({
        "session_id": "session-1", "design": "confirmed design",
    })

    assert "error" in result
    assert old_replaced == []
    assert discarded == ["new-1"]
