"""自动校验修复回归测试。"""
import json

from langchain_core.messages import AIMessage

from app.agent import nodes
from app.db import sqlite as sqlite_db
from app.db import sqlserver as sqlserver_db


class _VerifySqlFixLLM:
    def __init__(self):
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        return AIMessage(content=json.dumps({
            "fixed_sql": "SELECT COUNT(*) AS [RowCount] FROM OINV",
        }))


def test_verify_sql_error_repairs_query_instead_of_sp(monkeypatch):
    llm = _VerifySqlFixLLM()
    verify_query = {
        "id": "vq-1",
        "name": "校验行数",
        "sql_code": "SELECT COUNT(*) AS RowCount FROM OINV",
        "compare_columns": "",
    }
    sp_updates = []

    monkeypatch.setattr(nodes, "_get_llm", lambda: llm)
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    monkeypatch.setattr(nodes, "check_syntax", lambda _code: (True, ""))
    monkeypatch.setattr(
        nodes,
        "execute_query",
        lambda sql: (_ for _ in ()).throw(RuntimeError("RowCount syntax error"))
        if " AS RowCount " in sql
        else [{"RowCount": 1}],
    )
    monkeypatch.setattr(sqlserver_db, "deploy_procedure", lambda _name, _code: (True, ""))
    monkeypatch.setattr(
        sqlserver_db, "execute_sp_with_params",
        lambda _name, _params, _defs: [],
    )
    monkeypatch.setattr(sqlite_db, "get_verify_queries", lambda _sp_id: [dict(verify_query)])

    def update_verify_query(_query_id, **changes):
        verify_query.update(changes)

    monkeypatch.setattr(sqlite_db, "update_verify_query", update_verify_query)
    monkeypatch.setattr(
        sqlite_db, "update_sp",
        lambda _sp_id, **changes: sp_updates.append(changes),
    )

    result = nodes.verify_node({
        "session_id": "session-1",
        "sp_list": [{
            "id": "sp-1",
            "name": "sp_Test",
            "code": "CREATE PROCEDURE sp_Test AS SELECT 1",
            "parameters": "[]",
        }],
    })

    assert result["status"] == "verified"
    assert result["verify_results"][0]["business_ok"] is True
    assert verify_query["sql_code"] == "SELECT COUNT(*) AS [RowCount] FROM OINV"
    assert llm.calls == 1
    assert not any("code" in change for change in sp_updates)
