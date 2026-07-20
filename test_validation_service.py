"""统一 SP 校验服务的安全与回滚测试。"""
import sqlite3

import pytest

from app.db import sqlite as sqlite_db
from app.services import validation


class _Cursor:
    def __init__(self, write=False):
        self.write = write
        self.after = False
        self.description = None
        self.rows = []
        self.timeout = None
        self.executed = []

    def execute(self, sql, _params=None):
        normalized = " ".join(sql.split()).upper()
        self.executed.append(normalized)
        self.description = None
        self.rows = []
        if normalized == "SELECT DB_NAME()":
            self.description = [("database",)]
            self.rows = [("TestDB",)]
        elif normalized.startswith("EXEC #VERIFY_"):
            self.after = True
            if not self.write:
                self.description = [("TotalAmount",)]
                self.rows = [(10,)]
        elif "FROM DBO.EXPECTEDROWS" in normalized:
            self.description = [
                ("ChangeType",), ("Id",), ("Before_Amount",), ("After_Amount",),
            ]
            self.rows = [("delete", 1, 10, None)]
        elif "FROM DBO.TARGET" in normalized:
            self.description = [("Id",), ("Amount",)]
            self.rows = [(2, 20)] if self.after else [(1, 10), (2, 20)]
        elif normalized.startswith("SELECT 10 AS TOTALAMOUNT"):
            self.description = [("TotalAmount",)]
            self.rows = [(10,)]
        return self

    def fetchone(self):
        return self.rows.pop(0)

    def fetchmany(self, size):
        rows, self.rows = self.rows[:size], self.rows[size:]
        return rows

    def nextset(self):
        return False


class _Connection:
    def __init__(self, write=False):
        self._cursor = _Cursor(write=write)
        self.timeout = None
        self.rollback_called = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rollback_called = True
        self._cursor.after = False

    def close(self):
        self.closed = True


class _RollbackFailureConnection(_Connection):
    def rollback(self):
        raise RuntimeError("rollback failed")


def _database(monkeypatch, connection):
    monkeypatch.setattr(validation, "check_syntax", lambda _code: (True, ""))
    monkeypatch.setattr(
        validation, "get_db_config",
        lambda: {"database": "TestDB", "environment": "test"},
    )
    monkeypatch.setattr(
        validation, "get_connection", lambda *, autocommit: connection,
    )


def test_query_sp_is_executed_as_temporary_procedure_and_rolled_back(monkeypatch):
    connection = _Connection()
    _database(monkeypatch, connection)
    sp = {
        "id": "sp-1", "name": "sp_Total", "operation_type": "query",
        "parameters": "[]",
        "code": "CREATE PROCEDURE [dbo].[sp_Total] AS SELECT 10 AS TotalAmount",
    }
    queries = [{
        "id": "vq-1", "name": "总额对账",
        "sql_code": "SELECT 10 AS TotalAmount", "compare_columns": "TotalAmount",
        "validation_spec": {
            "mode": "scalar", "required": True,
            "compare_columns": ["TotalAmount"], "tolerance": {"TotalAmount": 0.01},
        },
    }]

    result = validation.validate_sp_bundle(sp, queries)

    assert result["syntax_ok"] is True
    assert result["business_ok"] is True
    assert connection._cursor.executed[:2] == [
        "SET TRANSACTION ISOLATION LEVEL SNAPSHOT",
        "SET XACT_ABORT ON",
    ]
    assert connection.timeout == validation.QUERY_TIMEOUT_SECONDS
    assert connection.rollback_called is True
    assert connection.closed is True


def test_write_sp_compares_deleted_rows_and_confirms_restore(monkeypatch):
    connection = _Connection(write=True)
    _database(monkeypatch, connection)
    sp = {
        "id": "sp-2", "name": "sp_Delete", "operation_type": "delete",
        "parameters": "[]",
        "code": "CREATE PROCEDURE sp_Delete AS DELETE FROM dbo.Target WHERE Id = 1",
    }
    queries = [{
        "id": "vq-2", "name": "删除结果对账",
        "sql_code": "SELECT Id, Amount FROM dbo.ExpectedRows",
        "validation_spec": {
            "mode": "change_set", "required": True,
            "affected_tables": [{
                "table": "dbo.Target", "operation": "delete",
                "key_columns": ["Id"], "compare_columns": ["Amount"],
                "max_affected_rows": 10,
            }],
            "snapshot_sql": "SELECT Id, Amount FROM dbo.Target",
        },
    }]

    result = validation.validate_sp_bundle(sp, queries)

    assert result["syntax_ok"] is True
    assert result["business_ok"] is True
    assert result["details"][0]["comparison"]["affected_rows"] == 1
    assert result["restore_confirmed"] is True
    assert connection.rollback_called is True
    assert connection.closed is True


def test_write_sp_rejects_undeclared_target_table():
    with pytest.raises(validation.ValidationError, match="未声明的表"):
        validation.validate_reporting_procedure(
            "CREATE PROCEDURE sp_Update AS UPDATE dbo.Target SET Amount = 0",
            "update",
            [{"validation_spec": {
                "mode": "change_set", "required": True,
                "snapshot_sql": "SELECT Id FROM dbo.Other",
                "affected_tables": [{
                    "table": "dbo.Other", "operation": "update",
                    "key_columns": ["Id"], "compare_columns": ["Amount"],
                    "max_affected_rows": 1,
                }],
            }}],
        )


def test_query_sp_rejects_permanent_table_write():
    with pytest.raises(validation.ValidationError, match="query 类型"):
        validation.validate_reporting_procedure(
            "CREATE PROCEDURE sp_Bad AS DELETE FROM dbo.Target",
            "query",
            [],
        )


def test_bundle_hash_changes_with_oracle_sql():
    sp = {"name": "sp_A", "code": "CREATE PROCEDURE sp_A AS SELECT 1",
          "parameters": "[]", "operation_type": "query"}
    first = [{"name": "v", "sql_code": "SELECT 1 AS X", "compare_columns": "X"}]
    second = [{"name": "v", "sql_code": "SELECT 2 AS X", "compare_columns": "X"}]
    assert validation.compute_bundle_hash(sp, first) != validation.compute_bundle_hash(sp, second)

def test_select_into_permanent_table_is_rejected():
    with pytest.raises(validation.ValidationError, match="SELECT INTO"):
        validation.validate_reporting_procedure(
            "CREATE PROCEDURE sp_Bad AS SELECT * INTO dbo.Copy FROM dbo.Source",
            "query",
            [],
        )


def test_cross_database_write_is_rejected():
    with pytest.raises(validation.ValidationError, match="跨数据库"):
        validation.validate_reporting_procedure(
            "CREATE PROCEDURE sp_Bad AS DELETE FROM OtherDb.dbo.Target",
            "delete",
            [{"validation_spec": {
                "mode": "change_set", "required": True,
                "snapshot_sql": "SELECT Id FROM OtherDb.dbo.Target",
                "affected_tables": [{
                    "table": "OtherDb.dbo.Target", "operation": "delete",
                    "key_columns": ["Id"], "compare_columns": [],
                    "max_affected_rows": 1,
                }],
            }}],
        )


def test_change_set_detects_insert_update_and_delete():
    insert_target = {
        "operation": "insert", "key_columns": ["Id"], "compare_columns": ["Amount"],
    }
    assert validation._change_set(
        [{"Id": 1, "Amount": 10}],
        [{"Id": 1, "Amount": 10}, {"Id": 2, "Amount": 20}],
        insert_target,
    ) == [{
        "Id": 2, "ChangeType": "insert",
        "Before_Amount": None, "After_Amount": 20,
    }]

    update_target = {
        "operation": "update", "key_columns": ["Id"], "compare_columns": ["Amount"],
    }
    assert validation._change_set(
        [{"Id": 1, "Amount": 10}], [{"Id": 1, "Amount": 12}], update_target,
    ) == [{
        "Id": 1, "ChangeType": "update",
        "Before_Amount": 10, "After_Amount": 12,
    }]

    delete_target = {
        "operation": "delete", "key_columns": ["Id"], "compare_columns": ["Amount"],
    }
    assert validation._change_set(
        [{"Id": 1, "Amount": 10}], [], delete_target,
    ) == [{
        "Id": 1, "ChangeType": "delete",
        "Before_Amount": 10, "After_Amount": None,
    }]


def test_change_set_rejects_declared_operation_mismatch():
    with pytest.raises(validation.ValidationError, match="实际写操作不一致"):
        validation.validate_reporting_procedure(
            "CREATE PROCEDURE sp_Delete AS DELETE FROM dbo.Target",
            "delete",
            [{"validation_spec": {
                "mode": "change_set", "required": True,
                "snapshot_sql": "SELECT Id FROM dbo.Target",
                "affected_tables": [{
                    "table": "dbo.Target", "operation": "insert",
                    "key_columns": ["Id"], "compare_columns": [],
                    "max_affected_rows": 1,
                }],
            }}],
        )


@pytest.mark.parametrize("sql", [
    "SELECT * FROM #Temp",
    "SELECT * FROM OPENQUERY(RemoteServer, 'SELECT 1')",
    "SELECT * FROM [RemoteServer].[OtherDb].[dbo].[Orders]",
    "SELECT 1 INTO dbo.NewTable",
    "SELECT 1 INTO OtherDb.dbo.NewTable",
])
def test_oracle_sql_rejects_external_or_temporary_sources(sql):
    with pytest.raises(validation.ValidationError):
        validation.validate_readonly_query(sql)


def test_existing_database_migration_adds_deployed_at(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE stored_procedures (
            id TEXT PRIMARY KEY, session_id TEXT, name TEXT, code TEXT
        );
        CREATE TABLE verify_queries (
            id TEXT PRIMARY KEY, sp_id TEXT, name TEXT, sql_code TEXT
        );
    """)
    conn.close()
    monkeypatch.setattr(sqlite_db, "DB_PATH", str(db_path))

    sqlite_db.init_db()

    conn = sqlite3.connect(db_path)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(stored_procedures)")
    }
    conn.close()
    assert "deployed_at" in columns


def test_write_validation_fails_when_rollback_fails(monkeypatch):
    connection = _RollbackFailureConnection(write=True)
    _database(monkeypatch, connection)
    sp = {
        "id": "sp-2", "name": "sp_Delete", "operation_type": "delete",
        "parameters": "[]",
        "code": "CREATE PROCEDURE sp_Delete AS DELETE FROM dbo.Target WHERE Id = 1",
    }
    queries = [{
        "id": "vq-2", "name": "删除结果对账",
        "sql_code": "SELECT * FROM dbo.ExpectedRows",
        "validation_spec": {
            "mode": "change_set", "required": True,
            "snapshot_sql": "SELECT Id, Amount FROM dbo.Target",
            "affected_tables": [{
                "table": "dbo.Target", "operation": "delete",
                "key_columns": ["Id"], "compare_columns": ["Amount"],
                "max_affected_rows": 10,
            }],
        },
    }]

    result = validation.validate_sp_bundle(sp, queries)

    assert result["business_ok"] is False
    assert result["rolled_back"] is False
    assert result["restore_confirmed"] is False


@pytest.mark.parametrize(("database", "environment"), [("", "test"), ("ProductionDB", "")])
def test_write_validation_requires_explicit_test_database(monkeypatch, database, environment):
    monkeypatch.setattr(validation, "check_syntax", lambda _code: (True, ""))
    monkeypatch.setattr(
        validation, "get_db_config",
        lambda: {"database": database, "environment": environment},
    )
    sp = {
        "name": "sp_Delete", "operation_type": "delete", "parameters": "[]",
        "code": "CREATE PROCEDURE sp_Delete AS DELETE FROM dbo.Target",
    }
    queries = [{
        "sql_code": "SELECT 'delete' AS ChangeType, Id FROM dbo.Target",
        "validation_spec": {
            "mode": "change_set", "required": True,
            "snapshot_sql": "SELECT Id FROM dbo.Target",
            "affected_tables": [{
                "table": "dbo.Target", "operation": "delete",
                "key_columns": ["Id"], "compare_columns": [],
                "max_affected_rows": 1,
            }],
        },
    }]

    result = validation.validate_sp_bundle(sp, queries)

    assert result["business_ok"] is False
    assert "已明确配置" in result["details"][0]["error"]
