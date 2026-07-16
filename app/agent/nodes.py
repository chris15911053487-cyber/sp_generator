"""LangGraph 节点实现 — 需求澄清、方案设计、代码生成、校验、部署。"""
import json
import re
from typing import TypedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from langgraph.types import interrupt
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from app.agent.prompts import (
    SYSTEM_PROMPT, CLARIFY_PROMPT, DESIGN_PROMPT,
    GENERATE_PROMPT, VERIFY_SQL_PROMPT, VERIFY_PROMPT,
    DESIGN_FEEDBACK_PROMPT, FIX_SP_PROMPT,
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
    clarify_count: int
    # 设计反馈阶段控制："new"=初次设计, "feedback"=修改后确认, None=完成
    design_phase: str | None
    # 上一次 LLM 对用户反馈的回复，供 chat.py 展示
    last_feedback_reply: str


def _get_llm() -> ChatOpenAI:
    cfg = get_llm_config()
    return ChatOpenAI(
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        model=cfg["model_name"],
        temperature=0.1,
        streaming=True,
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


def _invoke_with_tools(llm: ChatOpenAI, messages: list, max_rounds: int = 8,
                       stream_writer=None) -> AIMessage:
    """调用 LLM 并自动处理 tool calling 循环，直到 LLM 不再调 tool 为止。

    兼容两种工具调用格式：
    - 标准 OpenAI function calling（response.tool_calls）
    - DeepSeek 间歇性输出的 DSML 文本格式（LangChain 不识别，需手动解析执行）

    stream_writer: 可选的 Callable，传入时使用 stream() 逐 token 获取，
    最终响应（无工具调用）的 tokens 通过 stream_writer 逐个发送。
    未传入时行为与原来完全一致（invoke）。
    """
    tools = create_tools()
    tool_map = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)

    for _ in range(max_rounds):
        if stream_writer is not None:
            # 流式模式：用 stream() 逐 chunk 获取，有内容立即推送给前端
            full = None
            tokens = []
            for chunk in llm_with_tools.stream(messages):
                if full is None:
                    full = chunk
                else:
                    full += chunk
                if chunk.content:
                    tokens.append(chunk.content)
                    # 立即推送每个 token，实现逐字流式效果
                    stream_writer({"type": "token", "content": chunk.content})
            if full is None:
                break
            response = full
        else:
            # 非流式模式：保持原有行为
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
        dsml_calls = _parse_dsml_tool_calls(content) if "<zm" in content else []
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

        # 3) 无工具调用：最终响应（token 已在上面逐 chunk 推送，无需额外 flush）
        return response

    # 循环耗尽：LLM 仍想调工具时，强提示直接输出 JSON，避免 plain invoke 仍返回 DSML/空
    messages.append(HumanMessage(
        content="工具调用已达上限。请基于已获取的信息，直接输出最终的 JSON 响应，不要再调用任何工具。"
    ))
    return llm.invoke(messages)

def _build_chat_history(session_id: str, max_msgs: int = 10) -> str:
    msgs = get_messages(session_id)
    lines = []
    for m in msgs[-max_msgs:]:
        role = "用户" if m["role"] == "user" else "助手"
        lines.append(f"{role}: {m['content']}")
    return "\n".join(lines)


def _extract_first_question(content: str) -> str:
    """LLM 违规一次输出多个问题时，只截取第一个问题。

    识别"第二个问题"的标记：行首的 `### 问题`、`问题N`、`Q N` 等。
    只有一个问题或无标记时原样返回。
    """
    if not content:
        return content
    # 匹配行首的问题标记（第二个及以后）
    marks = list(re.finditer(r'(?:^|\n)\s*(?:#{1,4}\s*)?(?:问题|Q)\s*\d+\s*[：:]', content))
    if len(marks) >= 2:
        return content[:marks[1].start()].rstrip()
    return content


def _classify_design_feedback(llm: ChatOpenAI, design: str, feedback: str) -> tuple[str, str, str]:
    """调用 LLM 对设计反馈进行意图分类。

    返回 (intent, reply, new_design)。
    intent: "CONFIRM" | "MODIFY" | "IRRELEVANT"
    """
    prompt = DESIGN_FEEDBACK_PROMPT.format(design=design, user_feedback=feedback)
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
    # 意图分类不需要工具，纯 llm.invoke 减少延迟
    response = llm.invoke(messages)
    data = _parse_json(response.content)
    if data:
        return (
            data.get("intent", "IRRELEVANT"),
            data.get("reply", ""),
            data.get("new_design", ""),
        )
    return "IRRELEVANT", "无法理解您的反馈，请确认方案或提出修改意见。", ""


def clarify_node(state: AgentState, config: dict = None) -> dict:
    """需求澄清节点 — 系统控制编号，最多 5 个问题，可提前结束。"""
    llm = _get_llm()
    stream_writer = config.get("configurable", {}).get("__pregel_stream_writer") if config else None
    chat_history = _build_chat_history(state["session_id"])
    clarified = state.get("requirements", "")
    clarify_count = state.get("clarify_count", 0) or 0

    # 上限 5：已问满 5 个，强制进设计
    if clarify_count >= 5:
        return {
            "requirements": clarified,
            "mode": "design",
            "status": "clarified",
            "clarify_count": clarify_count,
        }

    # 外部安全网：用户消息过多仍强制进设计（防止 clarify_count 状态丢失）
    msgs = get_messages(state["session_id"])
    user_count = sum(1 for m in msgs if m["role"] == "user")
    if user_count >= 6:
        return {
            "requirements": clarified,
            "mode": "design",
            "status": "clarified",
            "clarify_count": clarify_count,
        }

    q_num = clarify_count + 1
    last_question_hint = (
        "这是最后一个问题：用户回答时若还有其他需求想补充，可直接一并说明；"
        "否则只回复选项即可，无需回复\"无\"。"
        if clarify_count == 4 else ""
    )

    prompt = CLARIFY_PROMPT.format(
        user_input=state["user_input"],
        chat_history=chat_history,
        clarified_info=clarified or "暂无",
        q_num=q_num,
        last_question_hint=last_question_hint,
    )
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
    response = _invoke_with_tools(llm, messages, max_rounds=1, stream_writer=stream_writer)

    if "INFO_SUFFICIENT" in response.content:
        return {
            "requirements": response.content.replace("INFO_SUFFICIENT", "").strip(),
            "mode": "design",
            "status": "clarified",
            "clarify_count": clarify_count,
        }

    # 截断 LLM 违规输出的多个问题，只取第一个；系统负责编号
    question = _extract_first_question(response.content)
    answer = interrupt({"type": "clarify", "question": question, "q_num": q_num})

    new_requirements = (
        clarified + f"\nQ{q_num}: {question}\nA: {answer}\n"
        if clarified
        else f"Q{q_num}: {question}\nA: {answer}\n"
    )
    return {
        "user_input": state["user_input"],
        "requirements": new_requirements,
        "mode": "clarify",
        "status": "clarifying",
        "clarify_count": clarify_count + 1,
    }


def design_node(state: AgentState, config: dict = None) -> dict:
    """方案设计节点 — 基于需求生成方案，支持多轮反馈确认。"""
    llm = _get_llm()
    stream_writer = config.get("configurable", {}).get("__pregel_stream_writer") if config else None
    design_phase = state.get("design_phase")
    design = state.get("design", "")

    if design_phase == "feedback":
        # === 第二阶段：展示修改后方案，再次等待确认 ===
        reply = state.get("last_feedback_reply", "")
        content = design
        if reply:
            content = f"{reply}\n\n{content}"

        decision = interrupt({"type": "design", "content": content, "phase": "feedback"})

        if isinstance(decision, dict) and decision.get("action") == "modify":
            design = decision.get("design", design)
            return {
                "design": design,
                "mode": "generate",
                "status": "designed",
                "design_phase": None,
                "last_feedback_reply": "",
            }

        if isinstance(decision, str) and decision.strip():
            intent, reply2, new_design = _classify_design_feedback(llm, design, decision.strip())
            if intent == "CONFIRM":
                return {
                    "design": design,
                    "mode": "generate",
                    "status": "designed",
                    "design_phase": None,
                    "last_feedback_reply": "",
                }
            elif intent == "MODIFY" and new_design:
                return {
                    "design": new_design,
                    "mode": "design",
                    "status": "designed",
                    "design_phase": "feedback",
                    "last_feedback_reply": reply2 or "方案已按您的意见修改，请确认。",
                }
            else:
                # IRRELEVANT
                hint = reply2 or "您的回复与当前方案无关，请确认方案或提出修改意见。"
                interrupt({"type": "design", "content": design, "reply": hint, "phase": "feedback"})
                return {
                    "design": design,
                    "mode": "design",
                    "status": "designed",
                    "design_phase": "feedback",
                    "last_feedback_reply": hint,
                }

        # 空响应视为确认
        return {
            "design": design,
            "mode": "generate",
            "status": "designed",
            "design_phase": None,
            "last_feedback_reply": "",
        }

    # === 第一阶段：初次生成方案（如已有方案则复用，避免 IRRELEVANT 循环时重新生成） ===
    design = state.get("design", "")
    if not design:
        prompt = DESIGN_PROMPT.format(requirements=state["requirements"])
        messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
        response = _invoke_with_tools(llm, messages, max_rounds=3, stream_writer=stream_writer)
        design = response.content

    decision = interrupt({"type": "design", "content": design, "phase": "new"})

    # dict 修改（前端手动修改推送）
    if isinstance(decision, dict) and decision.get("action") == "modify":
        return {
            "design": decision.get("design", design),
            "mode": "generate",
            "status": "designed",
            "design_phase": None,
            "last_feedback_reply": "",
        }

    # 文本反馈分类
    if isinstance(decision, str) and decision.strip():
        intent, reply, new_design = _classify_design_feedback(llm, design, decision.strip())
        if intent == "CONFIRM":
            return {
                "design": design,
                "mode": "generate",
                "status": "designed",
                "design_phase": None,
                "last_feedback_reply": "",
            }
        elif intent == "MODIFY" and new_design:
            return {
                "design": new_design,
                "mode": "design",
                "status": "designed",
                "design_phase": "feedback",
                "last_feedback_reply": reply or "方案已按您的意见修改，请确认。",
            }
        else:
            # IRRELEVANT
            hint = reply or "您的回复与当前方案无关，请确认方案或提出修改意见。"
            interrupt({"type": "design", "content": design, "reply": hint, "phase": "new"})
            return {
                "design": design,
                "mode": "design",
                "status": "designed",
                "design_phase": "new",
                "last_feedback_reply": hint,
            }

    # 默认：空响应视为确认
    return {
        "design": design,
        "mode": "generate",
        "status": "designed",
        "design_phase": None,
        "last_feedback_reply": "",
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


def _generate_verify_sql_for_sp(llm: ChatOpenAI, sp_row: dict, design: str) -> dict:
    """为单个 SP 生成校验 SQL（可并行调用）。返回更新后的 sp_row。"""
    sp_params = _parse_sp_params(sp_row.get("code", ""))
    verify_queries: list = []
    sql_placeholders: set[str] = set()
    llm_params: list = []

    vq_prompt = VERIFY_SQL_PROMPT.format(
        sp_name=sp_row["name"],
        sp_code=sp_row["code"],
        design=design,
    )
    vq_messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=vq_prompt)]
    vq_response = _invoke_with_tools(llm, vq_messages, max_rounds=3)
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

    # 合并参数并集
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

    return sp_row


def generate_node(state: AgentState, config: dict = None) -> dict:
    """代码生成节点 — 两阶段：先生成 SP，再为每个 SP 单独生成校验 SQL。"""
    from app.db.sqlite import delete_sps_except

    llm = _get_llm()
    session_id = state["session_id"]
    design = state["design"]

    # === 阶段 1：生成存储过程代码 ===
    prompt = GENERATE_PROMPT.format(design=design)
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
    response = _invoke_with_tools(llm, messages, max_rounds=8)
    data = _parse_json(response.content)
    print(f"[DEBUG generate_node] parsed={'OK' if data else 'FAIL'}, procedures={len(data.get('procedures',[])) if data else 0}", flush=True)

    if data is None:
        # FAIL 时不删除旧 SP：避免删了旧的又没存新的，导致 DB 变空、
        # 而 state.sp_list 仍残留旧值，造成"校验全对但右侧全空"的不一致
        return {
            "error": f"无法解析 LLM 响应为 JSON: {response.content[:500]}",
            "raw_response": response.content,
        }

    # parsed OK：先保存新 SP，再删除旧 SP（级联删除校验 SQL）。
    # 这样代码重新生成期间右侧列表始终有旧 SP，新 SP 全部就绪后才替换。
    sp_list = []
    for proc in data.get("procedures", []):
        # 清理代码：移除 GO 语句（SSMS 批处理分隔符，不是有效 T-SQL）
        code = proc["code"].strip()
        code = re.sub(r'\n\s*GO\s*\n', '\n', code, flags=re.IGNORECASE)
        code = re.sub(r'\n\s*GO\s*$', '', code, flags=re.IGNORECASE)
        sp = save_sp(session_id, proc["name"], code)
        sp_row = dict(sp) if not isinstance(sp, dict) else sp
        sp_list.append(sp_row)
    # === 阶段 2：并行生成校验 SQL ===
    # 注意：旧 SP 暂不删除，等阶段 2 全部完成后统一替换，
    # 避免校验 SQL 生成中途出错导致旧 SP 已丢、新 SP 不完整
    MAX_PARALLEL = 3  # 最大并行数，避免 LLM API 限流
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as executor:
        futures = {
            executor.submit(_generate_verify_sql_for_sp, llm, sp_row, design): sp_row
            for sp_row in sp_list
        }
        for future in as_completed(futures):
            sp_row = futures[future]
            try:
                future.result()
            except Exception as e:
                print(f"[ERROR] 校验 SQL 生成失败: {sp_row.get('name')}: {e}", flush=True)

    # 新 SP 全部就绪（代码 + 参数 + 校验 SQL），现在替换旧 SP
    delete_sps_except(session_id, [s["id"] for s in sp_list])

    return {
        "sp_list": sp_list,
        "mode": "verify",
        "status": "generated",
    }


def verify_node(state: AgentState, config: dict = None) -> dict:
    """校验节点 — 语法+业务校验，失败时自动修复（最多 2 次迭代）。"""
    from app.db.sqlite import get_verify_queries, get_sps, update_sp as db_update_sp, update_verify_query

    llm = _get_llm()

    sp_list = state.get("sp_list", [])
    # 回退：如果状态中 sp_list 为空，从数据库加载
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
    MAX_FIX_ROUNDS = 2  # 最多 2 次自动修复

    for sp in sp_list:
        sp_result = {"sp_id": sp["id"], "sp_name": sp.get("name", ""), "syntax_ok": False, "business_ok": False, "details": []}
        # 用局部变量跟踪当前代码，不直接改 sp_list
        current_code = sp["code"]

        for fix_round in range(MAX_FIX_ROUNDS + 1):  # 初始 + 最多 MAX_FIX_ROUNDS 次修复
            # === 语法校验 ===
            syntax_ok, syntax_err = check_syntax(current_code)

            # === 业务校验 ===
            vqs = get_verify_queries(sp["id"])
            params = {}
            try:
                param_list = json.loads(sp.get("parameters", "[]"))
                params = {p["name"]: p.get("default", "") for p in param_list if p.get("default")}
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

            biz_all_ok = True
            biz_errors = []
            for vq in vqs:
                try:
                    sql_to_run = substitute_params(vq["sql_code"], params)
                    verify_rows = execute_query(sql_to_run)
                    update_verify_query(vq["id"], status="pass", result_detail=json.dumps(verify_rows[:20], ensure_ascii=False, indent=2))
                except Exception as e:
                    biz_all_ok = False
                    biz_errors.append({"query": vq["name"], "error": str(e)})
                    update_verify_query(vq["id"], status="fail", result_detail=str(e))

            # 全部通过 → 跳出修复循环
            if syntax_ok and biz_all_ok:
                sp_result["syntax_ok"] = True
                sp_result["business_ok"] = True
                break

            # 最后一轮仍未通过 → 记录结果
            if fix_round >= MAX_FIX_ROUNDS:
                sp_result["syntax_ok"] = syntax_ok
                sp_result["business_ok"] = biz_all_ok
                if not syntax_ok:
                    sp_result["details"].append({"type": "syntax", "pass": False, "error": syntax_err})
                for be in biz_errors:
                    sp_result["details"].append({
                        "type": "business", "pass": False,
                        "query": be["query"], "error": be["error"],
                    })
                all_pass = False
                break

            # === 告知用户进度（interrupt 让 chat.py 向用户发送 SSE）===
            interrupt({
                "type": "auto_fix_progress",
                "message": f"校验失败，正在自动修正（第 {fix_round + 1}/{MAX_FIX_ROUNDS} 次）...",
                "sp_name": sp.get("name", ""),
            })

            # === 调用 LLM 修复代码 ===
            errors_text = []
            if not syntax_ok:
                errors_text.append(f"[语法错误] {syntax_err}")
            if biz_errors:
                errors_text.append(f"[业务校验失败] {json.dumps(biz_errors, ensure_ascii=False)}")

            fix_prompt = FIX_SP_PROMPT.format(
                sp_name=sp.get("name", ""),
                sp_code=current_code,
                errors="\n".join(errors_text),
            )
            fix_messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=fix_prompt)]
            fix_response = _invoke_with_tools(llm, fix_messages, max_rounds=2)
            fix_data = _parse_json(fix_response.content)

            if fix_data and fix_data.get("fixed_code"):
                current_code = fix_data["fixed_code"]
                db_update_sp(sp["id"], code=current_code)

        # 更新 SP 状态
        db_update_sp(sp["id"], syntax_valid=1 if sp_result["syntax_ok"] else 0)
        db_update_sp(sp["id"], business_valid=1 if sp_result["business_ok"] else 0)
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
