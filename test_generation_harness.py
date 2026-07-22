"""SQL 生成 harness 的字符化与回归测试。"""
import json

import pytest
from pydantic import ValidationError

from app.agent import nodes
from app.db import sqlite as sqlite_db, sqlserver
from app.services.generation_harness import QuerySpec, compile_query_spec
from app.services.schema_evidence import capture_schema_evidence
from app.services.candidate_pipeline import (
    CandidateBundle,
    VerifyQueryCandidate,
    run_candidate_gates,
    validate_candidate_with_repairs,
)


def test_generation_builds_memory_candidates_without_persistence(monkeypatch):
    spec = QuerySpec.model_validate(_query_spec_data())
    evidence = capture_schema_evidence(
        spec, lambda _refs: _schema_loader_for_spec(spec),
    )
    events = []
    query_spec_ids = []
    fingerprints = []

    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    monkeypatch.setattr(nodes, "_get_llm", lambda: object())
    monkeypatch.setattr(
        nodes,
        "_compile_design_query_spec",
        lambda *_args: pytest.fail("确认后不得再次编译 QuerySpec"),
    )
    monkeypatch.setattr(
        nodes,
        "capture_schema_evidence",
        lambda candidate: events.append("schema") or evidence,
    )

    def generate_sp(_llm, query_spec, procedure_spec, schema_evidence):
        events.append("sp")
        query_spec_ids.append(id(query_spec))
        fingerprints.append(schema_evidence.fingerprint)
        return (
            f"CREATE PROCEDURE dbo.{procedure_spec.name} @FromDate DATE AS "
            "SELECT SUM(DocTotal) AS TotalAmount FROM dbo.OINV "
            "WHERE DocDate >= @FromDate"
        )

    def generate_oracle(_llm, query_spec, _procedure_spec, schema_evidence):
        events.append("oracle")
        query_spec_ids.append(id(query_spec))
        fingerprints.append(schema_evidence.fingerprint)
        return [VerifyQueryCandidate(
            name="发票总额直接对账",
            sql_code=(
                "SELECT SUM(DocTotal) AS TotalAmount FROM dbo.OINV "
                "WHERE DocDate >= {FromDate}"
            ),
            compare_columns="TotalAmount",
            validation_spec={
                "mode": "scalar",
                "required": True,
                "compare_columns": ["TotalAmount"],
            },
        )]

    monkeypatch.setattr(nodes, "_generate_procedure_candidate", generate_sp)
    monkeypatch.setattr(nodes, "_generate_oracle_candidates", generate_oracle)
    monkeypatch.setattr(
        sqlite_db,
        "replace_session_sp_bundles_atomically",
        lambda *_args: pytest.fail("生成阶段不得持久化候选"),
    )

    result = nodes.generate_node({
        "session_id": "session-1",
        "design": "已确认设计",
        "query_spec": spec.model_dump(mode="json", by_alias=True),
    })

    assert result["status"] == "candidate_generated"
    assert events == ["schema", "sp", "oracle"]
    assert len(set(query_spec_ids)) == 1
    assert fingerprints == [evidence.fingerprint, evidence.fingerprint]
    assert result["candidate_bundles"][0]["status"] == "candidate_generated"


def test_safety_failure_preserves_old_sp_and_oracle_byte_for_byte(monkeypatch):
    old_artifacts = {
        "sp": "CREATE PROCEDURE sp_Current AS SELECT 42 AS Value",
        "oracle": "SELECT 42 AS Value",
    }
    before = json.dumps(old_artifacts, sort_keys=True).encode()
    bundle = _candidate_bundle(
        code=(
            "CREATE PROCEDURE dbo.sp_InvoiceSummary @FromDate DATE AS "
            "EXEC xp_cmdshell 'whoami'"
        ),
    )
    persisted = []
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    monkeypatch.setattr(nodes, "_get_llm", lambda: object())
    monkeypatch.setattr(
        sqlite_db,
        "replace_session_sp_bundles_atomically",
        lambda *_args: persisted.append(True),
    )

    result = nodes.verify_node({
        "session_id": "session-1",
        "candidate_bundles": [bundle.model_dump(mode="json", by_alias=True)],
    })

    assert result["status"] == "verify_failed"
    assert persisted == []
    assert json.dumps(old_artifacts, sort_keys=True).encode() == before

def _query_spec_data(table_count=1):
    sources = [
        {
            "schema": "dbo",
            "table": "OINV" if index == 0 else f"TABLE_{index}",
            "alias": "invoice" if index == 0 else f"source_{index}",
            "role": "发票主表" if index == 0 else f"来源 {index}",
        }
        for index in range(table_count)
    ]
    return {
        "design_version": "design-sha256",
        "procedures": [{
            "name": "sp_InvoiceSummary",
            "purpose": "按日期汇总发票",
            "operation_type": "reporting",
            "parameters": [{
                "name": "@FromDate",
                "sql_type": "DATE",
                "required": True,
                "default": None,
                "meaning": "开始日期",
            }],
            "sources": sources,
            "joins": [],
            "filters": [{
                "description": "只统计起始日期后的发票",
                "column_refs": [{"source_alias": "invoice", "column": "DocDate"}],
                "parameter_refs": ["@FromDate"],
            }],
            "grain": [],
            "outputs": [{
                "name": "TotalAmount",
                "meaning": "发票总额",
                "source_columns": [
                    {"source_alias": "invoice", "column": "DocTotal"},
                ],
                "aggregation": "SUM",
                "sql_type": "DECIMAL(19,6)",
            }],
            "writes": [],
            "verification_rules": [{
                "name": "发票总额直接对账",
                "mode": "scalar",
                "required_columns": ["TotalAmount"],
                "description": "独立汇总同一范围的发票总额",
            }],
        }],
    }


def _schema_loader_for_spec(spec, *, wrong_column=False, changed_type=False):
    objects = []
    for source in spec.procedures[0].sources:
        columns = [{
            "name": "DocCur" if wrong_column else "DocDate",
            "sql_type": "date",
            "max_length": None,
            "precision": None,
            "scale": None,
            "nullable": False,
            "description": None,
        }]
        if source.alias == "invoice":
            columns.append({
                "name": "DocTotal",
                "sql_type": "decimal",
                "max_length": None,
                "precision": 20 if changed_type else 19,
                "scale": 6,
                "nullable": False,
                "description": "单据总额",
            })
        objects.append({
            "schema": source.schema,
            "name": source.table,
            "object_type": "table",
            "columns": columns,
        })
    return {
        "database_name": "SBODEMO",
        "objects": objects,
        "available_objects": [f"{item['schema']}.{item['name']}" for item in objects],
    }


def test_query_spec_renders_the_same_contract_for_confirmation():
    spec = QuerySpec.model_validate(_query_spec_data())

    design = nodes._render_query_spec(spec)

    assert "sp_InvoiceSummary" in design
    assert "dbo.OINV" in design
    assert "@FromDate" in design
    assert "TotalAmount" in design
    assert "发票总额直接对账" in design


def test_compile_query_spec_is_strict_and_invokes_compiler_once():
    calls = []

    def compiler(prompt):
        calls.append(prompt)
        return json.dumps(_query_spec_data(), ensure_ascii=False)

    spec = compile_query_spec("已确认设计正文", compiler)

    assert len(calls) == 1
    assert "已确认设计正文" in calls[0]
    assert '"join_type"' in calls[0]
    assert '"inner"' in calls[0]
    assert spec.procedures[0].name == "sp_InvoiceSummary"
    assert spec.canonical_json() == QuerySpec.model_validate(
        _query_spec_data(),
    ).canonical_json()


def test_query_spec_normalizes_equivalent_enum_spelling():
    data = _query_spec_data()
    procedure = data["procedures"][0]
    procedure["operation_type"] = " REPORTING "
    procedure["sources"].append({
        "schema": "dbo",
        "table": "INV1",
        "alias": "line",
        "role": "发票明细",
    })
    procedure["joins"] = [{
        "join_type": "INNER",
        "left": {"source_alias": "invoice", "column": "DocEntry"},
        "right": {"source_alias": "line", "column": "DocEntry"},
        "reason": "关联发票头和明细",
    }]
    procedure["verification_rules"][0]["mode"] = " SCALAR "

    spec = QuerySpec.model_validate(data)

    assert spec.procedures[0].operation_type == "reporting"
    assert spec.procedures[0].joins[0].join_type == "inner"
    assert spec.procedures[0].verification_rules[0].mode == "scalar"


def test_query_spec_normalizes_write_operation_enum():
    data = _query_spec_data()
    procedure = data["procedures"][0]
    procedure["operation_type"] = "CONTROLLED_WRITE"
    procedure["writes"] = [{
        "schema": "dbo",
        "table": "OINV",
        "operation": " UPDATE ",
        "key_columns": ["DocEntry"],
        "max_affected_rows": 1,
    }]

    spec = QuerySpec.model_validate(data)

    assert spec.procedures[0].operation_type == "controlled_write"
    assert spec.procedures[0].writes[0].operation == "update"


def test_compile_query_spec_repairs_validation_errors_once():
    invalid = _query_spec_data()
    invalid["invented"] = True
    del invalid["procedures"][0]["outputs"][0]["meaning"]
    invalid["procedures"][0]["filters"][0]["column_refs"] = (
        "invoice.DocDate"
    )
    responses = [
        json.dumps(invalid, ensure_ascii=False),
        json.dumps(_query_spec_data(), ensure_ascii=False),
    ]
    calls = []

    def compiler(prompt):
        calls.append(prompt)
        return responses.pop(0)

    spec = compile_query_spec("已确认设计正文", compiler)

    assert spec.procedures[0].outputs[0].meaning == "发票总额"
    assert len(calls) == 2
    assert "上一次 QuerySpec 输出未通过严格校验" in calls[1]
    assert '"type": "missing"' in calls[1]
    assert '"type": "list_type"' in calls[1]
    assert '"type": "extra_forbidden"' in calls[1]
    assert "outputs" in calls[1]


def test_compile_query_spec_stops_after_one_failed_repair():
    invalid = _query_spec_data()
    del invalid["procedures"][0]["outputs"][0]["meaning"]
    calls = []

    def compiler(prompt):
        calls.append(prompt)
        return json.dumps(invalid, ensure_ascii=False)

    with pytest.raises(ValidationError, match="meaning"):
        compile_query_spec("已确认设计正文", compiler)

    assert len(calls) == 2


@pytest.mark.parametrize(
    ("mutate", "error_text"),
    [
        (lambda data: data.update({"invented": True}), "Extra inputs"),
        (
            lambda data: data["procedures"][0]["parameters"].append(
                dict(data["procedures"][0]["parameters"][0]),
            ),
            "重复参数",
        ),
        (
            lambda data: data["procedures"][0]["outputs"].append(
                dict(data["procedures"][0]["outputs"][0]),
            ),
            "重复输出",
        ),
        (
            lambda data: data["procedures"][0]["outputs"][0]["source_columns"][0].update(
                {"source_alias": "missing"},
            ),
            "未声明来源别名",
        ),
        (
            lambda data: data["procedures"][0]["filters"][0]["parameter_refs"].append(
                "@Missing",
            ),
            "未声明参数",
        ),
    ],
)
def test_query_spec_rejects_contract_drift(mutate, error_text):
    data = _query_spec_data()
    mutate(data)

    with pytest.raises(ValidationError, match=error_text):
        QuerySpec.model_validate(data)


def test_agent_state_keeps_query_spec_and_candidates_compatible():
    assert "query_spec" in nodes.AgentState.__annotations__
    assert "candidate_bundles" in nodes.AgentState.__annotations__


def test_schema_binding_rejects_nearby_column_without_auto_replacement():
    data = _query_spec_data()
    data["procedures"][0]["filters"][0]["column_refs"][0]["column"] = "DocCurrency"
    spec = QuerySpec.model_validate(data)

    evidence = capture_schema_evidence(
        spec,
        lambda _refs: _schema_loader_for_spec(spec, wrong_column=True),
    )

    assert [item.identifier for item in evidence.unresolved] == ["dbo.OINV.DocCurrency"]
    assert evidence.unresolved[0].candidates == ["dbo.OINV.DocCur"]
    assert evidence.objects[0].columns[0].name == "DocCur"


def test_schema_binding_requires_exact_schema_object_and_case():
    spec = QuerySpec.model_validate(_query_spec_data())

    def loader(_refs):
        loaded = _schema_loader_for_spec(spec)
        loaded["objects"][0]["schema"] = "custom"
        loaded["objects"][0]["name"] = "oinv"
        loaded["available_objects"] = ["custom.oinv", "dbo.oinv"]
        return loaded

    evidence = capture_schema_evidence(spec, loader)

    assert evidence.objects == []
    assert evidence.unresolved[0].identifier == "dbo.OINV"
    assert "dbo.oinv" in evidence.unresolved[0].candidates


def test_schema_binding_supports_user_tables_fields_and_more_than_twelve_tables():
    data = _query_spec_data(table_count=13)
    data["procedures"][0]["sources"][0].update({"table": "@CUSTOM", "role": "用户表"})
    data["procedures"][0]["filters"][0]["column_refs"][0]["column"] = "U_Color"
    spec = QuerySpec.model_validate(data)

    def loader(_refs):
        loaded = _schema_loader_for_spec(spec)
        loaded["objects"][0]["columns"][0]["name"] = "U_Color"
        return loaded

    evidence = capture_schema_evidence(spec, loader)

    assert evidence.unresolved == []
    assert len(evidence.objects) == 13
    assert evidence.objects[0].name == "@CUSTOM"
    assert evidence.objects[0].columns[0].name == "U_Color"


def test_schema_fingerprint_is_stable_and_structure_sensitive():
    spec = QuerySpec.model_validate(_query_spec_data())
    first = capture_schema_evidence(spec, lambda _refs: _schema_loader_for_spec(spec))
    second = capture_schema_evidence(spec, lambda _refs: _schema_loader_for_spec(spec))
    changed = capture_schema_evidence(
        spec,
        lambda _refs: _schema_loader_for_spec(spec, changed_type=True),
    )

    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != changed.fingerprint

def test_sqlserver_schema_reader_uses_only_catalogs_without_table_limit(monkeypatch):
    executed = []
    catalog = [
        ("dbo", f"TABLE_{index}", index, "U")
        for index in range(13)
    ]
    columns = [
        ("dbo", f"TABLE_{index}", "Id", "int", 4, 10, 0, False, None)
        for index in range(13)
    ]

    class Cursor:
        current = ""

        def execute(self, statement, *params):
            self.current = statement
            executed.append((statement, params))
            return self

        def fetchone(self):
            return ("SBODEMO",)

        def fetchall(self):
            if "o.object_id, o.type" in self.current:
                return catalog
            if "sys.columns" in self.current:
                return columns
            return []

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(sqlserver, "get_connection", lambda: Connection())

    result = sqlserver.read_schema_objects([
        ("dbo", f"TABLE_{index}") for index in range(13)
    ])

    assert len(result["objects"]) == 13
    assert len(executed[-1][1]) == 13
    statements = "\n".join(item[0] for item in executed)
    assert "SELECT DB_NAME()" in statements
    assert "FROM sys.objects" in statements
    assert "JOIN sys.columns" in statements
    assert "FROM dbo." not in statements

def _candidate_bundle(name="sp_InvoiceSummary", code=None):
    data = _query_spec_data()
    data["procedures"][0]["name"] = name
    spec = QuerySpec.model_validate(data)
    evidence = capture_schema_evidence(
        spec,
        lambda _refs: _schema_loader_for_spec(spec),
    )
    return CandidateBundle(
        query_spec=spec,
        procedure_spec=spec.procedures[0],
        procedure_sql=code or (
            f"CREATE PROCEDURE dbo.{name} @FromDate DATE AS "
            "SELECT SUM(DocTotal) AS TotalAmount FROM dbo.OINV "
            "WHERE DocDate >= @FromDate"
        ),
        verify_queries=[VerifyQueryCandidate(
            name="发票总额直接对账",
            sql_code=(
                "SELECT SUM(DocTotal) AS TotalAmount FROM dbo.OINV "
                "WHERE DocDate >= {FromDate}"
            ),
            compare_columns="TotalAmount",
            validation_spec={
                "mode": "scalar",
                "required": True,
                "compare_columns": ["TotalAmount"],
            },
        )],
        schema_evidence=evidence,
    )


def test_candidate_gates_stop_after_safety_failure():
    bundle = _candidate_bundle(
        code=(
            "CREATE PROCEDURE dbo.sp_InvoiceSummary @FromDate DATE AS "
            "EXEC xp_cmdshell 'whoami'"
        ),
    )
    compiled = []
    business = []

    result = run_candidate_gates(
        bundle,
        compiler=lambda *args: compiled.append(args) or {"ok": True},
        business_validator=lambda *args: business.append(args) or {},
    )

    assert result.status == "failed"
    assert [item.gate for item in result.gate_results] == [
        "query_spec", "schema", "safety",
    ]
    assert compiled == []
    assert business == []


def test_repair_restarts_all_gates_and_keeps_contract_invariants():
    bundle = _candidate_bundle(
        code=(
            "CREATE PROCEDURE dbo.sp_InvoiceSummary @FromDate DATE AS "
            "SELECT SUM(DocTotal) AS WrongName FROM dbo.OINV "
            "WHERE DocDate >= @FromDate"
        ),
    )
    compiled = []
    business = []

    def compiler(*args):
        compiled.append(args[0])
        return {"ok": True, "executed": False}

    def repair(candidate, _errors):
        repaired = candidate.model_copy(deep=True)
        repaired.procedure_sql = repaired.procedure_sql.replace(
            "WrongName", "TotalAmount",
        )
        return repaired

    result = validate_candidate_with_repairs(
        bundle,
        repair,
        compiler=compiler,
        business_validator=lambda *_args: business.append(True) or {
            "syntax_ok": True,
            "business_ok": True,
            "bundle_hash": "legacy",
            "details": [],
        },
    )

    assert result.status == "validated"
    assert result.repair_count == 1
    assert compiled == ["procedure", "oracle", "procedure", "oracle"]
    assert business == [True]


def test_unattributed_business_difference_needs_review_without_repair():
    repairs = []
    result = validate_candidate_with_repairs(
        _candidate_bundle(),
        lambda *_args: repairs.append(True),
        compiler=lambda *_args: {"ok": True, "executed": False},
        business_validator=lambda *_args: {
            "syntax_ok": True,
            "business_ok": False,
            "bundle_hash": "legacy",
            "details": [{
                "type": "business",
                "pass": False,
                "comparison": {"summary": "结果不同"},
            }],
        },
    )

    assert result.status == "needs_review"
    assert repairs == []


def test_atomic_bundle_replacement_rolls_back_when_second_insert_fails(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "atomic.db"
    monkeypatch.setattr(sqlite_db, "DB_PATH", str(db_path))
    sqlite_db.init_db()
    session = sqlite_db.create_session("atomic")
    old = sqlite_db.save_sp(
        session["id"], "sp_Old", "CREATE PROCEDURE sp_Old AS SELECT 1",
    )
    sqlite_db.save_verify_query(old["id"], "old", "SELECT 1")
    before_sps = sqlite_db.get_sps(session["id"])
    before_queries = sqlite_db.get_verify_queries(old["id"])

    bundles = [_candidate_bundle("sp_One"), _candidate_bundle("sp_Two")]
    for bundle in bundles:
        bundle.status = "validated"

    original_insert = sqlite_db._insert_candidate_bundle
    inserted = []

    def fail_second(conn, session_id, bundle):
        inserted.append(bundle.procedure_spec.name)
        if len(inserted) == 2:
            raise RuntimeError("second insert failed")
        return original_insert(conn, session_id, bundle)

    monkeypatch.setattr(sqlite_db, "_insert_candidate_bundle", fail_second)

    with pytest.raises(RuntimeError, match="second insert failed"):
        sqlite_db.replace_session_sp_bundles_atomically(session["id"], bundles)

    assert sqlite_db.get_sps(session["id"]) == before_sps
    assert sqlite_db.get_verify_queries(old["id"]) == before_queries


def test_verify_node_persists_only_after_entire_batch_validates(monkeypatch):
    from app.services import candidate_pipeline

    bundles = [_candidate_bundle("sp_One"), _candidate_bundle("sp_Two")]
    persisted = []
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    monkeypatch.setattr(nodes, "_get_llm", lambda: object())

    def validate(candidate, _repairer, **_kwargs):
        return run_candidate_gates(
            candidate,
            compiler=lambda *_args: {"ok": True, "executed": False},
            business_validator=lambda *_args: {
                "syntax_ok": True,
                "business_ok": True,
                "bundle_hash": "legacy",
                "details": [],
            },
        )

    monkeypatch.setattr(candidate_pipeline, "validate_candidate_with_repairs", validate)

    def replace(session_id, checked):
        persisted.append((session_id, [item.status for item in checked]))
        return [
            {"id": f"id-{index}", "name": item.procedure_spec.name}
            for index, item in enumerate(checked)
        ]

    monkeypatch.setattr(sqlite_db, "replace_session_sp_bundles_atomically", replace)

    result = nodes.verify_node({
        "session_id": "session-1",
        "candidate_bundles": [
            item.model_dump(mode="json", by_alias=True) for item in bundles
        ],
    })

    assert result["status"] == "persisted"
    assert persisted == [("session-1", ["validated", "validated"])]
    assert all(item["syntax_ok"] and item["business_ok"] for item in result["verify_results"])
    assert all(item["sp_id"] for item in result["verify_results"])


def test_verify_node_does_not_persist_partial_batch(monkeypatch):
    from app.services import candidate_pipeline

    bundles = [_candidate_bundle("sp_One"), _candidate_bundle("sp_Two")]
    persisted = []
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    monkeypatch.setattr(nodes, "_get_llm", lambda: object())

    def validate(candidate, _repairer, **_kwargs):
        if candidate.procedure_spec.name == "sp_Two":
            candidate.status = "failed"
            return candidate
        candidate.status = "validated"
        return candidate

    monkeypatch.setattr(candidate_pipeline, "validate_candidate_with_repairs", validate)
    monkeypatch.setattr(
        sqlite_db,
        "replace_session_sp_bundles_atomically",
        lambda *_args: persisted.append(True),
    )

    result = nodes.verify_node({
        "session_id": "session-1",
        "candidate_bundles": [
            item.model_dump(mode="json", by_alias=True) for item in bundles
        ],
    })

    assert result["status"] == "verify_failed"
    assert persisted == []


def test_atomic_bundle_replacement_success_updates_hashes(tmp_path, monkeypatch):
    db_path = tmp_path / "success.db"
    monkeypatch.setattr(sqlite_db, "DB_PATH", str(db_path))
    sqlite_db.init_db()
    session = sqlite_db.create_session("success")
    old = sqlite_db.save_sp(
        session["id"], "sp_Old", "CREATE PROCEDURE sp_Old AS SELECT 1",
    )
    sqlite_db.save_verify_query(old["id"], "old", "SELECT 1")
    bundles = [_candidate_bundle("sp_One"), _candidate_bundle("sp_Two")]
    for bundle in bundles:
        bundle.status = "validated"

    inserted = sqlite_db.replace_session_sp_bundles_atomically(
        session["id"], bundles,
    )

    assert {item["name"] for item in inserted} == {"sp_One", "sp_Two"}
    assert {item["name"] for item in sqlite_db.get_sps(session["id"])} == {
        "sp_One", "sp_Two",
    }
    assert all(item["bundle_hash"] == item["validated_hash"] for item in inserted)
    assert all(sqlite_db.get_verify_queries(item["id"]) for item in inserted)


def test_parameter_default_drift_is_rejected_before_business_validation():
    bundle = _candidate_bundle()
    parameter = bundle.procedure_spec.parameters[0]
    parameter.required = False
    parameter.default = "2026-01-01"
    bundle.procedure_sql = bundle.procedure_sql.replace(
        "@FromDate DATE AS", "@FromDate DATE = '2025-01-01' AS",
    )
    business = []

    result = run_candidate_gates(
        bundle,
        compiler=lambda *_args: {"ok": True, "executed": False},
        business_validator=lambda *_args: business.append(True) or {},
    )

    assert result.status == "failed"
    assert business == []
    assert any(
        error.code == "parameter_signature"
        for gate in result.gate_results for error in gate.errors
    )


def test_compile_207_refreshes_schema_once_before_rechecking():
    bundle = _candidate_bundle()
    compile_calls = []
    refreshes = []

    def compiler(artifact, *_args):
        compile_calls.append(artifact)
        if artifact == "procedure" and compile_calls.count("procedure") == 1:
            return {"ok": False, "code": "207", "error": "Invalid column"}
        return {"ok": True, "executed": False}

    result = validate_candidate_with_repairs(
        bundle,
        lambda *_args: pytest.fail("刷新后成功时不应调用修复模型"),
        compiler=compiler,
        schema_refresher=lambda spec: (
            refreshes.append(spec)
            or capture_schema_evidence(
                spec, lambda _refs: _schema_loader_for_spec(spec),
            )
        ),
        business_validator=lambda *_args: {
            "syntax_ok": True,
            "business_ok": True,
            "bundle_hash": "legacy",
            "details": [],
        },
    )

    assert result.status == "validated"
    assert len(refreshes) == 1
    assert compile_calls.count("procedure") == 2
    assert result.repair_count == 0


def test_change_set_write_scope_drift_is_rejected():
    data = _query_spec_data()
    procedure = data["procedures"][0]
    procedure["operation_type"] = "controlled_write"
    procedure["writes"] = [{
        "schema": "dbo",
        "table": "OINV",
        "operation": "update",
        "key_columns": ["DocEntry"],
        "max_affected_rows": 10,
    }]
    procedure["verification_rules"] = [{
        "name": "发票更新变化集",
        "mode": "change_set",
        "required_columns": ["TotalAmount"],
        "description": "核对更新范围",
    }]
    spec = QuerySpec.model_validate(data)
    loaded = _schema_loader_for_spec(spec)
    loaded["objects"][0]["columns"].append({
        "name": "DocEntry", "sql_type": "int", "max_length": None,
        "precision": 10, "scale": 0, "nullable": False,
        "description": None,
    })
    evidence = capture_schema_evidence(spec, lambda _refs: loaded)
    bundle = CandidateBundle(
        query_spec=spec,
        procedure_spec=spec.procedures[0],
        procedure_sql=(
            "CREATE PROCEDURE dbo.sp_InvoiceSummary @FromDate DATE AS "
            "UPDATE dbo.OINV SET DocTotal = DocTotal WHERE DocDate >= @FromDate; "
            "SELECT SUM(DocTotal) AS TotalAmount FROM dbo.OINV"
        ),
        verify_queries=[VerifyQueryCandidate(
            name="发票更新变化集",
            sql_code="SELECT SUM(DocTotal) AS TotalAmount FROM dbo.OINV",
            compare_columns="TotalAmount",
            validation_spec={
                "mode": "change_set",
                "required": True,
                "compare_columns": ["TotalAmount"],
                "snapshot_sql": "SELECT DocEntry FROM dbo.OINV",
                "affected_tables": [{
                    "table": "dbo.OINV",
                    "operation": "update",
                    "key_columns": ["DocEntry"],
                    "compare_columns": ["DocTotal"],
                    "max_affected_rows": 999,
                }],
            },
        )],
        schema_evidence=evidence,
    )

    result = run_candidate_gates(
        bundle,
        compiler=lambda *_args: {"ok": True, "executed": False},
        business_validator=lambda *_args: pytest.fail("契约失败不得执行业务校验"),
    )

    assert result.status == "failed"
    assert any(
        error.code == "write_scope"
        for gate in result.gate_results for error in gate.errors
    )


def test_sqlserver_result_metadata_type_drift_is_rejected():
    bundle = _candidate_bundle()

    def compiler(artifact, name, *_args):
        sql_type = "nvarchar(100)" if artifact == "procedure" else "decimal(19,6)"
        return {
            "ok": True,
            "executed": False,
            "result_columns": [{
                "name": "TotalAmount",
                "sql_type": sql_type,
                "nullable": True,
            }],
        }

    result = run_candidate_gates(
        bundle,
        compiler=compiler,
        business_validator=lambda *_args: pytest.fail("类型契约失败不得执行业务校验"),
    )

    assert result.status == "failed"
    assert any(
        error.code == "output_types"
        for gate in result.gate_results for error in gate.errors
    )
