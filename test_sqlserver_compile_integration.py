"""SQL Server 编译能力探针；仅显式授权的隔离测试库运行。"""
import os

import pytest

from app.db.sqlserver import compile_candidate, get_connection
from config import get_db_config, is_explicit_test_database


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SQLSERVER_COMPILE_INTEGRATION") != "1",
    reason="需要显式授权的隔离 SQL Server 测试库",
)
PROBE_TABLE = "dbo.__harness_compile_probe__"


def _require_isolated_test_database():
    config = get_db_config()
    if not is_explicit_test_database(config):
        pytest.fail("集成探针只允许 environment=test 且数据库名非空的隔离测试库")


@pytest.fixture(autouse=True)
def isolated_probe_table():
    _require_isolated_test_database()
    conn = get_connection(autocommit=True)
    cursor = conn.cursor()
    cursor.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
    cursor.execute(
        f"CREATE TABLE {PROBE_TABLE} (Id int NOT NULL PRIMARY KEY, Touched int NOT NULL)"
    )
    cursor.execute(f"INSERT INTO {PROBE_TABLE} (Id, Touched) VALUES (1, 0)")
    conn.close()
    try:
        yield
    finally:
        conn = get_connection(autocommit=True)
        conn.cursor().execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
        conn.close()


def test_invalid_column_and_object_return_207_and_208_without_execution():
    invalid_column = compile_candidate(
        "oracle",
        "invalid_column",
        f"SELECT MissingColumn FROM {PROBE_TABLE}",
        [],
    )
    invalid_object = compile_candidate(
        "oracle",
        "invalid_object",
        "SELECT 1 FROM dbo.__missing_harness_object__",
        [],
    )

    assert invalid_column["ok"] is False
    assert invalid_column["code"] == "207"
    assert invalid_object["ok"] is False
    assert invalid_object["code"] == "208"
    assert invalid_column["executed"] is False
    assert invalid_object["executed"] is False


def test_parameter_and_result_metadata_are_described():
    oracle = compile_candidate(
        "oracle",
        "parameterized_oracle",
        "SELECT CAST({Value} AS int) AS Value",
        [{"name": "@Value", "type": "INT"}],
    )
    procedure = compile_candidate(
        "procedure",
        "sp_HarnessMetadata",
        (
            "CREATE PROCEDURE dbo.sp_HarnessMetadata @Value int AS "
            "SELECT @Value AS Value"
        ),
        [{"name": "@Value", "type": "INT"}],
    )

    assert oracle["ok"] is True
    assert oracle["result_columns"][0]["name"] == "Value"
    assert procedure["ok"] is True
    assert procedure["result_columns"][0]["name"] == "Value"


def test_procedure_probe_does_not_execute_write_body():
    result = compile_candidate(
        "procedure",
        "sp_HarnessProbe",
        (
            "CREATE PROCEDURE dbo.sp_HarnessProbe AS "
            f"UPDATE {PROBE_TABLE} SET Touched = 1 WHERE Id = 1"
        ),
        [],
    )
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"SELECT Touched FROM {PROBE_TABLE} WHERE Id = 1")
    touched = cursor.fetchone()[0]
    conn.close()

    assert result["ok"] is True
    assert result["executed"] is False
    assert touched == 0


def test_invalid_reference_in_conditional_branch_is_rejected():
    result = compile_candidate(
        "procedure",
        "sp_HarnessBranch",
        (
            "CREATE PROCEDURE dbo.sp_HarnessBranch @Flag bit AS "
            f"IF @Flag = 1 SELECT MissingColumn FROM {PROBE_TABLE}; "
            f"ELSE SELECT Id FROM {PROBE_TABLE};"
        ),
        [{"name": "@Flag", "type": "BIT"}],
    )

    assert result["ok"] is False
    assert result["code"] == "207"
    assert result["executed"] is False


def test_temporary_procedure_is_cleaned_after_failure():
    compile_candidate(
        "procedure",
        "sp_HarnessCleanup",
        (
            "CREATE PROCEDURE dbo.sp_HarnessCleanup AS "
            f"SELECT MissingColumn FROM {PROBE_TABLE}"
        ),
        [],
    )
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM tempdb.sys.objects WHERE name LIKE '#compile[_]%'"
    )
    count = cursor.fetchone()[0]
    conn.close()

    assert count == 0
