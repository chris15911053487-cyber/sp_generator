"""LangGraph 节点实现 — 需求澄清、方案设计、代码生成、校验、部署。"""
import json
import re
import hashlib
from difflib import SequenceMatcher
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
    SYSTEM_PROMPT, DESIGN_FEEDBACK_PROMPT, ASSUMPTIONS_PROMPT,
    DECISION_PLAN_PROMPT, RELATIONAL_PLAN_V3_PROMPT,
    RESULT_CONTRACT_PROMPT, FACT_BLUEPRINT_PROMPT,
    COMPUTATION_BLUEPRINT_PROMPT, SOURCE_REQUIREMENTS_PROMPT,
)
from app.agent.tools import create_tools
from app.db.sqlite import get_messages, save_session_design
from app.services.decision_contract import (
    DecisionPlan, confirm_decision, freeze_decisions, parse_decision_plan,
)
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
    v3_artifacts: NotRequired[list[dict]]
    clarify_decisions: NotRequired[list[dict]]
    deferred_decisions: NotRequired[list[dict]]
    pending_clarify: NotRequired[dict | None]
    schema_fingerprint: NotRequired[str]
    decision_plan: NotRequired[dict]
    confirmed_decision_set: NotRequired[dict]
    semantic_design_hash: NotRequired[str]
    schema_catalog: NotRequired[dict]
    schema_artifacts: NotRequired[list[dict]]
    schema_resolution_checkpoints: NotRequired[list[dict]]
    schema_resolution_issues: NotRequired[list[dict]]
    pending_schema_interaction: NotRequired[dict | None]
    schema_resolution_status: NotRequired[str]
    semantic_revision: NotRequired[dict]
    semantic_revision_diff: NotRequired[dict]
    semantic_revision_count: NotRequired[int]
    result_contract: NotRequired[dict]
    fact_blueprint: NotRequired[dict]
    computation_blueprint: NotRequired[dict]
    semantic_inputs: NotRequired[dict]
    source_requirements: NotRequired[dict]
    expression_design: NotRequired[dict]
    semantic_obligations: NotRequired[dict]
    semantic_compile_result: NotRequired[dict]
    semantic_design_stage: NotRequired[str]
    semantic_design_diagnostics: NotRequired[list[dict]]


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


def _parse_clarify_decision(content: str) -> dict | None:
    """解析并校验模型返回的结构化澄清决策。"""
    data = _parse_json(content or "")
    if not isinstance(data, dict):
        return None

    action = str(data.get("action", "")).strip().lower()
    if action == "sufficient":
        summary = str(data.get("summary", "")).strip()
        return {"action": "sufficient", "summary": summary} if summary else None
    if action != "ask":
        return None

    key = str(data.get("decision_key", "")).strip().lower()
    decision_type = str(data.get("decision_type", "")).strip().lower()
    question = str(data.get("question", "")).strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", key):
        return None
    if decision_type not in {"blocking", "defaultable"} or not question:
        return None

    raw_options = data.get("options")
    if not isinstance(raw_options, list) or len(raw_options) < 2:
        return None
    options = []
    option_ids = set()
    for item in raw_options:
        if not isinstance(item, dict):
            return None
        option_id = str(item.get("id", "")).strip().upper()
        value = str(item.get("value", "")).strip()
        if not re.fullmatch(r"[A-Z]", option_id) or not value or option_id in option_ids:
            return None
        option_ids.add(option_id)
        options.append({"id": option_id, "value": value})

    return {
        "action": "ask",
        "decision_key": key,
        "decision_type": decision_type,
        "question": question,
        "options": options,
        "reason": str(data.get("reason", "")).strip(),
    }


def _normalize_decision_text(value: str) -> str:
    """生成领域无关的比较文本，仅移除格式差异。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value).lower())


def _decision_similarity(left: str, right: str) -> float:
    left = _normalize_decision_text(left)
    right = _normalize_decision_text(right)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _decision_bigram_overlap(left: str, right: str) -> float:
    left = _normalize_decision_text(left)
    right = _normalize_decision_text(right)
    left_parts = {left[i:i + 2] for i in range(max(0, len(left) - 1))}
    right_parts = {right[i:i + 2] for i in range(max(0, len(right) - 1))}
    if not left_parts or not right_parts:
        return 0.0
    return len(left_parts & right_parts) / len(left_parts | right_parts)


def _option_signature(decision: dict) -> str:
    values = [
        _normalize_decision_text(item.get("value", ""))
        for item in decision.get("options", [])
        if isinstance(item, dict)
    ]
    return "|".join(sorted(value for value in values if value))


def _is_duplicate_clarify_question(
    candidate: dict,
    existing_decisions: list[dict],
) -> bool:
    """稳定 key 主判重，题干和选项近似度作为改 key 后的兜底。"""
    candidate_key = candidate.get("decision_key")
    for existing in existing_decisions:
        if candidate_key and candidate_key == existing.get("decision_key"):
            return True
        question_similarity = _decision_similarity(
            candidate.get("question", ""),
            existing.get("question", ""),
        )
        if question_similarity >= 0.78:
            return True
        option_similarity = _decision_similarity(
            _option_signature(candidate),
            _option_signature(existing),
        )
        if question_similarity >= 0.55 and option_similarity >= 0.9:
            return True
    return False


def _resolve_clarify_answer(question_spec: dict, raw_answer) -> dict:
    """把选项字母展开成完整语义；自由文本原样保留。"""
    raw = str(raw_answer).strip()
    compact = re.sub(r"\s+", "", raw).strip("，,。.!！?？")
    match = re.fullmatch(r"(?:选(?:项)?|选择)?([a-z])", compact, re.IGNORECASE)
    selected_id = match.group(1).upper() if match else None

    options = question_spec.get("options", [])
    selected = next(
        (item for item in options if item.get("id") == selected_id),
        None,
    )
    if selected is None:
        normalized_raw = _normalize_decision_text(raw)
        matches = [
            item for item in options
            if _normalize_decision_text(item.get("value", "")) == normalized_raw
        ]
        selected = matches[0] if len(matches) == 1 else None

    if selected is not None:
        return {
            "selected_option_id": selected["id"],
            "answer": selected["value"],
            "raw_answer": raw,
        }
    return {
        "selected_option_id": None,
        "answer": raw,
        "raw_answer": raw,
    }


def _render_clarify_question(question_spec: dict) -> str:
    lines = [question_spec["question"]]
    lines.extend(
        f"{item['id']}. {item['value']}" for item in question_spec["options"]
    )
    return "\n".join(lines)


def _merge_assumptions(
    generated: list,
    deferred: list[dict],
    clarified: list[dict],
) -> list[dict]:
    """合并关键项，确保延后项不丢失且已确认项不再出现。"""
    answered_keys = {
        item.get("decision_key") for item in clarified if item.get("decision_key")
    }
    merged = []
    seen = set(answered_keys)
    for item in generated:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        title = str(item.get("title", "")).strip()
        value = str(item.get("value", "")).strip()
        if not key or key in seen or not title or not value:
            continue
        if any(
            _decision_similarity(title, decision.get("question", "")) >= 0.5
            and _decision_bigram_overlap(
                title, decision.get("question", ""),
            ) >= 0.25
            for decision in clarified
        ):
            continue
        merged.append({
            "key": key,
            "title": title,
            "value": value,
            "reason": str(item.get("reason", "")).strip(),
        })
        seen.add(key)

    for item in deferred:
        key = item.get("decision_key")
        if not key or key in seen:
            continue
        options = item.get("options") or []
        default_value = options[0].get("value", "") if options else ""
        if not default_value:
            continue
        merged.append({
            "key": key,
            "title": item.get("question", key),
            "value": default_value,
            "reason": item.get("reason", ""),
        })
        seen.add(key)
    return merged


def _ensure_reconciliation_assumptions(
    requirements: str,
    generated: list,
    resolved_decision_keys: set[str] | None = None,
) -> list:
    """为跨来源财务金额对账补齐不可静默假设的业务口径。"""
    text = str(requirements or "").casefold()
    is_reconciliation = any(
        token in text for token in ("对账", "比对", "核对", "reconcil", "compare")
    )
    is_financial_amount = any(
        token in text
        for token in (
            "收入", "金额", "发票", "凭证", "总账",
            "revenue", "amount", "invoice", "journal", "ledger",
        )
    )
    if not (is_reconciliation and is_financial_amount):
        return generated
    required = [
        {
            "key": "currency_basis",
            "title": "对账币种口径",
            "value": "销售收入与财务凭证统一按账套本位币比较",
            "reason": "两侧币种口径不同会产生不可解释的金额差异",
        },
        {
            "key": "revenue_amount_basis",
            "title": "销售收入金额口径",
            "value": "采用不含税销售收入",
            "reason": "含税价款与不含税收入的业务含义不同",
        },
        {
            "key": "journal_sign_basis",
            "title": "凭证收入方向",
            "value": "收入净发生额按贷方减借方计算",
            "reason": "借贷方向决定冲销和红字业务如何计入收入",
        },
        {
            "key": "cancellation_reversal_policy",
            "title": "作废与冲销处理",
            "value": "销售端排除作废单据，财务端保留借方冲销抵减收入",
            "reason": "两侧冲销处理不一致会形成虚假差异",
        },
    ]
    result = list(generated)
    existing_keys = {
        str(item.get("key", "")).strip()
        for item in result if isinstance(item, dict)
    }
    existing_text = " ".join(
        f"{item.get('title', '')} {item.get('value', '')}".casefold()
        for item in result if isinstance(item, dict)
    )
    coverage_tokens = {
        "currency_basis": ("币种", "本位币", "currency"),
        "revenue_amount_basis": ("含税", "不含税", "收入金额口径"),
        "journal_sign_basis": ("贷方减借方", "借贷方向", "净发生额"),
        "cancellation_reversal_policy": ("作废", "冲销", "红字"),
    }
    resolved = set(resolved_decision_keys or set())
    resolved_aliases = {
        "currency_basis": {"currency_basis", "currency_scope"},
        "revenue_amount_basis": {
            "revenue_amount_basis", "amount_basis", "amount_type",
        },
        "journal_sign_basis": {
            "journal_sign_basis", "debit_credit_direction", "sign_basis",
        },
        "cancellation_reversal_policy": {
            "cancellation_reversal_policy", "reversal_policy",
            "cancel_policy",
        },
    }
    for item in required:
        if resolved & resolved_aliases[item["key"]]:
            continue
        if item["key"] in existing_keys:
            continue
        if any(
            token in existing_text
            for token in coverage_tokens[item["key"]]
        ):
            continue
        result.append(item)
    return result


def _validate_assumption_semantics(items: list[dict]) -> None:
    from app.services.semantic_guard import assert_semantic_text

    for item in items:
        if not isinstance(item, dict):
            raise ValueError("关键项必须是结构化对象")
        key = str(item.get("key", "")).strip() or "(unknown)"
        assert_semantic_text(item.get("title", ""), f"关键项 {key} 的标题")
        assert_semantic_text(item.get("value", ""), f"关键项 {key} 的建议值")
        assert_semantic_text(item.get("reason", ""), f"关键项 {key} 的原因")


def _decision_plan_transition(
    plan: DecisionPlan,
    *,
    requirements: str,
    clarify_count: int,
    clarified_decisions: list[dict],
) -> dict:
    for item in list(plan.decisions):
        if item.status != "pending":
            continue
        candidate = {
            "decision_key": item.decision_key,
            "question": item.question,
            "options": [option.model_dump() for option in item.options],
        }
        existing = next(
            (
                decision for decision in clarified_decisions
                if _is_duplicate_clarify_question(candidate, [decision])
            ),
            None,
        )
        if existing is not None:
            plan = confirm_decision(
                plan,
                item.decision_key,
                value=existing.get("answer", ""),
                selected_option_id=existing.get("selected_option_id"),
                source="user",
            )
    pending_blocking = next(
        (
            item for item in plan.decisions
            if item.status == "pending" and item.decision_type == "blocking"
        ),
        None,
    )
    deferred = [
        {
            "action": "ask",
            "decision_key": item.decision_key,
            "decision_type": item.decision_type,
            "question": item.question,
            "options": [option.model_dump() for option in item.options],
            "reason": item.reason,
            "recommended_option_id": item.recommended_option_id,
        }
        for item in plan.decisions
        if item.status == "pending" and item.decision_type == "defaultable"
    ]
    base = {
        "requirements": requirements or plan.requirements_summary,
        "clarify_count": clarify_count,
        "clarify_decisions": clarified_decisions,
        "deferred_decisions": deferred,
        "decision_plan": plan.model_dump(mode="json"),
    }
    if pending_blocking is None:
        return {
            **base,
            "mode": "assumptions",
            "status": "clarified",
            "pending_clarify": None,
        }
    pending = {
        "action": "ask",
        "decision_key": pending_blocking.decision_key,
        "decision_type": pending_blocking.decision_type,
        "question": pending_blocking.question,
        "options": [option.model_dump() for option in pending_blocking.options],
        "reason": pending_blocking.reason,
    }
    return {
        **base,
        "mode": "clarify_answer",
        "status": "clarify_question_ready",
        "pending_clarify": pending,
    }


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


def _render_query_spec(query_spec: object) -> str:
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
        lines.append(
            f"- 结果形状：{procedure.result_contract.cardinality}；"
            f"允许空结果：{'是' if procedure.result_contract.allow_empty else '否'}"
        )
        for item in procedure.verification_rules:
            if item.mode == "aggregate":
                columns = "、".join(
                    (
                        f"{metric.operation}("
                        f"{_code(metric.actual_column) if metric.actual_column else '*'}"
                        f") → {_code(metric.expected_column)}"
                    )
                    for metric in item.metrics
                )
            elif item.mode == "zero_rows":
                columns = "异常证据：" + "、".join(
                    _code(name) for name in item.evidence_columns
                )
            else:
                parts = []
                if item.key_columns:
                    parts.append(
                        "键：" + "、".join(
                            _code(name) for name in item.key_columns
                        )
                    )
                if item.compare_columns:
                    parts.append(
                        "比较列：" + "、".join(
                            _code(name) for name in item.compare_columns
                        )
                    )
                columns = "；".join(parts)
            lines.append(
                f"- {item.name}（{item.mode}/{item.role}）："
                f"{item.description}；{columns or '无额外列'}"
            )

    return "\n".join(lines)


def _render_semantic_design(design) -> str:
    """渲染用户需要确认的纯业务语义，不泄露或提前选择物理 Schema。"""
    lines = ["## 业务语义方案", "", "以下内容确认后才会绑定实际数据库对象。"]
    for contract in design.contracts:
        lines.extend([
            "",
            f"### {_code(contract.procedure_name)}",
            "",
            f"- 业务目的：{contract.purpose}",
            f"- 结果模式：{contract.result_mode}",
            f"- 允许空结果：{'是' if contract.allow_empty else '否'}",
            "",
            "#### 业务实体",
        ])
        for entity in contract.entities:
            lines.append(f"- {_code(entity.id)}：{entity.meaning}")
        lines.extend(["", "#### 参数"])
        if contract.parameters:
            for item in contract.parameters:
                lines.append(
                    f"- {_code(item.name)}：{item.meaning}；类型 "
                    f"{item.logical_type}；边界 {item.boundary}；"
                    f"{'必填' if item.required else '可选'}"
                )
        else:
            lines.append("- 无")
        lines.extend(["", "#### 过滤口径"])
        if contract.filters:
            for item in contract.filters:
                lines.append(
                    f"- {_code(item.id)}：{item.meaning}；运算 {item.operator}"
                )
        else:
            lines.append("- 无")
        lines.extend(["", "#### 输出"])
        for item in contract.outputs:
            lines.append(
                f"- {_code(item.name)}：{item.meaning}（{item.logical_type}）"
            )
        lines.append(
            "- 业务粒度：" + (
                "、".join(_code(item) for item in contract.grain)
                if contract.grain else "单行汇总"
            )
        )
    return "\n".join(lines)


def clarify_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """生成并持久化下一项澄清决策，不在本节点内等待回答。"""
    llm = _get_llm()
    stream_writer = _get_writer(config)
    _write_progress(stream_writer, "clarify", "正在分析需求并准备下一个关键问题...")

    # 如果 mode 已经跳过了需求确认阶段，直接 pass-through
    current_mode = state.get("mode", "clarify")
    if current_mode in ("clarify_answer", "assumptions", "design", "generate"):
        return {
            "requirements": state.get("requirements", ""),
            "mode": current_mode,
            "status": state.get("status", ""),
            "clarify_count": state.get("clarify_count", 0),
            "clarify_decisions": state.get("clarify_decisions", []),
            "deferred_decisions": state.get("deferred_decisions", []),
            "pending_clarify": state.get("pending_clarify"),
        }

    chat_history = _build_chat_history(state["session_id"])
    clarified = state.get("requirements", "")
    clarify_count = state.get("clarify_count", 0) or 0
    clarified_decisions = list(state.get("clarify_decisions", []))
    deferred_decisions = list(state.get("deferred_decisions", []))

    raw_plan = state.get("decision_plan")
    if raw_plan:
        plan = DecisionPlan.model_validate(raw_plan)
        return _decision_plan_transition(
            plan,
            requirements=clarified,
            clarify_count=clarify_count,
            clarified_decisions=clarified_decisions,
        )

    # 没有结构化计划的旧状态才使用消息数安全上限；有计划时必须逐项解决阻塞决策。
    if clarify_count >= 5:
        return {
            "requirements": clarified,
            "mode": "assumptions",
            "status": "clarified",
            "clarify_count": clarify_count,
            "clarify_decisions": clarified_decisions,
            "deferred_decisions": deferred_decisions,
            "pending_clarify": None,
        }

    prompt = DECISION_PLAN_PROMPT.format(
        user_input=state["user_input"],
        chat_history=chat_history,
    )
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
    # 一次生成完整决策清单；无效时只纠正一次。
    for attempt in range(2):
        response = llm.invoke(
            messages,
            extra_body={"thinking": {"type": "disabled"}},
        )
        try:
            plan = parse_decision_plan(response.content or "")
            return _decision_plan_transition(
                plan,
                requirements=clarified,
                clarify_count=clarify_count,
                clarified_decisions=clarified_decisions,
            )
        except Exception as exc:
            if attempt == 0:
                reason = str(exc)
            else:
                break
            messages.extend([
                response,
                HumanMessage(content=(
                    f"DecisionPlan 不合法：{reason}。请返回完整、合法的 plan JSON。"
                )),
            ])

    return {
        "requirements": clarified or state["user_input"],
        "mode": "assumptions",
        "status": "clarified",
        "clarify_count": clarify_count,
        "clarify_decisions": clarified_decisions,
        "deferred_decisions": deferred_decisions,
        "pending_clarify": None,
    }


def clarify_answer_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    """仅等待并保存答案；恢复中断时不会重新调用模型生成问题。"""
    pending = state.get("pending_clarify")
    if not isinstance(pending, dict):
        return {
            "mode": "assumptions",
            "status": "clarified",
            "pending_clarify": None,
        }

    clarify_count = state.get("clarify_count", 0) or 0
    q_num = clarify_count + 1
    answer = interrupt({
        "type": "clarify",
        "question": _render_clarify_question(pending),
        "q_num": q_num,
        "decision_key": pending["decision_key"],
    })
    resolved = _resolve_clarify_answer(pending, answer)
    selected_suffix = (
        f"（选项 {resolved['selected_option_id']}）"
        if resolved["selected_option_id"] else "（自由输入）"
    )
    entry = {
        "decision_key": pending["decision_key"],
        "question": pending["question"],
        "options": pending["options"],
        "selected_option_id": resolved["selected_option_id"],
        "answer": resolved["answer"],
        "raw_answer": resolved["raw_answer"],
    }
    clarified_decisions = list(state.get("clarify_decisions", []))
    clarified_decisions.append(entry)
    updated_plan = state.get("decision_plan")
    if updated_plan:
        plan = confirm_decision(
            DecisionPlan.model_validate(updated_plan),
            pending["decision_key"],
            value=resolved["answer"],
            selected_option_id=resolved["selected_option_id"],
            source="user",
        )
        updated_plan = plan.model_dump(mode="json")
    block = (
        f"Q{q_num} [{pending['decision_key']}]: {pending['question']}\n"
        f"A: {resolved['answer']}{selected_suffix}\n"
    )
    requirements = state.get("requirements", "")
    new_requirements = f"{requirements}\n{block}" if requirements else block
    return {
        "requirements": new_requirements,
        "mode": "clarify",
        "status": "clarifying",
        "clarify_count": clarify_count + 1,
        "clarify_decisions": clarified_decisions,
        "deferred_decisions": state.get("deferred_decisions", []),
        "decision_plan": updated_plan,
        "pending_clarify": None,
    }


def assumptions_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """关键项确认节点 — LLM 生成关键假设列表，用户逐项确认/修改后进入设计。"""
    stream_writer = _get_writer(config)
    _write_progress(stream_writer, "assumptions", "正在整理需要确认的关键项...")

    # 如果 mode 已跳过，直接 pass-through
    current_mode = state.get("mode", "assumptions")
    if current_mode in ("design", "generate"):
        return {
            "confirmed_assumptions": state.get("confirmed_assumptions", ""),
            "mode": current_mode,
        }

    clarified_decisions = list(state.get("clarify_decisions", []))
    deferred_decisions = list(state.get("deferred_decisions", []))
    raw_plan = state.get("decision_plan")
    plan = DecisionPlan.model_validate(raw_plan) if raw_plan else None

    if plan is not None:
        assumptions_list = [
            {
                "key": item.decision_key,
                "title": item.question,
                "value": next(
                    option.value for option in item.options
                    if option.id == item.recommended_option_id
                ),
                "reason": item.reason,
            }
            for item in plan.decisions
            if item.status == "pending" and item.decision_type == "defaultable"
        ]
    else:
        # 旧状态兼容：没有 DecisionPlan 时沿用原关键项生成路径。
        llm = _get_llm()
        prompt = ASSUMPTIONS_PROMPT.format(
            requirements=state.get("requirements", ""),
            clarify_decisions=json.dumps(
                clarified_decisions, ensure_ascii=False, separators=(",", ":"),
            ),
            deferred_decisions=json.dumps(
                deferred_decisions, ensure_ascii=False, separators=(",", ":"),
            ),
        )
        generated_assumptions = []
        messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
        for attempt in range(2):
            response = llm.invoke(messages)
            assumptions_data = _parse_json(response.content)
            generated_assumptions = (
                assumptions_data["assumptions"]
                if assumptions_data
                and isinstance(assumptions_data.get("assumptions"), list)
                else []
            )
            try:
                _validate_assumption_semantics(generated_assumptions)
                break
            except Exception as exc:
                if attempt == 1:
                    raise
                messages.extend([
                    response,
                    HumanMessage(content=(
                        f"关键项违反纯业务语义边界：{exc}。"
                        "请删除所有数据库、表名、字段名、SQL、技术状态码，"
                        "只返回业务口径的完整 assumptions JSON。"
                    )),
                ])
        assumptions_list = _merge_assumptions(
            generated_assumptions,
            deferred_decisions,
            clarified_decisions,
        )

    assumptions_list = _ensure_reconciliation_assumptions(
        state.get("requirements", ""),
        assumptions_list,
        {
            str(item.get("decision_key", "")).strip()
            for item in clarified_decisions
            if isinstance(item, dict)
        },
    )

    if not assumptions_list:
        # 无关键项需确认，直接进入设计
        result = {
            "confirmed_assumptions": "无特殊关键项",
            "mode": "design",
            "status": "assumptions_confirmed",
        }
        if plan is not None:
            confirmed_set = freeze_decisions(plan)
            result["decision_plan"] = plan.model_dump(mode="json")
            result["confirmed_decision_set"] = confirmed_set.model_dump(mode="json")
        return result

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
        raise ValueError("关键项必须逐项确认或修改，不能用普通文本整体确认")

    # 构建确认结果文本
    lines = []
    for a in assumptions_list:
        key = a["key"]
        if key in modified_items:
            lines.append(f"- {a['title']}：{modified_items[key]}（用户修改）")
            if plan is not None:
                plan = confirm_decision(
                    plan, key,
                    value=str(modified_items[key]),
                    selected_option_id=None,
                    source="user",
                )
        elif key in confirmed_keys:
            lines.append(f"- {a['title']}：{a['value']}（已确认）")
            if plan is not None:
                decision = next(
                    (
                        item for item in plan.decisions
                        if item.decision_key == key
                    ),
                    None,
                )
                if decision is not None:
                    plan = confirm_decision(
                        plan, key,
                        value=a["value"],
                        selected_option_id=decision.recommended_option_id,
                        source="default",
                    )
        else:
            # 未勾选的项 — 忽略，不纳入设计
            pass

    confirmed_text = "\n".join(lines) if lines else "用户未确认任何关键项，使用默认设置"

    result = {
        "confirmed_assumptions": confirmed_text,
        "mode": "design",
        "status": "assumptions_confirmed",
    }
    if plan is not None:
        confirmed_set = freeze_decisions(plan)
        result["decision_plan"] = plan.model_dump(mode="json")
        result["confirmed_decision_set"] = confirmed_set.model_dump(mode="json")
    return result


class DesignContractError(ValueError):
    def __init__(self, message: str, summary: str = "", raw_draft=None):
        super().__init__(message)
        self.summary = summary
        self.raw_draft = raw_draft


def _design_version_for_state(state: AgentState) -> str:
    confirmed_set = state.get("confirmed_decision_set")
    if isinstance(confirmed_set, dict) and confirmed_set.get("decision_hash"):
        return str(confirmed_set["decision_hash"])
    payload = {
        "requirements": state.get("requirements", ""),
        "clarify_decisions": state.get("clarify_decisions", []),
        "confirmed_assumptions": state.get("confirmed_assumptions", ""),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _persist_design(
    state: AgentState,
    *,
    status: str,
    summary: str,
    raw_draft: dict | None,
    query_spec,
    diagnostics: list,
    schema_fingerprint: str | None = None,
) -> None:
    save_session_design(
        state["session_id"],
        status=status,
        summary=summary,
        decision_plan={
            "requirements": state.get("requirements", ""),
            "clarify_decisions": state.get("clarify_decisions", []),
            "confirmed_assumptions": state.get("confirmed_assumptions", ""),
        },
        decision_hash=_design_version_for_state(state),
        query_spec_draft=raw_draft,
        query_spec=(
            query_spec.model_dump(mode="json", by_alias=True)
            if query_spec is not None else None
        ),
        query_spec_version=(
            query_spec.version if query_spec is not None else None
        ),
        verification_plan=None,
        verification_plan_hash=None,
        diagnostics=diagnostics,
        schema_fingerprint=schema_fingerprint,
    )


def _design_envelope(content: str) -> tuple[str, dict]:
    data = _parse_json(content)
    if not isinstance(data, dict):
        raise DesignContractError("模型未返回有效的 DesignEnvelope JSON")
    if isinstance(data.get("semantic_design"), dict):
        return str(data.get("summary", "")).strip(), data["semantic_design"]
    if isinstance(data.get("contracts"), list):
        return "", data
    raise DesignContractError(
        "DesignEnvelope 缺少 semantic_design",
        str(data.get("summary", "")).strip(),
        data,
    )


def _canonicalize_full_day_boundaries(raw_draft: dict) -> None:
    """把已明确的自然日范围编译为唯一的参数边界表示。"""
    for contract in raw_draft.get("contracts") or []:
        parameters = {
            str(item.get("id")): item
            for item in contract.get("parameters") or []
            if isinstance(item, dict) and item.get("id")
        }
        for filter_item in contract.get("filters") or []:
            if not isinstance(filter_item, dict):
                continue
            parameter_ids = filter_item.get("parameter_ids") or []
            if len(parameter_ids) != 2:
                continue
            start = parameters.get(str(parameter_ids[0]))
            end = parameters.get(str(parameter_ids[1]))
            if (
                filter_item.get("operator") == "between"
                and start is not None
                and end is not None
                and start.get("logical_type") == "date"
                and end.get("logical_type") == "date"
                and end.get("boundary") == "inclusive_full_day"
            ):
                filter_item["operator"] = "full_day_range"
            if filter_item.get("operator") != "full_day_range":
                continue
            if start is not None:
                start["boundary"] = "inclusive"
            if end is not None:
                end["boundary"] = "inclusive_full_day"


def _explicit_output_count(requirements: str) -> int | None:
    match = re.search(
        r"(?:只(?:允许)?|共)\s*(?:返回)?\s*(\d{1,3})\s*列",
        str(requirements or ""),
    )
    return int(match.group(1)) if match else None


def _explicit_output_names(requirements: str) -> list[str]:
    match = re.search(
        r"(?:只(?:允许)?|共)?\s*(?:返回)?\s*\d{1,3}\s*列\s*[：:]"
        r"([^。\n]+)",
        str(requirements or ""),
    )
    if not match:
        return []
    return re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", match.group(1))


def _canonicalize_explicit_output_names(
    raw_draft: dict,
    requirements: str,
) -> None:
    names = _explicit_output_names(requirements)
    contracts = raw_draft.get("contracts") or []
    if len(contracts) != 1 or not names:
        return
    outputs = contracts[0].get("outputs") or []
    if len(names) != len(outputs):
        return
    for output, name in zip(outputs, names):
        if isinstance(output, dict):
            output["name"] = name


def _canonicalize_business_output_names_v3(raw_draft: dict) -> None:
    from app.services.semantic_guard import canonical_business_output_name

    for contract in raw_draft.get("contracts") or []:
        for output in contract.get("outputs") or []:
            if isinstance(output, dict) and output.get("name"):
                output["name"] = canonical_business_output_name(
                    output["name"]
                )


def _validate_explicit_output_count(design, requirements: str) -> None:
    expected = _explicit_output_count(requirements)
    if expected is None or len(design.contracts) != 1:
        return
    actual = len(design.contracts[0].outputs)
    if actual != expected:
        raise ValueError(
            f"用户明确要求 {expected} 个输出，SemanticDesign 实际包含 {actual} 个"
        )


def _generate_design_query_spec(
    llm: ChatOpenAI,
    state: AgentState,
) -> tuple[object, str, dict]:
    """从确认事实生成纯 SemanticDesign；此阶段禁止出现物理表和字段。"""
    from app.contracts.semantic import SemanticDesign

    schema = json.dumps(
        SemanticDesign.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    decisions = json.dumps(
        state.get("confirmed_decision_set")
        or state.get("clarify_decisions", []),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    prompt = f"""
根据已确认需求生成纯业务 SemanticDesign。

需求：
{state.get("requirements", "")}

已确认决策：
{decisions}

已确认假设：
{state.get("confirmed_assumptions", "无特殊关键项")}

修改上下文：
{state.get("user_input", "") if state.get("design_phase") in {"prepare_feedback", "invalid"} else ""}

硬性约束：
1. 只表达业务含义，不得出现数据库名、schema、表名、列名、object_id、column_id 或 SQL。
2. 第一阶段只允许查询型、单结果集存储过程。
3. entity.id、output.id、filter.field_ids 都是业务语义 ID。
4. 明细/异常结果必须声明稳定 grain。自然日范围必须使用
   full_day_range，起始参数 boundary=inclusive，结束参数
   boundary=inclusive_full_day；不得把起始参数标为 inclusive_full_day。
5. 输出必须与用户要求一一对应，不得为实体或 grain 额外创建重复键列；
   grain 必须引用已存在的唯一业务键输出。
6. 用户要求的每个输出都必须在 outputs 中出现。只有用户确认需要计算的输出
   才能进入 derived_fields；已确认“直接取单据记录值”的输出不得改写为派生值。
7. 不得输出 verification_rules、zero_rows、change_set 或任何旧校验协议。
8. 返回 JSON：{{"summary":"面向用户的方案摘要","semantic_design":{{...}}}}。
9. 每个 entity 必须只有一个明确的业务粒度。单据头、单据行、凭证头、
   凭证明细、科目等粒度不同的业务对象必须拆成独立 entity；不得用
   “包含头与行”“包含明细及主数据”等复合实体规避建模。一个 entity 在
   SchemaBinding 阶段只绑定一个物理对象。
10. 用于跨来源匹配的稳定内部业务标识与面向用户展示的单据/凭证编号是两个
    不同语义字段，不得混用。事实必须显式包含完成底层关联所需的实体和字段。

SemanticDesign JSON Schema：
{schema}
"""
    prompt += """

结构化事实规则：
- 单实体明细查询可以不声明 facts。
- 汇总查询或多实体/多来源查询必须声明 source_fields、facts、多事实时的
  fact_joins，以及覆盖全部输出的 result_bindings。
- facts 必须按可独立从底层数据证明的业务来源拆分；不能创建
  final_result、sp_result 一类复制最终 SP 的伪事实。
- 最终匹配键、合并方式和公式必须冻结在 fact_joins/result_bindings 中。
  exception_rows 还必须用 result_filter 声明异常选择条件。
- facts 合同的 derived_fields 必须为空；计算输出只写 result_bindings，
  可以用 kind=output 引用另一个已声明输出，但不得形成循环。
- 结果公式引用存储过程参数时必须用 kind=parameter 和 parameter_id；
  不得把参数伪装成 output。判断空值必须用 kind=unary、operator=IS NULL
  或 IS NOT NULL，不得生成“字段 = NULL”。
- source_fields 只能声明需要绑定到数据库的底层业务字段，不得虚构净额、
  差额等计算字段。事实指标需要“贷方－借方”等行级计算时，在 measure 的
  expression 中用 kind=field 引用多个 source_fields，再声明聚合方式。
- 事实维度如果就是底层字段，使用 field_id；如果“年月、分类、拼接键”等含义
  需要从底层字段派生，必须使用 dimension.expression 并声明 logical_type，
  禁止只在 meaning 中声称“由某字段派生”。维度表达式可使用 YEAR、MONTH、
  CONCAT、COALESCE 和受控算术。
- 每个事实的 entity_ids 必须覆盖形成该事实所需的全部单粒度实体。例如业务
  单据头与行、凭证头与明细、明细与科目分类分别是独立实体，再由冻结的
  SchemaBinding 关系连接；不得把多个粒度压进一个 entity。
- 只要参数出现在 filter.parameter_ids 中，就必须 required=true 或提供非空
  default；当前协议不支持用 NULL 表示“跳过过滤”，不得生成会把可选 NULL
  直接放进 eq/between/full_day_range 的合同。
"""
    response = _invoke_with_tools(
        llm,
        [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)],
        max_rounds=3,
    )
    raw_response = response.content or ""

    for attempt in range(2):
        summary = ""
        raw_draft = None
        try:
            summary, raw_draft = _design_envelope(raw_response)
            raw_draft = dict(raw_draft)
            decision_hash = _design_version_for_state(state)
            raw_draft["version"] = 3
            raw_draft["design_version"] = decision_hash
            raw_draft["decision_hash"] = decision_hash
            for contract_draft in raw_draft.get("contracts") or []:
                contract_draft["version"] = 3
                procedure_name = str(
                    contract_draft.get("procedure_name") or ""
                )
                contract_draft["contract_id"] = (
                    f"{decision_hash}:{procedure_name}"
                )
            _canonicalize_full_day_boundaries(raw_draft)
            _canonicalize_explicit_output_names(
                raw_draft, state.get("requirements", ""),
            )
            _canonicalize_business_output_names_v3(raw_draft)
            _promote_fact_derived_bindings_v3(raw_draft)
            _normalize_fact_expression_roles_v3(raw_draft)
            raw_draft = _strip_redundant_physical_annotations_v3(
                raw_draft
            )
            design = SemanticDesign.model_validate(raw_draft)
            _validate_explicit_output_count(
                design, state.get("requirements", ""),
            )
            return design, summary, raw_draft
        except Exception as exc:
            if attempt == 1:
                if isinstance(exc, DesignContractError):
                    raise
                raise DesignContractError(
                    f"方案无法形成有效业务契约：{exc}",
                    summary,
                    raw_draft,
                ) from exc
            error_payload = (
                exc.errors(include_url=False)
                if hasattr(exc, "errors") else [{"message": str(exc)}]
            )
            repair_prompt = f"""
修复以下 SemanticDesign JSON。不得引入物理表、字段、SQL 或旧校验协议。
修复时不得删除用户要求的输出，也不得把已确认直接取值的金额改成派生计算。
原响应：
{raw_response}
校验错误：
{json.dumps(error_payload, ensure_ascii=False, default=str, indent=2)}
目标 Schema：
{schema}
只返回修复后的 DesignEnvelope JSON。
"""
            repair_prompt += """

若合同包含 facts：
- derived_fields 必须为空，所有最终公式都放在 result_bindings；
- 计算输出可用 kind=output 引用其他输出；
- source_fields 只能是可绑定的底层字段；
- 多源行级计算写在 fact measure.expression 中，以 kind=field 引用源字段。
- 年月、分类等派生事实维度必须写 dimension.expression 和 logical_type，
  不能把日期 field_id 直接冒充字符串期间。
- 每个 entity 只能表达一个业务粒度；头/行、主记录/明细、凭证/科目等不同
  粒度必须拆成独立 entity，并在 fact.entity_ids 中完整引用。
- 跨来源匹配的稳定内部标识不能与展示编号混为一个语义字段。
- filter 引用的参数必须是必填参数或有非空默认值，不能把可选 NULL 参数直接
  用于比较。
- 结果公式引用参数使用 kind=parameter；空值判断使用 unary IS NULL /
  IS NOT NULL，不能把参数写成 output，也不能用二元等号比较 NULL。
"""
            repaired = llm.invoke([
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=repair_prompt),
            ])
            raw_response = repaired.content or ""

    raise DesignContractError("方案契约生成失败")


class SemanticStageError(ValueError):
    def __init__(
        self,
        stage: str,
        code: str,
        message: str,
        *,
        evidence: dict | None = None,
        repair_count: int = 0,
    ):
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.evidence = evidence or {}
        self.repair_count = repair_count


def _semantic_decision_hash(state: AgentState) -> str:
    value = _design_version_for_state(state)
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _confirmed_decision_keys(state: AgentState) -> set[str]:
    confirmed = state.get("confirmed_decision_set")
    if not isinstance(confirmed, dict):
        return set()
    return {
        str(item["key"])
        for item in confirmed.get("decisions", [])
        if (
            isinstance(item, dict)
            and item.get("key")
            and item.get("contract_relevant", True)
        )
    }


def _load_semantic_design_checkpoint(state: AgentState):
    from app.contracts.semantic_design_state import SemanticDesignCheckpoint
    from app.db.sqlite import (
        get_semantic_design_checkpoint,
        invalidate_semantic_design_checkpoint,
    )
    from app.services.semantic_design_checkpoints import (
        new_semantic_design_checkpoint,
    )

    decision_hash = _semantic_decision_hash(state)
    raw = get_semantic_design_checkpoint(state["session_id"])
    if raw is not None:
        raw.pop("updated_at", None)
        try:
            checkpoint = SemanticDesignCheckpoint.model_validate(raw)
        except Exception:
            checkpoint = None
        if checkpoint is not None:
            if (
                checkpoint.decision_hash == decision_hash
                and checkpoint.status != "invalidated"
            ):
                return checkpoint
        invalidate_semantic_design_checkpoint(
            state["session_id"],
            except_decision_hash=decision_hash,
        )
    return new_semantic_design_checkpoint(
        state["session_id"], decision_hash,
    )


def _generate_semantic_stage(
    llm,
    *,
    stage: str,
    contract_type,
    instruction: str,
    state: AgentState,
    upstream: dict,
    validator=None,
    transformer=None,
):
    schema = json.dumps(
        contract_type.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    base_prompt = f"""{instruction}

用户需求：
{state.get("requirements", "")}

已确认业务决策：
{json.dumps(state.get("confirmed_decision_set") or {}, ensure_ascii=False)}

已冻结上游契约：
{json.dumps(upstream, ensure_ascii=False, sort_keys=True)}

目标 JSON Schema：
{schema}
"""
    response_text = ""
    last_error = None
    for attempt in range(2):
        prompt = base_prompt
        if attempt:
            prompt += f"""

上一次输出：
{response_text}

确定性校验错误：
{last_error}

只修复当前 {stage}，不得修改或重述上游契约。只输出修复后的 JSON。
"""
        response = llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])
        response_text = response.content or ""
        try:
            data = _parse_json(response_text)
            if not isinstance(data, dict):
                raise ValueError("模型没有返回 JSON 对象")
            if isinstance(data.get(stage), dict):
                data = data[stage]
            value = contract_type.model_validate(data)
            if transformer is not None:
                value = transformer(value)
            if validator is not None:
                validator(value)
            return value, attempt
        except Exception as exc:
            if hasattr(exc, "errors"):
                raw_error = _normalize_semantic_validation_errors(
                    stage,
                    exc.errors(include_url=False),
                )
            elif hasattr(exc, "code") or hasattr(exc, "evidence"):
                raw_error = [{
                    "code": getattr(exc, "code", exc.__class__.__name__),
                    "message": str(exc),
                    "evidence": getattr(exc, "evidence", {}),
                }]
            else:
                raw_error = [{"message": str(exc)}]
            last_error = json.loads(json.dumps(
                raw_error,
                ensure_ascii=False,
                default=str,
            ))
    stable_codes = {
        item.get("code")
        for item in (last_error or [])
        if item.get("code")
    }
    raise SemanticStageError(
        stage,
        (
            next(iter(stable_codes))
            if len(stable_codes) == 1
            else "SEMANTIC_STAGE_INVALID"
        ),
        f"{stage} 连续两次无法通过确定性校验",
        evidence={"errors": last_error},
        repair_count=1,
    )


def _normalize_semantic_validation_errors(stage: str, errors: list[dict]):
    known_codes = (
        "COMPUTATION_TARGET_MISSING",
        "COMPUTATION_TARGET_DUPLICATE",
        "COMPUTATION_TARGET_CHANGED",
        "COMPUTATION_INPUT_UNKNOWN",
        "COMPUTATION_INPUT_MISSING",
        "COMPUTATION_INPUT_EXTRA",
        "COMPUTATION_INPUT_UNUSED",
        "COMPUTATION_INPUT_TYPE_MISMATCH",
        "COMPUTATION_RESULT_TYPE_MISMATCH",
        "COMPUTATION_DEPENDENCY_CYCLE",
        "PARAMETER_CONTEXT_INVALID",
        "SOURCE_INPUT_IMPLEMENTATION_MISSING",
        "SOURCE_INPUT_IMPLEMENTATION_EXTRA",
        "SOURCE_INPUT_OWNER_UNKNOWN",
        "POLICY_COMPUTATION_NOT_COVERED",
        "SOURCE_FILTER_ARGUMENT_COUNT_INVALID",
        "POLICY_RESULT_MODE_MISMATCH",
        "PARAMETER_BOUNDARY_INVALID",
    )
    normalized = []
    for item in errors:
        value = dict(item)
        message = str(value.get("msg") or value.get("message") or "")
        explicit = next(
            (code for code in known_codes if code in message),
            None,
        )
        if explicit:
            value["code"] = explicit
        elif stage == "computation_blueprint":
            value["code"] = (
                "COMPUTATION_TARGET_MISSING"
                if value.get("type") == "missing"
                else "COMPUTATION_TARGET_CHANGED"
                if value.get("type") == "extra_forbidden"
                else "COMPUTATION_INPUT_TYPE_MISMATCH"
            )
        elif stage == "source_requirements":
            location = tuple(str(part) for part in value.get("loc", ()))
            if "required_inputs" in location:
                value["code"] = (
                    "SOURCE_INPUT_IMPLEMENTATION_MISSING"
                    if value.get("type") == "missing"
                    else "SOURCE_INPUT_IMPLEMENTATION_EXTRA"
                )
            elif "policy_filters" in location:
                value["code"] = "OBLIGATION_IMPLEMENTATION_MISSING"
        normalized.append(value)
    return normalized


def _validate_fact_blueprint_stage(result, blueprint) -> None:
    output_symbols = {
        item.symbol.casefold(): item for item in result.outputs
    }
    unknown = sorted({
        value.result_output_symbol
        for fact in blueprint.facts
        for value in fact.dimensions + fact.measures
        if (
            value.result_output_symbol
            and value.result_output_symbol.casefold() not in output_symbols
        )
    } | {
        symbol
        for symbol in blueprint.derived_output_symbols
        if symbol.casefold() not in output_symbols
    })
    if unknown:
        raise ValueError(
            "FACT_RESULT_OUTPUT_UNKNOWN: " + ", ".join(unknown)
        )
    type_mismatches = sorted({
        f"{fact.symbol}.{value.symbol}"
        for fact in blueprint.facts
        for value in fact.dimensions + fact.measures
        if (
            value.result_output_symbol
            and value.result_output_symbol.casefold() in output_symbols
            and value.logical_type
            != output_symbols[
                value.result_output_symbol.casefold()
            ].logical_type
        )
    })
    if type_mismatches:
        raise ValueError(
            "FACT_RESULT_TYPE_MISMATCH: "
            + ", ".join(type_mismatches)
        )
    direct_outputs = [
        value.result_output_symbol.casefold()
        for fact in blueprint.facts
        for value in fact.dimensions + fact.measures
        if value.result_output_symbol
    ]
    derived_outputs = [
        item.casefold() for item in blueprint.derived_output_symbols
    ]
    all_targets = direct_outputs + derived_outputs
    duplicates = sorted({
        item for item in all_targets if all_targets.count(item) > 1
    })
    missing = sorted(set(output_symbols) - set(all_targets))
    if duplicates:
        raise ValueError(
            "FACT_RESULT_OUTPUT_DUPLICATE: " + ", ".join(duplicates)
        )
    if missing:
        raise ValueError(
            "FACT_RESULT_OUTPUT_MISSING: " + ", ".join(missing)
        )
    from app.services.semantic_obligation_compiler import (
        compile_semantic_obligations,
    )
    compile_semantic_obligations(result, blueprint)


def _validate_result_contract_stage(state: AgentState, result) -> None:
    confirmed = state.get("confirmed_decision_set") or {}
    expected = {
        str(item["key"]).casefold(): str(item.get("value") or "")
        for item in confirmed.get("decisions", [])
        if (
            isinstance(item, dict)
            and item.get("key")
            and item.get("contract_relevant", True)
        )
    }
    actual = {
        item.key.casefold(): item.value
        for item in result.business_policies
    }
    changed_values = sorted(
        key for key in set(expected) & set(actual)
        if expected[key] != actual[key]
    )
    if set(expected) != set(actual) or changed_values:
        raise ValueError(
            "DECISION_NOT_CONSUMED: "
            f"missing={sorted(set(expected) - set(actual))}; "
            f"unexpected={sorted(set(actual) - set(expected))}; "
            f"changed_values={changed_values}"
        )
    result_selection_policies = [
        item for item in result.business_policies
        if item.effect == "result_selection"
    ]
    if result_selection_policies and result.result_mode != "exception_rows":
        policy = result_selection_policies[0]
        error = ValueError(
            "POLICY_RESULT_MODE_MISMATCH: "
            "只输出满足差异/异常条件的政策要求 result_mode=exception_rows"
        )
        error.code = "POLICY_RESULT_MODE_MISMATCH"
        error.evidence = {
            "policy_key": policy.key,
            "policy_value": policy.value,
            "effect": policy.effect,
            "result_mode": result.result_mode,
            "required_result_mode": "exception_rows",
        }
        raise error
    inclusive_starts = sum(
        item.boundary == "inclusive" for item in result.parameters
    )
    inclusive_full_day_ends = sum(
        item.boundary == "inclusive_full_day"
        for item in result.parameters
    )
    if inclusive_full_day_ends > inclusive_starts:
        error = ValueError(
            "PARAMETER_BOUNDARY_INVALID: 单个截止日期不能标记为自然日范围终点"
        )
        error.code = "PARAMETER_BOUNDARY_INVALID"
        error.evidence = {
            "inclusive_parameters": inclusive_starts,
            "inclusive_full_day_parameters": inclusive_full_day_ends,
            "rule": (
                "单一截止日期使用 inclusive；只有双参数自然日区间的结束参数"
                "使用 inclusive_full_day"
            ),
        }
        raise error


def _validate_source_requirements_stage(
    result, blueprint, obligations, sources,
) -> None:
    entity_symbols = {item.symbol.casefold() for item in sources.entities}
    required_entities = {
        symbol.casefold()
        for fact in blueprint.facts
        for symbol in fact.entity_symbols
    }
    missing_entities = sorted(required_entities - entity_symbols)
    if missing_entities:
        raise ValueError(
            "SOURCE_ENTITY_UNKNOWN: " + ", ".join(missing_entities)
        )
    fact_symbols = {item.symbol.casefold() for item in blueprint.facts}
    parameter_symbols = {item.symbol.casefold() for item in result.parameters}
    for item in sources.filters:
        unknown_facts = sorted(
            set(value.casefold() for value in item.fact_symbols)
            - fact_symbols
        )
        unknown_parameters = sorted(
            set(value.casefold() for value in item.parameter_symbols)
            - parameter_symbols
        )
        if unknown_facts:
            raise ValueError(
                "SOURCE_FILTER_FACT_UNKNOWN: " + ", ".join(unknown_facts)
            )
        if unknown_parameters:
            raise ValueError(
                "SOURCE_FILTER_PARAMETER_UNKNOWN: "
                + ", ".join(unknown_parameters)
            )
    expected = {
        (item.policy_key.casefold(), item.fact_symbol.casefold())
        for item in obligations.obligations
        if item.kind == "fact_filter"
    }
    actual = {
        (str(item.policy_key).casefold(), fact.casefold())
        for item in sources.filters
        if item.policy_key
        for fact in item.fact_symbols
    }
    if expected != actual:
        raise ValueError(
            "OBLIGATION_IMPLEMENTATION_MISSING: "
            f"expected={sorted(expected)}; actual={sorted(actual)}"
        )


def _semantic_stage_failure(
    state: AgentState,
    checkpoint,
    error: SemanticStageError,
) -> dict:
    from app.contracts.semantic_design_state import SemanticDesignDiagnostic
    from app.db.sqlite import save_semantic_design_checkpoint

    stages = [
        "result_contract",
        "fact_blueprint",
        "computation_blueprint",
        "semantic_obligations",
        "semantic_inputs",
        "source_requirements",
        "expression_materialize",
        "semantic_compile",
        "schema",
        "reference",
        "stored_procedure",
        "validation",
    ]
    stage_index = (
        stages.index(error.stage)
        if error.stage in stages else len(stages) - 1
    )
    evidence = dict(error.evidence)
    evidence.setdefault("failure_stage", error.stage)
    evidence.setdefault("blocked_downstream", stages[stage_index + 1:])
    diagnostic = SemanticDesignDiagnostic(
        stage=error.stage,
        code=error.code,
        business_element=(
            str(evidence.get("policy_key"))
            if evidence.get("policy_key")
            else None
        ),
        message=str(error),
        evidence=evidence,
        system_action=f"已停止在 {error.stage}，未运行任何下游阶段",
        user_action="请检查业务口径或补充缺失信息",
    )
    counts = dict(checkpoint.repair_counts)
    counts[error.stage] = max(
        counts.get(error.stage, 0), error.repair_count,
    )
    failed = checkpoint.model_copy(update={
        "stage": error.stage,
        "status": "failed",
        "repair_counts": counts,
        "diagnostics": [diagnostic],
    })
    save_semantic_design_checkpoint(
        failed,
        expected_decision_hash=checkpoint.decision_hash,
        expected_stage_input_hash=checkpoint.stage_input_hash,
    )
    payload = diagnostic.model_dump(mode="json")
    return {
        "status": "semantic_design_failed",
        "semantic_design_stage": error.stage,
        "semantic_design_diagnostics": [payload],
        "error": str(error),
        "mode": "design",
    }


def _complete_llm_semantic_stage(
    state: AgentState,
    config,
    *,
    stage: str,
    field: str,
    contract_type,
    instruction: str,
    upstream: dict,
    progress: str,
    validator=None,
    transformer=None,
) -> dict:
    from app.db.sqlite import save_semantic_design_checkpoint
    from app.services.semantic_design_checkpoints import (
        advance_semantic_design_checkpoint,
    )

    checkpoint = _load_semantic_design_checkpoint(state)
    if checkpoint.status == "failed":
        diagnostics = [
            item.model_dump(mode="json")
            for item in checkpoint.diagnostics
        ]
        return {
            "status": "semantic_design_failed",
            "semantic_design_stage": checkpoint.stage,
            "semantic_design_diagnostics": diagnostics,
            "error": (
                checkpoint.diagnostics[0].message
                if checkpoint.diagnostics else "语义设计已经停止"
            ),
            "mode": "design",
        }
    existing = getattr(checkpoint, field)
    if existing is not None:
        return {
            field: existing.model_dump(mode="json"),
            "semantic_design_stage": checkpoint.stage,
            "status": checkpoint.status,
            "error": "",
        }

    writer = _get_writer(config)
    _write_progress(writer, stage, progress)
    try:
        value, repair_count = _generate_semantic_stage(
            _get_llm(),
            stage=stage,
            contract_type=contract_type,
            instruction=instruction,
            state=state,
            upstream=upstream,
            validator=validator,
            transformer=transformer,
        )
    except SemanticStageError as exc:
        return _semantic_stage_failure(state, checkpoint, exc)

    advanced = advance_semantic_design_checkpoint(
        checkpoint, stage, value,
    )
    if repair_count:
        counts = dict(advanced.repair_counts)
        counts[stage] = repair_count
        advanced = advanced.model_copy(update={"repair_counts": counts})
    save_semantic_design_checkpoint(
        advanced,
        expected_decision_hash=checkpoint.decision_hash,
        expected_stage_input_hash=checkpoint.stage_input_hash,
    )
    return {
        field: value.model_dump(mode="json"),
        "semantic_design_stage": advanced.stage,
        "semantic_design_diagnostics": [],
        "status": advanced.status,
        "error": "",
    }


def result_contract_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    from app.contracts.semantic_design import ResultContract

    return _complete_llm_semantic_stage(
        state,
        config,
        stage="result_contract",
        field="result_contract",
        contract_type=ResultContract,
        instruction=RESULT_CONTRACT_PROMPT,
        upstream={},
        validator=lambda value: _validate_result_contract_stage(state, value),
        progress="正在确定最终输出、参数和业务粒度…",
    )


def fact_blueprint_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    from app.contracts.semantic_design import ResultContract
    from app.services.fact_policy_schema import (
        create_fact_blueprint_response_model,
        materialize_fact_blueprint,
    )

    result = ResultContract.model_validate(state["result_contract"])
    response_model = create_fact_blueprint_response_model(result)
    return _complete_llm_semantic_stage(
        state,
        config,
        stage="fact_blueprint",
        field="fact_blueprint",
        contract_type=response_model,
        instruction=FACT_BLUEPRINT_PROMPT,
        upstream={"result_contract": result.model_dump(mode="json")},
        progress="正在拆分可独立校验的业务事实…",
        validator=lambda value: _validate_fact_blueprint_stage(result, value),
        transformer=lambda value: materialize_fact_blueprint(value, result),
    )


def computation_blueprint_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    from app.contracts.semantic_design import FactBlueprint, ResultContract
    from app.services.computation_blueprint_schema import (
        create_computation_blueprint_response_model,
        materialize_computation_blueprint,
    )
    from app.services.computation_blueprint_validator import (
        validate_computation_blueprint,
    )

    result = ResultContract.model_validate(state["result_contract"])
    blueprint = FactBlueprint.model_validate(state["fact_blueprint"])
    response_model = create_computation_blueprint_response_model(
        result,
        blueprint,
    )
    return _complete_llm_semantic_stage(
        state,
        config,
        stage="computation_blueprint",
        field="computation_blueprint",
        contract_type=response_model,
        instruction=COMPUTATION_BLUEPRINT_PROMPT,
        upstream={
            "result_contract": result.model_dump(mode="json"),
            "fact_blueprint": blueprint.model_dump(mode="json"),
        },
        progress="正在先于来源字段冻结业务输入和结构化计算公式…",
        transformer=lambda value: materialize_computation_blueprint(
            value,
            result,
            blueprint,
        ),
        validator=lambda value: validate_computation_blueprint(
            result,
            blueprint,
            value,
        ),
    )


def semantic_obligations_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    """Compile immutable policy obligations without an LLM call."""
    from app.contracts.semantic_design import FactBlueprint, ResultContract
    from app.db.sqlite import save_semantic_design_checkpoint
    from app.services.semantic_design_checkpoints import (
        advance_semantic_design_checkpoint,
    )
    from app.services.semantic_obligation_compiler import (
        SemanticObligationError,
        compile_semantic_obligations,
    )

    checkpoint = _load_semantic_design_checkpoint(state)
    if checkpoint.semantic_obligations is not None:
        return {
            "semantic_obligations": (
                checkpoint.semantic_obligations.model_dump(mode="json")
            ),
            "semantic_design_stage": checkpoint.stage,
            "status": checkpoint.status,
            "error": "",
        }
    _write_progress(
        _get_writer(config),
        "semantic_obligations",
        "正在把已确认业务政策编译为不可变实现义务…",
    )
    try:
        obligations = compile_semantic_obligations(
            ResultContract.model_validate(state["result_contract"]),
            FactBlueprint.model_validate(state["fact_blueprint"]),
        )
    except SemanticObligationError as exc:
        return _semantic_stage_failure(
            state,
            checkpoint,
            SemanticStageError(
                "semantic_obligations",
                exc.code,
                str(exc),
                evidence=exc.evidence,
                repair_count=0,
            ),
        )
    advanced = advance_semantic_design_checkpoint(
        checkpoint, "semantic_obligations", obligations,
    )
    save_semantic_design_checkpoint(
        advanced,
        expected_decision_hash=checkpoint.decision_hash,
        expected_stage_input_hash=checkpoint.stage_input_hash,
    )
    return {
        "semantic_obligations": obligations.model_dump(mode="json"),
        "semantic_design_stage": advanced.stage,
        "status": advanced.status,
        "error": "",
    }


def semantic_inputs_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    """Compile immutable source-input obligations without an LLM call."""
    from app.contracts.computation_blueprint import ComputationBlueprint
    from app.contracts.semantic_design import FactBlueprint, ResultContract
    from app.db.sqlite import save_semantic_design_checkpoint
    from app.services.semantic_design_checkpoints import (
        advance_semantic_design_checkpoint,
    )
    from app.services.semantic_input_compiler import (
        SemanticInputCompilerError,
        compile_semantic_input_obligations,
    )

    checkpoint = _load_semantic_design_checkpoint(state)
    if checkpoint.semantic_inputs is not None:
        return {
            "semantic_inputs": checkpoint.semantic_inputs.model_dump(
                mode="json",
            ),
            "semantic_design_stage": checkpoint.stage,
            "status": checkpoint.status,
            "error": "",
        }
    _write_progress(
        _get_writer(config),
        "semantic_inputs",
        "正在把冻结公式编译为不可增删的来源输入义务…",
    )
    try:
        inputs = compile_semantic_input_obligations(
            ResultContract.model_validate(state["result_contract"]),
            FactBlueprint.model_validate(state["fact_blueprint"]),
            ComputationBlueprint.model_validate(
                state["computation_blueprint"],
            ),
        )
    except (SemanticInputCompilerError, ValueError) as exc:
        return _semantic_stage_failure(
            state,
            checkpoint,
            SemanticStageError(
                "semantic_inputs",
                getattr(exc, "code", "SEMANTIC_INPUT_COMPILER_INVALID"),
                str(exc),
                evidence=getattr(exc, "evidence", {}),
                repair_count=0,
            ),
        )
    advanced = advance_semantic_design_checkpoint(
        checkpoint,
        "semantic_inputs",
        inputs,
    )
    save_semantic_design_checkpoint(
        advanced,
        expected_decision_hash=checkpoint.decision_hash,
        expected_stage_input_hash=checkpoint.stage_input_hash,
    )
    return {
        "semantic_inputs": inputs.model_dump(mode="json"),
        "semantic_design_stage": advanced.stage,
        "status": advanced.status,
        "error": "",
    }


def source_requirements_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    from app.contracts.semantic_design import (
        FactBlueprint,
        ResultContract,
    )
    from app.contracts.semantic_obligations import SemanticObligationSet
    from app.contracts.semantic_input_obligations import (
        SemanticInputObligationSet,
    )
    from app.services.source_obligation_schema import (
        create_source_requirements_response_model,
        materialize_source_requirements,
    )

    result = ResultContract.model_validate(state["result_contract"])
    blueprint = FactBlueprint.model_validate(state["fact_blueprint"])
    obligations = SemanticObligationSet.model_validate(
        state["semantic_obligations"],
    )
    input_obligations = SemanticInputObligationSet.model_validate(
        state["semantic_inputs"],
    )
    response_model = create_source_requirements_response_model(
        obligations,
        input_obligations,
    )
    return _complete_llm_semantic_stage(
        state,
        config,
        stage="source_requirements",
        field="source_requirements",
        contract_type=response_model,
        instruction=SOURCE_REQUIREMENTS_PROMPT,
        upstream={
            "result_contract": result.model_dump(mode="json"),
            "fact_blueprint": blueprint.model_dump(mode="json"),
            "semantic_obligations": obligations.model_dump(mode="json"),
            "semantic_inputs": input_obligations.model_dump(mode="json"),
        },
        progress="正在声明底层业务实体和字段需求…",
        validator=lambda value: _validate_source_requirements_stage(
            result, blueprint, obligations, value,
        ),
        transformer=lambda value: materialize_source_requirements(
            value,
            obligations,
            input_obligations,
            blueprint,
        ),
    )


def expression_materialize_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    """Materialize canonical expressions deterministically without an LLM."""
    from app.contracts.computation_blueprint import ComputationBlueprint
    from app.contracts.semantic_design import FactBlueprint, SourceRequirements
    from app.contracts.semantic_input_obligations import (
        SemanticInputObligationSet,
    )
    from app.db.sqlite import save_semantic_design_checkpoint
    from app.services.expression_materializer import (
        ExpressionMaterializationError,
        materialize_expression_design,
    )
    from app.services.semantic_design_checkpoints import (
        advance_semantic_design_checkpoint,
    )

    checkpoint = _load_semantic_design_checkpoint(state)
    if checkpoint.expression_design is not None:
        return {
            "expression_design": checkpoint.expression_design.model_dump(
                mode="json",
            ),
            "semantic_design_stage": checkpoint.stage,
            "status": checkpoint.status,
            "error": "",
        }
    blueprint = FactBlueprint.model_validate(state["fact_blueprint"])
    computations = ComputationBlueprint.model_validate(
        state["computation_blueprint"],
    )
    sources = SourceRequirements.model_validate(state["source_requirements"])
    inputs = SemanticInputObligationSet.model_validate(
        state["semantic_inputs"],
    )
    _write_progress(
        _get_writer(config),
        "expression_materialize",
        "正在按冻结公式确定性生成内部表达式，不调用模型…",
    )
    try:
        expressions = materialize_expression_design(
            blueprint,
            computations,
            inputs,
            sources,
        )
    except ExpressionMaterializationError as exc:
        return _semantic_stage_failure(
            state,
            checkpoint,
            SemanticStageError(
                "expression_materialize",
                exc.code,
                str(exc),
                evidence=exc.evidence,
                repair_count=0,
            ),
        )
    advanced = advance_semantic_design_checkpoint(
        checkpoint,
        "expression_materialize",
        expressions,
    )
    save_semantic_design_checkpoint(
        advanced,
        expected_decision_hash=checkpoint.decision_hash,
        expected_stage_input_hash=checkpoint.stage_input_hash,
    )
    return {
        "expression_design": expressions.model_dump(mode="json"),
        "semantic_design_stage": advanced.stage,
        "status": advanced.status,
        "error": "",
    }


def semantic_compile_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    from app.contracts.semantic import SemanticDesign
    from app.contracts.computation_blueprint import ComputationBlueprint
    from app.contracts.semantic_design import (
        ExpressionDesign,
        FactBlueprint,
        ResultContract,
        SourceRequirements,
    )
    from app.contracts.semantic_obligations import SemanticObligationSet
    from app.contracts.semantic_input_obligations import (
        SemanticInputObligationSet,
    )
    from app.db.sqlite import save_semantic_design_checkpoint
    from app.services.semantic_compiler_v3 import (
        SemanticCompileError,
        compile_semantic_contract,
    )
    from app.services.semantic_design_checkpoints import (
        advance_semantic_design_checkpoint,
    )

    checkpoint = _load_semantic_design_checkpoint(state)
    result = ResultContract.model_validate(state["result_contract"])
    blueprint = FactBlueprint.model_validate(state["fact_blueprint"])
    computations = ComputationBlueprint.model_validate(
        state["computation_blueprint"],
    )
    sources = SourceRequirements.model_validate(state["source_requirements"])
    expressions = ExpressionDesign.model_validate(state["expression_design"])
    obligations = SemanticObligationSet.model_validate(
        state["semantic_obligations"],
    )
    input_obligations = SemanticInputObligationSet.model_validate(
        state["semantic_inputs"],
    )
    _write_progress(
        _get_writer(config),
        "semantic_compile",
        "正在执行确定性符号解析、类型推导和引用完整性校验…",
    )
    try:
        contract, symbol_table = compile_semantic_contract(
            result,
            blueprint,
            sources,
            expressions,
            obligations=obligations,
            computations=computations,
            input_obligations=input_obligations,
            decision_hash=checkpoint.decision_hash,
            confirmed_decision_keys=_confirmed_decision_keys(state),
        )
    except SemanticCompileError as exc:
        return _semantic_stage_failure(
            state,
            checkpoint,
            SemanticStageError(
                "semantic_compile",
                exc.code,
                str(exc),
                evidence=exc.evidence,
            ),
        )
    except Exception as exc:
        return _semantic_stage_failure(
            state,
            checkpoint,
            SemanticStageError(
                "semantic_compile",
                "SEMANTIC_COMPILER_INTERNAL",
                f"确定性语义编译器内部失败：{exc}",
            ),
        )

    compile_result = {
        "contract": contract.model_dump(mode="json"),
        "contract_hash": contract.content_hash,
        "symbol_table": symbol_table,
        "policy_coverage": symbol_table.get("policy_coverage", []),
        "consumed_decision_keys": sorted(_confirmed_decision_keys(state)),
    }
    advanced = advance_semantic_design_checkpoint(
        checkpoint, "semantic_compile", compile_result,
    )
    save_semantic_design_checkpoint(
        advanced,
        expected_decision_hash=checkpoint.decision_hash,
        expected_stage_input_hash=checkpoint.stage_input_hash,
    )
    semantic_design = SemanticDesign(
        version=3,
        design_version=checkpoint.decision_hash,
        decision_hash=checkpoint.decision_hash,
        contracts=[contract],
    )
    design = _render_semantic_design(semantic_design)
    _persist_design(
        state,
        status="ready_for_confirmation",
        summary=design,
        raw_draft={
            "result_contract": result.model_dump(mode="json"),
            "fact_blueprint": blueprint.model_dump(mode="json"),
            "computation_blueprint": computations.model_dump(mode="json"),
            "semantic_obligations": obligations.model_dump(mode="json"),
            "semantic_inputs": input_obligations.model_dump(mode="json"),
            "source_requirements": sources.model_dump(mode="json"),
            "expression_design": expressions.model_dump(mode="json"),
        },
        query_spec=semantic_design,
        diagnostics=[],
    )
    return {
        "design": design,
        "query_spec": semantic_design.model_dump(mode="json"),
        "semantic_compile_result": compile_result,
        "semantic_design_hash": contract.content_hash,
        "semantic_design_stage": "semantic_compile",
        "semantic_design_diagnostics": [],
        "schema_fingerprint": "",
        "mode": "design",
        "status": "designed",
        "design_phase": "new",
        "last_feedback_reply": "",
        "error": "",
    }


def _legacy_design_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    """先固化方案及 QuerySpec，再让用户确认同一版本。"""
    llm = _get_llm()
    stream_writer = _get_writer(config)
    design_phase = state.get("design_phase")
    design = state.get("design", "")
    raw_query_spec = state.get("query_spec")

    if design_phase == "prepare_feedback" or not raw_query_spec or not design:
        _write_progress(
            stream_writer, "query_spec", "正在直接生成并校验方案业务契约...",
        )
        try:
            query_spec, summary, raw_draft = _generate_design_query_spec(llm, state)
        except Exception as exc:
            summary = getattr(exc, "summary", "") or "方案草稿已生成，但契约尚未通过。"
            _persist_design(
                state,
                status="contract_invalid",
                summary=summary,
                raw_draft=getattr(exc, "raw_draft", None),
                query_spec=None,
                diagnostics=[{"message": str(exc)}],
            )
            return {
                "design": summary,
                "query_spec": {},
                "mode": "design",
                "status": "design_failed",
                "design_phase": "invalid",
                "error": str(exc),
                "design_draft": getattr(exc, "raw_draft", None),
            }
        design = _render_semantic_design(query_spec)
        _persist_design(
            state,
            status="ready_for_confirmation",
            summary=summary or design,
            raw_draft=raw_draft,
            query_spec=query_spec,
            diagnostics=[],
        )
        return {
            "design": design,
            "query_spec": query_spec.model_dump(mode="json", by_alias=True),
            "schema_fingerprint": "",
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


def _confirm_semantic_design(state: AgentState) -> dict:
    from app.db.sqlite import save_semantic_design_checkpoint

    checkpoint = _load_semantic_design_checkpoint(state)
    confirmed = checkpoint.model_copy(update={"status": "confirmed"})
    save_semantic_design_checkpoint(
        confirmed,
        expected_decision_hash=checkpoint.decision_hash,
        expected_stage_input_hash=checkpoint.stage_input_hash,
        expected_status=checkpoint.status,
    )
    return {
        "design": state.get("design", ""),
        "query_spec": state.get("query_spec", {}),
        "mode": "generate",
        "status": "designed",
        "design_phase": None,
        "last_feedback_reply": "",
        "error": "",
    }


def _revise_semantic_design(state: AgentState, feedback: str) -> dict:
    from app.db.sqlite import invalidate_semantic_design_checkpoint

    invalidate_semantic_design_checkpoint(state["session_id"])
    return {
        "user_input": feedback,
        "requirements": (
            f"{state.get('requirements', '')}\n"
            f"用户对设计的修改意见：{feedback}"
        ).strip(),
        "design": "",
        "query_spec": {},
        "result_contract": {},
        "fact_blueprint": {},
        "computation_blueprint": {},
        "semantic_obligations": {},
        "semantic_inputs": {},
        "source_requirements": {},
        "expression_design": {},
        "semantic_compile_result": {},
        "clarify_decisions": [],
        "deferred_decisions": [],
        "decision_plan": {},
        "confirmed_decision_set": {},
        "pending_clarify": None,
        "mode": "clarify",
        "status": "design_revision_requested",
        "design_phase": None,
        "last_feedback_reply": "修改意见将重新进入业务口径确认。",
        "error": "",
    }


def design_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    """Display only a compiler-produced design and collect confirmation."""
    design = state.get("design", "")
    query_spec = state.get("query_spec")
    if (
        not design
        or not query_spec
        or not state.get("semantic_compile_result")
    ):
        return {
            "mode": "design",
            "status": "semantic_design_failed",
            "semantic_design_stage": "semantic_compile",
            "semantic_design_diagnostics": [{
                "stage": "semantic_compile",
                "code": "SEMANTIC_COMPILE_RESULT_MISSING",
                "business_element": None,
                "message": "设计确认节点没有收到编译器产物",
                "evidence": {},
                "system_action": "已停止，Schema 阶段不会运行",
                "user_action": None,
            }],
            "error": "SEMANTIC_COMPILE_RESULT_MISSING",
        }

    decision = interrupt({
        "type": "design",
        "content": design,
        "phase": state.get("design_phase") or "new",
        "stage": "ready_for_confirmation",
    })
    if isinstance(decision, dict):
        if decision.get("action") == "confirm":
            return _confirm_semantic_design(state)
        if decision.get("action") == "modify":
            feedback = str(
                decision.get("feedback")
                or decision.get("design")
                or "请重新调整设计"
            ).strip()
            return _revise_semantic_design(state, feedback)

    if isinstance(decision, str) and decision.strip():
        feedback = decision.strip()
        if _is_explicit_design_confirmation(feedback):
            return _confirm_semantic_design(state)
        intent, reply, _unused_design = _classify_design_feedback(
            _get_llm(), design, feedback,
        )
        if intent == "CONFIRM":
            return _confirm_semantic_design(state)
        if intent == "MODIFY":
            return _revise_semantic_design(state, feedback)
        hint = reply or "请确认方案，或明确指出要修改的业务口径。"
        interrupt({
            "type": "design",
            "content": design,
            "reply": hint,
            "phase": state.get("design_phase") or "new",
            "stage": "ready_for_confirmation",
        })
        return {
            "design": design,
            "query_spec": query_spec,
            "mode": "design",
            "status": "designed",
            "design_phase": state.get("design_phase") or "new",
            "last_feedback_reply": hint,
        }
    return _confirm_semantic_design(state)


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


def _normalize_oracle_candidates(
    raw_queries, procedure_spec, query_spec: object | None = None,
):
    """以已确认的 QuerySpec 补齐 Oracle 契约字段，避免让模型重述契约。"""
    rules = {rule.name: rule for rule in procedure_spec.verification_rules}
    plans = (
        {
            item.name: item
            for item in compile_verification_plan(
                query_spec, procedure_spec,
            ).rules
        }
        if query_spec is not None else {}
    )
    affected_tables = [
        {
            "table": f"{item.schema}.{item.table}",
            "operation": item.operation,
            "key_columns": list(item.key_columns),
            "max_affected_rows": item.max_affected_rows,
        }
        for item in procedure_spec.writes
    ]
    normalized = []
    for item in raw_queries:
        if not isinstance(item, dict):
            raise ValueError(f"{procedure_spec.name} Oracle 规则必须是对象")
        candidate = dict(item)
        rule = rules.get(candidate.get("name"))
        spec = candidate.get("validation_spec")
        if not isinstance(spec, dict):
            spec = {}
        else:
            spec = dict(spec)
        if rule is not None:
            plan = plans.get(rule.name)
            compare_columns = (
                list(plan.compare_columns)
                if plan is not None else list(rule.compare_columns)
            )
            candidate["compare_columns"] = ",".join(compare_columns)
            spec.update({
                "mode": rule.mode,
                "required": True,
                "compare_columns": compare_columns,
            })
            if plan is not None:
                spec.update({
                    "key_columns": list(plan.key_columns),
                    "tolerance": dict(plan.tolerance),
                    "metrics": [
                        item.model_dump(mode="json")
                        for item in plan.metrics
                    ],
                    "role": rule.role,
                    "expected_schema": [
                        item.model_dump(mode="json")
                        for item in plan.expected_schema
                    ],
                })
            if rule.mode == "change_set":
                spec["affected_tables"] = (
                    list(plan.affected_tables)
                    if plan is not None else affected_tables
                )
                targets = spec["affected_tables"]
                if plan is not None and len(targets) == 1:
                    target = targets[0]
                    snapshot_columns = list(dict.fromkeys(
                        list(target.get("key_columns") or [])
                        + list(target.get("compare_columns") or [])
                    ))
                    spec["snapshot_sql"] = (
                        "SELECT "
                        + ", ".join(f"[{name}]" for name in snapshot_columns)
                        + f" FROM {target['table']}"
                    )
        else:
            candidate["compare_columns"] = _normalize_compare_columns(
                candidate.get("compare_columns", ""),
            )
        candidate["sql_code"] = canonicalize_parameter_syntax(
            str(candidate.get("sql_code") or ""),
        )
        candidate["validation_spec"] = spec
        normalized.append(VerifyQueryCandidate.model_validate(candidate))
    return normalized


def _clean_procedure_code(code: str) -> str:
    code = canonicalize_parameter_syntax(code.strip())
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


def _procedure_schema_json(query_spec: object, procedure_spec,
                           schema_evidence) -> str:
    qualified = {
        (item.schema, item.table) for item in procedure_spec.sources
    } | {
        (item.schema, item.table) for item in procedure_spec.writes
    }
    payload = {
        "database_name": schema_evidence.database_name,
        "database_collation": schema_evidence.database_collation,
        "compatibility_level": schema_evidence.compatibility_level,
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


def _generate_procedure_candidate(llm: ChatOpenAI, query_spec: object,
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


def _generate_oracle_candidates(llm: ChatOpenAI, query_spec: object,
                                procedure_spec, schema_evidence
                                ) -> list:
    schema_json = _procedure_schema_json(
        query_spec, procedure_spec, schema_evidence,
    )
    plan = compile_verification_plan(query_spec, procedure_spec)
    tasks = oracle_sql_tasks(plan, procedure_spec)
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
            oracle_tasks=json.dumps(
                [item.model_dump(mode="json") for item in tasks],
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ),
        ),
        f"{procedure_spec.name} Oracle",
    )
    raw_queries = data.get("verify_queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ValueError(f"{procedure_spec.name} 未生成独立 Oracle 校验规则")
    return _normalize_oracle_candidates(
        raw_queries, procedure_spec, query_spec,
    )


def _repair_candidate(llm: ChatOpenAI, bundle: object,
                      errors: list) -> object:
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
                verification_plan=bundle.verification_plan.canonical_json(),
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
        repaired.verify_queries = _normalize_oracle_candidates(
            raw_queries, bundle.procedure_spec, bundle.query_spec,
        )
    return apply_collation_policy(repaired)


def _candidate_result(bundle: object) -> dict:
    result = candidate_result(bundle)
    result["bundle_hash"] = bundle.bundle_hash or result.get("bundle_hash", "")
    return result


def _generate_relational_plan_v3(
    llm: ChatOpenAI,
    role: str,
    semantic_contract,
    schema_binding,
    result_schema,
    repair_events: list[dict] | None = None,
    post_validator=None,
    allow_deterministic: bool = True,
):
    from app.contracts.relational_plan import RelationalPlan
    from app.services.plan_semantics_v3 import validate_plan_semantics
    from app.services.sql_renderer_v3 import SqlRendererV3

    if allow_deterministic:
        deterministic = _build_deterministic_relational_plan_v3(
            role,
            semantic_contract,
            schema_binding,
            result_schema,
        )
        if deterministic is not None:
            validate_plan_semantics(
                deterministic,
                semantic_contract,
                schema_binding,
                output_projection=(
                    [item.name for item in result_schema]
                    if role.startswith("reference") else None
                ),
                allow_entity_subset=role.startswith("reference"),
            )
            SqlRendererV3(
                semantic_contract, schema_binding,
            ).render_query(deterministic)
            if post_validator is not None:
                post_validator(deterministic)
            if repair_events is not None:
                repair_events.append({
                    "role": role,
                    "status": "compiled_deterministically",
                    "frozen": ["SemanticContract", "SchemaBinding"],
                    "allowed_changes": [],
                })
            return deterministic

    base_prompt = RELATIONAL_PLAN_V3_PROMPT.format(
        role=role,
        semantic_contract=semantic_contract.canonical_json(),
        schema_binding=schema_binding.canonical_json(),
        plan_schema=json.dumps(
            RelationalPlan.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    base_prompt += (
        "\n\n本次必须输出的确定性 result_schema（名称、顺序、类型不可改变）：\n"
        + json.dumps(
            [item.model_dump(mode="json") for item in result_schema],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    last_exc = None
    previous = None
    for attempt in range(2):
        prompt = base_prompt
        if attempt:
            event = {
                "role": role,
                "status": "repairing",
                "allowed_changes": [
                    "关系计划结构", "别名", "聚合", "排序",
                    "白名单表达式", "受控类型转换", "确定性日期表达式",
                ],
                "frozen": [
                    "SemanticContract", "SchemaBinding",
                    "输出合同", "业务过滤",
                    *(
                        ["ReferenceBundle"]
                        if not role.startswith("reference") else []
                    ),
                ],
                "error": str(last_exc),
                "evidence": getattr(last_exc, "evidence", {}),
                "before": previous,
            }
            if repair_events is not None:
                repair_events.append(event)
            prompt += f"""

上一次计划未通过严格校验。只能修复关系计划结构、别名、聚合、排序、白名单表达式、
受控 cast
或确定性日期表达式；不得改变 SemanticContract、SchemaBinding、输出合同或业务过滤。
上一次计划：
{json.dumps(previous, ensure_ascii=False, default=str)}
错误：
{last_exc}
错误证据：
{json.dumps(
    getattr(last_exc, "evidence", {}),
    ensure_ascii=False,
    default=str,
)}
只返回修复后的 RelationalPlan JSON。
"""
        try:
            data = _candidate_json(
                llm,
                prompt,
                f"{semantic_contract.procedure_name} {role} plan",
            )
            data = _canonicalize_nary_boolean_expressions(data)
            data = _lower_sibling_output_dependencies_v3(data)
            previous = data
            # result_schema 是 SemanticContract 的确定性投影，不由模型自由重述。
            data["result_schema"] = [
                item.model_dump(mode="json") for item in result_schema
            ]
            plan = RelationalPlan.model_validate(data)
            validate_plan_semantics(
                plan,
                semantic_contract,
                schema_binding,
                output_projection=(
                    [item.name for item in result_schema]
                    if role.startswith("reference") else None
                ),
                allow_entity_subset=role.startswith("reference"),
            )
            # 在返回计划前完成确定性渲染校验，使不受支持的函数/作用域错误
            # 进入同一个有限修复循环，而不是在冻结 Reference 时令会话崩溃。
            SqlRendererV3(
                semantic_contract, schema_binding,
            ).render_query(plan)
            if post_validator is not None:
                post_validator(plan)
            if attempt and repair_events is not None:
                repair_events[-1].update(
                    status="repaired",
                    after=plan.model_dump(mode="json"),
                )
            return plan
        except Exception as exc:
            last_exc = exc
    try:
        raise last_exc or ValueError("未知关系计划错误")
    except Exception as exc:
        if role.startswith("reference"):
            from app.services.reference_planner import ReferenceBuildError

            raise ReferenceBuildError(
                "REFERENCE_PLAN_INVALID",
                f"独立 Reference 关系计划无效: {exc}",
                evidence=getattr(exc, "evidence", {}),
            ) from exc
        from app.services.procedure_generator_v3 import ProcedureBuildError

        raise ProcedureBuildError(
            "PROCEDURE_PLAN_INVALID",
            f"SP 关系计划无效: {exc}",
            evidence=getattr(exc, "evidence", {}),
        ) from exc


def _canonicalize_nary_boolean_expressions(value):
    """归一化模型常见的等价表达式协议，不放宽运算白名单。"""
    if isinstance(value, list):
        return [
            _canonicalize_nary_boolean_expressions(item)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    result = {
        key: _canonicalize_nary_boolean_expressions(item)
        for key, item in value.items()
    }
    if result.get("kind") == "function" and not result.get("operator"):
        allowed_functions = {
            "ABS", "AVG", "COALESCE", "CONCAT", "COUNT", "DATEADD",
            "COUNT_DISTINCT", "DATEDIFF", "LOWER", "LTRIM", "MAX", "MIN", "NULLIF",
            "RTRIM", "SUM", "UPPER",
        }
        aliases = [
            key for key in ("value", "name", "function_name")
            if isinstance(result.get(key), str)
            and result[key].upper() in allowed_functions
        ]
        if len(aliases) == 1:
            alias = aliases[0]
            result["operator"] = result.pop(alias).upper()
    operator = str(result.get("operator") or "").strip().upper()
    args = result.get("args")
    if (
        result.get("kind") == "binary"
        and operator in {"AND", "OR"}
        and isinstance(args, list)
        and len(args) > 2
    ):
        folded = {
            "kind": "binary",
            "operator": operator,
            "args": [args[0], args[1]],
        }
        for item in args[2:]:
            folded = {
                "kind": "binary",
                "operator": operator,
                "args": [folded, item],
            }
        return folded
    return result


def _promote_fact_derived_bindings_v3(raw_draft: dict) -> None:
    """把 facts 合同中旧公式入口无损提升为唯一的 result_bindings。"""

    def convert(expression):
        if not isinstance(expression, dict):
            return expression
        result = {
            key: convert(item)
            for key, item in expression.items()
        }
        if result.get("kind") == "output":
            result = {
                "kind": "output",
                "output_id": result.get("output_id"),
            }
        if isinstance(result.get("args"), list):
            result["args"] = [
                convert(item) for item in result["args"]
            ]
        if isinstance(result.get("cases"), list):
            result["cases"] = [
                {
                    "when": convert(item.get("when")),
                    "then": convert(item.get("then")),
                }
                for item in result["cases"]
            ]
        if result.get("else_expr") is not None:
            result["else_expr"] = convert(result["else_expr"])
        return result

    for contract in raw_draft.get("contracts") or []:
        if not contract.get("facts"):
            continue
        derived = contract.get("derived_fields") or []
        if not derived:
            continue
        bindings = contract.setdefault("result_bindings", [])
        existing = {
            item.get("output_id") for item in bindings
            if isinstance(item, dict)
        }
        unresolved = []
        for item in derived:
            if not isinstance(item, dict):
                continue
            output_id = item.get("output_id")
            expression = convert(item.get("expression"))
            if output_id not in existing:
                bindings.append({
                    "output_id": output_id,
                    "expression": expression,
                })
                existing.add(output_id)
                continue
            target = next(
                binding for binding in bindings
                if (
                    isinstance(binding, dict)
                    and binding.get("output_id") == output_id
                )
            )
            current = target.get("expression")
            is_self_reference = (
                isinstance(current, dict)
                and current.get("kind") == "output"
                and current.get("output_id") == output_id
            )
            if is_self_reference:
                target["expression"] = expression
                continue
            if current == expression:
                continue
            # 真正存在两套不同公式时保留冲突，交给严格契约拒绝。
            unresolved.append(item)
        contract["derived_fields"] = unresolved


def _normalize_fact_expression_roles_v3(raw_draft: dict) -> None:
    """规范结果表达式角色，并把 NULL 比较降为明确的一元谓词。"""

    def normalize(expression, output_ids: set[str], parameter_ids: set[str]):
        if isinstance(expression, list):
            return [
                normalize(item, output_ids, parameter_ids)
                for item in expression
            ]
        if not isinstance(expression, dict):
            return expression
        result = {
            key: normalize(item, output_ids, parameter_ids)
            for key, item in expression.items()
        }
        if (
            result.get("kind") == "output"
            and result.get("output_id") not in output_ids
            and result.get("output_id") in parameter_ids
        ):
            return {
                "kind": "parameter",
                "parameter_id": result.get("output_id"),
            }
        operator = str(result.get("operator") or "").upper()
        args = result.get("args")
        binary_aliases = {
            "ADD": "+",
            "SUBTRACT": "-",
            "MULTIPLY": "*",
            "DIVIDE": "/",
            "EQ": "=",
            "EQUAL": "=",
            "EQUALS": "=",
            "NE": "<>",
            "NOT_EQUAL": "<>",
            "NOT_EQUALS": "<>",
            "GT": ">",
            "GREATER_THAN": ">",
            "GE": ">=",
            "GTE": ">=",
            "GREATER_THAN_OR_EQUAL": ">=",
            "LT": "<",
            "LESS_THAN": "<",
            "LE": "<=",
            "LTE": "<=",
            "LESS_THAN_OR_EQUAL": "<=",
        }
        if (
            result.get("kind") in {"binary", "function"}
            and operator in binary_aliases
            and isinstance(args, list)
            and len(args) == 2
        ):
            result["kind"] = "binary"
            result["operator"] = binary_aliases[operator]
            operator = result["operator"]
        if (
            result.get("kind") == "binary"
            and operator in {"=", "<>"}
            and isinstance(args, list)
            and len(args) == 2
        ):
            null_indexes = [
                index for index, item in enumerate(args)
                if (
                    isinstance(item, dict)
                    and item.get("kind") == "literal"
                    and item.get("value") is None
                )
            ]
            if len(null_indexes) == 1:
                return {
                    "kind": "unary",
                    "operator": (
                        "IS NULL" if operator == "=" else "IS NOT NULL"
                    ),
                    "args": [args[1 - null_indexes[0]]],
                }
        return result

    for contract in raw_draft.get("contracts") or []:
        if not contract.get("facts"):
            continue
        output_ids = {
            item.get("id") for item in contract.get("outputs") or []
            if isinstance(item, dict)
        }
        parameter_ids = {
            item.get("id") for item in contract.get("parameters") or []
            if isinstance(item, dict)
        }
        for binding in contract.get("result_bindings") or []:
            if isinstance(binding, dict):
                binding["expression"] = normalize(
                    binding.get("expression"),
                    output_ids,
                    parameter_ids,
                )
        if contract.get("result_filter") is not None:
            contract["result_filter"] = normalize(
                contract["result_filter"],
                output_ids,
                parameter_ids,
            )


def _strip_redundant_physical_annotations_v3(value):
    """Clean harmless physical-name parentheticals before purity validation."""
    from app.services.semantic_guard import (
        strip_redundant_physical_annotations,
    )

    if isinstance(value, list):
        return [
            _strip_redundant_physical_annotations_v3(item)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    result = {
        key: _strip_redundant_physical_annotations_v3(item)
        for key, item in value.items()
    }
    if isinstance(result.get("meaning"), str):
        result["meaning"] = strip_redundant_physical_annotations(
            result["meaning"]
        )
    return result


def _lower_sibling_output_dependencies_v3(value):
    """把同层输出别名依赖确定性降为内外两层 project。"""
    if isinstance(value, list):
        return [
            _lower_sibling_output_dependencies_v3(item)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    result = {
        key: _lower_sibling_output_dependencies_v3(item)
        for key, item in value.items()
    }
    if result.get("kind") != "project":
        return result
    projections = result.get("projections")
    if not isinstance(projections, list) or len(projections) < 2:
        return result
    names = {
        str(item.get("name")).casefold()
        for item in projections
        if isinstance(item, dict) and item.get("name")
    }

    def output_references(expression) -> set[str]:
        if not isinstance(expression, dict):
            return set()
        references = set()
        if expression.get("kind") == "output" and expression.get("output_name"):
            references.add(str(expression["output_name"]).casefold())
        for item in expression.get("args") or []:
            references.update(output_references(item))
        for item in expression.get("cases") or []:
            references.update(output_references(item.get("when")))
            references.update(output_references(item.get("then")))
        references.update(output_references(expression.get("else_expr")))
        return references

    base = [
        item for item in projections
        if isinstance(item, dict)
        and not (output_references(item.get("expression")) & names)
    ]
    if not base or len(base) == len(projections):
        return result
    available = {str(item["name"]).casefold() for item in base}
    dependent = [
        item for item in projections
        if item not in base
    ]
    if any(
        not (output_references(item.get("expression")) & names).issubset(
            available
        )
        for item in dependent
    ):
        # 多层或循环依赖不做猜测，交给严格校验拒绝。
        return result
    inner_id = str(result.get("node_id") or "project") + "_base"
    inner = {
        "node_id": inner_id,
        "kind": "project",
        "input": result.get("input"),
        "projections": base,
    }
    result["input"] = inner
    result["projections"] = [
        (
            {
                "name": item["name"],
                "expression": {
                    "kind": "output",
                    "output_name": item["name"],
                },
            }
            if item in base else item
        )
        for item in projections
    ]
    return result


def _v3_comparator(semantic_contract):
    from app.contracts.reference import ComparatorSpec

    names = {item.id: item.name for item in semantic_contract.outputs}
    output_names = [item.name for item in semantic_contract.outputs]
    if semantic_contract.result_mode == "scalar_summary":
        return ComparatorSpec(
            type="scalar_metrics_equal",
            compare_columns=output_names,
            tolerance={
                item.name: semantic_contract.money_tolerance
                for item in semantic_contract.outputs
                if item.logical_type in {"money", "decimal"}
            },
        )
    if semantic_contract.grain:
        keys = [names[item] for item in semantic_contract.grain]
        compare_columns = [
            item for item in output_names
            if item.casefold() not in {key.casefold() for key in keys}
        ] or output_names
        return ComparatorSpec(
            type="keyed_rows_equal",
            key_columns=keys,
            compare_columns=compare_columns,
            tolerance={
                item.name: semantic_contract.money_tolerance
                for item in semantic_contract.outputs
                if item.name in compare_columns
                and item.logical_type in {"money", "decimal"}
            },
        )
    return ComparatorSpec(
        type="multiset_rows_equal",
        compare_columns=output_names,
        tolerance={
            item.name: semantic_contract.money_tolerance
            for item in semantic_contract.outputs
            if item.logical_type in {"money", "decimal"}
        },
    )


def _result_schema_v3(contract, projection: list[str] | None = None):
    from app.contracts.relational_plan import ResultColumn

    wanted = (
        {item.casefold() for item in projection}
        if projection is not None else None
    )
    return [
        ResultColumn(
            name=item.name,
            logical_type=item.logical_type,
            nullable=item.nullable,
        )
        for item in contract.outputs
        if wanted is None or item.name.casefold() in wanted
    ]


def _build_deterministic_relational_plan_v3(
    role,
    contract,
    binding,
    result_schema,
):
    """编译单实体明细合同；无法无歧义编译时返回 None 交给受限 LLM。"""
    from datetime import date, datetime
    from decimal import Decimal

    from app.contracts.relational_plan import (
        Expression,
        NamedExpression,
        PlanNode,
        RelationalPlan,
    )

    if len(contract.entities) != 1 or binding.joins:
        return None
    entity_id = contract.entities[0].id
    fields_by_semantic = {}
    for field in binding.fields:
        fields_by_semantic.setdefault(field.semantic_id, []).append(field)
    if any(len(items) != 1 for items in fields_by_semantic.values()):
        return None
    field_by_semantic = {
        key: items[0] for key, items in fields_by_semantic.items()
    }

    def literal(value):
        if value is None:
            value_type = "null"
        elif isinstance(value, bool):
            value_type = "boolean"
        elif isinstance(value, int):
            value_type = "integer"
        elif isinstance(value, (float, Decimal)):
            value_type = "decimal"
        elif isinstance(value, datetime):
            value_type = "datetime"
        elif isinstance(value, date):
            value_type = "date"
        else:
            value_type = "string"
        return Expression(kind="literal", value=value, value_type=value_type)

    output_name_by_id = {item.id: item.name for item in contract.outputs}

    def semantic_expression(value):
        if value.kind == "output":
            return Expression(
                kind="output",
                output_name=output_name_by_id[value.output_id],
            )
        if value.kind == "literal":
            return literal(value.value)
        if value.kind in {"binary", "function"}:
            return Expression(
                kind=value.kind,
                operator=value.operator,
                args=[semantic_expression(item) for item in value.args],
            )
        if value.kind == "case":
            from app.contracts.relational_plan import WhenThen

            return Expression(
                kind="case",
                cases=[
                    WhenThen(
                        when=semantic_expression(item.when),
                        then=semantic_expression(item.then),
                    )
                    for item in value.cases
                ],
                else_expr=(
                    semantic_expression(value.else_expr)
                    if value.else_expr is not None else None
                ),
            )
        raise ValueError(f"不支持派生表达式 {value.kind}")

    predicates = []
    comparison_operators = {
        "eq": "=",
        "ne": "<>",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
        "like": "LIKE",
    }
    for item in contract.filters:
        if len(item.field_ids) != 1:
            return None
        field = field_by_semantic.get(item.field_ids[0])
        if field is None:
            return None
        column = Expression(
            kind="column", field_binding_id=field.binding_id,
        )
        if item.operator == "full_day_range":
            if len(item.parameter_ids) != 2:
                return None
            predicate = Expression(
                kind="binary",
                operator="AND",
                args=[
                    Expression(
                        kind="binary",
                        operator=">=",
                        args=[
                            column,
                            Expression(
                                kind="parameter",
                                parameter_id=item.parameter_ids[0],
                            ),
                        ],
                    ),
                    Expression(
                        kind="binary",
                        operator="<",
                        args=[
                            column,
                            Expression(
                                kind="function",
                                operator="DATEADD",
                                args=[
                                    literal("day"),
                                    literal(1),
                                    Expression(
                                        kind="parameter",
                                        parameter_id=item.parameter_ids[1],
                                    ),
                                ],
                            ),
                        ],
                    ),
                ],
            )
        elif item.operator == "between":
            if len(item.parameter_ids) != 2:
                return None
            predicate = Expression(
                kind="binary",
                operator="AND",
                args=[
                    Expression(
                        kind="binary",
                        operator=">=",
                        args=[
                            column,
                            Expression(
                                kind="parameter",
                                parameter_id=item.parameter_ids[0],
                            ),
                        ],
                    ),
                    Expression(
                        kind="binary",
                        operator="<=",
                        args=[
                            column,
                            Expression(
                                kind="parameter",
                                parameter_id=item.parameter_ids[1],
                            ),
                        ],
                    ),
                ],
            )
        elif item.operator in {"is_null", "is_not_null"}:
            predicate = Expression(
                kind="unary",
                operator=(
                    "IS NULL"
                    if item.operator == "is_null" else "IS NOT NULL"
                ),
                args=[column],
            )
        elif item.operator in comparison_operators:
            values = []
            values.extend(
                Expression(kind="parameter", parameter_id=value)
                for value in item.parameter_ids
            )
            values.extend(
                literal(field.literal_map.get(str(value), value))
                for value in item.literal_values
            )
            if len(values) != 1:
                return None
            predicate = Expression(
                kind="binary",
                operator=comparison_operators[item.operator],
                args=[column, values[0]],
            )
        else:
            return None
        predicates.append(predicate)

    root = PlanNode(
        node_id="scan_" + entity_id,
        kind="scan",
        entity_id=entity_id,
    )
    if predicates:
        predicate = predicates[0]
        for item in predicates[1:]:
            predicate = Expression(
                kind="binary", operator="AND", args=[predicate, item],
            )
        root = PlanNode(
            node_id="filter_contract",
            kind="filter",
            input=root,
            predicate=predicate,
        )

    derived_by_output = {
        item.output_id: item.expression for item in contract.derived_fields
    }

    def output_expression(output):
        derived = derived_by_output.get(output.id)
        if derived is not None:
            return semantic_expression(derived)
        field = field_by_semantic.get(output.id)
        if field is None:
            raise KeyError(output.id)
        direct = Expression(
            kind="column", field_binding_id=field.binding_id,
        )
        if (
            output.logical_type == "date"
            and field.sql_type.casefold() in {
                "datetime", "datetime2", "smalldatetime", "datetimeoffset",
            }
        ):
            return Expression(
                kind="cast", target_type="date", args=[direct],
            )
        return direct

    try:
        all_projections = [
            NamedExpression(
                name=output.name,
                expression=output_expression(output),
            )
            for output in contract.outputs
        ]
    except KeyError:
        return None
    full = RelationalPlan(
        plan_id="deterministic_" + contract.contract_id.replace(":", "_"),
        purpose=contract.purpose,
        root=PlanNode(
            node_id="project_contract",
            kind="project",
            input=root,
            projections=all_projections,
        ),
        result_schema=_result_schema_v3(contract),
    )
    full = RelationalPlan.model_validate(
        _lower_sibling_output_dependencies_v3(
            full.model_dump(mode="json")
        )
    )
    wanted = [item.name for item in result_schema]
    if [item.name for item in full.result_schema] == wanted:
        return full
    outputs_by_name = {item.name: item for item in contract.outputs}
    selected = []
    for name in wanted:
        output = outputs_by_name.get(name)
        if output is None:
            return None
        expression = (
            semantic_expression(derived_by_output[output.id])
            if output.id in derived_by_output
            else Expression(kind="output", output_name=name)
        )
        selected.append(NamedExpression(name=name, expression=expression))
    return RelationalPlan(
        plan_id=full.plan_id + "_projection",
        purpose=full.purpose,
        root=PlanNode(
            node_id="project_fact",
            kind="project",
            input=full.root,
            projections=selected,
        ),
        result_schema=result_schema,
    )


def _compact_catalog_candidates_payload(catalog) -> list[dict]:
    return [
        {
            "schema": item.schema,
            "object": item.name,
            "object_type": item.object_type,
            "column_sample": [column.name for column in item.columns[:8]],
        }
        for item in catalog.objects
    ]


def _resolve_catalog_candidates(
    catalog,
    payload: dict,
    *,
    max_objects: int = 16,
):
    requested = payload.get("objects") if isinstance(payload, dict) else None
    if (
        not isinstance(requested, list)
        or not requested
        or len(requested) > max_objects
        or not all(isinstance(item, str) and "." in item for item in requested)
    ):
        raise ValueError(
            f"Schema 候选对象必须是 1~{max_objects} 个 schema.object 字符串"
        )
    by_name = {
        f"{item.schema}.{item.name}".casefold(): item
        for item in catalog.objects
    }
    selected = []
    seen = set()
    for name in requested:
        key = name.strip().casefold()
        if key in seen:
            continue
        if key not in by_name:
            raise ValueError(f"Schema 候选对象不在 Catalog 中: {name}")
        selected.append(by_name[key])
        seen.add(key)
    if not selected:
        raise ValueError("Schema 候选对象为空")
    return selected


def _generate_schema_binding_proposal_v3(
    llm,
    contract,
    catalog,
    repair_error=None,
    *,
    return_draft: bool = False,
):
    from app.contracts.schema import SchemaBindingDraft
    from app.services.schema_binding_v3 import SchemaBindingError

    candidate_prompt = f"""
从 SQL Server Catalog 的紧凑对象清单中召回可能承载业务合同的候选对象。
这是召回阶段，不做最终字段绑定；宁可保留多个合理候选，也不能编造对象。
最多返回 16 个真实的 schema.object 名称。

SemanticContract：
{contract.canonical_json()}

紧凑 Catalog：
{json.dumps(_compact_catalog_candidates_payload(catalog), ensure_ascii=False, separators=(",", ":"))}

只返回 JSON：{{"objects":["schema.object"]}}。
"""
    try:
        candidate_objects = _resolve_catalog_candidates(
            catalog,
            _candidate_json(
                llm,
                candidate_prompt,
                f"{contract.procedure_name} schema candidate retrieval",
            ),
        )
    except Exception as exc:
        raise SchemaBindingError(
            "SCHEMA_CANDIDATE_RETRIEVAL_FAILED",
            f"无法从完整 Catalog 召回有限候选对象: {exc}",
        ) from exc

    candidate_ids = {item.object_id for item in candidate_objects}
    derived_outputs = {item.output_id for item in contract.derived_fields}
    required_semantics = sorted(
        (
            {item.id for item in contract.source_fields}
            if contract.facts else {
                item.id for item in contract.outputs
                if item.id not in derived_outputs
            }
        )
        | {
            field_id
            for item in contract.filters
            for field_id in item.field_ids
        }
    )
    required_literal_mappings = [
        {
            "filter_id": item.id,
            "field_ids": item.field_ids,
            "semantic_literals": item.literal_values,
        }
        for item in contract.filters
        if item.literal_values
    ]
    catalog_payload = {
        "database": catalog.database_name,
        "objects": [
            {
                "schema": item.schema,
                "object": item.name,
                "object_id": item.object_id,
                "columns": [
                    {
                        "name": column.name,
                        "column_id": column.column_id,
                        "sql_type": column.sql_type,
                    }
                    for column in item.columns
                ],
                "primary_key": item.primary_key,
                "unique_keys": item.unique_keys,
            }
            for item in candidate_objects
        ],
        "foreign_keys": [
            item.model_dump(mode="json") for item in catalog.foreign_keys
            if (
                item.parent_object_id in candidate_ids
                and item.referenced_object_id in candidate_ids
            )
        ],
    }
    prompt = f"""
把已确认的纯业务 SemanticContract 映射到给定 SQL Server Catalog。
只能选择 Catalog 中真实存在且语义明确的对象和字段；不确定或存在多个候选时不要猜测。
当前 SchemaBinding 协议中，每个 SemanticContract entity 必须且只能绑定一个
物理对象；proposal.fields 的列必须真实属于该 entity 已绑定的对象。不得把
其他对象的列挂到当前 entity 上，也不得用一个复合 entity 代替头/行等多个对象。
已经唯一确定的实体、字段和关联必须写入 proposal；仍有两个及以上合理候选的
semantic_id 必须写入 ambiguities。proposal 与 ambiguities 可以同时存在，
用于表达“部分绑定已确定、部分仍待决”。不得修改业务合同。
如果多个真实字段只是计算业务指标所需的组成部分，而没有任何一个字段能单独完整
表达该业务含义，不能让用户从中硬选一个。此时 required_semantic_shape 必须是
derived_expression、multi_entity_fact 或 missing_join，candidates 仅作为
Catalog 证据。只有每个候选都能独立完整表达相同业务含义时，才使用
user_choice_required。
proposal.fields 必须逐项覆盖以下 semantic_id（不得遗漏，派生输出除外）：
{json.dumps(required_semantics, ensure_ascii=False)}
以下业务常量必须在对应 FieldBindingProposal.literal_map 中映射为真实物理存储值。
literal_map 的键是业务常量的字符串形式，值是 SQL 应使用的物理值：
{json.dumps(required_literal_mappings, ensure_ascii=False)}

SemanticContract：
{contract.canonical_json()}

Catalog：
{json.dumps(catalog_payload, ensure_ascii=False, separators=(",", ":"))}

SchemaBindingDraft JSON Schema：
{json.dumps(SchemaBindingDraft.model_json_schema(), ensure_ascii=False, separators=(",", ":"))}

{(
    "前一次绑定提案未通过确定性校验。错误："
    + str(repair_error)
    + "；证据："
    + json.dumps(
        getattr(repair_error, "evidence", {}),
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    + "。只能修复遗漏或错误的实体/字段绑定，不得修改业务合同。"
) if repair_error is not None else ""}

只返回 SchemaBindingDraft JSON。
"""
    prompt += """

候选数量协议：0 个候选表示缺失；1 个候选表示已经确定，必须写入完整
proposal；只有 2 个及以上候选才允许写入 ambiguities。不得把唯一候选包装成
歧义。proposal 与 ambiguities 可以同时存在，用于表达“部分绑定已确定、部分
仍待决”；但只要 ambiguities 非空，系统就不会冻结或执行该绑定。
"""
    raw_draft = None
    draft = None
    last_error = None
    for attempt in range(2):
        current_prompt = prompt
        if attempt:
            current_prompt += f"""

上一份 SchemaBindingDraft 不符合结构协议：
{json.dumps(raw_draft, ensure_ascii=False, default=str)}
错误：{last_error}

只修复 SchemaBindingDraft 的结构，不得修改 SemanticContract：
- 唯一候选必须进入完整 proposal；
- 真正存在两个及以上候选时才进入 ambiguities；
- proposal 必须覆盖全部实体、字段、关联和 literal_map。
只返回修复后的完整 SchemaBindingDraft JSON。
"""
        raw_draft = _candidate_json(
            llm,
            current_prompt,
            f"{contract.procedure_name} schema binding",
        )
        try:
            draft = SchemaBindingDraft.model_validate(raw_draft)
            draft = _resolve_deterministic_schema_ambiguities_v3(
                draft, contract, catalog,
            )
            break
        except Exception as exc:
            last_error = exc
    if draft is None:
        raise SchemaBindingError(
            "SCHEMA_BINDING_DRAFT_INVALID",
            f"SchemaBindingDraft 连续两次不符合结构协议: {last_error}",
            evidence={"draft": raw_draft},
        )
    if draft.ambiguities:
        if return_draft:
            return draft
        raise SchemaBindingError(
            "SCHEMA_OBJECT_AMBIGUOUS",
            "业务语义存在多个合理物理绑定，系统拒绝自动选择",
            evidence={
                "ambiguities": [
                    item.model_dump(mode="json") for item in draft.ambiguities
                ],
                "draft": draft.model_dump(mode="json", by_alias=True),
            },
        )
    return draft if return_draft else draft.proposal


def _resolve_deterministic_schema_ambiguities_v3(
    draft,
    contract,
    catalog,
):
    """用合同含义和目录证据消解唯一可行候选，保留真正多解项。"""
    from app.contracts.schema import SchemaBindingDraft
    from app.services.schema_binding_v3 import (
        SchemaBindingError,
        _validate_currency_scope,
    )

    if draft.proposal is None:
        return draft
    payload = draft.model_dump(mode="python", by_alias=True)
    proposal = payload["proposal"]
    entity_proposals = {
        item["entity_id"]: item for item in proposal["entities"]
    }
    source_fields = {
        item.id: item for item in contract.source_fields
    }
    sole_entity_id = (
        contract.entities[0].id if len(contract.entities) == 1 else None
    )
    remaining = []
    for ambiguity in draft.ambiguities:
        if ambiguity.required_semantic_shape != "user_choice_required":
            remaining.append(ambiguity.model_dump(mode="python"))
            continue
        source = source_fields.get(ambiguity.semantic_id)
        semantic_entity_id = (
            source.entity_id if source is not None else sole_entity_id
        )
        if semantic_entity_id is None:
            remaining.append(ambiguity.model_dump(mode="python"))
            continue
        entity = entity_proposals.get(semantic_entity_id)
        if entity is None:
            remaining.append(ambiguity.model_dump(mode="python"))
            continue
        physical = next(
            (
                item for item in catalog.objects
                if (
                    item.schema.casefold()
                    == str(entity["schema"]).casefold()
                    and item.name.casefold()
                    == str(entity["object"]).casefold()
                )
            ),
            None,
        )
        if physical is None:
            remaining.append(ambiguity.model_dump(mode="python"))
            continue
        columns = {item.name.casefold(): item.name for item in physical.columns}
        compatible = []
        for candidate in ambiguity.candidates:
            parts = str(candidate).split(".")
            column_key = parts[-1].casefold()
            if len(parts) > 1 and parts[-2].casefold() != physical.name.casefold():
                continue
            column = columns.get(column_key)
            if column is None:
                continue
            try:
                _validate_currency_scope(
                    contract,
                    physical,
                    ambiguity.semantic_id,
                    column,
                )
            except SchemaBindingError:
                continue
            compatible.append(column)
        if len(compatible) != 1:
            remaining.append(ambiguity.model_dump(mode="python"))
            continue
        existing = next(
            (
                item for item in proposal["fields"]
                if item["semantic_id"] == ambiguity.semantic_id
            ),
            None,
        )
        if existing is not None:
            existing["column"] = compatible[0]
        else:
            proposal["fields"].append({
                "binding_id": ambiguity.semantic_id,
                "semantic_id": ambiguity.semantic_id,
                "entity_id": semantic_entity_id,
                "column": compatible[0],
                "literal_map": {},
            })
    for field in proposal["fields"]:
        semantic_id = field["semantic_id"]
        source = source_fields.get(semantic_id)
        semantic_entity_id = (
            source.entity_id if source is not None else sole_entity_id
        )
        entity = entity_proposals.get(semantic_entity_id)
        if entity is None:
            continue
        physical = next(
            (
                item for item in catalog.objects
                if (
                    item.schema.casefold()
                    == str(entity["schema"]).casefold()
                    and item.name.casefold()
                    == str(entity["object"]).casefold()
                )
            ),
            None,
        )
        if physical is None:
            continue
        try:
            _validate_currency_scope(
                contract,
                physical,
                semantic_id,
                field["column"],
            )
            continue
        except SchemaBindingError as exc:
            candidates = exc.evidence.get("candidates") or []
        compatible = []
        for column in candidates:
            try:
                _validate_currency_scope(
                    contract,
                    physical,
                    semantic_id,
                    column,
                )
            except SchemaBindingError:
                continue
            compatible.append(column)
        if len(compatible) == 1:
            field["column"] = compatible[0]
    payload["ambiguities"] = remaining
    return SchemaBindingDraft.model_validate(payload)


def _canonicalize_reference_fact_projections(
    payload: dict,
    contract,
) -> None:
    names = {item.name.casefold(): item.name for item in contract.outputs}
    ids = {item.id.casefold(): item.name for item in contract.outputs}
    for fact in payload.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        projection = fact.get("actual_projection")
        if not isinstance(projection, list):
            continue
        fact["actual_projection"] = [
            names.get(str(item).casefold())
            or ids.get(str(item).casefold())
            or item
            for item in projection
        ]


def _validate_reference_fact_designs_v3(payload, contract, adapter):
    from app.services.reference_planner import ReferenceBuildError

    _canonicalize_reference_fact_projections(payload, contract)
    facts = adapter.validate_python(payload.get("facts"))
    output_names = {item.name.casefold() for item in contract.outputs}
    grain_names = {
        item.name.casefold()
        for item in contract.outputs
        if item.id in contract.grain
    }
    covered = set()
    seen_ids = set()
    for fact in facts:
        if fact.fact_id in seen_ids:
            raise ReferenceBuildError(
                "REFERENCE_FACT_ID_DUPLICATE",
                f"Reference Fact ID 重复: {fact.fact_id}",
            )
        seen_ids.add(fact.fact_id)
        projected = {item.casefold() for item in fact.actual_projection}
        covered.update(projected)
        unknown = sorted(projected - output_names)
        if unknown:
            raise ReferenceBuildError(
                "REFERENCE_FACT_OUTPUT_UNKNOWN",
                f"Reference Fact {fact.fact_id} 引用未知输出: "
                + ", ".join(unknown),
                evidence={"unknown": unknown},
            )
        if (
            contract.result_mode != "scalar_summary"
            and not grain_names.issubset(projected)
        ):
            missing = sorted(grain_names - projected)
            raise ReferenceBuildError(
                "REFERENCE_FACT_GRAIN_MISSING",
                f"Reference Fact {fact.fact_id} 缺少稳定业务粒度输出",
                evidence={"missing": missing},
            )
    missing_outputs = sorted(output_names - covered)
    if missing_outputs:
        raise ReferenceBuildError(
            "REFERENCE_FACT_COVERAGE_INCOMPLETE",
            "Reference Facts 未覆盖全部合同输出",
            evidence={"missing": missing_outputs},
        )
    return facts


def _generate_reference_fact_designs_v3(
    llm, contract, repair_events: list[dict] | None = None,
):
    from pydantic import TypeAdapter
    from app.contracts.reference import ReferenceFactDesign
    from app.services.reference_planner import ReferenceBuildError

    adapter = TypeAdapter(list[ReferenceFactDesign])
    prompt = f"""
把业务合同拆成一个或多个独立、最小、可从底层数据证明的 Reference Fact。
多来源对账必须按来源拆分事实，例如业务收入和凭证收入分别形成事实；
不能只创建名为 final_result 的整体复制事实。每个 actual_projection 只能引用合同输出名，
每个明细事实必须包含适用的稳定业务粒度输出；多个事实合起来必须覆盖全部关键输出。

SemanticContract：
{contract.canonical_json()}

JSON Schema：
{json.dumps(adapter.json_schema(), ensure_ascii=False, separators=(",", ":"))}

只返回 JSON 对象：{{"facts":[...]}}。
"""
    previous = None
    last_exc = None
    for attempt in range(2):
        current_prompt = prompt
        if attempt:
            event = {
                "role": "reference_fact_design",
                "status": "repairing",
                "allowed_changes": ["Fact 拆分", "业务含义", "输出投影"],
                "frozen": ["SemanticContract"],
                "error": str(last_exc),
                "evidence": getattr(last_exc, "evidence", {}),
                "before": previous,
            }
            if repair_events is not None:
                repair_events.append(event)
            current_prompt += f"""

上一次 Reference Fact 设计未通过严格校验。只能修复 Fact 拆分、业务含义和输出投影；
不得改变 SemanticContract。每个明细 Fact 必须包含全部稳定粒度输出，所有 Fact 合起来
必须覆盖全部合同输出。
上一次输出：
{json.dumps(previous, ensure_ascii=False, default=str)}
错误：{last_exc}
错误证据：
{json.dumps(getattr(last_exc, "evidence", {}), ensure_ascii=False, default=str)}
只返回修复后的 JSON 对象：{{"facts":[...]}}。
"""
        try:
            payload = _candidate_json(
                llm,
                current_prompt,
                f"{contract.procedure_name} reference facts",
            )
            previous = payload
            facts = _validate_reference_fact_designs_v3(
                payload, contract, adapter,
            )
            if attempt and repair_events is not None:
                repair_events[-1].update(
                    status="repaired",
                    after=[
                        item.model_dump(mode="json") for item in facts
                    ],
                )
            return facts
        except Exception as exc:
            last_exc = exc
    if isinstance(last_exc, ReferenceBuildError):
        raise last_exc
    raise ReferenceBuildError(
        "REFERENCE_FACT_DESIGN_INVALID",
        f"Reference Fact 设计无效: {last_exc}",
        evidence=getattr(last_exc, "evidence", {}),
    ) from last_exc


def _comparator_for_projection_v3(contract, projection: list[str]):
    from app.contracts.reference import ComparatorSpec

    projected = {item.casefold() for item in projection}
    outputs = [
        item for item in contract.outputs
        if item.name.casefold() in projected
    ]
    names_by_id = {item.id: item.name for item in contract.outputs}
    keys = [
        names_by_id[item] for item in contract.grain
        if names_by_id[item].casefold() in projected
    ]
    tolerance = {
        item.name: contract.money_tolerance
        for item in outputs
        if item.logical_type in {"money", "decimal"} and item.name not in keys
    }
    if contract.result_mode == "scalar_summary":
        return ComparatorSpec(
            type="scalar_metrics_equal",
            compare_columns=[item.name for item in outputs],
            tolerance=tolerance,
        )
    if keys:
        compare = [
            item.name for item in outputs if item.name not in keys
        ] or keys
        return ComparatorSpec(
            type="keyed_rows_equal",
            key_columns=keys,
            compare_columns=compare,
            tolerance=tolerance,
        )
    return ComparatorSpec(
        type="multiset_rows_equal",
        compare_columns=[item.name for item in outputs],
        tolerance=tolerance,
    )


def schema_capture_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    """Capture the physical catalog before any Reference or procedure work."""
    from app.contracts.semantic import SemanticDesign
    from app.services.catalog_v3 import (
        capture_catalog_snapshot,
        catalog_fingerprint,
    )
    from app.db.sqlite import invalidate_schema_resolution_checkpoints

    writer = _get_writer(config)
    try:
        semantic_design = SemanticDesign.model_validate(state.get("query_spec"))
        _write_progress(
            writer, "schema_capture", "正在读取测试数据库的 Schema 元数据...",
        )
        catalog = capture_catalog_snapshot()
        fingerprint = catalog_fingerprint(catalog)
        invalidate_schema_resolution_checkpoints(
            state["session_id"],
            except_design_hash=semantic_design.content_hash,
            except_catalog_fingerprint=fingerprint,
        )
        return {
            "semantic_design_hash": semantic_design.content_hash,
            "schema_catalog": catalog.model_dump(mode="json", by_alias=True),
            "schema_fingerprint": fingerprint,
            "schema_resolution_status": "captured",
            "status": "schema_resolving",
            "mode": "schema_resolve",
            "error": "",
        }
    except Exception as exc:
        return {
            "schema_resolution_status": "failed",
            "status": "generation_failed",
            "error": f"无法读取数据库 Schema：{exc}",
            "issues": [_generation_issue_v3(exc)],
        }


def _proposal_with_schema_selections(
    contract,
    checkpoint,
    response,
):
    from app.contracts.schema import SchemaBindingProposal

    if not isinstance(response, dict):
        raise ValueError("Schema 选择必须是结构化对象")
    if response.get("checkpoint_id") != checkpoint.checkpoint_id:
        raise ValueError("SCHEMA_CHECKPOINT_STALE: checkpoint_id")
    selections = response.get("selections")
    if not isinstance(selections, dict):
        raise ValueError("Schema 选择缺少 selections")
    selectable = {
        issue.issue_id: {
            candidate.candidate_id: candidate
            for candidate in issue.physical_candidates
        }
        for issue in checkpoint.issues
        if issue.category == "physical_ambiguity"
    }
    if set(selections) != set(selectable):
        raise ValueError("必须逐项完成当前全部 Schema 选择")
    proposal = checkpoint.partial_proposal
    if proposal is None:
        raise ValueError("Schema 选择缺少已确定的部分绑定")
    payload = proposal.model_dump(mode="python", by_alias=True)
    source_entities = {
        item.id: item.entity_id for item in contract.source_fields
    }
    sole_entity = (
        contract.entities[0].id if len(contract.entities) == 1 else None
    )
    for issue_id, candidate_id in selections.items():
        candidate = selectable[issue_id].get(candidate_id)
        if candidate is None:
            raise ValueError("Schema 候选已失效，请刷新后重新选择")
        fragment = candidate.physical_binding_fragment
        semantic_id = candidate.semantic_id
        entity_id = source_entities.get(semantic_id) or sole_entity
        if entity_id is None:
            raise ValueError(
                f"业务字段 {semantic_id} 尚未归属单一实体，必须修订设计"
            )
        existing = next(
            (
                item for item in payload["fields"]
                if item["semantic_id"] == semantic_id
            ),
            None,
        )
        field = {
            "binding_id": (
                existing["binding_id"] if existing else semantic_id
            ),
            "semantic_id": semantic_id,
            "entity_id": entity_id,
            "column": fragment["column"],
            "literal_map": (
                existing.get("literal_map", {}) if existing else {}
            ),
        }
        if existing:
            existing.update(field)
        else:
            payload["fields"].append(field)
    return SchemaBindingProposal.model_validate(payload), selections


def schema_resolve_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    """Resolve every contract to one immutable physical SchemaBinding."""
    import uuid

    from app.contracts.schema import CatalogSnapshot
    from app.contracts.schema_resolution import SchemaResolutionCheckpoint
    from app.contracts.semantic import SemanticDesign
    from app.db.sqlite import (
        get_schema_resolution_checkpoint,
        save_schema_resolution_checkpoint,
    )
    from app.services.catalog_v3 import catalog_fingerprint
    from app.services.schema_binding_v3 import build_schema_binding
    from app.services.schema_resolution_v3 import (
        issue_from_exception,
        issues_from_draft,
    )

    llm = _get_llm()
    writer = _get_writer(config)
    design = SemanticDesign.model_validate(state.get("query_spec"))
    catalog = CatalogSnapshot.model_validate_json(
        json.dumps(state.get("schema_catalog"), ensure_ascii=False)
    )
    fingerprint = catalog_fingerprint(catalog)
    if state.get("semantic_design_hash") != design.content_hash:
        return {
            "status": "generation_failed",
            "schema_resolution_status": "failed",
            "error": "业务设计版本已变化，请重新捕获 Schema",
        }
    resolved = []
    checkpoints = []
    for index, contract in enumerate(design.contracts, start=1):
        _write_progress(
            writer,
            "schema_resolution",
            f"正在解析 Schema {index}/{len(design.contracts)}："
            f"{contract.procedure_name}",
        )
        raw_checkpoint = get_schema_resolution_checkpoint(
            state["session_id"], contract.contract_id,
        )
        checkpoint = None
        if raw_checkpoint:
            raw_checkpoint.pop("updated_at", None)
            candidate = SchemaResolutionCheckpoint.model_validate(
                raw_checkpoint,
            )
            if (
                candidate.design_hash == design.content_hash
                and candidate.catalog_fingerprint == fingerprint
                and candidate.status != "invalidated"
            ):
                checkpoint = candidate

        proposal = None
        repair_error = None
        repair_count = checkpoint.repair_count if checkpoint else 0
        if checkpoint and checkpoint.status == "awaiting_schema_choice":
            response = interrupt({
                "type": "schema_choice",
                "checkpoint_id": checkpoint.checkpoint_id,
                "design_hash": checkpoint.design_hash,
                "catalog_fingerprint": checkpoint.catalog_fingerprint,
                "issues": [
                    item.model_dump(mode="json")
                    for item in checkpoint.issues
                ],
            })
            proposal, selections = _proposal_with_schema_selections(
                contract, checkpoint, response,
            )
            checkpoint = checkpoint.model_copy(update={
                "partial_proposal": proposal,
                "issues": [],
                "user_selections": selections,
                "status": "proposing",
            })
            save_schema_resolution_checkpoint(
                checkpoint,
                expected_design_hash=design.content_hash,
                expected_catalog_fingerprint=fingerprint,
                expected_status="awaiting_schema_choice",
            )
        elif checkpoint and checkpoint.status == "resolved":
            proposal = checkpoint.partial_proposal

        for attempt in range(2):
            if proposal is None:
                try:
                    draft = _generate_schema_binding_proposal_v3(
                        llm,
                        contract,
                        catalog,
                        repair_error,
                        return_draft=True,
                    )
                except Exception as exc:
                    issue = issue_from_exception(contract, exc)
                    if (
                        issue.category == "binding_repairable"
                        and repair_count < 1
                        and attempt == 0
                    ):
                        repair_error = exc
                        repair_count += 1
                        continue
                    return {
                        "status": "generation_failed",
                        "schema_resolution_status": "failed",
                        "schema_resolution_issues": [
                            issue.model_dump(mode="json")
                        ],
                        "error": issue.reason,
                    }
                issues = issues_from_draft(contract, catalog, draft)
                capability = [
                    item for item in issues
                    if item.category == "semantic_capability_gap"
                ]
                if capability:
                    checkpoint = SchemaResolutionCheckpoint(
                        checkpoint_id=(
                            checkpoint.checkpoint_id
                            if checkpoint else str(uuid.uuid4())
                        ),
                        session_id=state["session_id"],
                        contract_id=contract.contract_id,
                        design_hash=design.content_hash,
                        catalog_fingerprint=fingerprint,
                        partial_proposal=draft.proposal,
                        issues=capability,
                        revision_count=(
                            checkpoint.revision_count
                            if checkpoint else int(
                                state.get("semantic_revision_count", 0)
                            )
                        ),
                        repair_count=repair_count,
                        status="awaiting_design_reconfirmation",
                    )
                    save_schema_resolution_checkpoint(checkpoint)
                    return {
                        "status": "semantic_revision_required",
                        "schema_resolution_status": (
                            "semantic_revision_required"
                        ),
                        "schema_resolution_issues": [
                            item.model_dump(mode="json")
                            for item in capability
                        ],
                        "pending_schema_interaction": {
                            "contract_id": contract.contract_id,
                            "checkpoint_id": checkpoint.checkpoint_id,
                        },
                        "schema_resolution_checkpoints": [
                            checkpoint.model_dump(
                                mode="json",
                                by_alias=True,
                            )
                        ],
                        "error": "",
                    }
                ambiguity = [
                    item for item in issues
                    if item.category == "physical_ambiguity"
                ]
                if ambiguity:
                    checkpoint = SchemaResolutionCheckpoint(
                        checkpoint_id=(
                            checkpoint.checkpoint_id
                            if checkpoint else str(uuid.uuid4())
                        ),
                        session_id=state["session_id"],
                        contract_id=contract.contract_id,
                        design_hash=design.content_hash,
                        catalog_fingerprint=fingerprint,
                        partial_proposal=draft.proposal,
                        issues=ambiguity,
                        revision_count=(
                            checkpoint.revision_count
                            if checkpoint else int(
                                state.get("semantic_revision_count", 0)
                            )
                        ),
                        repair_count=repair_count,
                        status="awaiting_schema_choice",
                    )
                    save_schema_resolution_checkpoint(checkpoint)
                    response = interrupt({
                        "type": "schema_choice",
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "design_hash": checkpoint.design_hash,
                        "catalog_fingerprint": fingerprint,
                        "issues": [
                            item.model_dump(mode="json")
                            for item in ambiguity
                        ],
                    })
                    proposal, selections = _proposal_with_schema_selections(
                        contract, checkpoint, response,
                    )
                    checkpoint = checkpoint.model_copy(update={
                        "partial_proposal": proposal,
                        "issues": [],
                        "user_selections": selections,
                        "status": "proposing",
                    })
                    save_schema_resolution_checkpoint(
                        checkpoint,
                        expected_design_hash=design.content_hash,
                        expected_catalog_fingerprint=fingerprint,
                        expected_status="awaiting_schema_choice",
                    )
                else:
                    proposal = draft.proposal
            try:
                binding = build_schema_binding(contract, catalog, proposal)
                break
            except Exception as exc:
                issue = issue_from_exception(contract, exc)
                if (
                    issue.category == "binding_repairable"
                    and repair_count < 1
                    and attempt == 0
                ):
                    repair_error = exc
                    repair_count += 1
                    proposal = None
                    continue
                return {
                    "status": "generation_failed",
                    "schema_resolution_status": "failed",
                    "schema_resolution_issues": [
                        issue.model_dump(mode="json")
                    ],
                    "error": issue.reason,
                }
        else:
            return {
                "status": "generation_failed",
                "schema_resolution_status": "failed",
                "error": "Schema 绑定修复次数已用尽",
            }
        checkpoint = SchemaResolutionCheckpoint(
            checkpoint_id=(
                checkpoint.checkpoint_id
                if checkpoint else str(uuid.uuid4())
            ),
            session_id=state["session_id"],
            contract_id=contract.contract_id,
            design_hash=design.content_hash,
            catalog_fingerprint=fingerprint,
            partial_proposal=proposal,
            issues=[],
            user_selections=(
                checkpoint.user_selections if checkpoint else {}
            ),
            revision_count=(
                checkpoint.revision_count
                if checkpoint else int(
                    state.get("semantic_revision_count", 0)
                )
            ),
            repair_count=repair_count,
            status="resolved",
        )
        save_schema_resolution_checkpoint(checkpoint)
        checkpoints.append(
            checkpoint.model_dump(mode="json", by_alias=True)
        )
        resolved.append({
            "contract_id": contract.contract_id,
            "catalog_snapshot": catalog.model_dump(
                mode="json", by_alias=True,
            ),
            "schema_binding": binding.model_dump(
                mode="json", by_alias=True,
            ),
        })
    return {
        "schema_artifacts": resolved,
        "schema_resolution_checkpoints": checkpoints,
        "schema_resolution_issues": [],
        "schema_resolution_status": "resolved",
        "status": "schema_resolved",
        "mode": "generate",
        "error": "",
    }


def semantic_revise_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    """Propose a constrained implementation-shape revision."""
    from app.contracts.schema import CatalogSnapshot
    from app.contracts.schema_resolution import (
        SchemaResolutionCheckpoint,
        SchemaResolutionIssue,
        SemanticRevisionProposal,
    )
    from app.contracts.semantic import SemanticDesign
    from app.services.semantic_revision_context import (
        build_semantic_revision_evidence,
        validate_semantic_revision_prompt,
    )
    from app.services.semantic_revision_v3 import evaluate_semantic_revision

    llm = _get_llm()
    writer = _get_writer(config)
    design = SemanticDesign.model_validate(state.get("query_spec"))
    catalog = CatalogSnapshot.model_validate_json(
        json.dumps(state.get("schema_catalog"), ensure_ascii=False)
    )
    pending = state.get("pending_schema_interaction") or {}
    contract = next(
        item for item in design.contracts
        if item.contract_id == pending.get("contract_id")
    )
    issues = [
        SchemaResolutionIssue.model_validate(item)
        for item in state.get("schema_resolution_issues", [])
    ]
    raw_checkpoint = next(
        (
            item
            for item in state.get("schema_resolution_checkpoints", [])
            if item.get("checkpoint_id") == pending.get("checkpoint_id")
        ),
        None,
    )
    checkpoint = (
        SchemaResolutionCheckpoint.model_validate(raw_checkpoint)
        if raw_checkpoint is not None else None
    )
    if int(state.get("semantic_revision_count", 0)) >= 2:
        return {
            "status": "generation_failed",
            "schema_resolution_status": "failed",
            "error": (
                "同一业务设计已连续两次无法映射到当前 Schema，"
                "系统已停止自动修订，请返回业务设计阶段人工确认口径"
            ),
        }
    _write_progress(
        writer,
        "semantic_revision",
        "当前业务口径无法按单字段直接落地，正在准备受限的数据来源修订...",
    )
    try:
        evidence = build_semantic_revision_evidence(
            contract=contract,
            catalog=catalog,
            issues=issues,
            partial_proposal=(
                checkpoint.partial_proposal if checkpoint is not None else None
            ),
        )
    except Exception as exc:
        return {
            "status": "generation_failed",
            "schema_resolution_status": "failed",
            "error": f"无法形成受限 Schema 证据包：{exc}",
        }
    base_prompt = f"""
当前已确认的业务口径不能按现有实现形状绑定到真实数据库。请只修订实现形状：
可以拆分实体、增加必要源字段、把单字段指标改为源字段表达式、补充事实实体和事实
关联；不得改变存储过程用途、参数、金额与币种口径、日期边界、输出、粒度、容差、
结果模式或取消冲销政策。SemanticContract 中仍然禁止出现物理表名和列名。

原 SemanticContract：
{contract.canonical_json()}

与当前问题直接相关的局部 Schema 证据包：
{json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))}

返回 SemanticRevisionProposal JSON，base_contract_hash 必须是
{contract.content_hash}，addressed_issue_ids 必须覆盖全部问题：
{json.dumps(SemanticRevisionProposal.model_json_schema(), ensure_ascii=False)}
"""
    last_error = None
    for _attempt in range(2):
        prompt = base_prompt
        if last_error is not None:
            prompt += (
                "\n上一份修订未通过确定性边界检查："
                + str(last_error)[:12_000]
                + "\n"
            )
        try:
            validate_semantic_revision_prompt(prompt)
        except Exception as exc:
            return {
                "status": "generation_failed",
                "schema_resolution_status": "failed",
                "error": str(exc),
            }
        try:
            payload = _candidate_json(
                llm, prompt, f"{contract.procedure_name} semantic revision",
            )
            proposal = SemanticRevisionProposal.model_validate(payload)
            diff = evaluate_semantic_revision(contract, proposal, issues)
            if not diff.allowed:
                raise ValueError(
                    "受限语义修订越过业务口径边界："
                    + json.dumps(
                        diff.model_dump(mode="json"),
                        ensure_ascii=False,
                    )
                )
            return {
                "semantic_revision": proposal.model_dump(mode="json"),
                "semantic_revision_diff": diff.model_dump(mode="json"),
                "status": "awaiting_design_reconfirmation",
                "schema_resolution_status": (
                    "awaiting_design_reconfirmation"
                ),
                "mode": "design_reconfirm",
                "error": "",
            }
        except Exception as exc:
            last_error = exc
    return {
        "status": "generation_failed",
        "schema_resolution_status": "failed",
        "error": f"无法形成安全的实现形状修订：{last_error}",
    }


def design_reconfirm_node(
    state: AgentState,
    config: RunnableConfig | None = None,
) -> dict:
    """Require explicit confirmation after an allowed semantic revision."""
    from app.contracts.schema_resolution import SemanticRevisionProposal
    from app.contracts.semantic import SemanticDesign
    from app.db.sqlite import save_session_design

    design = SemanticDesign.model_validate(state.get("query_spec"))
    proposal = SemanticRevisionProposal.model_validate(
        state.get("semantic_revision"),
    )
    diff = state.get("semantic_revision_diff") or {}
    response = interrupt({
        "type": "design_revision",
        "content": {
            "reason": "当前数据库无法按原来的单字段/单实体实现形状可靠落地",
            "business_contract_unchanged": True,
            "changes": diff.get("allowed_changes", []),
            "revised_contract": proposal.revised_contract.model_dump(
                mode="json", by_alias=True,
            ),
        },
    })
    if not (
        isinstance(response, dict)
        and response.get("action") == "confirm"
    ):
        return {
            "mode": "design",
            "status": "",
            "design_phase": "feedback",
            "last_feedback_reply": "已返回业务设计阶段，请说明希望修改的业务口径。",
            "error": "",
        }
    contracts = [
        (
            proposal.revised_contract
            if item.content_hash == proposal.base_contract_hash
            else item
        )
        for item in design.contracts
    ]
    revised_payload = design.model_dump(mode="python", by_alias=True)
    revised_payload["contracts"] = [
        item.model_dump(mode="python", by_alias=True) for item in contracts
    ]
    revised_payload["design_version"] = (
        f"schema-revision-{proposal.revised_contract.content_hash[:12]}"
    )
    revised_design = SemanticDesign.model_validate(revised_payload)
    save_session_design(
        state["session_id"],
        status="confirmed",
        query_spec=revised_design.model_dump(mode="json", by_alias=True),
        query_spec_version=3,
        decision_hash=revised_design.decision_hash,
    )
    return {
        "query_spec": revised_design.model_dump(mode="json", by_alias=True),
        "semantic_design_hash": revised_design.content_hash,
        "schema_artifacts": [],
        "schema_resolution_issues": [],
        "semantic_revision": {},
        "semantic_revision_diff": {},
        "semantic_revision_count": (
            int(state.get("semantic_revision_count", 0)) + 1
        ),
        "status": "design_reconfirmed",
        "schema_resolution_status": "design_reconfirmed",
        "mode": "schema_capture",
        "error": "",
    }


def _generate_node_v3(
    state: AgentState,
    semantic_design,
    llm: ChatOpenAI,
    writer,
) -> dict:
    from app.services.catalog_v3 import capture_catalog_snapshot
    from app.services.procedure_generator_v3 import generate_procedure_candidate
    from app.services.reference_planner import (
        ReferenceFactDraft,
        ReferenceBuildError,
        freeze_reference_bundle,
    )
    from app.services.schema_binding_v3 import build_schema_binding
    from app.services.validation_cases import discover_validation_cases
    from app.services.validation_runner_v3 import SqlServerValidationExecutor
    from app.services.sql_compile_v3 import (
        compile_reference,
        validate_compiled_result_schema,
    )
    from app.services.sql_renderer_v3 import SqlRendererV3

    resolved_schema = {
        item["contract_id"]: item
        for item in (state.get("schema_artifacts") or [])
    }
    if resolved_schema:
        from app.contracts.schema import CatalogSnapshot

        catalog = CatalogSnapshot.model_validate_json(
            json.dumps(
                next(iter(resolved_schema.values()))["catalog_snapshot"],
                ensure_ascii=False,
            )
        )
    else:
        _write_progress(
            writer, "schema", "正在捕获 SQL Server 系统目录快照...",
        )
        catalog = capture_catalog_snapshot()
    executor = SqlServerValidationExecutor()
    artifacts = []
    for index, semantic in enumerate(semantic_design.contracts, start=1):
        repair_events = []
        _write_progress(
            writer,
            "reference",
            f"正在建立独立 Reference {index}/{len(semantic_design.contracts)}："
            f"{semantic.procedure_name}",
        )
        if semantic.contract_id in resolved_schema:
            from app.contracts.schema import SchemaBinding

            binding = SchemaBinding.model_validate_json(
                json.dumps(
                    resolved_schema[semantic.contract_id]["schema_binding"],
                    ensure_ascii=False,
                )
            )
        else:
            binding_error = None
            for binding_attempt in range(2):
                try:
                    proposal = _generate_schema_binding_proposal_v3(
                        llm,
                        semantic,
                        catalog,
                        binding_error,
                    )
                    binding = build_schema_binding(
                        semantic, catalog, proposal,
                    )
                    if binding_attempt:
                        repair_events[-1]["status"] = "repaired"
                    break
                except Exception as exc:
                    binding_error = exc
                    if getattr(exc, "code", None) == "SCHEMA_OBJECT_AMBIGUOUS":
                        raise
                    if binding_attempt == 1:
                        raise
                    repair_events.append({
                        "role": "schema_binding",
                        "status": "repairing",
                        "allowed_changes": ["遗漏或错误的实体/字段绑定"],
                        "frozen": ["SemanticContract", "CatalogSnapshot"],
                        "error": str(exc),
                        "evidence": getattr(exc, "evidence", {}),
                    })
        validation_cases = discover_validation_cases(
            semantic, binding, executor
        )
        fact_drafts = []
        result_comparator = None
        deterministic_procedure_plan = None
        if semantic.facts:
            from app.services.fact_compiler_v3 import (
                compile_contract_plan,
                compile_fact_plan,
            )

            for semantic_fact in semantic.facts:
                fact_drafts.append(
                    ReferenceFactDraft(
                        fact_id=semantic_fact.id,
                        meaning=semantic_fact.meaning,
                        actual_projection=[],
                        plan=compile_fact_plan(
                            semantic, binding, semantic_fact,
                        ),
                        comparator=None,
                        comparison_role="source_fact",
                    )
                )
            result_comparator = _comparator_for_projection_v3(
                semantic, [item.name for item in semantic.outputs],
            )
            deterministic_procedure_plan = compile_contract_plan(
                semantic, binding,
            )
            fact_designs = []
        else:
            fact_designs = _generate_reference_fact_designs_v3(
                llm, semantic, repair_events,
            )
        for fact in fact_designs:
            fact_schema = _result_schema_v3(
                semantic, fact.actual_projection
            )

            def validate_reference_plan(
                plan,
                *,
                fact_id=fact.fact_id,
                expected_schema=fact_schema,
            ):
                sql = SqlRendererV3(semantic, binding).render_query(plan)
                compiled = compile_reference(sql, semantic)
                if not compiled.get("ok"):
                    raise ReferenceBuildError(
                        "REFERENCE_COMPILE_FAILED",
                        f"Reference {fact_id} 未通过 SQL Server 静态编译",
                        evidence=compiled,
                    )
                validate_compiled_result_schema(
                    compiled,
                    expected_schema,
                    artifact=f"Reference {fact_id}",
                )

            reference_plan = _generate_relational_plan_v3(
                llm,
                f"reference fact {fact.fact_id}: {fact.meaning}",
                semantic,
                binding,
                fact_schema,
                repair_events,
                post_validator=validate_reference_plan,
            )
            fact_drafts.append(
                ReferenceFactDraft(
                    fact_id=fact.fact_id,
                    meaning=fact.meaning,
                    actual_projection=fact.actual_projection,
                    plan=reference_plan,
                    comparator=_comparator_for_projection_v3(
                        semantic, fact.actual_projection
                    ),
                )
            )
        reference = freeze_reference_bundle(
            semantic,
            binding,
            fact_drafts,
            validation_cases,
            preflight_executor=lambda sql, values, contract=semantic: (
                executor.preflight_reference(contract, sql, values)
            ),
            result_comparator=result_comparator,
        )

        _write_progress(
            writer,
            "candidate",
            f"Reference 已冻结，正在独立生成 SP {index}/{len(semantic_design.contracts)}："
            f"{semantic.procedure_name}",
        )
        procedure_plan = (
            deterministic_procedure_plan
            if deterministic_procedure_plan is not None
            else _generate_relational_plan_v3(
                llm,
                "procedure",
                semantic,
                binding,
                _result_schema_v3(semantic),
                repair_events,
            )
        )
        try:
            candidate = generate_procedure_candidate(
                semantic,
                binding,
                reference,
                lambda _contract, _binding, value=procedure_plan: value,
            )
        except Exception as exc:
            from app.services.procedure_generator_v3 import ProcedureBuildError

            if isinstance(exc, ProcedureBuildError):
                raise
            raise ProcedureBuildError(
                "PROCEDURE_PLAN_INVALID",
                str(exc),
                evidence=getattr(exc, "evidence", {}),
            ) from exc
        artifacts.append(
            {
                "semantic_contract": semantic.model_dump(
                    mode="json", by_alias=True,
                ),
                "catalog_snapshot": catalog.model_dump(
                    mode="json", by_alias=True,
                ),
                "schema_binding": binding.model_dump(
                    mode="json", by_alias=True,
                ),
                "reference_bundle": reference.model_dump(
                    mode="json", by_alias=True,
                ),
                "procedure_candidate": candidate.model_dump(
                    mode="json", by_alias=True,
                ),
                "repair_events": repair_events,
            }
        )
    return {
        "query_spec": semantic_design.model_dump(mode="json", by_alias=True),
        "schema_fingerprint": artifacts[0]["schema_binding"][
            "catalog_fingerprint"
        ] if artifacts else "",
        "v3_artifacts": artifacts,
        "candidate_bundles": [],
        "sp_list": [
            {
                "name": item["semantic_contract"]["procedure_name"],
                "status": "candidate_generated",
            }
            for item in artifacts
        ],
        "mode": "verify",
        "status": "candidate_generated",
        "error": "",
    }


def _verify_node_v3(
    state: AgentState,
    writer,
) -> dict:
    from app.contracts.reference import ReferenceBundle
    from app.contracts.schema import CatalogSnapshot, SchemaBinding
    from app.contracts.semantic import SemanticContract
    from app.contracts.validation import ProcedureCandidateV3
    from app.db.sqlite import (
        delete_sps_except,
        save_sp,
        save_v3_artifacts,
        save_v3_validation_run,
        update_sp,
    )
    from app.services.catalog_v3 import capture_catalog_snapshot
    from app.services.validation_runner_v3 import (
        SqlServerValidationExecutor,
        validate_candidate_v3,
    )

    raw_artifacts = state.get("v3_artifacts") or []
    if not raw_artifacts:
        return {
            "status": "verify_failed",
            "verify_results": [],
            "error": "没有可校验的 V3 候选",
        }
    results = []
    saved_sps = []
    kept_ids = []
    executor = SqlServerValidationExecutor()
    for index, raw in enumerate(raw_artifacts, start=1):
        _write_progress(
            writer,
            "verify",
            f"正在执行同快照对账 {index}/{len(raw_artifacts)}",
        )
        semantic = SemanticContract.model_validate_json(
            json.dumps(raw["semantic_contract"], ensure_ascii=False)
        )
        binding = SchemaBinding.model_validate_json(
            json.dumps(raw["schema_binding"], ensure_ascii=False)
        )
        reference = ReferenceBundle.model_validate_json(
            json.dumps(raw["reference_bundle"], ensure_ascii=False)
        )
        candidate = ProcedureCandidateV3.model_validate_json(
            json.dumps(raw["procedure_candidate"], ensure_ascii=False)
        )
        current_catalog = capture_catalog_snapshot()
        case_evidence = []
        ordered_cases = sorted(
            reference.validation_cases,
            key=lambda item: item.kind == "coverage",
        )
        for case in ordered_cases:
            case_evidence.append(
                validate_candidate_v3(
                    semantic,
                    current_catalog,
                    binding,
                    reference,
                    candidate,
                    executor=executor,
                    case_id=case.case_id,
                )
            )
        evidence = next(
            (
                item for item in case_evidence
                if item.status in {"failed", "inconclusive", "needs_review"}
            ),
            None,
        )
        if evidence is None:
            evidence = next(
                (
                    item for item in case_evidence
                    if item.validation_case.get("kind") == "coverage"
                ),
                case_evidence[0] if case_evidence else None,
            )
        if evidence is None:
            return {
                "status": "verify_failed",
                "verify_results": results,
                "error": (
                    f"{semantic.procedure_name} 没有可执行的验证用例，"
                    "请重新生成 ReferenceBundle"
                ),
            }
        artifact_ids = save_v3_artifacts(
            state["session_id"],
            semantic_contract=semantic,
            catalog_snapshot=current_catalog,
            schema_binding=binding,
            reference_bundle=reference,
            procedure_candidate=candidate,
        )
        runs = [
            save_v3_validation_run(state["session_id"], item)
            for item in case_evidence
        ]
        run = next(
            item for item, item_evidence in zip(runs, case_evidence)
            if item_evidence.content_hash == evidence.content_hash
        )

        sp = save_sp(
            state["session_id"],
            semantic.procedure_name,
            candidate.procedure_sql,
            json.dumps(candidate.parameters, ensure_ascii=False),
            "query",
        )
        kept_ids.append(sp["id"])
        result = evidence.model_dump(mode="json")
        result.update(
            {
                "sp_id": sp["id"],
                "sp_name": semantic.procedure_name,
                "run_id": run["id"],
                "artifact_ids": artifact_ids,
                "repair_events": raw.get("repair_events", []),
                "validation_suite": [
                    {
                        "case": item.validation_case,
                        "status": item.status,
                    }
                    for item in case_evidence
                ],
                "deployment_eligible": evidence.status == "validated",
            }
        )
        validated = evidence.status == "validated"
        update_sp(
            sp["id"],
            status="verified" if validated else (
                "needs_review"
                if evidence.status == "inconclusive"
                else "verify_failed"
            ),
            verify_result=json.dumps(result, ensure_ascii=False),
            validated_hash=candidate.content_hash if validated else None,
            bundle_hash=candidate.content_hash,
            schema_fingerprint=evidence.catalog_fingerprint,
            verification_plan_json=json.dumps(
                {
                    "version": 3,
                    "candidate_hash": candidate.content_hash,
                    "reference_bundle_hash": reference.content_hash,
                },
                ensure_ascii=False,
            ),
        )
        sp.update(
            {
                "status": "verified" if validated else (
                    "needs_review"
                    if evidence.status == "inconclusive"
                    else "verify_failed"
                ),
                "verify_result": json.dumps(result, ensure_ascii=False),
            }
        )
        saved_sps.append(sp)
        results.append(result)
    delete_sps_except(state["session_id"], kept_ids)

    if all(item["status"] == "validated" for item in results):
        status = "persisted"
    elif any(item["status"] == "inconclusive" for item in results):
        status = "needs_review"
    else:
        status = "verify_failed"
    return {
        "status": status,
        "sp_list": saved_sps,
        "v3_artifacts": raw_artifacts,
        "candidate_bundles": [],
        "verify_results": results,
        "error": "",
    }


def _generation_issue_v3(exc: Exception) -> dict:
    from app.contracts.validation import IssueLocation
    from app.services.issues_v3 import issue

    code = getattr(exc, "code", exc.__class__.__name__.upper())
    if code.startswith("ENV_"):
        stage = "environment"
    elif code.startswith("CONTRACT_"):
        stage = "semantic_contract"
    elif code.startswith("SCHEMA_"):
        stage = "schema_binding"
    elif code.startswith("REFERENCE_"):
        stage = (
            "reference_compile"
            if "COMPILE" in code else "reference_preflight"
            if "COVERAGE" in code or "PREFLIGHT" in code
            else "reference_plan"
        )
    elif code.startswith("PROCEDURE_"):
        stage = (
            "procedure_compile" if "COMPILE" in code else "procedure_plan"
        )
    elif code.startswith("PLAN_"):
        stage = "reference_plan"
    else:
        stage = "environment"
    return issue(
        code=re.sub(r"[^A-Z0-9_]", "_", str(code).upper()),
        stage=stage,
        artifact="v3_generation",
        title="V3 生成流水线已停止",
        summary=str(exc),
        evidence=getattr(exc, "evidence", {}),
        technical_detail=str(exc),
        location=IssueLocation(
            contract_path=getattr(exc, "contract_path", None),
            plan_path=getattr(exc, "plan_path", None),
            sql_line=getattr(exc, "sql_line", None),
        ),
        retryable=stage in {"environment", "reference_preflight"},
        user_action=(
            "请根据错误阶段修正业务合同或覆盖参数后重新生成；"
            "系统不会跳过失败步骤继续生成 SP。"
        ),
    ).model_dump(mode="json")


def generate_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """仅从用户确认的 SemanticDesign V3 生成制品。"""
    from app.contracts.semantic import SemanticDesign

    llm = _get_llm()
    writer = _get_writer(config)
    try:
        raw_design = state.get("query_spec")
        if not raw_design:
            raise ValueError("已确认方案缺少 SemanticDesign，请返回方案阶段重新确认")
        semantic_design = SemanticDesign.model_validate(raw_design)
        confirmed_set = state.get("confirmed_decision_set")
        if (
            isinstance(confirmed_set, dict)
            and confirmed_set.get("decision_hash")
            and semantic_design.decision_hash != confirmed_set["decision_hash"]
        ):
            raise ValueError("方案使用的业务决策版本已失效，请返回方案阶段重新确认")
        if not state.get("schema_artifacts"):
            raise ValueError(
                "SchemaBinding 尚未冻结，禁止跳过 Schema 解析直接生成 Reference 或 SP"
            )
        return _generate_node_v3(state, semantic_design, llm, writer)
    except Exception as exc:
        return {
            "status": "generate_failed",
            "error": str(exc),
            "issues": [_generation_issue_v3(exc)],
        }


def verify_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """校验并保存整批候选；只有通过校验的版本具备部署资格。"""
    writer = _get_writer(config)
    if state.get("v3_artifacts"):
        try:
            return _verify_node_v3(state, writer)
        except Exception as exc:
            return {
                "status": "verify_failed",
                "verify_results": [],
                "error": f"V3 校验执行失败：{exc}",
            }
    return {
        "status": "verify_failed",
        "verify_results": [],
        "error": "缺少 V3 冻结制品，旧校验协议已停用，请重新生成",
    }

    # 以下旧候选代码保留到数据库清理迁移完成，但主链路不可达。
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
            existing = {item.gate: item for item in bundle.gate_results}
            existing["business"] = GateResult(
                gate="business",
                status="failed",
                errors=[GateError(
                    artifact="bundle",
                    category="business",
                    code="harness_exception",
                    message=str(exc),
                    schema_subset=None,
                    repairable=False,
                )],
            )
            bundle.gate_results = [
                existing.get(name, GateResult(gate=name))
                for name in (
                    "query_spec", "schema", "safety",
                    "compile", "contract", "business",
                )
            ]
            validated.append(bundle)
            continue
        validated.append(checked)

    results = [_candidate_result(item) for item in validated]
    if any(item.status != "validated" for item in validated):
        status = (
            "needs_review"
            if any(item.status == "needs_review" for item in validated)
            else "verify_failed"
        )
        try:
            inserted = replace_session_candidate_bundles_atomically(
                state["session_id"], validated, results,
            )
        except Exception as exc:
            return {
                "status": status,
                "candidate_bundles": [
                    item.model_dump(mode="json", by_alias=True)
                    for item in validated
                ],
                "verify_results": results,
                "error": f"候选校验未通过且草稿保存失败，旧制品保持不变：{exc}",
            }
        saved_by_name = {item["name"]: item for item in inserted}
        for result in results:
            saved = saved_by_name.get(result["sp_name"]) or {}
            result["sp_id"] = saved.get("id")
            result["status"] = saved.get("status", status)
        return {
            "status": status,
            "sp_list": inserted,
            "candidate_bundles": [
                item.model_dump(mode="json", by_alias=True) for item in validated
            ],
            "verify_results": results,
            "error": "",
        }

    try:
        inserted = replace_session_candidate_bundles_atomically(
            state["session_id"], validated, results,
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
