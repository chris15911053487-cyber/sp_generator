"""LangGraph 节点实现 — 需求澄清、方案设计、代码生成、校验、部署。"""
import json
import re
from typing import TypedDict
from langgraph.types import interrupt
from langchain_openai import ChatOpenAI
from app.agent.prompts import (
    SYSTEM_PROMPT, CLARIFY_PROMPT, DESIGN_PROMPT,
    GENERATE_PROMPT, VERIFY_PROMPT,
)
from app.db.sqlserver import check_syntax, execute_query, deploy_procedure
from app.db.sqlite import save_sp, save_verify_query, get_messages
from config import get_llm_config


class AgentState(TypedDict):
    session_id: str
    user_input: str
    mode: str
    requirements: str
    design: str
    sp_list: list
    status: str
    error: str


def _get_llm() -> ChatOpenAI:
    cfg = get_llm_config()
    return ChatOpenAI(
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        model=cfg["model_name"],
        temperature=0.1,
    )


def _build_chat_history(session_id: str, max_msgs: int = 10) -> str:
    msgs = get_messages(session_id)
    lines = []
    for m in msgs[-max_msgs:]:
        role = "用户" if m["role"] == "user" else "助手"
        lines.append(f"{role}: {m['content']}")
    return "\n".join(lines)


def clarify_node(state: AgentState, config: dict = None) -> dict:
    """需求澄清节点 — 一次最多问一个问题，直到信息充分。"""
    llm = _get_llm()
    chat_history = _build_chat_history(state["session_id"])
    clarified = state.get("requirements", "")

    prompt = CLARIFY_PROMPT.format(
        user_input=state["user_input"],
        chat_history=chat_history,
        clarified_info=clarified or "暂无",
    )
    response = llm.invoke([("system", SYSTEM_PROMPT), ("user", prompt)])

    if "INFO_SUFFICIENT" in response.content:
        return {
            "requirements": response.content.replace("INFO_SUFFICIENT", "").strip(),
            "mode": "design",
            "status": "clarified",
        }

    question = response.content
    answer = interrupt({"type": "clarify", "question": question})

    new_requirements = (
        clarified + f"\nQ: {question}\nA: {answer}\n"
        if clarified
        else f"Q: {question}\nA: {answer}\n"
    )
    return {
        "user_input": state["user_input"],
        "requirements": new_requirements,
        "mode": "clarify",
        "status": "clarifying",
    }


def design_node(state: AgentState, config: dict = None) -> dict:
    """方案设计节点 — 基于需求生成方案，等待用户确认。"""
    llm = _get_llm()
    prompt = DESIGN_PROMPT.format(requirements=state["requirements"])
    response = llm.invoke([("system", SYSTEM_PROMPT), ("user", prompt)])
    design = response.content

    decision = interrupt({"type": "design", "content": design})

    if isinstance(decision, dict) and decision.get("action") == "modify":
        design = decision.get("design", design)

    return {
        "design": design,
        "mode": "generate",
        "status": "designed",
    }


def generate_node(state: AgentState, config: dict = None) -> dict:
    """代码生成节点 — 生成存储过程和校验 SQL。"""
    llm = _get_llm()
    prompt = GENERATE_PROMPT.format(design=state["design"])
    response = llm.invoke([("system", SYSTEM_PROMPT), ("user", prompt)])

    content = response.content
    json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
    if json_match:
        data = json.loads(json_match.group(1))
    else:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            bracket_match = re.search(r'\{.*\}', content, re.DOTALL)
            if bracket_match:
                data = json.loads(bracket_match.group(0))
            else:
                return {"error": f"无法解析 LLM 响应为 JSON: {content[:500]}"}

    sp_list = []
    for proc in data.get("procedures", []):
        sp = save_sp(state["session_id"], proc["name"], proc["code"])
        sp_row = dict(sp) if not isinstance(sp, dict) else sp
        for vq in proc.get("verify_queries", []):
            save_verify_query(
                sp_row["id"],
                vq["name"],
                vq["sql_code"],
                vq.get("compare_columns", ""),
            )
        sp_list.append(sp_row)

    return {
        "sp_list": sp_list,
        "mode": "verify",
        "status": "generated",
    }


def verify_node(state: AgentState, config: dict = None) -> dict:
    """校验节点 — 对每个 SP 执行语法校验和业务校验。"""
    from app.db.sqlite import get_verify_queries, update_sp as db_update_sp, update_verify_query

    results = []
    all_pass = True

    for sp in state.get("sp_list", []):
        sp_result = {"sp_id": sp["id"], "syntax_ok": False, "business_ok": False, "details": []}

        # 语法校验
        ok, err = check_syntax(sp["code"])
        sp_result["syntax_ok"] = ok
        if not ok:
            sp_result["details"].append({"type": "syntax", "pass": False, "error": err})
            all_pass = False
            db_update_sp(sp["id"], syntax_valid=0, verify_result=str(sp_result))
        else:
            db_update_sp(sp["id"], syntax_valid=1)

        # 业务校验
        vqs = get_verify_queries(sp["id"])
        for vq in vqs:
            try:
                verify_rows = execute_query(vq["sql_code"])
                db_update_sp(sp["id"], business_valid=1)
                update_verify_query(vq["id"], status="pass", result_detail=str(verify_rows[:20]))
                sp_result["details"].append(
                    {"type": "business", "pass": True, "query": vq["name"], "data": verify_rows[:10]}
                )
            except Exception as e:
                all_pass = False
                update_verify_query(vq["id"], status="fail", result_detail=str(e))
                sp_result["details"].append(
                    {"type": "business", "pass": False, "query": vq["name"], "error": str(e)}
                )
                sp_result["business_ok"] = False

        sp_result["business_ok"] = all(
            d["pass"] for d in sp_result["details"] if d["type"] == "business"
        )
        results.append(sp_result)

    return {
        "status": "verified" if all_pass else "verify_failed",
        "verify_results": results,
    }


def deploy_check_node(state: AgentState, config: dict = None) -> dict:
    """部署预检节点 — 最终校验所有 SP。"""
    all_pass = True
    results = []
    for sp in state.get("sp_list", []):
        ok, err = check_syntax(sp["code"])
        results.append({"sp_id": sp["id"], "name": sp["name"], "syntax_ok": ok, "error": err})
        if not ok:
            all_pass = False

    if not all_pass:
        interrupt({"type": "deploy_check", "pass": False, "results": results})

    return {"status": "ready_to_deploy", "precheck_results": results}


def deploy_node(state: AgentState, config: dict = None) -> dict:
    """部署节点 — 执行 CREATE PROCEDURE。"""
    from app.db.sqlite import update_sp as db_update_sp
    import datetime

    results = []
    for sp in state.get("sp_list", []):
        ok, err = deploy_procedure(sp["name"], sp["code"])
        if ok:
            db_update_sp(sp["id"], status="deployed", deployed_at=datetime.datetime.now().isoformat())
        results.append({"sp_id": sp["id"], "name": sp["name"], "success": ok, "error": err})

    all_ok = all(r["success"] for r in results)
    return {"status": "deployed" if all_ok else "deploy_failed", "deploy_results": results}
