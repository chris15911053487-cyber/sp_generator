from app.db import sqlite as sqlite_db
from app.services.procedure_generator_v3 import generate_procedure_candidate
from app.services.validation_runner_v3 import (
    RuntimeResult,
    SnapshotExecution,
    validate_candidate_v3,
)
from v3_test_helpers import binding, catalog, contract, plan, reference_bundle


class Executor:
    def inspect_environment(self):
        return {
            "server_identity": "TEST-SQL",
            "database_name": "TEST_DB",
            "database_id": 7,
            "snapshot_isolation_state": "ON",
        }

    def execute_same_snapshot(self, *_args, **_kwargs):
        rows = [{"InvoiceId": 1, "Amount": 100.0}]
        return SnapshotExecution(
            snapshot_id="tx-persist",
            database_identity="TEST-SQL/TEST_DB/7",
            references={
                "invoice_income": RuntimeResult(
                    columns=("InvoiceId", "Amount"),
                    rows=rows,
                )
            },
            actual=RuntimeResult(
                columns=("InvoiceId", "Amount"),
                rows=rows,
            ),
        )


def test_v3_artifact_chain_and_evidence_are_persisted_atomically(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(sqlite_db, "DB_PATH", str(tmp_path / "v3.db"))
    sqlite_db.init_db()
    session = sqlite_db.create_session("v3-test")
    semantic = contract()
    schema_catalog = catalog()
    schema_binding = binding(semantic, schema_catalog)
    reference = reference_bundle(semantic, schema_binding)
    candidate = generate_procedure_candidate(
        semantic,
        schema_binding,
        reference,
        lambda *_: plan(),
        compiler=lambda *args: {"ok": True},
    )
    evidence = validate_candidate_v3(
        semantic,
        schema_catalog,
        schema_binding,
        reference,
        candidate,
        executor=Executor(),
    )
    ids = sqlite_db.save_v3_artifacts(
        session["id"],
        semantic_contract=semantic,
        catalog_snapshot=schema_catalog,
        schema_binding=schema_binding,
        reference_bundle=reference,
        procedure_candidate=candidate,
    )
    run = sqlite_db.save_v3_validation_run(session["id"], evidence)
    latest = sqlite_db.get_latest_v3_validation_run(session["id"])

    assert all(ids.values())
    assert run["status"] == "validated"
    assert latest["status"] == "validated"
    assert latest["run_id"] == run["id"]
