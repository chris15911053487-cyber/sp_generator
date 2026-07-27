"""从已确认并持久化的 SemanticDesign 继续真实生成与校验。"""

from __future__ import annotations

import json
import os
import sys

from app.agent.nodes import _generate_node_v3, _get_llm, _verify_node_v3
from app.contracts.semantic import SemanticDesign
from app.db.sqlite import (
    get_schema_resolution_checkpoint,
    get_session_design,
)
from app.db.sqlserver import get_connection
from app.services.schema_binding_v3 import SchemaBindingError
from app.services.schema_binding_v3 import build_schema_binding
from app.services.catalog_v3 import capture_catalog_snapshot, catalog_fingerprint
from app.contracts.schema import SchemaBindingProposal
from config import get_db_config, is_explicit_test_database
from scripts.run_ar_invoice_e2e_guarded import _snapshot_state


def _writer(event: dict) -> None:
    print(
        "PROGRESS " + json.dumps(
            {
                key: value for key, value in event.items()
                if key in {"stage", "status", "message", "error"}
            },
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(
            encoding="utf-8",
            errors="backslashreplace",
        )
    if os.getenv("RUN_V3_E2E") != "1":
        raise RuntimeError("必须显式设置 RUN_V3_E2E=1")
    session_id = str(os.getenv("E2E_RESUME_SESSION") or "").strip()
    if not session_id:
        raise RuntimeError("必须设置 E2E_RESUME_SESSION")
    design_record = get_session_design(session_id)
    if not design_record or not design_record.get("query_spec_json"):
        raise RuntimeError("指定会话没有已确认可用的 SemanticDesign")
    semantic_design = SemanticDesign.model_validate_json(
        design_record["query_spec_json"]
    )

    db_config = get_db_config()
    if not is_explicit_test_database(db_config):
        raise RuntimeError("只允许对明确标记为 test 的数据库执行")
    database = str(db_config["database"])
    escaped_database = database.replace("]", "]]")
    connection = get_connection(autocommit=True)
    cursor = connection.cursor()
    original_state = _snapshot_state(cursor, database)
    changed = original_state == "OFF"
    print(f"SNAPSHOT_ORIGINAL {original_state}", flush=True)
    try:
        if changed:
            cursor.execute(
                f"ALTER DATABASE [{escaped_database}] "
                "SET ALLOW_SNAPSHOT_ISOLATION ON"
            )
        print(
            f"SNAPSHOT_E2E {_snapshot_state(cursor, database)}",
            flush=True,
        )
        current_catalog = capture_catalog_snapshot()
        current_fingerprint = catalog_fingerprint(current_catalog)
        schema_artifacts = []
        for contract in semantic_design.contracts:
            checkpoint = get_schema_resolution_checkpoint(
                session_id, contract.contract_id,
            )
            if not checkpoint or checkpoint.get("status") != "resolved":
                raise RuntimeError(
                    f"{contract.contract_id} 没有已冻结的 Schema checkpoint"
                )
            if checkpoint["catalog_fingerprint"] != current_fingerprint:
                raise RuntimeError(
                    f"{contract.contract_id} 的 Schema checkpoint 已过期"
                )
            proposal = SchemaBindingProposal.model_validate(
                checkpoint["partial_proposal"]
            )
            binding = build_schema_binding(
                contract, current_catalog, proposal,
            )
            schema_artifacts.append({
                "contract_id": contract.contract_id,
                "catalog_snapshot": current_catalog.model_dump(
                    mode="json", by_alias=True,
                ),
                "schema_binding": binding.model_dump(
                    mode="json", by_alias=True,
                ),
            })
        state = {
            "session_id": session_id,
            "schema_artifacts": schema_artifacts,
        }
        try:
            generated = _generate_node_v3(
                state,
                semantic_design,
                _get_llm(),
                _writer,
            )
        except SchemaBindingError as exc:
            print(
                "SCHEMA_BINDING_ERROR " + json.dumps(
                    {
                        "code": exc.code,
                        "message": str(exc),
                        "evidence": exc.evidence,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                flush=True,
            )
            raise
        except Exception as exc:
            print(
                "GENERATION_ERROR " + json.dumps(
                    {
                        "code": getattr(exc, "code", type(exc).__name__),
                        "message": str(exc),
                        "evidence": getattr(exc, "evidence", {}),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                flush=True,
            )
            raise
        if generated.get("status") != "candidate_generated":
            print(
                "RESUME_RESULT " + json.dumps(
                    {
                        "status": generated.get("status"),
                        "error": generated.get("error"),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                flush=True,
            )
            return 2
        state.update(generated)
        verified = _verify_node_v3(state, _writer)
        print(
            "RESUME_RESULT " + json.dumps(
                {
                    "status": verified.get("status"),
                    "error": verified.get("error"),
                    "verify_results": verified.get("verify_results"),
                },
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )
        return 0 if verified.get("status") == "persisted" else 2
    finally:
        if changed:
            cursor.execute(
                f"ALTER DATABASE [{escaped_database}] "
                "SET ALLOW_SNAPSHOT_ISOLATION OFF"
            )
        print(
            f"SNAPSHOT_RESTORED {_snapshot_state(cursor, database)}",
            flush=True,
        )
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
