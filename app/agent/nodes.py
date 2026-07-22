"""LangGraph 节点实现 — 需求澄清、方案设计、代码生成、校验、部署。"""
import json
import re
from functools import lru_cache
from threading import Lock
from typing import NotRequired, TypedDict
from langgraph.types import interrupt
try:
    from langgraph.config import get_stream_writer
except ImportError:  # 兼容较早的 langgraph 版本
    get_stream_writer = None
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from app.agent.prompts import (
    SYSTEM_PROMPT, CLARIFY_PROMPT, DESIGN_PROMPT,
    DESIGN_FEEDBACK_PROMPT, ASSUMPTIONS_PROMPT, PROCEDURE_CANDIDATE_PROMPT,
    ORACLE_CANDIDATE_PROMPT, REPAIR_PROCEDURE_CANDIDATE_PROMPT,
    REPAIR_ORACLE_CANDIDATE_PROMPT,
)
from app.agent.tools import create_tools
from app.db.sqlite import get_messages
from app.services.candidate_pipeline import CandidateBundle, GateResult, VerifyQueryCandidate
from app.services.generation_harness import (
    GateError, QuerySpec, compile_query_spec,
)
from app.services.schema_evidence import capture_schema_evidence
from config import get_llm_config


class AgentState(TypedDict):
    session_id: str
    user_input: str
    mode: str
    requirements: str
    confirmed_assumptions: str
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
    query_spec: NotRequired[dict]
    candidate_bundles: NotRequired[list[dict]]


_tools = create_tools()
_bound_llms: dict[int, tuple[ChatOpenAI, object]] = {}
_bound_llms_lock = Lock()


@lru_cache(maxsize=4)
def _create_llm(api_key: str, base_url: str, model_name: str) -> ChatOpenAI:
    """按配置复用底层 HTTP 客户端；配置变化会自然创建一个新实例。"""
    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model_name,
        temperature=0.1,
        streaming=True,
        timeout=120,
        max_retries=0,
    )


def _get_llm() -> ChatOpenAI:
    cfg = get_llm_config()
    return _create_llm(cfg["api_key"], cfg["base_url"], cfg["model_name"])


def _bind_tools(llm: ChatOpenAI):
    """复用 bind_tools 生成的工具 schema，避免每轮调用重复构造。"""
    key = id(llm)
    cached = _bound_llms.get(key)
    if cached and cached[0] is llm:
        return cached[1]
    with _bound_llms_lock:
        cached = _bound_llms.get(key)
        if cached and cached[0] is llm:
            return cached[1]
        bound = llm.bind_tools(_tools)
        _bound_llms[key] = (llm, bound)
        return bound


def _get_writer(config: RunnableConfig | None = None):
    """获取 LangGraph custom stream writer，并兼容旧版本的私有注入方式。"""
    if get_stream_writer is not None:
        try:
            return get_stream_writer()
        except (RuntimeError, LookupError):
            pass
    if config:
        return config.get("configurable", {}).get("__pregel_stream_writer")
    return None


def _write_progress(writer, stage: str, content: str) -> None:
    if writer is not None:
        writer({"type": "progress", "stage": stage, "content": content})


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
    tool_map = {t.name: t for t in _tools}
    llm_with_tools = _bind_tools(llm)

    for _ in range(max_rounds):
        if stream_writer is not None:
            # 流式模式：用 stream() 逐 chunk 获取，有内容立即推送给前端
            full = None
            for chunk in llm_with_tools.stream(messages):
                if full is None:
                    full = chunk
                else:
                    full += chunk
                if chunk.content:
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
    LLM 违规直接输出设计方案 JSON 时，返回友好提示。

    识别行首的问题编号标记；系统会在 SSE 层统一加上正确编号，因此模型
    输出中的第一个编号也必须移除。
    """
    if not content:
        return content

    # 检测 LLM 是否违规输出了 JSON 设计方案（而非提问）
    stripped = content.strip()
    # 情况1：内容以 { 开头，是纯 JSON
    if stripped.startswith('{') and len(stripped) > 200:
        return "信息已足够，我将为您生成设计方案。"
    # 情况2：少量文字后跟大段 JSON
    json_start = stripped.find('{')
    if json_start > 0 and json_start < 100 and (len(stripped) - json_start) > 200:
        # 尝试提取 JSON 之前的文字作为问题
        before_json = stripped[:json_start].strip()
        # 如果前面的文字像问题（含问号或选项），保留
        if '?' in before_json or '？' in before_json or '\nA' in before_json:
            return before_json
        return "信息已足够，我将为您生成设计方案。"
    # 情况3：```json 代码块
    if '```json' in stripped and len(stripped) > 300:
        before_code = stripped.split('```json')[0].strip()
        if before_code and ('?' in before_code or '？' in before_code):
            return before_code
        return "信息已足够，我将为您生成设计方案。"

    # 先截断第二个问题，再移除模型自带的第一个编号。即使模型
    # 只输出了一个错误编号（如系统当前是 Q2，模型却写 Q3）也能归一化。
    marker_pattern = r'(?m)^[ \t]*(?:#{1,4}[ \t]*)?(?:问题|Q)[ \t]*\d+[ \t]*[：:][ \t]*'
    marks = list(re.finditer(marker_pattern, content))
    if len(marks) >= 2:
        content = content[:marks[1].start()].rstrip()
    return re.sub(marker_pattern, '', content, count=1).strip()


def _is_explicit_design_confirmation(feedback: str) -> bool:
    """只匹配无歧义的短确认，含修改内容的回复仍交给 LLM 分类。"""
    normalized = re.sub(r"[\s，,。.!！?？]", "", feedback).lower()
    return normalized in {
        "确认", "确认请开始生成存储过程", "确认方案开始生成", "开始生成",
        "可以", "好的", "好", "没问题", "同意", "继续", "生成", "ok", "yes",
    }


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


def _markdown_cell(value) -> str:
    if value is None:
        return "NULL"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _code(value) -> str:
    return chr(96) + _markdown_cell(value) + chr(96)


def _column_refs(items) -> str:
    if not items:
        return "无"
    return "、".join(
        _code(f"{item.source_alias}.{item.column}") for item in items
    )


def _render_query_spec(query_spec: QuerySpec) -> str:
    """把唯一业务契约确定性渲染为供用户确认的中文方案。"""
    lines = ["## 1. 存储过程方案"]
    operation_labels = {
        "reporting": "查询",
        "controlled_write": "受控写入",
    }
    for procedure in query_spec.procedures:
        lines.extend([
            "",
            f"### {_code(procedure.name)}",
            "",
            f"- 用途：{procedure.purpose}",
            f"- 操作类型：{operation_labels[procedure.operation_type]}",
            "",
            "#### 参数",
            "",
            "| 参数 | 类型 | 必填 | 默认值 | 含义 |",
            "|---|---|---|---|---|",
        ])
        if procedure.parameters:
            for item in procedure.parameters:
                lines.append(
                    f"| {_code(item.name)} | {_code(item.sql_type)} | "
                    f"{'是' if item.required else '否'} | "
                    f"{_markdown_cell(item.default)} | "
                    f"{_markdown_cell(item.meaning)} |"
                )
        else:
            lines.append("| 无 | — | — | — | — |")

        lines.extend([
            "",
            "#### 数据来源",
            "",
            "| 表 | 别名 | 用途 |",
            "|---|---|---|",
        ])
        for item in procedure.sources:
            lines.append(
                f"| {_code(f'{item.schema}.{item.table}')} | "
                f"{_code(item.alias)} | {_markdown_cell(item.role)} |"
            )

        lines.extend(["", "#### 业务规则", ""])
        if procedure.joins:
            for item in procedure.joins:
                lines.append(
                    f"- {item.join_type.upper()} JOIN："
                    f"{_code(f'{item.left.source_alias}.{item.left.column}')} = "
                    f"{_code(f'{item.right.source_alias}.{item.right.column}')}；"
                    f"{item.reason}"
                )
        for item in procedure.filters:
            refs = _column_refs(item.column_refs)
            params = "、".join(_code(name) for name in item.parameter_refs)
            suffix = f"；参数：{params}" if params else ""
            lines.append(f"- 过滤：{item.description}；字段：{refs}{suffix}")
        lines.append(f"- 结果粒度：{_column_refs(procedure.grain)}")

        lines.extend([
            "",
            "#### 输出",
            "",
            "| 输出列 | 类型 | 来源字段 | 聚合 | 含义 |",
            "|---|---|---|---|---|",
        ])
        for item in procedure.outputs:
            lines.append(
                f"| {_code(item.name)} | {_code(item.sql_type)} | "
                f"{_column_refs(item.source_columns)} | "
                f"{_markdown_cell(item.aggregation or '无')} | "
                f"{_markdown_cell(item.meaning)} |"
            )

        if procedure.writes:
            lines.extend(["", "#### 写入范围", ""])
            for item in procedure.writes:
                lines.append(
                    f"- {item.operation.upper()} "
                    f"{_code(f'{item.schema}.{item.table}')}；"
                    f"键：{', '.join(item.key_columns)}；"
                    f"最多影响 {item.max_affected_rows} 行"
                )

        lines.extend(["", "#### 校验规则", ""])
        for item in procedure.verification_rules:
            columns = "、".join(_code(name) for name in item.required_columns)
            lines.append(
                f"- {item.name}（{item.mode}）：{item.description}；"
                f"校验列：{columns or '无'}"
            )

    return "\n".join(lines)


def clarify_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """需求确认节点 — 系统控制编号，最多 5 个问题，可提前结束。"""
    llm = _get_llm()
    stream_writer = _get_writer(config)
    _write_progress(stream_writer, "clarify", "正在分析需求并准备下一个关键问题...")

    # 如果 mode 已经跳过了需求确认阶段，直接 pass-through
    current_mode = state.get("mode", "clarify")
    if current_mode in ("assumptions", "design", "generate"):
        return {
            "requirements": state.get("requirements", ""),
            "mode": current_mode,
            "status": state.get("status", ""),
            "clarify_count": state.get("clarify_count", 0),
        }

    chat_history = _build_chat_history(state["session_id"])
    clarified = state.get("requirements", "")
    clarify_count = state.get("clarify_count", 0) or 0

    # 上限 5：已问满 5 个，强制进入关键项确认
    if clarify_count >= 5:
        return {
            "requirements": clarified,
            "mode": "assumptions",
            "status": "clarified",
            "clarify_count": clarify_count,
        }

    # 外部安全网：用户消息过多仍强制进入关键项确认（防止 clarify_count 状态丢失）
    msgs = get_messages(state["session_id"])
    user_count = sum(1 for m in msgs if m["role"] == "user")
    if user_count >= 6:
        return {
            "requirements": clarified,
            "mode": "assumptions",
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
    # 澄清只判断业务需求，不做 schema 探测；避免携带工具 schema 及误触发后的
    # 第二次模型往返。最终问题通过 interrupt 事件发送。
    response = llm.invoke(messages)

    if "INFO_SUFFICIENT" in response.content:
        return {
            "requirements": response.content.replace("INFO_SUFFICIENT", "").strip(),
            "mode": "assumptions",
            "status": "clarified",
            "clarify_count": clarify_count,
        }

    # 检测 LLM 是否违规输出了 JSON 设计方案（而非提问）
    # 如果是，说明 LLM 认为信息足够，直接进入 assumptions 阶段
    content_stripped = response.content.strip()
    if content_stripped.startswith('{') and len(content_stripped) > 200:
        try:
            obj = json.loads(content_stripped)
            if any(k in obj for k in ('stored_procedure', 'procedures', '存储过程列表', 'sp_list')):
                return {
                    "requirements": clarified or state["user_input"],
                    "mode": "assumptions",
                    "status": "clarified",
                    "clarify_count": clarify_count,
                }
        except (json.JSONDecodeError, ValueError):
            pass

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


def assumptions_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """关键项确认节点 — LLM 生成关键假设列表，用户逐项确认/修改后进入设计。"""
    stream_writer = _get_writer(config)
    _write_progress(stream_writer, "assumptions", "正在整理需要确认的关键项...")
    llm = _get_llm()

    # 如果 mode 已跳过，直接 pass-through
    current_mode = state.get("mode", "assumptions")
    if current_mode in ("design", "generate"):
        return {
            "confirmed_assumptions": state.get("confirmed_assumptions", ""),
            "mode": current_mode,
        }

    # 调用 LLM 生成关键项列表
    prompt = ASSUMPTIONS_PROMPT.format(requirements=state.get("requirements", ""))
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
    response = llm.invoke(messages)

    # 解析 JSON
    assumptions_data = _parse_json(response.content)
    assumptions_list = []
    if assumptions_data and "assumptions" in assumptions_data:
        assumptions_list = assumptions_data["assumptions"]

    if not assumptions_list:
        # 无关键项需确认，直接进入设计
        return {
            "confirmed_assumptions": "无特殊关键项",
            "mode": "design",
            "status": "assumptions_confirmed",
        }

    # 中断等待用户确认：前端渲染勾选列表
    user_response = interrupt({
        "type": "assumptions",
        "assumptions": assumptions_list,
    })

    # user_response 格式：{"confirmed": [...], "modified": {...}}
    # confirmed: 用户同意的 key 列表
    # modified: {key: "用户修改后的值"} 用户修改了的项
    confirmed_keys = []
    modified_items = {}
    if isinstance(user_response, dict):
        confirmed_keys = user_response.get("confirmed", [])
        modified_items = user_response.get("modified", {})
    elif isinstance(user_response, str):
        # fallback: 用户直接输入文本，视为全部确认
        confirmed_keys = [a["key"] for a in assumptions_list]

    # 构建确认结果文本
    lines = []
    for a in assumptions_list:
        key = a["key"]
        if key in modified_items:
            lines.append(f"- {a['title']}：{modified_items[key]}（用户修改）")
        elif key in confirmed_keys:
            lines.append(f"- {a['title']}：{a['value']}（已确认）")
        else:
            # 未勾选的项 — 忽略，不纳入设计
            pass

    confirmed_text = "\n".join(lines) if lines else "用户未确认任何关键项，使用默认设置"

    return {
        "confirmed_assumptions": confirmed_text,
        "mode": "design",
        "status": "assumptions_confirmed",
    }


def design_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """先固化方案及 QuerySpec，再让用户确认同一版本。"""
    llm = _get_llm()
    stream_writer = _get_writer(config)
    design_phase = state.get("design_phase")
    design = state.get("design", "")
    raw_query_spec = state.get("query_spec")

    if design_phase == "prepare_feedback" or not raw_query_spec or not design:
        if not design:
            confirmed_assumptions = state.get(
                "confirmed_assumptions", "无特殊关键项",
            )
            prompt = DESIGN_PROMPT.format(
                requirements=state["requirements"],
                confirmed_assumptions=confirmed_assumptions,
            )
            messages = [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ]
            response = _invoke_with_tools(
                llm, messages, max_rounds=3, stream_writer=stream_writer,
            )
            design = response.content

        _write_progress(
            stream_writer, "query_spec", "正在固化并校验方案业务契约...",
        )
        try:
            query_spec = _compile_design_query_spec(llm, design)
        except Exception as exc:
            return {
                "design": design,
                "query_spec": {},
                "mode": "design",
                "status": "design_failed",
                "error": f"方案无法形成有效业务契约：{exc}",
            }
        design = _render_query_spec(query_spec)
        return {
            "design": design,
            "query_spec": query_spec.model_dump(mode="json", by_alias=True),
            "mode": "design",
            "status": "designed",
            "design_phase": (
                "feedback" if design_phase == "prepare_feedback" else "new"
            ),
            "last_feedback_reply": state.get("last_feedback_reply", ""),
            "error": "",
        }

    if design_phase == "feedback":
        # === 第二阶段：展示修改后方案，再次等待确认 ===
        reply = state.get("last_feedback_reply", "")
        content = design
        if reply:
            content = f"{reply}\n\n{content}"

        decision = interrupt({"type": "design", "content": content, "phase": "feedback"})
        if isinstance(decision, dict) and decision.get("action") == "confirm":
            return {
                "design": design,
                "query_spec": raw_query_spec,
                "mode": "generate",
                "status": "designed",
                "design_phase": None,
                "last_feedback_reply": "",
            }


        if isinstance(decision, dict) and decision.get("action") == "modify":
            return {
                "design": decision.get("design", design),
                "query_spec": {},
                "mode": "design",
                "status": "designed",
                "design_phase": "prepare_feedback",
                "last_feedback_reply": "方案已按您的意见修改。",
            }

        if isinstance(decision, str) and decision.strip():
            if _is_explicit_design_confirmation(decision):
                return {
                    "design": design,
                    "query_spec": raw_query_spec,
                    "mode": "generate",
                    "status": "designed",
                    "design_phase": None,
                    "last_feedback_reply": "",
                }
            intent, reply2, new_design = _classify_design_feedback(llm, design, decision.strip())
            if intent == "CONFIRM":
                return {
                    "design": design,
                    "query_spec": raw_query_spec,
                    "mode": "generate",
                    "status": "designed",
                    "design_phase": None,
                    "last_feedback_reply": "",
                }
            elif intent == "MODIFY" and new_design:
                return {
                    "design": new_design,
                    "query_spec": {},
                    "mode": "design",
                    "status": "designed",
                    "design_phase": "prepare_feedback",
                    "last_feedback_reply": reply2 or "方案已按您的意见修改。",
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
            "query_spec": raw_query_spec,
            "mode": "generate",
            "status": "designed",
            "design_phase": None,
            "last_feedback_reply": "",
        }

    # 展示并确认已经固化的初始方案。
    decision = interrupt({"type": "design", "content": design, "phase": "new"})
    if isinstance(decision, dict) and decision.get("action") == "confirm":
        return {
            "design": design,
            "query_spec": raw_query_spec,
            "mode": "generate",
            "status": "designed",
            "design_phase": None,
            "last_feedback_reply": "",
        }


    # dict 修改（前端手动修改推送）
    if isinstance(decision, dict) and decision.get("action") == "modify":
        return {
            "design": decision.get("design", design),
            "query_spec": {},
            "mode": "design",
            "status": "designed",
            "design_phase": "prepare_feedback",
            "last_feedback_reply": "方案已按您的意见修改。",
        }

    # 文本反馈分类
    if isinstance(decision, str) and decision.strip():
        if _is_explicit_design_confirmation(decision):
            return {
                "design": design,
                "query_spec": raw_query_spec,
                "mode": "generate",
                "status": "designed",
                "design_phase": None,
                "last_feedback_reply": "",
            }
        intent, reply, new_design = _classify_design_feedback(llm, design, decision.strip())
        if intent == "CONFIRM":
            return {
                "design": design,
                "query_spec": raw_query_spec,
                "mode": "generate",
                "status": "designed",
                "design_phase": None,
                "last_feedback_reply": "",
            }
        elif intent == "MODIFY" and new_design:
            return {
                "design": new_design,
                "query_spec": {},
                "mode": "design",
                "status": "designed",
                "design_phase": "prepare_feedback",
                "last_feedback_reply": reply or "方案已按您的意见修改。",
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
        "query_spec": raw_query_spec,
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


def _normalize_compare_columns(value) -> str:
    """将 LLM 返回的对比列规范化为逗号分隔文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if not all(isinstance(column, str) for column in value):
            raise ValueError("compare_columns 列表只能包含字符串")
        return ",".join(column.strip() for column in value if column.strip())
    raise ValueError("compare_columns 必须是字符串或字符串列表")


def _clean_procedure_code(code: str) -> str:
    code = code.strip()
    code = re.sub(r'\n\s*GO\s*\n', '\n', code, flags=re.IGNORECASE)
    return re.sub(r'\n\s*GO\s*$', '', code, flags=re.IGNORECASE)


def _candidate_json(llm: ChatOpenAI, prompt: str, label: str) -> dict:
    response = llm.invoke([
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ])
    data = _parse_json(response.content)
    if not isinstance(data, dict):
        raise ValueError(f"{label} 未返回有效 JSON 对象")
    return data


def _compile_design_query_spec(llm: ChatOpenAI, design: str) -> QuerySpec:
    return compile_query_spec(
        design,
        lambda prompt: llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]),
    )


def _procedure_schema_json(query_spec: QuerySpec, procedure_spec,
                           schema_evidence) -> str:
    qualified = {
        (item.schema, item.table) for item in procedure_spec.sources
    } | {
        (item.schema, item.table) for item in procedure_spec.writes
    }
    payload = {
        "database_name": schema_evidence.database_name,
        "captured_at": schema_evidence.captured_at.isoformat(),
        "fingerprint": schema_evidence.fingerprint,
        "objects": [
            item.model_dump(mode="json", by_alias=True)
            for item in schema_evidence.objects
            if (item.schema, item.name) in qualified
        ],
        "unresolved": [
            item.model_dump(mode="json")
            for item in schema_evidence.unresolved
            if any(
                item.identifier.startswith(f"{schema}.{table}")
                for schema, table in qualified
            )
        ],
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _generate_procedure_candidate(llm: ChatOpenAI, query_spec: QuerySpec,
                                  procedure_spec, schema_evidence) -> str:
    schema_json = _procedure_schema_json(
        query_spec, procedure_spec, schema_evidence,
    )
    data = _candidate_json(
        llm,
        PROCEDURE_CANDIDATE_PROMPT.format(
            query_spec=query_spec.canonical_json(),
            procedure_spec=json.dumps(
                procedure_spec.model_dump(mode="json", by_alias=True),
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ),
            schema_fingerprint=schema_evidence.fingerprint,
            schema_evidence=schema_json,
        ),
        procedure_spec.name,
    )
    code = data.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError(f"{procedure_spec.name} 缺少完整存储过程 SQL")
    return _clean_procedure_code(code)


def _generate_oracle_candidates(llm: ChatOpenAI, query_spec: QuerySpec,
                                procedure_spec, schema_evidence
                                ) -> list[VerifyQueryCandidate]:
    schema_json = _procedure_schema_json(
        query_spec, procedure_spec, schema_evidence,
    )
    data = _candidate_json(
        llm,
        ORACLE_CANDIDATE_PROMPT.format(
            query_spec=query_spec.canonical_json(),
            procedure_spec=json.dumps(
                procedure_spec.model_dump(mode="json", by_alias=True),
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ),
            schema_fingerprint=schema_evidence.fingerprint,
            schema_evidence=schema_json,
        ),
        f"{procedure_spec.name} Oracle",
    )
    raw_queries = data.get("verify_queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ValueError(f"{procedure_spec.name} 未生成独立 Oracle 校验规则")
    normalized = []
    for item in raw_queries:
        if not isinstance(item, dict):
            raise ValueError(f"{procedure_spec.name} Oracle 规则必须是对象")
        candidate = dict(item)
        candidate["compare_columns"] = _normalize_compare_columns(
            candidate.get("compare_columns", ""),
        )
        normalized.append(VerifyQueryCandidate.model_validate(candidate))
    return normalized


def _repair_candidate(llm: ChatOpenAI, bundle: CandidateBundle,
                      errors: list) -> CandidateBundle:
    repaired = bundle.model_copy(deep=True)
    serialized_errors = json.dumps(
        [item.model_dump(mode="json") for item in errors],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    schema_json = _procedure_schema_json(
        bundle.query_spec, bundle.procedure_spec, bundle.schema_evidence,
    )
    artifacts = {item.artifact for item in errors}
    if "procedure" in artifacts:
        data = _candidate_json(
            llm,
            REPAIR_PROCEDURE_CANDIDATE_PROMPT.format(
                procedure_spec=json.dumps(
                    bundle.procedure_spec.model_dump(mode="json", by_alias=True),
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ),
                schema_fingerprint=bundle.schema_evidence.fingerprint,
                schema_evidence=schema_json,
                errors=serialized_errors,
                sql=bundle.procedure_sql,
            ),
            f"{bundle.procedure_spec.name} 修复",
        )
        fixed_sql = data.get("fixed_sql")
        if not isinstance(fixed_sql, str) or not fixed_sql.strip():
            raise ValueError("SP 修复模型未返回 fixed_sql")
        repaired.procedure_sql = _clean_procedure_code(fixed_sql)

    if "oracle" in artifacts:
        data = _candidate_json(
            llm,
            REPAIR_ORACLE_CANDIDATE_PROMPT.format(
                procedure_spec=json.dumps(
                    bundle.procedure_spec.model_dump(mode="json", by_alias=True),
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ),
                schema_fingerprint=bundle.schema_evidence.fingerprint,
                schema_evidence=schema_json,
                errors=serialized_errors,
                verify_queries=json.dumps(
                    [item.model_dump(mode="json") for item in bundle.verify_queries],
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ),
            ),
            f"{bundle.procedure_spec.name} Oracle 修复",
        )
        raw_queries = data.get("verify_queries")
        if not isinstance(raw_queries, list) or not raw_queries:
            raise ValueError("Oracle 修复模型未返回 verify_queries")
        repaired.verify_queries = [
            VerifyQueryCandidate.model_validate(item) for item in raw_queries
        ]
    return repaired


def _candidate_result(bundle: CandidateBundle) -> dict:
    business_gate = next(
        (item for item in bundle.gate_results if item.gate == "business"),
        None,
    )
    business = business_gate.details.get("result") if business_gate else None
    if isinstance(business, dict):
        result = dict(business)
        result.setdefault("sp_id", None)
        result.setdefault("sp_name", bundle.procedure_spec.name)
    else:
        details = []
        for gate in bundle.gate_results:
            for error in gate.errors:
                details.append({
                    "type": error.category,
                    "pass": False,
                    "error": error.message,
                    "code": error.code,
                    "artifact": error.artifact,
                })
        passed_before_business = all(
            gate.passed for gate in bundle.gate_results
            if gate.gate != "business"
        )
        result = {
            "sp_id": None,
            "sp_name": bundle.procedure_spec.name,
            "syntax_ok": passed_before_business,
            "business_ok": False,
            "operation_type": bundle.sp_dict()["operation_type"],
            "bundle_hash": bundle.bundle_hash,
            "details": details,
        }
    result["candidate_status"] = bundle.status
    result["repair_count"] = bundle.repair_count
    result["bundle_hash"] = bundle.bundle_hash or result.get("bundle_hash", "")
    return result


def generate_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """使用已确认的 QuerySpec 生成纯内存候选；不得再次解释设计。"""
    llm = _get_llm()
    writer = _get_writer(config)
    try:
        raw_query_spec = state.get("query_spec")
        if not raw_query_spec:
            raise ValueError("已确认方案缺少 QuerySpec，请返回方案阶段重新确认")
        query_spec = QuerySpec.model_validate(raw_query_spec)
        _write_progress(writer, "schema", "正在绑定目标数据库实时 Schema...")
        schema_evidence = capture_schema_evidence(query_spec)
        if schema_evidence.unresolved:
            unresolved = "；".join(
                f"{item.identifier}: {item.reason}"
                for item in schema_evidence.unresolved
            )
            raise ValueError(f"Schema 精确绑定失败：{unresolved}")

        bundles = []
        for index, procedure_spec in enumerate(query_spec.procedures, start=1):
            _write_progress(
                writer,
                "candidate",
                f"正在生成候选 {index}/{len(query_spec.procedures)}：{procedure_spec.name}",
            )
            procedure_sql = _generate_procedure_candidate(
                llm, query_spec, procedure_spec, schema_evidence,
            )
            verify_queries = _generate_oracle_candidates(
                llm, query_spec, procedure_spec, schema_evidence,
            )
            bundles.append(CandidateBundle(
                query_spec=query_spec,
                procedure_spec=procedure_spec,
                procedure_sql=procedure_sql,
                verify_queries=verify_queries,
                schema_evidence=schema_evidence,
            ))
    except Exception as exc:
        return {
            "status": "generate_failed",
            "error": str(exc),
        }

    return {
        "query_spec": query_spec.model_dump(mode="json", by_alias=True),
        "candidate_bundles": [
            item.model_dump(mode="json", by_alias=True) for item in bundles
        ],
        "sp_list": [
            {"name": item.procedure_spec.name, "status": "candidate_generated"}
            for item in bundles
        ],
        "mode": "verify",
        "status": "candidate_generated",
        "error": "",
    }


def verify_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """校验整批内存候选；全部通过后在一个 SQLite 事务中替换。"""
    from app.db.sqlite import replace_session_sp_bundles_atomically
    from app.services.candidate_pipeline import validate_candidate_with_repairs

    writer = _get_writer(config)
    raw_bundles = state.get("candidate_bundles") or []
    if not raw_bundles:
        return {
            "status": "verify_failed",
            "verify_results": [],
            "error": "没有可校验的内存候选，旧制品保持不变",
        }

    llm = _get_llm()
    bundles = [
        CandidateBundle.model_validate_json(json.dumps(item, ensure_ascii=False))
        for item in raw_bundles
    ]
    validated = []
    for index, bundle in enumerate(bundles, start=1):
        _write_progress(
            writer,
            "verify",
            f"正在执行候选闸门 {index}/{len(bundles)}：{bundle.procedure_spec.name}",
        )
        try:
            checked = validate_candidate_with_repairs(
                bundle,
                lambda candidate, errors: _repair_candidate(
                    llm, candidate, errors,
                ),
                schema_refresher=capture_schema_evidence,
            )
        except Exception as exc:
            bundle.status = "failed"
            bundle.gate_results.append(GateResult(
                gate="business",
                passed=False,
                errors=[GateError(
                    artifact="bundle",
                    category="business",
                    code="harness_exception",
                    message=str(exc),
                    schema_subset=None,
                    repairable=False,
                )],
            ))
            validated.append(bundle)
            continue
        validated.append(checked)

    results = [_candidate_result(item) for item in validated]
    if any(item.status == "needs_review" for item in validated):
        return {
            "status": "needs_review",
            "candidate_bundles": [
                item.model_dump(mode="json", by_alias=True) for item in validated
            ],
            "verify_results": results,
            "error": "",
        }
    if any(item.status != "validated" for item in validated):
        return {
            "status": "verify_failed",
            "candidate_bundles": [
                item.model_dump(mode="json", by_alias=True) for item in validated
            ],
            "verify_results": results,
            "error": "",
        }

    try:
        inserted = replace_session_sp_bundles_atomically(
            state["session_id"], validated,
        )
    except Exception as exc:
        return {
            "status": "verify_failed",
            "candidate_bundles": [
                item.model_dump(mode="json", by_alias=True) for item in validated
            ],
            "verify_results": results,
            "error": f"候选已通过但原子保存失败，旧制品保持不变：{exc}",
        }

    ids_by_name = {item["name"]: item["id"] for item in inserted}
    for result in results:
        result["sp_id"] = ids_by_name.get(result["sp_name"])
        result["status"] = "persisted"
    return {
        "status": "persisted",
        "sp_list": inserted,
        "candidate_bundles": [
            item.model_dump(mode="json", by_alias=True) for item in validated
        ],
        "verify_results": results,
        "error": "",
    }
