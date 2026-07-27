from app.contracts.schema_resolution import SchemaResolutionCheckpoint
from app.db import sqlite as sqlite_db


def _checkpoint(session_id, *, design_hash="d" * 64, status="proposing"):
    return SchemaResolutionCheckpoint(
        checkpoint_id="checkpoint-1",
        session_id=session_id,
        contract_id="contract-1",
        design_hash=design_hash,
        catalog_fingerprint="c" * 64,
        status=status,
    )


def test_checkpoint_round_trip_and_stale_write(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_db, "DB_PATH", str(tmp_path / "checkpoint.db"))
    sqlite_db.init_db()
    session = sqlite_db.create_session("checkpoint")
    value = _checkpoint(session["id"])
    sqlite_db.save_schema_resolution_checkpoint(value)

    loaded = sqlite_db.get_schema_resolution_checkpoint(
        session["id"], "contract-1",
    )
    assert loaded["design_hash"] == "d" * 64
    assert loaded["status"] == "proposing"

    updated = value.model_copy(update={"status": "failed"})
    try:
        sqlite_db.save_schema_resolution_checkpoint(
            updated,
            expected_design_hash="x" * 64,
        )
    except ValueError as exc:
        assert "SCHEMA_CHECKPOINT_STALE" in str(exc)
    else:
        raise AssertionError("陈旧 checkpoint 写入必须失败")


def test_design_change_invalidates_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_db, "DB_PATH", str(tmp_path / "invalidate.db"))
    sqlite_db.init_db()
    session = sqlite_db.create_session("invalidate")
    sqlite_db.save_schema_resolution_checkpoint(_checkpoint(session["id"]))

    changed = sqlite_db.invalidate_schema_resolution_checkpoints(
        session["id"],
        except_design_hash="n" * 64,
        except_catalog_fingerprint="c" * 64,
    )

    loaded = sqlite_db.get_schema_resolution_checkpoint(
        session["id"], "contract-1",
    )
    assert changed == 1
    assert loaded["status"] == "invalidated"
