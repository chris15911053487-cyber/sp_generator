import hashlib

import pytest
from pydantic import ValidationError

from app.contracts.semantic_design import ResultContract, ResultOutputSpec
from app.contracts.semantic_design_state import SemanticDesignCheckpoint
from app.db import sqlite as sqlite_db


def _result_contract(name: str = "usp_Test") -> ResultContract:
    return ResultContract(
        procedure_name=name,
        purpose="测试语义设计检查点",
        result_mode="full_rows",
        outputs=[
            ResultOutputSpec(
                symbol="document_number",
                name="DocumentNumber",
                meaning="业务单据编号",
                logical_type="string",
                nullable=False,
            ),
        ],
        grain_output_symbols=["document_number"],
    )


def _checkpoint(session_id: str, *, decision_hash: str = "d" * 64):
    return SemanticDesignCheckpoint(
        checkpoint_id="semantic-checkpoint-1",
        session_id=session_id,
        decision_hash=decision_hash,
        stage="fact_blueprint",
        stage_input_hash=hashlib.sha256(b"result").hexdigest(),
        result_contract=_result_contract(),
        status="building_facts",
    )


def test_semantic_design_checkpoint_round_trip_and_stale_write(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(sqlite_db, "DB_PATH", str(tmp_path / "design.db"))
    sqlite_db.init_db()
    session = sqlite_db.create_session("semantic checkpoint")
    value = _checkpoint(session["id"])
    sqlite_db.save_semantic_design_checkpoint(value)

    loaded = sqlite_db.get_semantic_design_checkpoint(session["id"])
    assert loaded["result_contract"]["procedure_name"] == "usp_Test"
    assert loaded["status"] == "building_facts"

    with pytest.raises(ValueError, match="SEMANTIC_DESIGN_CHECKPOINT_STALE"):
        sqlite_db.save_semantic_design_checkpoint(
            value,
            expected_stage_input_hash="x" * 64,
        )


def test_decision_change_invalidates_all_design_products(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(sqlite_db, "DB_PATH", str(tmp_path / "invalidate.db"))
    sqlite_db.init_db()
    session = sqlite_db.create_session("semantic invalidate")
    sqlite_db.save_semantic_design_checkpoint(_checkpoint(session["id"]))

    assert sqlite_db.invalidate_semantic_design_checkpoint(
        session["id"],
        except_decision_hash="n" * 64,
    ) == 1
    loaded = sqlite_db.get_semantic_design_checkpoint(session["id"])
    assert loaded["status"] == "invalidated"


def test_checkpoint_repair_count_is_bounded():
    value = _checkpoint("session-1")
    with pytest.raises(
        ValueError, match="SEMANTIC_DESIGN_REPAIR_LIMIT_EXCEEDED",
    ):
        value.model_copy(
            update={"repair_counts": {"fact_blueprint": 2}},
        ).model_validate(
            value.model_copy(
                update={"repair_counts": {"fact_blueprint": 2}},
            ).model_dump(),
        )


def test_version_two_checkpoint_is_rejected_without_compatibility_branch():
    value = _checkpoint("session-legacy").model_dump(mode="json")
    value["version"] = 2
    with pytest.raises(ValidationError):
        SemanticDesignCheckpoint.model_validate(value)
