"""候选生成状态与 SQL Server 兼容入口测试。"""
from app.db import sqlserver


def test_generate_only_flows_to_verify_after_success():
    from app.agent.graph import _after_generate

    assert _after_generate({"status": "candidate_generated", "error": ""}) == "verify"
    assert _after_generate({"status": "generate_failed", "error": "failed"}) == "end"


def test_check_syntax_compiles_procedure_against_real_objects(monkeypatch):
    executed = []

    class _Cursor:
        def execute(self, statement, *_params):
            executed.append(statement)
            if "CREATE PROCEDURE #compile_" in statement and "DocCurrency" in statement:
                raise RuntimeError("Invalid column name 'DocCurrency'")
            return self

    class _Connection:
        def cursor(self):
            return _Cursor()

        def close(self):
            pass

    monkeypatch.setattr(sqlserver, "get_connection", lambda: _Connection())

    ok, error = sqlserver.check_syntax(
        "CREATE PROCEDURE sp_Test AS SELECT DocCurrency FROM OINV",
    )

    assert not ok
    assert "DocCurrency" in error
    assert any("CREATE PROCEDURE #compile_" in item for item in executed)


def test_schema_context_includes_sap_user_table_and_field(monkeypatch):
    class _Cursor:
        rows = []

        def execute(self, statement, *_params):
            if "SELECT s.name, o.name, o.object_id" in statement:
                self.rows = [
                    ("dbo", "OINV", 1),
                    ("dbo", "@CUSTOM", 2),
                    ("dbo", "IGNORED", 3),
                ]
            else:
                self.rows = [
                    ("dbo", "OINV", "DocCur", "nvarchar", 6, 0, 0, 1, None),
                    (
                        "dbo", "@CUSTOM", "U_Color", "nvarchar", 40, 0, 0, 1,
                        "自定义颜色",
                    ),
                ]
            return self

        def fetchall(self):
            return self.rows

    class _Connection:
        def cursor(self):
            return _Cursor()

        def close(self):
            pass

    monkeypatch.setattr(sqlserver, "get_connection", lambda: _Connection())

    context = sqlserver.get_schema_context(
        "从 OINV 关联 [@CUSTOM]，返回 U_Color",
    )

    assert "[dbo].[OINV]" in context
    assert "DocCur: nvarchar(3)" in context
    assert "[dbo].[@CUSTOM]" in context
    assert "U_Color: nvarchar(20)" in context
    assert "自定义颜色" in context
    assert "IGNORED" not in context
