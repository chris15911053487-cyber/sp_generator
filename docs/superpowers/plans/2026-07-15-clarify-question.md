# 需求澄清问题改进 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让澄清阶段问题编号连续、严格一次只问一个、最多 5 个可提前结束，最后一问末尾允许用户顺便补充，design 阶段显式列出假设兜底质量。

**Architecture:** `clarify_node` 系统控制编号与轮数（`clarify_count` 状态字段），`CLARIFY_PROMPT` 强约束并注入编号/末尾补充提示，`DESIGN_PROMPT` 要求列出假设清单。`chat.py` 在 `input_state` 透传 `clarify_count`。

**Tech Stack:** Python 3.11、LangGraph、LangChain、FastAPI。无 pytest，测试用 `assert` 脚本经 `.venv/Scripts/python.exe` 运行。

## Global Constraints

- 虚拟环境 `.venv/`：所有命令用 `.venv/Scripts/python.exe`。
- 遵循现有代码风格（中文注释、`flush=True` 的 debug print）。
- `clarify_count` 语义为**已问问题数**，初始 0；即将问第 `clarify_count+1` 个。
- 最多 5 个问题：`clarify_count >= 5` 强制进 design。
- 最后一问（第 5 个）在 `clarify_count == 4` 时激活补充提示。
- 保留现有 `user_count >= 6` 作为外部安全网。

---

### Task 1: AgentState 加 clarify_count 字段

**Files:**
- Modify: `app/agent/nodes.py:18-27`（`AgentState`）

**Interfaces:**
- Produces: `AgentState` 新增 `clarify_count: int` 字段，供 Task 4（clarify_node）、Task 5（chat.py）使用。

- [ ] **Step 1: 修改 AgentState**

在 `app/agent/nodes.py` 的 `AgentState` 末尾（`error: str` 后）加字段：

```python
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
```

- [ ] **Step 2: 验证导入不报错**

Run: `.venv/Scripts/python.exe -c "from app.agent.nodes import AgentState; print('clarify_count' in AgentState.__annotations__)"`
Expected: `True`

- [ ] **Step 3: Commit**

```bash
git add app/agent/nodes.py
git commit -m "feat: AgentState 增加 clarify_count 字段"
```

---

### Task 2: 提取 _extract_first_question 纯函数 + 测试

**Files:**
- Modify: `app/agent/nodes.py`（在 `clarify_node` 前加函数）
- Create: `test_clarify.py`

**Interfaces:**
- Produces: `_extract_first_question(content: str) -> str`，截断 LLM 违规输出的多个问题，只返回第一个。

- [ ] **Step 1: 写失败测试 `test_clarify.py`**

```python
"""clarify 相关纯函数测试。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.agent.nodes import _extract_first_question


def test_single_question_unchanged():
    content = "统计维度是按客户还是按月份？\nA. 按客户\nB. 按月份"
    assert _extract_first_question(content) == content


def test_multiple_questions_truncated():
    content = "### 问题1：统计维度？\nA. 按客户\nB. 按月份\n\n### 问题2：时间范围？\nA. 本月\nB. 本年"
    result = _extract_first_question(content)
    assert "问题1" in result
    assert "问题2" not in result


def test_numbered_prefix_truncated():
    content = "问题1：维度？\nA. x\nB. y\n\n问题2：范围？\nA. a\nB. b"
    result = _extract_first_question(content)
    assert "维度" in result
    assert "范围" not in result


def test_empty_string():
    assert _extract_first_question("") == ""


if __name__ == "__main__":
    test_single_question_unchanged()
    test_multiple_questions_truncated()
    test_numbered_prefix_truncated()
    test_empty_string()
    print("PASS")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -X utf8 test_clarify.py`
Expected: FAIL（`ImportError: cannot import name '_extract_first_question'`）

- [ ] **Step 3: 实现 `_extract_first_question`**

在 `app/agent/nodes.py` 的 `clarify_node` 定义之前（`_build_chat_history` 之后）加：

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -X utf8 test_clarify.py`
Expected: `PASS`

- [ ] **Step 5: Commit**

```bash
git add app/agent/nodes.py test_clarify.py
git commit -m "feat: 新增 _extract_first_question 截断 LLM 多问题输出"
```

---

### Task 3: CLARIFY_PROMPT 改写

**Files:**
- Modify: `app/agent/prompts.py:57-70`（`CLARIFY_PROMPT`）

**Interfaces:**
- Produces: `CLARIFY_PROMPT` 新增模板变量 `{q_num}`（当前问题编号）和 `{last_question_hint}`（最后一问的补充提示，非最后一问为空）。供 Task 4 使用。

- [ ] **Step 1: 改写 CLARIFY_PROMPT**

将 `app/agent/prompts.py` 的 `CLARIFY_PROMPT` 替换为：

```python
CLARIFY_PROMPT = """基于用户的以下需求，你正在进行需求澄清（当前是第 {q_num} 个问题，最多 5 个）。
先分析需求涉及哪些 B1 模块和表，然后提出下一个最关键的问题。

## 严格规则（必须遵守）
- **只提出 1 个问题**，不要一次列多个问题，不要把多个子问题合并提问。
- **不要自行编号**（系统会自动编号为 Q{q_num}），输出中不要出现"问题1""Q1"等编号字样。
- 问题应该具体、专业，用选择题形式呈现（A/B/C 选项）。
- 不要把设计方案（SP 划分、参数等）当作澄清问题来问——设计在后续阶段做。

用户需求：
{user_input}

当前对话历史：
{chat_history}

已澄清的信息：
{clarified_info}

请提出第 {q_num} 个需要澄清的问题（只 1 个）。{last_question_hint}
如果信息已经足够充分，请回复 "INFO_SUFFICIENT" 并提供需求摘要。"""
```

- [ ] **Step 2: 验证 format 不报错（占位符齐全）**

Run: `.venv/Scripts/python.exe -c "from app.agent.prompts import CLARIFY_PROMPT; CLARIFY_PROMPT.format(user_input='x', chat_history='', clarified_info='', q_num=1, last_question_hint=''); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/agent/prompts.py
git commit -m "feat: CLARIFY_PROMPT 强约束一次一问、系统编号、末尾补充提示"
```

---

### Task 4: clarify_node 集成编号/限制/截断/hint/答案解析

**Files:**
- Modify: `app/agent/nodes.py:127-171`（`clarify_node`）

**Interfaces:**
- Consumes: `_extract_first_question`（Task 2）、`CLARIFY_PROMPT` 新变量（Task 3）、`AgentState.clarify_count`（Task 1）
- Produces: `clarify_node` 返回值含 `clarify_count`（+1 或保持），供 chat.py 透传。

- [ ] **Step 1: 重写 clarify_node**

将 `app/agent/nodes.py` 的 `clarify_node`（从 `def clarify_node` 到该函数末尾 `}` ）替换为：

```python
def clarify_node(state: AgentState, config: dict = None) -> dict:
    """需求澄清节点 — 系统控制编号，最多 5 个问题，可提前结束。"""
    llm = _get_llm()
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
    response = _invoke_with_tools(llm, messages)

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
```

- [ ] **Step 2: 验证语法与导入**

Run: `.venv/Scripts/python.exe -c "from app.agent.nodes import clarify_node; print('OK')"`
Expected: `OK`

- [ ] **Step 3: 回归测试不破坏**

Run: `.venv/Scripts/python.exe -X utf8 test_clarify.py`
Expected: `PASS`

- [ ] **Step 4: Commit**

```bash
git add app/agent/nodes.py
git commit -m "feat: clarify_node 系统编号、限制5问、截断多问题、最后一问补充提示"
```

---

### Task 5: chat.py 透传 clarify_count

**Files:**
- Modify: `app/routes/chat.py:64-74`、`app/routes/chat.py:84-94`、`app/routes/chat.py:98-108`（三处 input_state）

**Interfaces:**
- Consumes: `AgentState.clarify_count`（Task 1）
- Produces: graph stream 的 input_state 含 `clarify_count`，使跨请求保持计数。

- [ ] **Step 1: 强制 design 分支加 clarify_count**

`app/routes/chat.py` 第 64-74 行的 `new_input` 字典，在 `"error": ""` 后加一行：

```python
                    new_input = {
                        "session_id": req.session_id,
                        "user_input": req.message,
                        "mode": "design",
                        "requirements": requirements,
                        "design": "",
                        "sp_list": [],
                        "verify_results": [],
                        "status": "",
                        "error": "",
                        "clarify_count": state.values.get("clarify_count", 0) if state.values else 0,
                    }
```

- [ ] **Step 2: 继续会话分支加 clarify_count**

`app/routes/chat.py` 第 84-94 行的 `input_state` 字典，在 `"error": ...` 后加一行：

```python
                input_state = {
                    "session_id": req.session_id,
                    "user_input": req.message,
                    "mode": mode,
                    "requirements": state.values.get("requirements", ""),
                    "design": state.values.get("design", ""),
                    "sp_list": state.values.get("sp_list", []),
                    "verify_results": state.values.get("verify_results", []),
                    "status": state.values.get("status", ""),
                    "error": state.values.get("error", ""),
                    "clarify_count": state.values.get("clarify_count", 0),
                }
```

- [ ] **Step 3: 全新会话分支加 clarify_count**

`app/routes/chat.py` 第 98-108 行的 `input_state` 字典，在 `"error": ""` 后加一行：

```python
                input_state = {
                    "session_id": req.session_id,
                    "user_input": req.message,
                    "mode": "clarify",
                    "requirements": "",
                    "design": "",
                    "sp_list": [],
                    "verify_results": [],
                    "status": "",
                    "error": "",
                    "clarify_count": 0,
                }
```

- [ ] **Step 4: 发送 clarify question 事件时拼接系统编号**

`app/routes/chat.py` 流结束后检查中断的代码（约 162-164 行，`if itype == "clarify":` 分支），把编号拼到问题前：

```python
                    if itype == "clarify":
                        q_num = interrupt_val.get("q_num", "") if isinstance(interrupt_val, dict) else ""
                        prefix = f"Q{q_num}：" if q_num else ""
                        assistant_response = prefix + (interrupt_val.get("question", "") if isinstance(interrupt_val, dict) else "")
                        yield f"data: {json.dumps({'type': 'question', 'content': assistant_response})}\n\n"
```

- [ ] **Step 5: 验证语法**

Run: `.venv/Scripts/python.exe -c "from app.routes.chat import router; print('OK')"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add app/routes/chat.py
git commit -m "feat: chat.py 透传 clarify_count 并在问题前拼接系统编号"
```

---

### Task 6: DESIGN_PROMPT 加显式假设清单

**Files:**
- Modify: `app/agent/prompts.py:72-84`（`DESIGN_PROMPT`）

**Interfaces:**
- Produces: `DESIGN_PROMPT` 末尾要求"## 我的假设"清单，作为未澄清项的质量兜底。

- [ ] **Step 1: 改写 DESIGN_PROMPT**

将 `app/agent/prompts.py` 的 `DESIGN_PROMPT` 替换为：

```python
DESIGN_PROMPT = """基于已澄清的需求，现在设计存储过程方案。

需求摘要：
{requirements}

请设计方案，包括：
1. **存储过程列表**：列出需要创建哪些 SP，每个的名称和用途
2. **输入参数**：每个 SP 的参数定义
3. **核心逻辑**：每个 SP 的关键查询步骤
4. **校验方案**：每个 SP 的等价校验 SQL 思路
5. **依赖关系**：SP 之间是否有调用关系
6. **我的假设**：逐条列出所有未在澄清中明确、但 SP 逻辑依赖的决策（例如"假设作废发票=排除""假设科目范围=全量""假设期间粒度=按季度"）。用户确认方案时可一并核对修改。

请用中文输出，格式清晰。"""
```

- [ ] **Step 2: 验证 format 不报错**

Run: `.venv/Scripts/python.exe -c "from app.agent.prompts import DESIGN_PROMPT; DESIGN_PROMPT.format(requirements='x'); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/agent/prompts.py
git commit -m "feat: DESIGN_PROMPT 要求列出显式假设清单作为质量兜底"
```

---

### Task 7: 端到端验证

**Files:**
- 无新文件，手动验证

- [ ] **Step 1: 启动服务器**

Run: `.venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000`（后台）

- [ ] **Step 2: 走完整澄清流程**

在浏览器新建会话，发需求"生成多个存储过程：销售收入统计和财务凭证比对"。
确认：
- 每轮只问 1 个问题，题干带 Q1/Q2/Q3 连续编号（由前端基于 question 内容展示，编号在 requirements 里可见）。
- 问题不超过 5 个；第 5 个问题末尾含"还有其他需求可一并补充"提示。
- LLM 判断信息充足会提前进 design（< 5 个）。
- design 方案含"## 我的假设"小节。

- [ ] **Step 3: 检查 requirements 里的编号连续**

通过 `/api/chat/messages/{sid}` 查看历史，确认 `Q1/Q2/Q3...` 连续不跳跃，答案含选项 + 可能的补充。

- [ ] **Step 4: 若验证通过，收尾**

无需 commit（无代码改动）。若有问题回到对应 Task 修复。
