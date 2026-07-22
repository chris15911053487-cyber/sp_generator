"""SQLite 持久化层 — 会话、消息、存储过程、校验 SQL。"""
import json
import sqlite3
import uuid
from config import DB_PATH


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = _get_conn()
    # WAL 模式 + busy_timeout：支持多线程并发写入不报 database is locked
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS stored_procedures (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            name TEXT NOT NULL,
            code TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            syntax_valid INTEGER DEFAULT 0,
            business_valid INTEGER DEFAULT 0,
            verify_result TEXT,
            parameters TEXT DEFAULT '[]',
            operation_type TEXT DEFAULT 'query',
            validated_hash TEXT,
            deployed_hash TEXT,
            deployed_at TIMESTAMP,
            query_spec_json TEXT,
            schema_fingerprint TEXT,
            bundle_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS verify_queries (
            id TEXT PRIMARY KEY,
            sp_id TEXT NOT NULL,
            name TEXT NOT NULL,
            sql_code TEXT NOT NULL,
            compare_columns TEXT,
            validation_spec TEXT DEFAULT '{}',
            status TEXT DEFAULT 'pending',
            result_detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (sp_id) REFERENCES stored_procedures(id) ON DELETE CASCADE
        );
    """)
    migrations = {
        "stored_procedures": {
            "parameters": "TEXT DEFAULT '[]'",
            "operation_type": "TEXT DEFAULT 'query'",
            "validated_hash": "TEXT",
            "deployed_hash": "TEXT",
            "deployed_at": "TIMESTAMP",
            "query_spec_json": "TEXT",
            "schema_fingerprint": "TEXT",
            "bundle_hash": "TEXT",
        },
        "verify_queries": {
            "validation_spec": "TEXT DEFAULT '{}'",
        },
    }
    for table, columns in migrations.items():
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )
    conn.commit()
    conn.close()


# --- Sessions ---

def create_session(name: str) -> dict:
    conn = _get_conn()
    sid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (id, name) VALUES (?, ?)", (sid, name)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    conn.close()
    return dict(row)


def get_sessions() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_session(session_id: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()


# --- Messages ---

def save_message(session_id: str, role: str, content: str) -> dict:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
        (session_id, role, content),
    )
    conn.execute(
        "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (session_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM messages WHERE id = last_insert_rowid()"
    ).fetchone()
    conn.close()
    return dict(row)


def get_messages(session_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Stored Procedures ---

def save_sp(session_id: str, name: str, code: str, parameters: str = '[]',
            operation_type: str = 'query') -> dict:
    conn = _get_conn()
    sp_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO stored_procedures
           (id, session_id, name, code, parameters, operation_type)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (sp_id, session_id, name, code, parameters, operation_type),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM stored_procedures WHERE id = ?", (sp_id,)
    ).fetchone()
    conn.close()
    return dict(row)


def get_sps(session_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM stored_procedures WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_sp(sp_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM stored_procedures WHERE id = ?", (sp_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_sp(sp_id: str, **kwargs) -> None:
    allowed = {"name", "code", "status", "syntax_valid",
               "business_valid", "verify_result", "parameters", "deployed_at",
               "operation_type", "validated_hash", "deployed_hash",
               "query_spec_json", "schema_fingerprint", "bundle_hash"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    set_parts = []
    params = []
    for k, v in updates.items():
        set_parts.append(f"{k} = ?")
        params.append(v)
    set_parts.append("updated_at = CURRENT_TIMESTAMP")
    params.append(sp_id)
    conn = _get_conn()
    conn.execute(
        f"UPDATE stored_procedures SET {', '.join(set_parts)} WHERE id = ?",
        params,
    )
    conn.commit()
    conn.close()


def delete_sp(sp_id: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM stored_procedures WHERE id = ?", (sp_id,))
    conn.commit()
    conn.close()


def delete_sps_by_session(session_id: str) -> int:
    """删除指定会话下所有 SP（级联删除校验 SQL）。返回删除数量。"""
    conn = _get_conn()
    cursor = conn.execute(
        "DELETE FROM stored_procedures WHERE session_id = ?", (session_id,)
    )
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


def delete_sps_except(session_id: str, keep_ids: list) -> int:
    """删除指定会话下除 keep_ids 外的所有 SP（级联删除校验 SQL）。

    用于"先保存新 SP 再删除旧 SP"：新 SP 已写入后再清旧 SP，
    避免代码重新生成期间右侧列表变空。
    """
    conn = _get_conn()
    if keep_ids:
        placeholders = ",".join("?" * len(keep_ids))
        cursor = conn.execute(
            f"DELETE FROM stored_procedures WHERE session_id = ? AND id NOT IN ({placeholders})",
            [session_id] + list(keep_ids),
        )
    else:
        cursor = conn.execute(
            "DELETE FROM stored_procedures WHERE session_id = ?", (session_id,)
        )
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


# --- Verify Queries ---

def save_verify_query(sp_id: str, name: str, sql_code: str,
                      compare_columns: str = "", validation_spec: str = "{}") -> dict:
    conn = _get_conn()
    vq_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO verify_queries
           (id, sp_id, name, sql_code, compare_columns, validation_spec)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (vq_id, sp_id, name, sql_code, compare_columns, validation_spec),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM verify_queries WHERE id = ?", (vq_id,)
    ).fetchone()
    conn.close()
    return dict(row)


def get_verify_queries(sp_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM verify_queries WHERE sp_id = ? ORDER BY created_at",
        (sp_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_verify_query(query_id: str, **kwargs) -> None:
    allowed = {"name", "sql_code", "compare_columns", "validation_spec",
               "status", "result_detail"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    set_parts = []
    params = []
    for k, v in updates.items():
        set_parts.append(f"{k} = ?")
        params.append(v)
    params.append(query_id)
    conn = _get_conn()
    conn.execute(
        f"UPDATE verify_queries SET {', '.join(set_parts)} WHERE id = ?",
        params,
    )
    conn.commit()
    conn.close()


def save_sp_bundle(
    sp_id: str,
    code: str,
    parameters: str,
    operation_type: str,
    verify_queries: list[dict],
    validation_result: dict | None = None,
) -> dict:
    """在一个事务中保存 SP、Oracle 和可选的已通过校验状态。"""
    validation_result = validation_result or {}
    validated = bool(
        validation_result.get("syntax_ok")
        and validation_result.get("business_ok")
    )
    validated_hash = validation_result.get("bundle_hash") if validated else None
    status = validation_result.get("status", "verified") if validated else "draft"
    verify_result = (
        json.dumps(validation_result, ensure_ascii=False) if validated else None
    )
    details_by_id = {
        item.get("query_id"): item
        for item in validation_result.get("details", [])
        if item.get("query_id")
    }
    details_by_name = {
        item.get("query"): item
        for item in validation_result.get("details", [])
        if item.get("query")
    }

    conn = _get_conn()
    try:
        cursor = conn.execute(
            """UPDATE stored_procedures
               SET code = ?, parameters = ?, operation_type = ?, status = ?,
                   syntax_valid = ?, business_valid = ?, verify_result = ?,
                   validated_hash = ?, bundle_hash = ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (
                code,
                parameters,
                operation_type,
                status,
                1 if validated else 0,
                1 if validated else 0,
                verify_result,
                validated_hash,
                validated_hash,
                sp_id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError("SP 不存在")

        existing_ids = {
            row["id"] for row in conn.execute(
                "SELECT id FROM verify_queries WHERE sp_id = ?", (sp_id,)
            ).fetchall()
        }
        kept_ids = set()
        for item in verify_queries:
            query_id = item.get("id")
            validation_spec = item.get("validation_spec", "{}")
            if not isinstance(validation_spec, str):
                validation_spec = json.dumps(validation_spec, ensure_ascii=False)
            detail = (
                details_by_id.get(query_id)
                or details_by_name.get(item.get("name"))
            )
            query_status = "pass" if validated else "pending"
            result_detail = (
                json.dumps(detail, ensure_ascii=False, indent=2)
                if detail is not None else None
            )
            values = (
                item.get("name", "未命名校验"),
                item.get("sql_code", ""),
                item.get("compare_columns", ""),
                validation_spec,
                query_status,
                result_detail,
            )
            if query_id in existing_ids:
                conn.execute(
                    """UPDATE verify_queries
                       SET name = ?, sql_code = ?, compare_columns = ?,
                           validation_spec = ?, status = ?, result_detail = ?
                       WHERE id = ? AND sp_id = ?""",
                    values + (query_id, sp_id),
                )
                kept_ids.add(query_id)
            else:
                query_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO verify_queries
                       (id, sp_id, name, sql_code, compare_columns,
                        validation_spec, status, result_detail)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (query_id, sp_id) + values,
                )
                kept_ids.add(query_id)

        for query_id in existing_ids - kept_ids:
            conn.execute("DELETE FROM verify_queries WHERE id = ?", (query_id,))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM stored_procedures WHERE id = ?", (sp_id,)
        ).fetchone()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _insert_candidate_bundle(conn: sqlite3.Connection, session_id: str, bundle) -> dict:
    """在调用方事务中插入一个已验证候选包。"""
    from app.services.validation import compute_bundle_hash

    sp = bundle.sp_dict()
    queries = bundle.query_dicts()
    computed_hash = compute_bundle_hash(sp, queries)
    if bundle.bundle_hash and bundle.bundle_hash != computed_hash:
        raise ValueError(f"{sp['name']} 的 bundle_hash 与实际内容不一致")
    bundle_hash = computed_hash
    sp_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO stored_procedures
           (id, session_id, name, code, status, syntax_valid, business_valid,
            parameters, operation_type, validated_hash, query_spec_json,
            schema_fingerprint, bundle_hash)
           VALUES (?, ?, ?, ?, 'persisted', 1, 1, ?, ?, ?, ?, ?, ?)""",
        (
            sp_id,
            session_id,
            sp["name"],
            sp["code"],
            sp["parameters"],
            sp["operation_type"],
            bundle_hash,
            sp["query_spec_json"],
            sp["schema_fingerprint"],
            bundle_hash,
        ),
    )
    for query in queries:
        query_id = str(uuid.uuid4())
        validation_spec = query.get("validation_spec", {})
        if not isinstance(validation_spec, str):
            validation_spec = json.dumps(validation_spec, ensure_ascii=False)
        conn.execute(
            """INSERT INTO verify_queries
               (id, sp_id, name, sql_code, compare_columns, validation_spec,
                status, result_detail)
               VALUES (?, ?, ?, ?, ?, ?, 'pass', NULL)""",
            (
                query_id,
                sp_id,
                query.get("name", "未命名校验"),
                query.get("sql_code", ""),
                query.get("compare_columns", ""),
                validation_spec,
            ),
        )
    row = conn.execute(
        "SELECT * FROM stored_procedures WHERE id = ?", (sp_id,)
    ).fetchone()
    return dict(row)


def replace_session_sp_bundles_atomically(session_id: str, bundles: list) -> list[dict]:
    """整批候选全部写入成功后替换旧产物；异常时完整回滚。"""
    if not bundles:
        raise ValueError("候选包不能为空")
    invalid = [
        bundle.procedure_spec.name
        for bundle in bundles
        if bundle.status != "validated"
    ]
    if invalid:
        raise ValueError("存在未通过全部闸门的候选: " + ", ".join(invalid))

    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        inserted = [
            _insert_candidate_bundle(conn, session_id, bundle)
            for bundle in bundles
        ]
        new_ids = [item["id"] for item in inserted]
        placeholders = ",".join("?" for _ in new_ids)
        conn.execute(
            f"""DELETE FROM stored_procedures
                WHERE session_id = ? AND id NOT IN ({placeholders})""",
            [session_id, *new_ids],
        )
        conn.execute(
            "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
