# 澄清阶段决策协议与防重复改造计划

日期：2026-07-24

## 1. 背景

最新会话 17 中，同一个“未收款金额口径”问题连续出现。会话状态并未卡死：

- 问题编号和 `clarify_count` 正常递增。
- 用户回答只以 `A: b` 的形式追加到 `requirements`。
- 下一轮模型需要自行回看上一道题，推断 `b` 对应的完整业务含义。
- 系统只有“最多问 5 次”的上限，没有判断某个业务决策是否已经确认。
- `CLARIFY_PROMPT` 虽然要求把计算口径等内容留到关键项确认阶段，但这只是自然语言约束，模型违反后没有程序兜底。

因此，重复不是由计数器或最近的 Schema/校验改造直接造成，而是现有澄清协议过度依赖模型理解自由文本。关闭 thinking 可能影响模型遵循复杂提示的稳定性，但不是根因；即使恢复 thinking，只要缺少语义答案和去重门禁，同类问题仍可能再次出现。

## 2. 目标

### 2.1 用户体验目标

- 用户选择 `B` 或输入 `b` 后，系统保存该选项的完整含义，而不是只保存字母。
- 已经确认的业务决策不得再次询问。
- 只有真正阻塞功能定义的问题才在澄清阶段出现。
- 有合理默认值的问题统一进入“关键项确认”阶段。
- 金额口径、状态条件、计算方式等问题不靠关键词硬编码分流。
- 同一模型连续返回重复或无效问题时，系统安全退出澄清阶段，不循环打扰用户。

### 2.2 工程目标

- 用结构化决策协议替代对问题文本的猜测。
- 用稳定 `decision_key` 做主去重，用领域无关的文本相似度做兜底。
- 保持现有 LangGraph 流程、SSE 接口和前端问答方式兼容。
- 不为本次问题新增数据库表或引入向量库、Embedding 服务。
- 通过纯单元测试复现并覆盖会话 17 的重复场景。

## 3. 非目标

- 不改动 SP 生成、Schema、校验、保存、编辑或部署逻辑。
- 不建立“金额口径”“状态条件”等业务关键词黑名单。
- 不在本次改造中解决 LangGraph `MemorySaver` 重启后状态丢失问题。
- 不要求用户必须点击选项；继续支持键盘输入 `A/B/C` 和自由文本。
- 不自动替用户回答真正阻塞的业务问题。
- 不运行会调用真实 LLM 或 SQL Server 的 E2E 测试。

## 4. 核心设计

### 4.1 结构化澄清决策

模型不再直接返回 Markdown 问题，而是返回以下 JSON：

```json
{
  "action": "ask",
  "decision_key": "invoice_scope",
  "decision_type": "blocking",
  "question": "本次查询需要覆盖哪些应收发票？",
  "options": [
    {"id": "A", "value": "全部应收发票"},
    {"id": "B", "value": "仅未结清应收发票"},
    {"id": "C", "value": "仅已结清应收发票"}
  ],
  "reason": "该选择决定查询数据范围"
}
```

信息充分时返回：

```json
{
  "action": "sufficient",
  "summary": "查询未结清应收发票并返回指定明细字段"
}
```

字段约束：

- `decision_key`：描述业务决策本身，不描述第几问；使用稳定、小写的英文标识。
- `decision_type=blocking`：没有答案就无法确定用户要求的功能、输入或输出。
- `decision_type=defaultable`：存在合理默认值，可以在关键项确认阶段一次性确认。
- 澄清阶段只允许向用户展示 `blocking` 问题。

### 4.2 结构化状态

`AgentState` 新增：

```python
clarify_decisions: list[dict]
deferred_decisions: list[dict]
pending_clarify: dict | None
```

`clarify_decisions` 中每项至少保存：

```json
{
  "decision_key": "invoice_scope",
  "question": "本次查询需要覆盖哪些应收发票？",
  "selected_option_id": "B",
  "answer": "仅未结清应收发票"
}
```

`deferred_decisions` 保存模型在澄清阶段识别出的 `defaultable` 项，交给关键项确认阶段使用。

`pending_clarify` 先保存即将展示的问题，再由独立回答节点执行
`interrupt`。这是必要的检查点边界：LangGraph 恢复中断时会重放当前节点，
若模型调用和 `interrupt` 位于同一节点，恢复时可能重新生成另一道题，导致用户
选择被映射到错误问题。

兼容规则：

- 老会话没有这两个字段时按空列表处理。
- `requirements` 继续保留可读文本，避免影响后续设计 Prompt。
- 新答案写成完整语义，例如：

```text
Q2 [invoice_scope]: 本次查询需要覆盖哪些应收发票？
A: 仅未结清应收发票（选项 B）
```

- 不再把 `A: b` 作为后续模型唯一可见的信息。

### 4.3 答案语义解析

新增纯函数将用户输入映射为结构化答案：

```python
resolve_clarify_answer(question_spec, raw_answer) -> dict
```

解析顺序：

1. 去除首尾空格和常见标点，大小写归一。
2. `A`、`a`、`A.`、`选A` 等明确选项输入映射到该选项的 `value`。
3. 用户直接输入完整选项文本时映射到对应选项。
4. 其他输入视为自由文本，完整保留，不猜测选项。

重要约束：

- 只在唯一匹配时映射。
- 不使用模糊匹配替用户选择选项。
- 原始答案可留作诊断，但后续 Prompt 以完整语义答案为准。

### 4.4 双层防重复

新增纯函数：

```python
is_duplicate_clarify_question(candidate, answered_decisions) -> bool
```

第一层：稳定键去重。

- `candidate.decision_key` 已存在于 `clarify_decisions`，直接判重。

第二层：领域无关的语义近似兜底。

- 对题干和选项文本做统一规范化：去编号、空白、标点和选项字母。
- 使用字符 n-gram 相似度比较，不依赖中文分词和业务关键词。
- 题干高度相似，且选项集合也高度相似时才判重。
- 阈值采用保守值，避免把同一主题下的不同阻塞决策误判成重复。

处理策略：

1. 第一次得到重复问题：向同一个模型追加“该决策已确认”的结构化诊断，最多重试 1 次。
2. 重试仍重复、仍是 `defaultable` 或仍无法解析：进入关键项确认阶段。
3. 不增加 `clarify_count`，也不向用户展示被拦截的问题。
4. 不允许因模型持续重复而形成内部无限重试。

### 4.5 阶段分流

分流依据是 `decision_type`，不是问题主题：

- `blocking`：可以在澄清阶段提前询问。
- `defaultable`：写入 `deferred_decisions`，进入关键项确认阶段统一展示。
- `sufficient`：直接进入关键项确认阶段。

因此，“金额口径”并非永远不能提前出现：

- 如果用户要求的结果在不同金额定义下代表完全不同功能，且没有安全默认值，它可以是 `blocking`。
- 如果只是“含税/不含税”之类可提供建议值的配置项，它应是 `defaultable`。

这避免了业务词表与流程代码耦合，也避免把真正阻塞的问题机械推迟。

### 4.6 关键项确认合并

`ASSUMPTIONS_PROMPT` 同时接收：

- 已确认的 `requirements`
- `deferred_decisions`
- 已确认的 `clarify_decisions`

规则：

- 优先把 `deferred_decisions` 规范化为关键项。
- 不得再次生成已存在于 `clarify_decisions` 的 `decision_key`。
- 模型补充的新关键项仍使用现有 `key/title/value/reason` 结构。
- 程序按 `key` 去重；标题相似度只作为兜底。

这样即使某个默认项在澄清阶段被识别，也不会丢失，也不会重新以另一种措辞追问。

## 5. 实施步骤

### Task 1：建立结构化问题解析与校验

涉及文件：

- `app/agent/nodes.py`
- `app/agent/prompts.py`
- `test_clarify.py`

工作内容：

1. 定义澄清问题所需的最小 TypedDict，或使用现有字典风格加集中校验函数。
2. 新增 `_parse_clarify_decision(content)`：
   - 支持纯 JSON 和 Markdown fenced JSON。
   - 校验 `action`。
   - `ask` 必须包含非空 `decision_key`、`decision_type`、`question` 和至少两个合法选项。
   - `sufficient` 必须包含摘要。
3. 修改 `CLARIFY_PROMPT`，明确结构化输出协议和 blocking/defaultable 判定标准。
4. 删除依赖 `INFO_SUFFICIENT` 文本包含关系的主路径；保留旧格式兼容分支，后续可单独清理。
5. 模型输出无法解析时最多纠正重试 1 次；仍失败则进入关键项确认，不能把 JSON 或异常文本展示给用户。

验证：

- 正常 `ask` JSON 可解析。
- fenced JSON 可解析。
- `sufficient` 可解析。
- 缺少选项、重复选项 ID、未知 `decision_type` 会被拒绝。
- 两次无效输出不会产生澄清循环。

### Task 2：保存完整答案语义

涉及文件：

- `app/agent/nodes.py`
- `app/agent/graph.py`
- `test_clarify.py`

工作内容：

1. 为 `AgentState` 增加 `clarify_decisions` 和 `deferred_decisions`。
2. 新增 `_resolve_clarify_answer(question_spec, raw_answer)` 纯函数。
3. `clarify_node` 生成问题并写入 `pending_clarify`。
4. 新增 `clarify_answer_node`，只执行 `interrupt` 和答案保存，不调用模型。
5. 将结构化决策追加到 `clarify_decisions`。
6. 将完整语义写入 `requirements`，不再只写原始字母。
7. 自由文本答案保持原文，并明确标记为自由输入。

验证：

- `b`、`B`、`B.`、`选B` 均解析为 B 的完整含义。
- 完整选项文本可唯一匹配。
- 非选项自由文本不被错误替换。
- 会话 17 的输入 `b` 会保存为“未收款金额口径”的具体选项含义。

### Task 3：增加稳定键和相似度双层去重

涉及文件：

- `app/agent/nodes.py`
- `test_clarify.py`

工作内容：

1. 新增文本规范化与字符 n-gram 相似度纯函数。
2. 新增 `_is_duplicate_clarify_question`。
3. `clarify_node` 在 `interrupt` 前执行去重，禁止把重复候选发给用户。
4. 重复时最多要求模型重新生成 1 次，并在重试 Prompt 中传入：
   - 已确认的 `decision_key`
   - 已确认问题的简短摘要
   - 本次被拒绝的重复原因
5. 第二次仍重复时进入关键项确认阶段。
6. 被拦截的问题不增加 `clarify_count`。

验证：

- 相同 `decision_key`、不同措辞仍被拦截。
- 不同 `decision_key`、相同题干和相同选项仍被相似度兜底拦截。
- 同一主题但不同问题、不同选项不会被误拦截。
- 模型连续返回同一问题时只调用有限次数，不会再次展示给用户。

### Task 4：实现 blocking/defaultable 通用分流

涉及文件：

- `app/agent/nodes.py`
- `app/agent/prompts.py`
- `test_clarify.py`

工作内容：

1. 在 `CLARIFY_PROMPT` 中给出领域无关判定标准：
   - 缺少答案是否无法定义功能、输入或输出。
   - 是否存在可解释且可让用户稍后修改的默认值。
2. `blocking` 通过现有 `interrupt` 展示。
3. `defaultable` 不展示为逐轮问题，写入 `deferred_decisions` 后进入 assumptions。
4. 不再用“过滤条件、金额口径、计算方式”等词语作为程序规则。
5. 保留最多 5 问和用户主动跳过作为最终安全网。

验证：

- 输出粒度缺失且无法默认时可提前询问。
- 有建议默认值的状态过滤进入关键项确认。
- 问题标题即使不含预设关键词也能按类型分流。
- 提前出现的真正阻塞问题不会被错误压到后续阶段。

### Task 5：关键项确认消费延后决策

涉及文件：

- `app/agent/nodes.py`
- `app/agent/prompts.py`
- `test_clarify.py`

工作内容：

1. `ASSUMPTIONS_PROMPT` 增加 `deferred_decisions` 和已确认决策上下文。
2. 将延后项转换为现有 assumptions 数据结构。
3. 按 `key` 合并模型补充项和延后项。
4. 已在澄清阶段确认的 key 不得再次出现。
5. 保持前端 `renderAssumptions` 的输入协议不变，避免无关 UI 改造。

验证：

- defaultable 项会出现在关键项确认卡片。
- 已确认的 blocking 项不会再次出现。
- 模型返回同 key 两次时只保留一项。
- 没有延后项时维持现有行为。

### Task 6：状态透传与旧会话兼容

涉及文件：

- `app/routes/chat.py`
- `app/agent/nodes.py`
- `test_clarify.py`

工作内容：

1. 在全新会话输入状态中初始化两个列表。
2. 在继续会话、跳过澄清、设计反馈等分支中原样透传两个列表。
3. 所有读取使用 `state.get(..., [])`，兼容旧 checkpoint。
4. 不修改 SQLite 表结构。
5. 不改变现有 SSE `question` 事件；问题 JSON 只在后端内部使用，前端仍收到格式化后的题干与选项。

验证：

- 老会话缺少新字段时不报错。
- 多轮请求后已确认决策仍存在。
- 跳过澄清后 deferred 项仍能进入关键项确认。
- 设计反馈、重新生成不会清空既有澄清结果。

### Task 7：会话 17 回归测试

涉及文件：

- `test_clarify.py`

新增一个不调用真实 LLM 的节点级测试，模拟：

1. 模型第一次返回“未收款金额按哪个口径计算”，选项 B 为一个明确语义。
2. 用户回答 `b`。
3. 下一轮模型再次返回相同 `decision_key` 的同一问题。
4. 重试时模型换了 key，但题干和选项语义基本相同。

预期：

- `requirements` 保存 B 的完整含义。
- `clarify_decisions` 保存稳定 key 和语义答案。
- 两个重复候选都不会再次触发用户中断。
- 节点进入 assumptions，或改问一个确实不同的 blocking 问题。
- 模型调用次数受控，不出现无限循环。

## 6. 测试计划

### 6.1 先写失败测试

按以下顺序增加测试并确认在旧实现下失败：

1. 选项字母展开为语义答案。
2. 相同 `decision_key` 防重复。
3. 更换 key 后的相似问题防重复。
4. defaultable 问题延后。
5. 无效 JSON 有限重试。
6. 会话 17 完整回归。

### 6.2 实施后运行

```powershell
.venv\Scripts\python.exe -m pytest test_clarify.py -q --basetemp=.pytest_tmp
```

然后运行与状态图和设计阶段相关的回归：

```powershell
.venv\Scripts\python.exe -m pytest test_clarify.py test_design_confirmation.py test_generation_harness.py test_validation_service.py test_deploy_validation.py test_verify_autofix.py -q --basetemp=.pytest_tmp
```

附加静态检查：

```powershell
.venv\Scripts\python.exe -m compileall app
git diff --check
```

限制：

- 不把 `test_improvements.py` 混入默认测试，它需要访问本地服务。
- 不运行 `test_e2e.py`，除非用户明确授权真实 LLM 和 SQL Server E2E。

## 7. 推荐实施顺序

1. 结构化问题解析与测试。
2. 答案语义展开与结构化状态。
3. 稳定键去重。
4. 相似度兜底和有限重试。
5. blocking/defaultable 分流。
6. assumptions 合并延后项。
7. `chat.py` 状态透传。
8. 会话 17 回归和相关测试集。

每一步完成后都运行 `test_clarify.py`，避免一次性修改整个对话流程后再定位问题。

## 8. 完成标准

满足以下条件才算完成：

1. 用户输入选项字母后，后续上下文包含完整业务含义。
2. 相同 `decision_key` 的问题不会重复展示。
3. 模型更换 key 但返回高度相似问题时仍能被拦截。
4. 重复或无效输出最多重试 1 次，不形成循环。
5. 澄清与关键项确认按 blocking/defaultable 分流，不依赖业务关键词。
6. 真正阻塞的问题允许提前询问。
7. defaultable 问题集中出现在关键项确认阶段。
8. 旧会话和现有前端协议保持兼容。
9. 会话 17 的复现测试通过。
10. 相关单元测试和静态检查全部通过。
