from app.services.procedure_generator_v3 import generate_procedure_candidate
from app.services.validation_runner_v3 import (
    RuntimeResult,
    SnapshotExecution,
    validate_candidate_v3,
)
from app.contracts.reference import ReferenceBundle, ValidationCase
from app.contracts.validation import ProcedureCandidateV3
from v3_test_helpers import binding, catalog, contract, plan, reference_bundle


class FakeExecutor:
    def __init__(
        self,
        actual_rows,
        expected_rows,
        *,
        snapshot_state="ON",
        expected_from_date="2026-01-01",
    ):
        self.actual_rows = actual_rows
        self.expected_rows = expected_rows
        self.snapshot_state = snapshot_state
        self.expected_from_date = expected_from_date

    def inspect_environment(self):
        return {
            "server_identity": "TEST-SQL",
            "database_name": "TEST_DB",
            "database_id": 7,
            "snapshot_isolation_state": self.snapshot_state,
        }

    def execute_same_snapshot(
        self,
        contract_value,
        reference_sql,
        actual_procedure_sql,
        actual_procedure_name,
        parameters,
    ):
        assert set(reference_sql) == {"invoice_income"}
        assert actual_procedure_name.startswith("#v3_")
        assert "CREATE PROCEDURE [#v3_" in actual_procedure_sql
        assert parameters["from_date"] == self.expected_from_date
        return SnapshotExecution(
            snapshot_id="tx-100",
            database_identity="TEST-SQL/TEST_DB/7",
            references={
                "invoice_income": RuntimeResult(
                    columns=("InvoiceId", "Amount"),
                    rows=self.expected_rows,
                )
            },
            actual=RuntimeResult(
                columns=("InvoiceId", "Amount"),
                rows=self.actual_rows,
            ),
        )


def artifacts():
    semantic = contract()
    schema_catalog = catalog()
    schema_binding = binding(semantic, schema_catalog)
    reference = reference_bundle(semantic, schema_binding)
    candidate = generate_procedure_candidate(
        semantic,
        schema_binding,
        reference,
        lambda _contract, _binding: plan(),
        compiler=lambda *args: {"ok": True, "method": "fake_compile"},
    )
    return semantic, schema_catalog, schema_binding, reference, candidate


def test_all_gates_pass_only_when_results_match_with_effective_coverage():
    semantic, schema_catalog, schema_binding, reference, candidate = artifacts()
    rows = [{"InvoiceId": 1, "Amount": 100.0}]
    evidence = validate_candidate_v3(
        semantic,
        schema_catalog,
        schema_binding,
        reference,
        candidate,
        executor=FakeExecutor(rows, rows),
    )
    assert evidence.status == "validated"
    assert all(item.status == "passed" for item in evidence.stages)
    assert evidence.coverage.effective


def test_result_mismatch_is_failed_and_evidence_integrity_not_run():
    semantic, schema_catalog, schema_binding, reference, candidate = artifacts()
    evidence = validate_candidate_v3(
        semantic,
        schema_catalog,
        schema_binding,
        reference,
        candidate,
        executor=FakeExecutor(
            [{"InvoiceId": 1, "Amount": 99.0}],
            [{"InvoiceId": 1, "Amount": 100.0}],
        ),
    )
    by_stage = {item.stage: item for item in evidence.stages}
    assert evidence.status == "failed"
    assert by_stage["business_comparison"].status == "failed"
    assert by_stage["business_comparison"].issues[0].code == "COMPARE_RESULT_MISMATCH"
    assert by_stage["evidence_integrity"].status == "not_run"
    assert evidence.comparisons[0].differences


def test_no_snapshot_is_inconclusive_not_green():
    semantic, schema_catalog, schema_binding, reference, candidate = artifacts()
    evidence = validate_candidate_v3(
        semantic,
        schema_catalog,
        schema_binding,
        reference,
        candidate,
        executor=FakeExecutor([], [], snapshot_state="OFF"),
    )
    assert evidence.status == "inconclusive"
    assert evidence.validation_case["kind"] == "coverage"
    assert evidence.stages[0].status == "inconclusive"
    assert all(item.status == "not_run" for item in evidence.stages[1:])


def _reference_with_case(reference, case):
    payload = reference.model_dump(mode="python")
    payload["validation_cases"].append(case.model_dump(mode="python"))
    return ReferenceBundle.model_validate(payload)


def test_boundary_case_must_hit_data_to_pass():
    semantic, schema_catalog, schema_binding, reference, candidate = artifacts()
    reference = _reference_with_case(
        reference,
        ValidationCase(
            case_id="boundary_same_day",
            kind="boundary",
            parameters={
                "from_date": "1900-01-01",
                "to_date": "1900-01-01",
            },
        ),
    )
    candidate_payload = candidate.model_dump(mode="python")
    candidate_payload["reference_bundle_hash"] = reference.content_hash
    candidate = ProcedureCandidateV3.model_validate(candidate_payload)
    evidence = validate_candidate_v3(
        semantic,
        schema_catalog,
        schema_binding,
        reference,
        candidate,
        executor=FakeExecutor([], [], expected_from_date="1900-01-01"),
        case_id="boundary_same_day",
    )
    assert evidence.status == "inconclusive"
    assert evidence.stages[-2].issues[0].code == "COMPARE_COVERAGE_EMPTY"


def test_legal_empty_case_passes_only_when_both_sides_are_empty():
    semantic, schema_catalog, schema_binding, reference, candidate = artifacts()
    reference = _reference_with_case(
        reference,
        ValidationCase(
            case_id="empty_period",
            kind="empty",
            parameters={
                "from_date": "1900-01-01",
                "to_date": "1900-01-01",
            },
        ),
    )
    candidate_payload = candidate.model_dump(mode="python")
    candidate_payload["reference_bundle_hash"] = reference.content_hash
    candidate = ProcedureCandidateV3.model_validate(candidate_payload)
    evidence = validate_candidate_v3(
        semantic,
        schema_catalog,
        schema_binding,
        reference,
        candidate,
        executor=FakeExecutor([], [], expected_from_date="1900-01-01"),
        case_id="empty_period",
    )
    assert evidence.status == "validated"
    assert not evidence.coverage.effective


def test_empty_case_that_returns_rows_is_inconclusive():
    semantic, schema_catalog, schema_binding, reference, candidate = artifacts()
    reference = _reference_with_case(
        reference,
        ValidationCase(
            case_id="empty_period",
            kind="empty",
            parameters={
                "from_date": "1900-01-01",
                "to_date": "1900-01-01",
            },
        ),
    )
    candidate_payload = candidate.model_dump(mode="python")
    candidate_payload["reference_bundle_hash"] = reference.content_hash
    candidate = ProcedureCandidateV3.model_validate(candidate_payload)
    rows = [{"InvoiceId": 1, "Amount": 100.0}]
    evidence = validate_candidate_v3(
        semantic,
        schema_catalog,
        schema_binding,
        reference,
        candidate,
        executor=FakeExecutor(
            rows, rows, expected_from_date="1900-01-01",
        ),
        case_id="empty_period",
    )
    assert evidence.status == "inconclusive"
    assert (
        evidence.stages[-2].issues[0].code
        == "COMPARE_EMPTY_CASE_INEFFECTIVE"
    )


def test_reference_sql_cannot_be_changed_after_plan_is_frozen():
    semantic, schema_catalog, schema_binding, reference, candidate = artifacts()
    payload = reference.model_dump()
    payload["facts"][0]["expected_sql"] += "\n-- tampered"
    tampered = ReferenceBundle.model_validate(payload)
    candidate_payload = candidate.model_dump()
    candidate_payload["reference_bundle_hash"] = tampered.content_hash
    tampered_candidate = ProcedureCandidateV3.model_validate(candidate_payload)
    evidence = validate_candidate_v3(
        semantic,
        schema_catalog,
        schema_binding,
        tampered,
        tampered_candidate,
        executor=FakeExecutor([], []),
    )
    by_stage = {item.stage: item for item in evidence.stages}
    assert by_stage["reference_plan"].status == "failed"
    assert by_stage["procedure_plan"].status == "not_run"


def test_procedure_sql_cannot_diverge_from_procedure_plan():
    semantic, schema_catalog, schema_binding, reference, candidate = artifacts()
    payload = candidate.model_dump()
    payload["procedure_sql"] += "\n-- tampered"
    tampered = ProcedureCandidateV3.model_validate(payload)
    evidence = validate_candidate_v3(
        semantic,
        schema_catalog,
        schema_binding,
        reference,
        tampered,
        executor=FakeExecutor([], []),
    )
    by_stage = {item.stage: item for item in evidence.stages}
    assert by_stage["procedure_plan"].status == "failed"
    assert by_stage["procedure_compile"].status == "not_run"
