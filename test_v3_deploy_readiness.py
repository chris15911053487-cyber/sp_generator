from app.routes import deploy
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
            snapshot_id="deploy-proof",
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


def test_v3_deploy_uses_frozen_chain_not_legacy_verify_queries(monkeypatch):
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
    chain = {
        "semantic_contract": semantic.model_dump(mode="json", by_alias=True),
        "schema_binding": schema_binding.model_dump(mode="json", by_alias=True),
        "reference_bundle": reference.model_dump(mode="json", by_alias=True),
        "procedure_candidate": candidate.model_dump(mode="json", by_alias=True),
        "validation_evidence": evidence.model_dump(mode="json"),
    }
    sp = {
        "id": "sp-1",
        "name": semantic.procedure_name,
        "code": candidate.procedure_sql,
        "parameters": "[]",
        "operation_type": "query",
        "business_valid": 1,
        "validated_hash": candidate.content_hash,
    }
    monkeypatch.setattr(deploy, "get_sps", lambda _session: [sp])
    monkeypatch.setattr(deploy, "get_db_config", lambda: {})
    monkeypatch.setattr(deploy, "is_explicit_test_database", lambda _cfg: True)
    monkeypatch.setattr(
        deploy,
        "compile_candidate",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        deploy,
        "get_v3_deployment_chain",
        lambda *_args: chain,
    )
    monkeypatch.setattr(
        deploy,
        "capture_catalog_snapshot",
        lambda: schema_catalog,
    )

    ready, results, _ = deploy._readiness("session")
    assert ready, results
    assert results[0]["ready"]
    assert results[0]["reasons"] == []
