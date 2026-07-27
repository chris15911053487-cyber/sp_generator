import json
from pathlib import Path

import pytest

from app.db import sqlserver
from app.services.sql_artifact_compiler import (
    SqlArtifactError,
    canonicalize_parameter_syntax,
    compile_odbc_binding,
    describe_parameter_declaration,
    normalize_collation_clauses,
    normalize_qualified_column_collations,
    parameter_manifest,
    scan_parameter_references,
)


PARAMETERS = [
    {"name": "@FromDate", "type": "DATE", "required": True, "default": None},
    {"name": "@CardCode", "type": "NVARCHAR(50)", "required": False, "default": None},
]


def test_repeated_native_parameter_is_declared_once():
    sql, declaration, manifest = describe_parameter_declaration(
        "SELECT @FromDate AS A WHERE @FromDate IS NOT NULL",
        PARAMETERS,
    )

    assert sql.count("@FromDate") == 2
    assert declaration == "@FromDate DATE"
    assert manifest[0]["occurrences"] == 2


def test_legacy_parameter_is_canonicalized_outside_literals_and_comments():
    sql = (
        "SELECT {FromDate}, '{FromDate}' AS Literal -- {FromDate}\n"
        "/* {FromDate} */ WHERE CardCode = {CardCode}"
    )

    canonical = canonicalize_parameter_syntax(sql)

    assert canonical.startswith("SELECT @FromDate")
    assert "'{FromDate}'" in canonical
    assert "-- {FromDate}" in canonical
    assert "/* {FromDate} */" in canonical
    assert canonical.endswith("CardCode = @CardCode")


def test_scanner_ignores_strings_comments_identifiers_and_system_variables():
    sql = (
        "SELECT '@Ignored', [@Identifier], \"@Quoted\", @@ROWCOUNT, @CardCode "
        "-- @Comment\n/* @Block */"
    )

    assert [item.name for item in scan_parameter_references(sql)] == ["@CardCode"]


def test_undeclared_parameter_fails_before_database_access():
    with pytest.raises(SqlArtifactError, match="未声明参数") as exc:
        parameter_manifest("SELECT @Missing", PARAMETERS)

    assert exc.value.code == "undeclared_parameter"


def test_duplicate_parameter_names_are_case_insensitive():
    with pytest.raises(SqlArtifactError, match="重复参数") as exc:
        parameter_manifest(
            "SELECT @Value",
            [
                {"name": "@Value", "type": "INT"},
                {"name": "@value", "type": "INT"},
            ],
        )

    assert exc.value.code == "duplicate_parameter"


def test_odbc_binding_preserves_occurrence_order_and_repetition():
    sql, values = compile_odbc_binding(
        "SELECT @FromDate WHERE @CardCode IS NULL OR CardCode = @CardCode",
        {"FromDate": "2026-07-01", "@CardCode": "C001"},
    )

    assert sql == "SELECT ? WHERE ? IS NULL OR CardCode = ?"
    assert values == ["2026-07-01", "C001", "C001"]


def test_odbc_binding_rejects_missing_value():
    with pytest.raises(SqlArtifactError, match="缺少校验参数") as exc:
        compile_odbc_binding("SELECT @FromDate", {})

    assert exc.value.code == "missing_parameter_value"


def test_procedure_compile_describes_body_without_tempdb_object(monkeypatch):
    executed = []

    class Cursor:
        def execute(self, statement, *params):
            executed.append((statement, params))
            return self

        def fetchall(self):
            return [(False, None, "Value", False, None, "int")]

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(sqlserver, "get_connection", lambda **_kwargs: Connection())

    result = sqlserver.compile_candidate(
        "procedure",
        "sp_Test",
        (
            "CREATE PROCEDURE dbo.sp_Test @Value INT AS BEGIN "
            "DECLARE @Local INT = @Value; SELECT @Local AS Value; END"
        ),
        [{"name": "@Value", "type": "INT", "required": True}],
    )

    assert result["ok"] is True
    assert result["method"] == "procedure_body_sp_describe"
    assert not any("#compile_" in statement for statement, _params in executed)
    assert not any("CREATE PROCEDURE" in statement for statement, _params in executed)
    assert executed[0][1][1] == "@Value INT"


def test_undeclared_verification_parameter_does_not_connect(monkeypatch):
    monkeypatch.setattr(
        sqlserver,
        "get_connection",
        lambda **_kwargs: pytest.fail("参数契约失败时不得连接数据库"),
    )

    result = sqlserver.compile_candidate(
        "oracle",
        "check",
        "SELECT @Missing",
        PARAMETERS,
    )

    assert result["ok"] is False
    assert result["code"] == "undeclared_parameter"
    assert result["method"] == "parameter_contract"


def test_explicit_collations_follow_captured_database_policy():
    sql, decisions = normalize_collation_clauses(
        (
            "SELECT CardCode COLLATE DATABASE_DEFAULT, "
            "CardName COLLATE SQL_Latin1_General_CP1_CI_AS, "
            "'COLLATE DATABASE_DEFAULT' AS Literal"
        ),
        "SQL_Latin1_General_CP850_CI_AS",
    )

    assert sql.count("COLLATE SQL_Latin1_General_CP850_CI_AS") == 2
    assert "'COLLATE DATABASE_DEFAULT'" in sql
    assert len(decisions) == 2


def test_invalid_target_collation_is_rejected():
    with pytest.raises(SqlArtifactError) as exc:
        normalize_collation_clauses("SELECT 1", "bad; DROP TABLE X")

    assert exc.value.code == "collation_evidence_missing"


def test_mismatched_qualified_text_column_gets_deterministic_collation():
    sql, decisions = normalize_qualified_column_collations(
        (
            "SELECT inv.CardCode FROM dbo.OINV inv "
            "WHERE inv.CardCode = @CardCode "
            "AND 'inv.CardCode' <> ''"
        ),
        [{
            "qualifier": "inv",
            "column": "CardCode",
            "source_collation": "SQL_Latin1_General_CP1_CI_AS",
        }],
        "SQL_Latin1_General_CP850_CI_AS",
    )

    assert sql.count(
        "inv.CardCode COLLATE SQL_Latin1_General_CP850_CI_AS"
    ) == 2
    assert "'inv.CardCode'" in sql
    assert len(decisions) == 2


def test_existing_collation_clause_is_not_duplicated():
    sql, decisions = normalize_qualified_column_collations(
        "SELECT inv.CardCode COLLATE SQL_Latin1_General_CP1_CI_AS FROM dbo.OINV inv",
        [{
            "qualifier": "inv",
            "column": "CardCode",
            "source_collation": "SQL_Latin1_General_CP1_CI_AS",
        }],
        "SQL_Latin1_General_CP850_CI_AS",
    )

    assert sql.count("COLLATE") == 1
    assert decisions == []


def test_session_19_repeated_native_parameters_compile_to_one_declaration_each():
    fixture = json.loads(
        (
            Path(__file__).parent
            / "tests"
            / "fixtures"
            / "session_19_compile_failure.json"
        ).read_text(encoding="utf-8")
    )

    for sql in fixture["verification_sql"]:
        _canonical, declaration, manifest = describe_parameter_declaration(
            sql, fixture["parameters"],
        )

        assert declaration.count("@FromDate DATETIME") == 1
        assert {item["name"] for item in manifest} == {
            "@FromDate", "@ToDate", "@CardCode", "@DocStatus",
        }
        assert next(
            item for item in manifest if item["name"] == "@FromDate"
        )["occurrences"] == 2


def test_stored_procedure_execution_binds_values_without_sql_concatenation(
    monkeypatch,
):
    calls = []

    class Cursor:
        description = None

        def execute(self, statement, values):
            calls.append((statement, values))

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(sqlserver, "get_connection", lambda: Connection())

    sqlserver.execute_sp_with_params(
        "dbo.sp_Test",
        {"CardCode": "C001'; DROP TABLE dbo.OINV; --"},
        [{"name": "@CardCode", "type": "NVARCHAR(50)"}],
    )

    statement, values = calls[0]
    assert statement == "EXEC [dbo].[sp_Test] @CardCode = ?"
    assert "DROP TABLE" not in statement
    assert values == ["C001'; DROP TABLE dbo.OINV; --"]
