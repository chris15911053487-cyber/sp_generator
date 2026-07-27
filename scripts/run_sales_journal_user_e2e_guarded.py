"""从自然语言开始模拟用户完成销售收入与财务凭证对账 E2E。"""

from __future__ import annotations

import json
import os
import sys
from fnmatch import fnmatchcase

from langgraph.types import Command

from app.agent.graph import create_graph
from app.db.sqlite import create_session, init_db
from app.db.sqlserver import get_connection
from config import get_db_config, is_explicit_test_database
from scripts.run_ar_invoice_e2e_guarded import _snapshot_state


USER_REQUEST = os.getenv(
    "E2E_USER_REQUEST",
    "我现在要做一个销售收入统计和财务凭证比对的存储过程",
)
CLARIFY_ANSWERS = json.loads(
    os.getenv("E2E_CLARIFY_ANSWERS", "{}")
)
SCHEMA_COLUMN_CHOICES = json.loads(
    os.getenv("E2E_SCHEMA_COLUMN_CHOICES", "{}")
)


def _interrupt_value(snapshot):
    for task in snapshot.tasks or []:
        for item in task.interrupts or []:
            return item.value
    return None


def _recommended_clarify_answer(values: dict) -> str:
    pending = values.get("pending_clarify") or {}
    decision_key = pending.get("decision_key")
    if decision_key in CLARIFY_ANSWERS:
        return str(CLARIFY_ANSWERS[decision_key])
    plan = values.get("decision_plan") or {}
    decision = next(
        (
            item for item in plan.get("decisions", [])
            if item.get("decision_key") == decision_key
        ),
        {},
    )
    recommended = decision.get("recommended_option_id")
    option_ids = [
        item.get("id") for item in pending.get("options", [])
        if item.get("id")
    ]
    if recommended in option_ids:
        return str(recommended)
    if option_ids:
        return str(option_ids[0])
    raise RuntimeError(f"澄清项 {decision_key} 没有可选择的业务口径")


def _desired_schema_column(semantic_id: str) -> str | None:
    exact = SCHEMA_COLUMN_CHOICES.get(semantic_id)
    if exact:
        return str(exact)
    matching_patterns = [
        str(value)
        for pattern, value in SCHEMA_COLUMN_CHOICES.items()
        if (
            ("*" in pattern or "?" in pattern)
            and fnmatchcase(
                semantic_id.casefold(),
                str(pattern).casefold(),
            )
        )
    ]
    return (
        matching_patterns[0]
        if len(set(matching_patterns)) == 1
        else None
    )


def _schema_choice_response(interrupt_value: dict) -> dict:
    selections = {}
    for issue in interrupt_value.get("issues", []):
        semantic_id = str(issue.get("semantic_id") or "")
        desired_column = _desired_schema_column(semantic_id)
        if not desired_column:
            raise RuntimeError(
                "E2E 遇到未配置的真实物理歧义："
                + json.dumps(
                    {
                        "semantic_id": semantic_id,
                        "candidates": [
                            item.get(
                                "physical_binding_fragment",
                                {},
                            ).get("column")
                            for item in issue.get(
                                "physical_candidates",
                                [],
                            )
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        matches = [
            item
            for item in issue.get("physical_candidates", [])
            if str(
                item.get("physical_binding_fragment", {}).get("column")
                or ""
            ).casefold() == str(desired_column).casefold()
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"E2E Schema 选择 {semantic_id}={desired_column} "
                "没有唯一候选"
            )
        selections[str(issue["issue_id"])] = str(
            matches[0]["candidate_id"]
        )
    return {
        "checkpoint_id": interrupt_value["checkpoint_id"],
        "selections": selections,
    }


def _run_user_flow() -> dict:
    init_db()
    session = create_session("E2E-user-sales-vs-journal")
    session_id = session["id"]
    graph = create_graph()
    config = {
        "configurable": {"thread_id": session_id},
        "recursion_limit": 80,
    }
    initial_state = {
        "session_id": session_id,
        "user_input": USER_REQUEST,
        "mode": "clarify",
        "requirements": "",
        "confirmed_assumptions": "",
        "design": "",
        "sp_list": [],
        "verify_results": [],
        "status": "",
        "error": "",
        "clarify_count": 0,
        "design_phase": None,
        "last_feedback_reply": "",
        "query_spec": {},
        "schema_fingerprint": "",
        "clarify_decisions": [],
        "deferred_decisions": [],
        "pending_clarify": None,
        "decision_plan": {},
        "confirmed_decision_set": {},
    }
    graph.invoke(initial_state, config)

    interactions = []
    for _ in range(20):
        snapshot = graph.get_state(config)
        interrupt_value = _interrupt_value(snapshot)
        if interrupt_value is None:
            values = dict(snapshot.values)
            return {
                "session_id": session_id,
                "interactions": interactions,
                "state": values,
            }

        interrupt_type = interrupt_value.get("type")
        if interrupt_type == "clarify":
            answer = _recommended_clarify_answer(snapshot.values)
            interactions.append({
                "type": "clarify",
                "decision_key": interrupt_value.get("decision_key"),
                "question": interrupt_value.get("question"),
                "answer": answer,
            })
            resume = answer
        elif interrupt_type == "assumptions":
            keys = [
                item["key"]
                for item in interrupt_value.get("assumptions", [])
            ]
            interactions.append({
                "type": "assumptions",
                "confirmed": keys,
                "values": interrupt_value.get("assumptions", []),
            })
            resume = {"confirmed": keys, "modified": {}}
        elif interrupt_type == "design":
            interactions.append({
                "type": "design",
                "action": "confirm",
                "content": interrupt_value.get("content"),
            })
            resume = {"action": "confirm"}
        elif interrupt_type == "design_revision":
            interactions.append({
                "type": "design_revision",
                "action": "confirm",
                "content": interrupt_value.get("content"),
            })
            resume = {"action": "confirm"}
        elif interrupt_type == "schema_choice":
            resume = _schema_choice_response(interrupt_value)
            interactions.append({
                "type": "schema_choice",
                "checkpoint_id": interrupt_value.get("checkpoint_id"),
                "selections": resume["selections"],
            })
        else:
            raise RuntimeError(f"未知用户交互类型: {interrupt_type}")
        graph.invoke(Command(resume=resume), config)
    raise RuntimeError("用户流程超过 20 次交互，疑似状态图未收敛")


def _summary(result: dict) -> dict:
    state = result["state"]
    contracts = (state.get("query_spec") or {}).get("contracts") or []
    verify_results = state.get("verify_results") or []
    return {
        "session_id": result["session_id"],
        "status": state.get("status"),
        "error": state.get("error"),
        "interactions": result["interactions"],
        "contracts": [
            {
                "procedure_name": item.get("procedure_name"),
                "result_mode": item.get("result_mode"),
                "entities": [
                    entity.get("id") for entity in item.get("entities", [])
                ],
                "facts": [
                    fact.get("id") for fact in item.get("facts", [])
                ],
                "fact_joins": [
                    join.get("id") for join in item.get("fact_joins", [])
                ],
                "outputs": [
                    output.get("name") for output in item.get("outputs", [])
                ],
            }
            for item in contracts
        ],
        "verify_results": [
            {
                "name": item.get("name") or item.get("sp_name"),
                "status": item.get("status"),
                "deployment_eligible": item.get("deployment_eligible"),
                "stages": [
                    {
                        "stage": stage.get("stage"),
                        "status": stage.get("status"),
                    }
                    for stage in (
                        item.get("stages")
                        or item.get("stage_results")
                        or []
                    )
                ],
                "issues": [
                    {
                        "code": issue.get("code"),
                        "stage": issue.get("stage"),
                        "summary": (
                            issue.get("summary") or issue.get("message")
                        ),
                    }
                    for issue in item.get("issues", [])
                ],
            }
            for item in verify_results
        ],
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(
            encoding="utf-8",
            errors="backslashreplace",
        )
    if os.getenv("RUN_V3_E2E") != "1":
        raise RuntimeError("必须显式设置 RUN_V3_E2E=1")
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

        result = _run_user_flow()
        summary = _summary(result)
        print(
            "USER_E2E_RESULT "
            + json.dumps(summary, ensure_ascii=False, default=str),
            flush=True,
        )
        validated = (
            summary["status"] == "persisted"
            and summary["verify_results"]
            and all(
                item["status"] == "validated"
                and item["deployment_eligible"] is True
                and all(
                    stage["status"] == "passed"
                    for stage in item["stages"]
                )
                for item in summary["verify_results"]
            )
        )
        return 0 if validated else 2
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
