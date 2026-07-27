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

        CREATE TABLE IF NOT EXISTS session_designs (
            session_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            summary TEXT,
            decision_plan_json TEXT NOT NULL DEFAULT '{}',
            decision_hash TEXT,
            query_spec_draft_json TEXT,
            query_spec_json TEXT,
            query_spec_version INTEGER,
            verification_plan_json TEXT,
            verification_plan_hash TEXT,
            diagnostics_json TEXT NOT NULL DEFAULT '[]',
            schema_fingerprint TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
            verification_plan_json TEXT,
            verification_plan_hash TEXT,
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

        CREATE TABLE IF NOT EXISTS semantic_contracts_v3 (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            body_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id, content_hash),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS catalog_snapshots_v3 (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            body_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id, fingerprint),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS schema_bindings_v3 (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            catalog_fingerprint TEXT NOT NULL,
            body_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id, content_hash),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS schema_resolution_checkpoints_v3 (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            contract_id TEXT NOT NULL,
            design_hash TEXT NOT NULL,
            catalog_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            revision_count INTEGER NOT NULL DEFAULT 0,
            repair_count INTEGER NOT NULL DEFAULT 0,
            body_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id, contract_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS semantic_design_checkpoints_v3 (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            decision_hash TEXT NOT NULL,
            stage TEXT NOT NULL,
            stage_input_hash TEXT NOT NULL,
            result_contract_json TEXT,
            fact_blueprint_json TEXT,
            computation_blueprint_json TEXT,
            semantic_obligations_json TEXT,
            semantic_inputs_json TEXT,
            source_requirements_json TEXT,
            expression_design_json TEXT,
            compile_result_json TEXT,
            diagnostics_json TEXT NOT NULL DEFAULT '[]',
            repair_counts_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            body_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS reference_bundles_v3 (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            binding_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            body_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id, content_hash),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS procedure_candidates_v3 (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            binding_hash TEXT NOT NULL,
            reference_bundle_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            body_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id, content_hash),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS validation_runs_v3 (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            candidate_hash TEXT NOT NULL,
            reference_bundle_hash TEXT NOT NULL,
            catalog_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            body_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS validation_issues_v3 (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            code TEXT NOT NULL,
            status TEXT NOT NULL,
            body_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES validation_runs_v3(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS validation_differences_v3 (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            difference_type TEXT NOT NULL,
            body_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES validation_runs_v3(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_validation_runs_v3_session
            ON validation_runs_v3(session_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_validation_issues_v3_run
            ON validation_issues_v3(run_id, stage);
        CREATE INDEX IF NOT EXISTS idx_validation_differences_v3_run
            ON validation_differences_v3(run_id, fact_id);
        CREATE INDEX IF NOT EXISTS idx_schema_resolution_session_v3
            ON schema_resolution_checkpoints_v3(session_id, status);
        CREATE INDEX IF NOT EXISTS idx_semantic_design_session_v3
            ON semantic_design_checkpoints_v3(session_id, status);
    """)
    migrations = {
        "session_designs": {
            "query_spec_version": "INTEGER",
            "verification_plan_json": "TEXT",
            "verification_plan_hash": "TEXT",
        },
        "stored_procedures": {
            "parameters": "TEXT DEFAULT '[]'",
            "operation_type": "TEXT DEFAULT 'query'",
            "validated_hash": "TEXT",
            "deployed_hash": "TEXT",
            "deployed_at": "TIMESTAMP",
            "query_spec_json": "TEXT",
            "verification_plan_json": "TEXT",
            "verification_plan_hash": "TEXT",
            "schema_fingerprint": "TEXT",
            "bundle_hash": "TEXT",
        },
        "verify_queries": {
            "validation_spec": "TEXT DEFAULT '{}'",
        },
        "semantic_design_checkpoints_v3": {
            "computation_blueprint_json": "TEXT",
            "semantic_obligations_json": "TEXT",
            "semantic_inputs_json": "TEXT",
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


# --- Session Designs ---

def save_session_design(
    session_id: str,
    *,
    status: str,
    summary: str = "",
    decision_plan: dict | list | None = None,
    decision_hash: str | None = None,
    query_spec_draft: dict | None = None,
    query_spec: dict | None = None,
    query_spec_version: int | None = None,
    verification_plan: dict | list | None = None,
    verification_plan_hash: str | None = None,
    diagnostics: list | None = None,
    schema_fingerprint: str | None = None,
) -> dict:
    conn = _get_conn()
    conn.execute(
        """INSERT INTO session_designs
           (session_id, status, summary, decision_plan_json, decision_hash,
            query_spec_draft_json, query_spec_json, diagnostics_json,
            schema_fingerprint, query_spec_version, verification_plan_json,
            verification_plan_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
             status=excluded.status,
             summary=excluded.summary,
             decision_plan_json=excluded.decision_plan_json,
             decision_hash=excluded.decision_hash,
             query_spec_draft_json=excluded.query_spec_draft_json,
             query_spec_json=excluded.query_spec_json,
             diagnostics_json=excluded.diagnostics_json,
             schema_fingerprint=excluded.schema_fingerprint,
             query_spec_version=excluded.query_spec_version,
             verification_plan_json=excluded.verification_plan_json,
             verification_plan_hash=excluded.verification_plan_hash,
             updated_at=CURRENT_TIMESTAMP""",
        (
            session_id,
            status,
            summary,
            json.dumps(decision_plan or {}, ensure_ascii=False),
            decision_hash,
            (
                json.dumps(query_spec_draft, ensure_ascii=False)
                if query_spec_draft is not None else None
            ),
            (
                json.dumps(query_spec, ensure_ascii=False)
                if query_spec is not None else None
            ),
            json.dumps(diagnostics or [], ensure_ascii=False),
            schema_fingerprint,
            query_spec_version,
            (
                json.dumps(verification_plan, ensure_ascii=False)
                if verification_plan is not None else None
            ),
            verification_plan_hash,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM session_designs WHERE session_id = ?", (session_id,),
    ).fetchone()
    conn.close()
    return dict(row)


def get_session_design(session_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM session_designs WHERE session_id = ?", (session_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


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
               "query_spec_json", "verification_plan_json",
               "verification_plan_hash", "schema_fingerprint", "bundle_hash"}
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
    validated = bool(validation_result.get(
        "deployment_eligible",
        validation_result.get("syntax_ok")
        and validation_result.get("business_ok"),
    ))
    validated_hash = validation_result.get("bundle_hash") if validated else None
    status = (
        validation_result.get("status", "verified")
        if validated else validation_result.get("status", "verify_failed")
    )
    verify_result = (
        json.dumps(validation_result, ensure_ascii=False)
        if validation_result else None
    )
    syntax_ok = validation_result.get("syntax_ok")
    business_ok = validation_result.get("business_ok")
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
                   verification_plan_json = ?,
                   verification_plan_hash = ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (
                code,
                parameters,
                operation_type,
                status,
                None if syntax_ok is None else (1 if syntax_ok else 0),
                None if business_ok is None else (1 if business_ok else 0),
                verify_result,
                validated_hash,
                validation_result.get("bundle_hash"),
                validation_result.get("verification_plan_json"),
                validation_result.get("verification_plan_hash"),
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
            query_status = (
                "pass" if detail and detail.get("pass")
                else "fail" if detail and not detail.get("pass")
                else "pass" if validated
                else "pending"
            )
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

def _insert_candidate_bundle(
    conn: sqlite3.Connection,
    session_id: str,
    bundle,
    validation_result: dict | None = None,
) -> dict:
    """在调用方事务中插入候选包；校验状态只影响部署资格，不影响保存。"""
    from app.services.validation import compute_bundle_hash

    validation_result = validation_result or {}
    sp = bundle.sp_dict()
    queries = bundle.query_dicts()
    computed_hash = compute_bundle_hash(sp, queries)
    if bundle.bundle_hash and bundle.bundle_hash != computed_hash:
        raise ValueError(f"{sp['name']} 的 bundle_hash 与实际内容不一致")
    bundle_hash = computed_hash
    validated = bundle.status == "validated"
    syntax_ok = validation_result.get("syntax_ok", True if validated else None)
    business_ok = validation_result.get(
        "business_ok", True if validated else None,
    )
    status = {
        "validated": "persisted",
        "needs_review": "needs_review",
        "failed": "verify_failed",
    }.get(bundle.status, "draft")
    details_by_name = {
        item.get("query"): item
        for item in validation_result.get("details", [])
        if item.get("query")
    }
    sp_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO stored_procedures
           (id, session_id, name, code, status, syntax_valid, business_valid,
            verify_result, parameters, operation_type, validated_hash,
            query_spec_json, verification_plan_json, verification_plan_hash,
            schema_fingerprint, bundle_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sp_id,
            session_id,
            sp["name"],
            sp["code"],
            status,
            None if syntax_ok is None else (1 if syntax_ok else 0),
            None if business_ok is None else (1 if business_ok else 0),
            (
                json.dumps(validation_result, ensure_ascii=False)
                if validation_result else None
            ),
            sp["parameters"],
            sp["operation_type"],
            bundle_hash if validated else None,
            sp["query_spec_json"],
            sp.get("verification_plan_json"),
            sp.get("verification_plan_hash"),
            sp["schema_fingerprint"],
            bundle_hash,
        ),
    )
    for query in queries:
        query_id = str(uuid.uuid4())
        validation_spec = query.get("validation_spec", {})
        if not isinstance(validation_spec, str):
            validation_spec = json.dumps(validation_spec, ensure_ascii=False)
        detail = details_by_name.get(query.get("name"))
        query_status = (
            "pass" if detail and detail.get("pass")
            else "fail" if detail and not detail.get("pass")
            else "pass" if validated
            else "pending"
        )
        result_detail = (
            json.dumps(detail, ensure_ascii=False, indent=2)
            if detail is not None else None
        )
        conn.execute(
            """INSERT INTO verify_queries
               (id, sp_id, name, sql_code, compare_columns, validation_spec,
                status, result_detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                query_id,
                sp_id,
                query.get("name", "未命名校验"),
                query.get("sql_code", ""),
                query.get("compare_columns", ""),
                validation_spec,
                query_status,
                result_detail,
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


def replace_session_candidate_bundles_atomically(
    session_id: str,
    bundles: list,
    validation_results: list[dict],
) -> list[dict]:
    """原子保存最新整批候选，包括校验失败和待复核草稿。"""
    if not bundles:
        raise ValueError("候选包不能为空")
    if len(bundles) != len(validation_results):
        raise ValueError("候选包与校验结果数量不一致")

    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        inserted = [
            _insert_candidate_bundle(conn, session_id, bundle, result)
            for bundle, result in zip(bundles, validation_results)
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


# --- V3 immutable artifacts and validation evidence ---

def _insert_immutable_v3(
    conn: sqlite3.Connection,
    table: str,
    session_id: str,
    content_hash: str,
    columns: dict,
) -> str:
    artifact_id = str(uuid.uuid4())
    names = ["id", "session_id", "content_hash", *columns]
    placeholders = ", ".join("?" for _ in names)
    values = [artifact_id, session_id, content_hash, *columns.values()]
    conn.execute(
        f"""INSERT INTO {table} ({", ".join(names)})
            VALUES ({placeholders})
            ON CONFLICT(session_id, content_hash) DO NOTHING""",
        values,
    )
    row = conn.execute(
        f"SELECT id FROM {table} WHERE session_id = ? AND content_hash = ?",
        (session_id, content_hash),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"{table} 制品保存失败")
    return str(row["id"])


def save_v3_artifacts(
    session_id: str,
    *,
    semantic_contract,
    catalog_snapshot,
    schema_binding,
    reference_bundle,
    procedure_candidate,
) -> dict:
    """在一个事务中保存一条完整、不可变的 V3 制品链。"""
    from app.services.catalog_v3 import catalog_fingerprint

    fingerprint = catalog_fingerprint(catalog_snapshot)
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        contract_id = _insert_immutable_v3(
            conn,
            "semantic_contracts_v3",
            session_id,
            semantic_contract.content_hash,
            {
                "status": "confirmed",
                "body_json": semantic_contract.canonical_json(),
            },
        )
        snapshot_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO catalog_snapshots_v3
               (id, session_id, fingerprint, body_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(session_id, fingerprint) DO NOTHING""",
            (
                snapshot_id,
                session_id,
                fingerprint,
                catalog_snapshot.canonical_json(),
            ),
        )
        snapshot_row = conn.execute(
            """SELECT id FROM catalog_snapshots_v3
               WHERE session_id = ? AND fingerprint = ?""",
            (session_id, fingerprint),
        ).fetchone()
        binding_id = _insert_immutable_v3(
            conn,
            "schema_bindings_v3",
            session_id,
            schema_binding.content_hash,
            {
                "contract_hash": schema_binding.contract_hash,
                "catalog_fingerprint": schema_binding.catalog_fingerprint,
                "body_json": schema_binding.canonical_json(),
            },
        )
        reference_id = _insert_immutable_v3(
            conn,
            "reference_bundles_v3",
            session_id,
            reference_bundle.content_hash,
            {
                "contract_hash": reference_bundle.contract_hash,
                "binding_hash": reference_bundle.binding_hash,
                "status": reference_bundle.status,
                "body_json": reference_bundle.canonical_json(),
            },
        )
        candidate_id = _insert_immutable_v3(
            conn,
            "procedure_candidates_v3",
            session_id,
            procedure_candidate.content_hash,
            {
                "contract_hash": procedure_candidate.contract_hash,
                "binding_hash": procedure_candidate.binding_hash,
                "reference_bundle_hash": (
                    procedure_candidate.reference_bundle_hash
                ),
                "status": procedure_candidate.status,
                "body_json": procedure_candidate.canonical_json(),
            },
        )
        conn.execute(
            "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        conn.commit()
        return {
            "semantic_contract_id": contract_id,
            "catalog_snapshot_id": str(snapshot_row["id"]),
            "schema_binding_id": binding_id,
            "reference_bundle_id": reference_id,
            "procedure_candidate_id": candidate_id,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_semantic_design_checkpoint(
    checkpoint,
    *,
    expected_decision_hash: str | None = None,
    expected_stage_input_hash: str | None = None,
    expected_status: str | None = None,
) -> dict:
    """Atomically save one resumable semantic-design checkpoint per session."""
    from app.contracts.semantic_design_state import SemanticDesignCheckpoint

    value = SemanticDesignCheckpoint.model_validate(checkpoint)
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT decision_hash, stage_input_hash, status
               FROM semantic_design_checkpoints_v3
               WHERE session_id = ?""",
            (value.session_id,),
        ).fetchone()
        if existing is not None:
            expected = (
                ("decision_hash", expected_decision_hash),
                ("stage_input_hash", expected_stage_input_hash),
                ("status", expected_status),
            )
            stale = [
                name for name, expected_value in expected
                if (
                    expected_value is not None
                    and str(existing[name]) != expected_value
                )
            ]
            if stale:
                raise ValueError(
                    "SEMANTIC_DESIGN_CHECKPOINT_STALE: "
                    + ", ".join(stale)
                )

        def dump_optional(item) -> str | None:
            return item.canonical_json() if item is not None else None

        conn.execute(
            """INSERT INTO semantic_design_checkpoints_v3
               (id, session_id, decision_hash, stage, stage_input_hash,
                result_contract_json, fact_blueprint_json,
                computation_blueprint_json, semantic_obligations_json,
                semantic_inputs_json,
                source_requirements_json, expression_design_json,
                compile_result_json, diagnostics_json, repair_counts_json,
                status, body_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 id=excluded.id,
                 decision_hash=excluded.decision_hash,
                 stage=excluded.stage,
                 stage_input_hash=excluded.stage_input_hash,
                 result_contract_json=excluded.result_contract_json,
                 fact_blueprint_json=excluded.fact_blueprint_json,
                 computation_blueprint_json=excluded.computation_blueprint_json,
                 semantic_obligations_json=excluded.semantic_obligations_json,
                 semantic_inputs_json=excluded.semantic_inputs_json,
                 source_requirements_json=excluded.source_requirements_json,
                 expression_design_json=excluded.expression_design_json,
                 compile_result_json=excluded.compile_result_json,
                 diagnostics_json=excluded.diagnostics_json,
                 repair_counts_json=excluded.repair_counts_json,
                 status=excluded.status,
                 body_json=excluded.body_json,
                 updated_at=CURRENT_TIMESTAMP""",
            (
                value.checkpoint_id,
                value.session_id,
                value.decision_hash,
                value.stage,
                value.stage_input_hash,
                dump_optional(value.result_contract),
                dump_optional(value.fact_blueprint),
                dump_optional(value.computation_blueprint),
                dump_optional(value.semantic_obligations),
                dump_optional(value.semantic_inputs),
                dump_optional(value.source_requirements),
                dump_optional(value.expression_design),
                (
                    json.dumps(
                        value.compile_result,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if value.compile_result is not None else None
                ),
                json.dumps(
                    [
                        item.model_dump(mode="json")
                        for item in value.diagnostics
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    value.repair_counts,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                value.status,
                value.canonical_json(),
            ),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (value.session_id,),
        )
        conn.commit()
        return {"id": value.checkpoint_id, "status": value.status}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_semantic_design_checkpoint(session_id: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT body_json, updated_at
               FROM semantic_design_checkpoints_v3
               WHERE session_id = ?""",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row["body_json"])
        value["updated_at"] = row["updated_at"]
        return value
    finally:
        conn.close()


def invalidate_semantic_design_checkpoint(
    session_id: str,
    *,
    except_decision_hash: str | None = None,
) -> int:
    """Invalidate a checkpoint when its confirmed decisions are no longer current."""
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT id, decision_hash, status, body_json
               FROM semantic_design_checkpoints_v3
               WHERE session_id = ?""",
            (session_id,),
        ).fetchone()
        if (
            row is None
            or row["status"] == "invalidated"
            or (
                except_decision_hash is not None
                and row["decision_hash"] == except_decision_hash
            )
        ):
            conn.commit()
            return 0
        value = json.loads(row["body_json"])
        value["status"] = "invalidated"
        conn.execute(
            """UPDATE semantic_design_checkpoints_v3
               SET status = 'invalidated', body_json = ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                row["id"],
            ),
        )
        conn.commit()
        return 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_schema_resolution_checkpoint(
    checkpoint,
    *,
    expected_design_hash: str | None = None,
    expected_catalog_fingerprint: str | None = None,
    expected_status: str | None = None,
) -> dict:
    """Atomically save a resumable Schema checkpoint with stale-write checks."""
    from app.contracts.schema_resolution import SchemaResolutionCheckpoint

    value = SchemaResolutionCheckpoint.model_validate(checkpoint)
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT design_hash, catalog_fingerprint, status
               FROM schema_resolution_checkpoints_v3
               WHERE session_id = ? AND contract_id = ?""",
            (value.session_id, value.contract_id),
        ).fetchone()
        if existing is not None:
            expected = (
                ("design_hash", expected_design_hash),
                ("catalog_fingerprint", expected_catalog_fingerprint),
                ("status", expected_status),
            )
            stale = [
                name for name, expected_value in expected
                if (
                    expected_value is not None
                    and str(existing[name]) != expected_value
                )
            ]
            if stale:
                raise ValueError(
                    "SCHEMA_CHECKPOINT_STALE: " + ", ".join(stale)
                )
        conn.execute(
            """INSERT INTO schema_resolution_checkpoints_v3
               (id, session_id, contract_id, design_hash,
                catalog_fingerprint, status, revision_count, repair_count,
                body_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, contract_id) DO UPDATE SET
                 id=excluded.id,
                 design_hash=excluded.design_hash,
                 catalog_fingerprint=excluded.catalog_fingerprint,
                 status=excluded.status,
                 revision_count=excluded.revision_count,
                 repair_count=excluded.repair_count,
                 body_json=excluded.body_json,
                 updated_at=CURRENT_TIMESTAMP""",
            (
                value.checkpoint_id,
                value.session_id,
                value.contract_id,
                value.design_hash,
                value.catalog_fingerprint,
                value.status,
                value.revision_count,
                value.repair_count,
                value.canonical_json(),
            ),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (value.session_id,),
        )
        conn.commit()
        return {
            "id": value.checkpoint_id,
            "status": value.status,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_schema_resolution_checkpoint(
    session_id: str,
    contract_id: str,
) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT body_json, updated_at
               FROM schema_resolution_checkpoints_v3
               WHERE session_id = ? AND contract_id = ?""",
            (session_id, contract_id),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row["body_json"])
        value["updated_at"] = row["updated_at"]
        return value
    finally:
        conn.close()


def list_schema_resolution_checkpoints(session_id: str) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT body_json, updated_at
               FROM schema_resolution_checkpoints_v3
               WHERE session_id = ?
               ORDER BY created_at, rowid""",
            (session_id,),
        ).fetchall()
        result = []
        for row in rows:
            value = json.loads(row["body_json"])
            value["updated_at"] = row["updated_at"]
            result.append(value)
        return result
    finally:
        conn.close()


def invalidate_schema_resolution_checkpoints(
    session_id: str,
    *,
    except_design_hash: str | None = None,
    except_catalog_fingerprint: str | None = None,
) -> int:
    """Invalidate checkpoints that cannot be safely resumed."""
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT id, body_json
               FROM schema_resolution_checkpoints_v3
               WHERE session_id = ? AND status != 'invalidated'""",
            (session_id,),
        ).fetchall()
        changed = 0
        for row in rows:
            value = json.loads(row["body_json"])
            still_current = (
                (
                    except_design_hash is None
                    or value["design_hash"] == except_design_hash
                )
                and (
                    except_catalog_fingerprint is None
                    or value["catalog_fingerprint"]
                    == except_catalog_fingerprint
                )
            )
            if still_current:
                continue
            value["status"] = "invalidated"
            conn.execute(
                """UPDATE schema_resolution_checkpoints_v3
                   SET status = 'invalidated', body_json = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    row["id"],
                ),
            )
            changed += 1
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_v3_validation_run(session_id: str, evidence) -> dict:
    """保存 Gate、错误和行级差异；失败证据与成功证据同等持久化。"""
    run_id = str(uuid.uuid4())
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO validation_runs_v3
               (id, session_id, candidate_hash, reference_bundle_hash,
                catalog_fingerprint, status, body_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                session_id,
                evidence.candidate_hash,
                evidence.reference_bundle_hash,
                evidence.catalog_fingerprint,
                evidence.status,
                evidence.canonical_json(),
            ),
        )
        for stage in evidence.stages:
            for item in stage.issues:
                conn.execute(
                    """INSERT INTO validation_issues_v3
                       (id, run_id, stage, code, status, body_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        item.issue_id,
                        run_id,
                        item.stage,
                        item.code,
                        item.status,
                        item.canonical_json(),
                    ),
                )
        for comparison in evidence.comparisons:
            groups = {
                "missing": comparison.missing,
                "extra": comparison.extra,
                "duplicate_key": comparison.duplicate_keys,
                "value_difference": comparison.differences,
            }
            for difference_type, rows in groups.items():
                for row in rows:
                    conn.execute(
                        """INSERT INTO validation_differences_v3
                           (id, run_id, fact_id, difference_type, body_json)
                           VALUES (?, ?, ?, ?, ?)""",
                        (
                            str(uuid.uuid4()),
                            run_id,
                            comparison.fact_id,
                            difference_type,
                            json.dumps(row, ensure_ascii=False),
                        ),
                    )
        conn.execute(
            "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        conn.commit()
        return {"id": run_id, "status": evidence.status}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_latest_v3_validation_run(session_id: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT id, status, body_json, created_at
               FROM validation_runs_v3
               WHERE session_id = ?
               ORDER BY created_at DESC, rowid DESC
               LIMIT 1""",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        result = json.loads(row["body_json"])
        result["run_id"] = row["id"]
        result["persisted_at"] = row["created_at"]
        return result
    finally:
        conn.close()


def get_v3_deployment_chain(
    session_id: str,
    candidate_hash: str,
) -> dict | None:
    conn = _get_conn()
    try:
        candidate_row = conn.execute(
            """SELECT body_json FROM procedure_candidates_v3
               WHERE session_id = ? AND content_hash = ?""",
            (session_id, candidate_hash),
        ).fetchone()
        if candidate_row is None:
            return None
        candidate = json.loads(candidate_row["body_json"])
        lookups = {
            "semantic_contract": (
                "semantic_contracts_v3",
                candidate["contract_hash"],
            ),
            "schema_binding": (
                "schema_bindings_v3",
                candidate["binding_hash"],
            ),
            "reference_bundle": (
                "reference_bundles_v3",
                candidate["reference_bundle_hash"],
            ),
        }
        result = {"procedure_candidate": candidate}
        for name, (table, content_hash) in lookups.items():
            row = conn.execute(
                f"""SELECT body_json FROM {table}
                    WHERE session_id = ? AND content_hash = ?""",
                (session_id, content_hash),
            ).fetchone()
            if row is None:
                return None
            result[name] = json.loads(row["body_json"])
        validation = conn.execute(
            """SELECT body_json FROM validation_runs_v3
               WHERE session_id = ? AND candidate_hash = ?
                 AND status = 'validated'
               ORDER BY created_at DESC, rowid DESC
               LIMIT 1""",
            (session_id, candidate_hash),
        ).fetchone()
        if validation is None:
            return None
        result["validation_evidence"] = json.loads(validation["body_json"])
        return result
    finally:
        conn.close()
