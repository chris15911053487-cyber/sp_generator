"""在测试库临时启用 Snapshot Isolation，执行一次 E2E，并恢复原状态。"""

from __future__ import annotations

import os

from app.db.sqlserver import get_connection
from config import get_db_config, is_explicit_test_database
from scripts.run_ar_invoice_e2e import main as run_e2e


def _snapshot_state(cursor, database: str) -> str:
    cursor.execute(
        """
        SELECT snapshot_isolation_state_desc
        FROM sys.databases
        WHERE name = ?
        """,
        database,
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("无法读取测试数据库 Snapshot Isolation 状态")
    return str(row[0]).upper()


def main() -> int:
    config = get_db_config()
    if not is_explicit_test_database(config):
        raise RuntimeError("只允许对明确标记为 test 的数据库执行")
    database = str(config["database"])
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
        current_state = _snapshot_state(cursor, database)
        print(f"SNAPSHOT_E2E {current_state}", flush=True)
        if current_state != "ON":
            raise RuntimeError(
                f"Snapshot Isolation 未进入 ON，当前为 {current_state}"
            )
        os.environ["RUN_V3_E2E"] = "1"
        return run_e2e()
    finally:
        if changed:
            cursor.execute(
                f"ALTER DATABASE [{escaped_database}] "
                "SET ALLOW_SNAPSHOT_ISOLATION OFF"
            )
        restored_state = _snapshot_state(cursor, database)
        print(f"SNAPSHOT_RESTORED {restored_state}", flush=True)
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
