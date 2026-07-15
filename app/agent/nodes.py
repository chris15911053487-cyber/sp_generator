"""LangGraph 节点实现 — 需求澄清、方案设计、代码生成、校验、部署。"""
import json
import re
from typing import TypedDict
from langgraph.types import interrupt
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from app.agent.prompts import (
    SYSTEM_PROMPT, CLARIFY_PROMPT, DESIGN_PROMPT,
    GENERATE_PROMPT, VERIFY_SQL_PROMPT, VERIFY_PROMPT,
)
from app.agent.tools import create_tools
from app.db.sqlserver import check_syntax, execute_query, deploy_procedure, substitute_params
from app.db.sqlite import save_sp, save_verify_query, get_messages
from config import get_llm_config


class AgentState(TypedDict):
    session_id: str
    user_input: str
    mode: str
    requirements: str
    design: str
    sp_list: list
    verify_results: list
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


def _parse_dsml_tool_calls(content: str) -> list[tuple[str, dict]]:
    """解析 DeepSeek 非标准 DSML 格式的工具调用。

    格式形如：
      <｜｜DSML｜｜tool_calls>
      <｜｜DSML｜｜invoke name="run_sql_tool">
      <｜｜DSML｜｜parameter name="sql" string="true">SELECT ...</｜｜DSML｜｜parameter>
      </｜｜DSML｜｜invoke>
      </｜｜DSML｜｜tool_calls>

    返回 [(tool_name, args_dict), ...]。
    """
    calls = []
    for inv in re.finditer(r'<｜｜DSML｜｜invoke name="([^"]+)">(.*?)</｜｜DSML｜｜invoke>', content, re.DOTALL):
        name = inv.group(1)
        body = inv.group(2)
        args = {}
        for p in re.finditer(r'<｜｜DSML｜｜parameter name="([^"]+)"[^>]*>(.*?)</｜｜DSML｜｜parameter>', body, re.DOTALL):
            args[p.group(1)] = p.group(2).strip()
        calls.append((name, args))
    return calls


def _invoke_with_tools(llm: ChatOpenAI, messages: list, max_rounds: int = 8) -> AIMessage:
    """调用 LLM 并自动处理 tool calling 循环，直到 LLM 不再调 tool 为止。

    兼容两种工具调用格式：
    - 标准 OpenAI function calling（response.tool_calls）
    - DeepSeek 间歇性输出的 DSML 文本格式（LangChain 不识别，需手动解析执行）
    """
    tools = create_tools()
    tool_map = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)

    for _ in range(max_rounds):
        response = llm_with_tools.invoke(messages)

        # 1) 标准工具调用：append AIMessage + ToolMessage
        if response.tool_calls:
            messages.append(response)
            for tc in response.tool_calls:
                tool_fn = tool_map.get(tc["name"])
                if tool_fn:
                    try:
                        result = tool_fn.invoke(tc["args"])
                    except Exception as e:
                        result = f"工具执行失败: {e}"
                else:
                    result = f"未知工具: {tc['name']}"
                messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
            continue

        # 2) DSML 非标准工具调用：解析执行，结果作为 HumanMessage 追加
        #    （不 append 含 DSML 的 AIMessage，避免 tool_call_id 配对报错）
        content = response.content or ""
        dsml_calls = _parse_dsml_tool_calls(content) if "<｜｜DSML｜｜" in content else []
        if dsml_calls:
            result_parts = []
            for name, args in dsml_calls:
                tool_fn = tool_map.get(name)
                if tool_fn:
                    try:
                        result = tool_fn.invoke(args)
                    except Exception as e:
                        result = f"工具执行失败: {e}"
                else:
                    result = f"未知工具: {name}"
                result_parts.append(f"[工具 {name} 执行结果]\n{result}")
            messages.append(HumanMessage(content="\n\n".join(result_parts)))
            continue

        # 3) 无工具调用：最终响应
        return response

    # 循环耗尽：用不带工具的 LLM 强制生成最终响应
    return llm.invoke(messages)


def _build_chat_history(session_id: str, max_msgs: int = 10) -> str:
    msgs = get_messages(session_id)
    lines = []
    for m in msgs[-max_msgs:]:
        role = "用户" if m["role"] == "user" else "助手"
        lines.append(f"{role}: {m['content']}")
    return "\n".join(lines)


def clarify_node(state: AgentState, config: dict = None) -> dict:
    """需求澄清节点 — 最多 3 轮提问，超限自动进入设计阶段。"""
    llm = _get_llm()
    chat_history = _build_chat_history(state["session_id"])
    clarified = state.get("requirements", "")

    # 安全限制：超过 5 轮提问后强制进入设计（正常情况下 LLM 会自行 INFO_SUFFICIENT）
    msgs = get_messages(state["session_id"])
    user_count = sum(1 for m in msgs if m["role"] == "user")
    if user_count >= 6:  # 第 1 条是原始需求，之后每条回答算 1 轮
        return {
            "requirements": clarified,
            "mode": "design",
            "status": "clarified",
        }

    prompt = CLARIFY_PROMPT.format(
        user_input=state["user_input"],
        chat_history=chat_history,
        clarified_info=clarified or "暂无",
    )
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
    response = _invoke_with_tools(llm, messages)

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
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
    response = _invoke_with_tools(llm, messages)
    design = response.content

    decision = interrupt({"type": "design", "content": design})

    if isinstance(decision, dict) and decision.get("action") == "modify":
        design = decision.get("design", design)

    return {
        "design": design,
        "mode": "generate",
        "status": "designed",
    }


def _parse_json(content: str) -> dict | None:
    """多层回退解析 LLM 响应中的 JSON。"""
    # 1. ```json ... ``` 代码块
    m = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 2. 纯 JSON
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # 3. 花括号内容
    m = re.search(r'\{[\s\S]*\}', content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _parse_sp_params(code: str) -> list[dict]:
    """从 SP 代码中解析 @参数 声明，返回 [{name, type}, ...]"""
    params = []
    pattern = r'@(\w+)\s+(\w+(?:\((?:MAX|\d+(?:,\d+)?)\))?)'
    for m in re.finditer(pattern, code, re.IGNORECASE):
        name = m.group(1)
        if name.upper() in ('NOCOUNT', 'RETURNS', 'MESSAGE', 'ERROR'):
            continue
        params.append({"name": name, "type": m.group(2).upper(), "default": ""})
    return params


def _parse_sql_placeholders(sql_code: str) -> set[str]:
    """从校验 SQL 中解析 {参数名} 占位符"""
    return set(re.findall(r'\{(\w+)\}', sql_code))


def _merge_parameters(sp_params: list[dict], sql_placeholders: set[str],
                      llm_params: list[dict]) -> list[dict]:
    """合并 SP 参数 + 校验 SQL 占位符 + LLM 默认值，取并集"""
    param_map: dict[str, dict] = {}
    # 1. SP 声明提供类型信息
    for p in sp_params:
        param_map[p["name"]] = {"name": p["name"], "type": p["type"], "default": p.get("default", "")}
    # 2. SQL 占位符补充（无类型信息时默认 VARCHAR）
    for name in sql_placeholders:
        if name not in param_map:
            param_map[name] = {"name": name, "type": "VARCHAR", "default": ""}
    # 3. LLM 参数覆盖默认值
    for p in llm_params:
        name = p.get("name", "")
        if not name:
            continue
        if name in param_map:
            if p.get("type"):
                param_map[name]["type"] = str(p["type"]).upper()
            if p.get("default") is not None and str(p.get("default")) != "":
                param_map[name]["default"] = str(p["default"])
        else:
            param_map[name] = {
                "name": name,
                "type": str(p.get("type", "VARCHAR")).upper(),
                "default": str(p.get("default", "")),
            }
    return list(param_map.values())


def generate_node(state: AgentState, config: dict = None) -> dict:
    """代码生成节点 — 两阶段：先生成 SP，再为每个 SP 单独生成校验 SQL。"""
    from app.db.sqlite import delete_sps_by_session

    llm = _get_llm()
    session_id = state["session_id"]
    design = state["design"]

    # === 阶段 1：生成存储过程代码 ===
    prompt = GENERATE_PROMPT.format(design=design)
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
    response = _invoke_with_tools(llm, messages)
    data = _parse_json(response.content)
    print(f"[DEBUG generate_node] parsed={'OK' if data else 'FAIL'}, procedures={len(data.get('procedures',[])) if data else 0}", flush=True)

    if data is None:
        # FAIL 时不删除旧 SP：避免删了旧的又没存新的，导致 DB 变空、
        # 而 state.sp_list 仍残留旧值，造成"校验全对但右侧全空"的不一致
        return {
            "error": f"无法解析 LLM 响应为 JSON: {response.content[:500]}",
            "raw_response": response.content,
        }

    # parsed OK：删除该会话下的旧 SP（级联删除校验 SQL），再保存新的
    delete_sps_by_session(session_id)

    sp_list = []
    for proc in data.get("procedures", []):
        # 清理代码：移除 GO 语句（SSMS 批处理分隔符，不是有效 T-SQL）
        code = proc["code"].strip()
        code = re.sub(r'\n\s*GO\s*\n', '\n', code, flags=re.IGNORECASE)
        code = re.sub(r'\n\s*GO\s*$', '', code, flags=re.IGNORECASE)
        sp = save_sp(session_id, proc["name"], code)
        sp_row = dict(sp) if not isinstance(sp, dict) else sp
        sp_list.append(sp_row)

    # === 阶段 2：为每个 SP 单独生成校验 SQL ===
    for sp_row in sp_list:
        # 1. 先从 SP 代码解析 @参数 声明（始终执行，不依赖 LLM）
        sp_params = _parse_sp_params(sp_row.get("code", ""))
        verify_queries: list = []
        sql_placeholders: set[str] = set()
        llm_params: list = []

        # 2. 生成校验 SQL（LLM 可调用 tools 确认表结构）
        vq_prompt = VERIFY_SQL_PROMPT.format(
            sp_name=sp_row["name"],
            sp_code=sp_row["code"],
            design=design,
        )
        vq_messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=vq_prompt)]
        vq_response = _invoke_with_tools(llm, vq_messages)
        vq_data = _parse_json(vq_response.content)

        if vq_data:
            raw_queries = vq_data.get("verify_queries", [])
            if isinstance(raw_queries, list):
                verify_queries = raw_queries
            for vq in verify_queries:
                if not isinstance(vq, dict):
                    continue
                save_verify_query(
                    sp_row["id"],
                    vq.get("name", "未命名校验"),
                    vq.get("sql_code", ""),
                    vq.get("compare_columns", ""),
                )
                sql_placeholders |= _parse_sql_placeholders(vq.get("sql_code", ""))
            llm_params = vq_data.get("parameters", [])

        # 3. 合并参数并集（SP @参数 + 校验SQL {参数} + LLM defaults）
        print(f"[DEBUG params] SP={sp_row['name']}", flush=True)
        print(f"[DEBUG params]   sp_params from code: {sp_params}", flush=True)
        print(f"[DEBUG params]   sql_placeholders: {sql_placeholders}", flush=True)
        print(f"[DEBUG params]   llm_params: {llm_params}", flush=True)
        merged = _merge_parameters(sp_params, sql_placeholders, llm_params)
        print(f"[DEBUG params]   merged={merged}", flush=True)
        if merged:
            from app.db.sqlite import update_sp as db_update_sp2
            db_update_sp2(sp_row["id"], parameters=json.dumps(merged, ensure_ascii=False))
            sp_row["parameters"] = json.dumps(merged, ensure_ascii=False)

    return {
        "sp_list": sp_list,
        "mode": "verify",
        "status": "generated",
    }


def verify_node(state: AgentState, config: dict = None) -> dict:
    """校验节点 — 对每个 SP 执行语法校验和业务校验。"""
    from app.db.sqlite import get_verify_queries, get_sps, update_sp as db_update_sp, update_verify_query

    sp_list = state.get("sp_list", [])
    # 回退：如果状态中 sp_list 为空，从数据库加载（避免 LangGraph 状态传递问题）
    if not sp_list:
        session_id = state.get("session_id", "")
        if session_id:
            sp_list = get_sps(session_id)
            print(f"[DEBUG verify_node] fallback to DB, loaded {len(sp_list)} SPs", flush=True)
    print(f"[DEBUG verify_node] sp_list count={len(sp_list)}, keys={list(state.keys())}", flush=True)
    for i, sp in enumerate(sp_list):
        print(f"[DEBUG verify_node]   [{i}] id={sp.get('id','?')[:8]}, name={sp.get('name','?')}, code_len={len(sp.get('code',''))}", flush=True)

    results = []
    all_pass = True

    for sp in sp_list:
        sp_result = {"sp_id": sp["id"], "sp_name": sp.get("name", ""), "syntax_ok": False, "business_ok": False, "details": []}

        # 语法校验
        ok, err = check_syntax(sp["code"])
        sp_result["syntax_ok"] = ok
        if not ok:
            sp_result["details"].append({"type": "syntax", "pass": False, "error": err})
            all_pass = False
            db_update_sp(sp["id"], syntax_valid=0)
        else:
            db_update_sp(sp["id"], syntax_valid=1)

        # 业务校验
        vqs = get_verify_queries(sp["id"])
        # 加载默认参数
        params = {}
        try:
            param_list = json.loads(sp.get("parameters", "[]"))
            params = {p["name"]: p.get("default", "") for p in param_list if p.get("default")}
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

        biz_all_ok = True
        for vq in vqs:
            try:
                sql_to_run = substitute_params(vq["sql_code"], params)
                verify_rows = execute_query(sql_to_run)
                update_verify_query(vq["id"], status="pass", result_detail=json.dumps(verify_rows[:20], ensure_ascii=False, indent=2))
                sp_result["details"].append(
                    {"type": "business", "pass": True, "query": vq["name"], "data": verify_rows[:10]}
                )
            except Exception as e:
                biz_all_ok = False
                all_pass = False
                update_verify_query(vq["id"], status="fail", result_detail=str(e))
                sp_result["details"].append(
                    {"type": "business", "pass": False, "query": vq["name"], "error": str(e)}
                )

        sp_result["business_ok"] = biz_all_ok
        db_update_sp(sp["id"], business_valid=1 if biz_all_ok else 0)

        # 更新 SP 状态
        sp_status = "verified" if sp_result["syntax_ok"] and sp_result["business_ok"] else "verify_failed"
        db_update_sp(sp["id"], status=sp_status, verify_result=str(sp_result))
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
