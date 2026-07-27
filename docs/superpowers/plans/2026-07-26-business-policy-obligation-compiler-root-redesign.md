# 业务政策义务编译器根因重构实施计划

日期：2026-07-26  
状态：待实施  
适用范围：V3 查询型 / 报表型 SQL Server 存储过程 Agent  
当前基线：离线回归 `200 passed, 8 skipped`  
兼容策略：所有历史会话均为测试数据；不兼容旧 checkpoint、旧
`business_policies`、旧 `filter_policy_keys`

## 1. 执行结论

下一步不得继续修改 SourceRequirements 提示词来提醒模型补写 `policy_key`。
必须把业务政策从“跨阶段传递的文本”改造成“由程序生成并冻结的实现义务”。

当前错误链路：

```text
ResultContract.business_policies: dict[str, str]
→ FactBlueprint.filter_policy_keys: list[str]
→ LLM 自行在 SourceRequirements.filters 中重复 policy_key/fact_symbols
→ 编译器事后比较 expected/actual
→ 模型可能遗漏、重复或把 join/result/calculation 政策误当来源过滤
```

目标链路：

```text
ConfirmedDecisionSet
→ ResultContract（强类型 BusinessPolicySpec）
→ FactBlueprint（强类型 PolicyBinding）
→ SemanticObligationCompiler（确定性）
→ SemanticObligationSet（冻结）
→ 动态 SourceRequirements 必填 Schema
→ ExpressionDesign
→ SemanticCompiler 覆盖证明
→ SemanticContract
→ Schema → Reference → SP → Validation
```

## 2. 成功标准

实施完成必须同时满足：

- `business_policies` 不再是字符串字典。
- 删除 `FactBlueprintItem.filter_policy_keys`。
- 每项已确认政策都有明确 effect、作用域和目标。
- 程序确定性生成义务 ID、义务槽位和目标，LLM 不能修改。
- 只有 `fact_filter` 政策进入 SourceRequirements 动态必填 Schema。
- join、calculation、result_filter、presentation 政策不得伪装成来源过滤。
- SourceRequirements 无法遗漏、增加或改变政策归属。
- ExpressionDesign 无法把计算政策实现到错误事实值。
- 编译结果包含完整政策覆盖矩阵。
- 任一义务未实现时，Schema、Reference、SP 均不得运行。
- 完整离线回归通过。
- 五类真实数据库 E2E 各连续通过三次。

## 3. 明确不做

- 不为 `comparison_scope`、`journal_sign_basis` 等具体名字编写特判。
- 不根据 SAP 表名或字段名推断政策作用域。
- 不自动把遗漏政策复制到所有事实。
- 不保留旧 `dict[str, str]` 或 `filter_policy_keys` 兼容分支。
- 不通过降低编译器校验强行变绿。
- 不让独立校验 SQL 读取或复制 SP SQL。

## 4. 合同设计

### 4.1 ResultContract：强类型业务政策

修改：

- `app/contracts/semantic_design.py`
- `test_semantic_compiler_v3.py`
- `test_semantic_design_graph_v3.py`

新增：

```python
BusinessPolicyEffect = Literal[
    "source_population",
    "calculation",
    "matching",
    "result_selection",
    "presentation",
]

class BusinessPolicySpec(StrictContract):
    key: Symbol
    value: str
    effect: BusinessPolicyEffect
    meaning: str
```

将：

```python
business_policies: dict[str, str]
```

替换为：

```python
business_policies: list[BusinessPolicySpec]
```

确定性校验：

- policy key 唯一。
- policy value/meaning 通过语义纯度校验。
- 每个 ConfirmedDecision key 恰好对应一个 policy。
- 不允许模型创建未确认政策。
- `money_tolerance` 等已有结构字段不得再重复成为 presentation 文本。

验证：

```powershell
.venv\Scripts\python.exe -m pytest -q `
  test_semantic_compiler_v3.py `
  test_semantic_design_graph_v3.py
```

### 4.2 FactBlueprint：判别式 PolicyBinding

修改：

- `app/contracts/semantic_design.py`
- `app/agent/prompts.py`
- `test_semantic_compiler_v3.py`

删除：

```python
FactBlueprintItem.filter_policy_keys
```

新增互斥绑定：

```python
class FactFilterPolicyBinding:
    kind: Literal["fact_filter"]
    policy_key: Symbol
    fact_symbol: Symbol

class FactExpressionPolicyBinding:
    kind: Literal["fact_expression"]
    policy_key: Symbol
    fact_symbol: Symbol
    value_symbol: Symbol

class JoinPolicyBinding:
    kind: Literal["join"]
    policy_key: Symbol
    join_symbol: Symbol
    match_mode: Literal[
        "matched_only",
        "left_preserved",
        "include_unmatched",
    ]

class ResultFilterPolicyBinding:
    kind: Literal["result_filter"]
    policy_key: Symbol

class ContractOnlyPolicyBinding:
    kind: Literal["contract_only"]
    policy_key: Symbol
```

`PolicyBinding` 必须使用 `kind` discriminator。

effect 与 binding 的唯一合法映射：

| BusinessPolicyEffect | 合法 binding |
|---|---|
| source_population | fact_filter |
| calculation | fact_expression |
| matching | join |
| result_selection | result_filter |
| presentation | contract_only |

确定性校验：

- 每项 policy 至少有一个 binding。
- `fact_filter` 只能引用已声明事实。
- `fact_expression` 必须引用已声明维度或指标。
- `join` 必须引用已声明关联。
- `matched_only` 对应 inner join。
- `left_preserved` 对应 left join。
- `include_unmatched` 对应 full join。
- `result_filter` 与 ResultContract.result_mode 一致。
- 不允许作用域与 effect 不一致。

## 5. 确定性义务编译器

新增：

- `app/contracts/semantic_obligations.py`
- `app/services/semantic_obligation_compiler.py`
- `test_semantic_obligation_compiler_v3.py`

核心合同：

```python
class SemanticPolicyObligation(StrictContract):
    obligation_id: str
    slot_name: Symbol
    policy_key: Symbol
    kind: Literal[
        "fact_filter",
        "fact_expression",
        "join",
        "result_filter",
        "contract_only",
    ]
    fact_symbol: Symbol | None
    value_symbol: Symbol | None
    join_symbol: Symbol | None

class SemanticObligationSet(StrictContract):
    version: Literal[1]
    result_contract_hash: str
    fact_blueprint_hash: str
    obligations: list[SemanticPolicyObligation]
```

义务 ID：

```text
sha256(policy_key + kind + target symbols + upstream hashes)
```

槽位名称：

```text
policy_<kind>_<target>_<policy_key>
```

要求：

- 相同上游输入产生完全一致的 obligation set。
- obligation ID 与 slot_name 由程序生成。
- 不读取数据库 Schema。
- 不包含 SAP、表名、列名或 SQL。
- 发现未覆盖、错误目标或冲突绑定时立即失败。

稳定错误码：

- `POLICY_UNKNOWN`
- `POLICY_EFFECT_BINDING_MISMATCH`
- `POLICY_BINDING_MISSING`
- `POLICY_BINDING_TARGET_UNKNOWN`
- `POLICY_BINDING_DUPLICATE`
- `POLICY_JOIN_MODE_MISMATCH`

## 6. Agent 图与 checkpoint

修改：

- `app/agent/graph.py`
- `app/agent/nodes.py`
- `app/contracts/semantic_design_state.py`
- `app/services/semantic_design_checkpoints.py`
- `app/db/sqlite.py`
- `test_semantic_design_checkpoint_transitions_v3.py`
- `test_semantic_design_persistence_v3.py`

新主链：

```text
result_contract
→ fact_blueprint
→ semantic_obligations
→ source_requirements
→ expression_design
→ semantic_compile
```

新增 checkpoint 字段：

```python
semantic_obligations: SemanticObligationSet | None
```

checkpoint 版本提升，旧测试 checkpoint 直接失效，不做兼容迁移。

失效规则：

- ResultContract 变化：Fact、Obligation、Source、Expression、Compile 全失效。
- FactBlueprint 变化：Obligation、Source、Expression、Compile 全失效。
- ObligationSet 变化：Source、Expression、Compile 全失效。
- SourceRequirements 变化：Expression 与 Compile 失效。
- ExpressionDesign 变化：Compile 失效。

义务编译失败时必须停止在 `semantic_obligations`，不得运行 SourceRequirements。

## 7. 动态 SourceRequirements 必填 Schema

新增：

- `app/contracts/source_requirements_draft.py`
- `app/services/source_obligation_schema.py`
- `test_source_obligation_schema_v3.py`

修改：

- `app/agent/nodes.py`
- `app/agent/prompts.py`
- `app/contracts/semantic_design.py`

### 7.1 LLM 草稿结构

```python
class PolicyFilterImplementation(StrictContract):
    source_symbol: Symbol
    operator: FilterOperator
    parameter_symbols: list[Symbol]
    literal_values: list[Any]
    meaning: str

class SourceRequirementsDraft(StrictContract):
    entities: list[EntityRequirement]
    fields: list[SourceFieldRequirement]
    ordinary_filters: list[OrdinaryFilterRequirement]
    policy_filters: DynamicRequiredPolicyFilters
```

### 7.2 动态模型

`create_source_requirements_response_model(obligation_set)` 必须：

- 只选择 `kind=fact_filter` 的义务。
- 为每个 obligation.slot_name 创建一个必填字段。
- 动态字段值只能是 `PolicyFilterImplementation`。
- JSON Schema 中所有政策槽位均位于 `required`。
- 不把 policy_key、fact_symbol 或 obligation_id 交给 LLM填写。

### 7.3 确定性物化

`materialize_source_requirements(...)`：

- 从 obligation 写入 policy_key。
- 从 obligation 写入 fact_symbols。
- 由 slot_name 生成稳定 filter symbol。
- 合并 ordinary filters。
- 拒绝未知槽位、重复槽位和未使用源字段。
- 输出现有 canonical `SourceRequirements`。

必须证明：

- 模型无法遗漏凭证或销售事实的必填过滤义务。
- 模型无法把销售事实义务改成凭证事实。
- 普通日期参数过滤的 policy_key 永远为 null。

## 8. ExpressionDesign 义务落实

修改：

- `app/contracts/semantic_design.py`
- `app/services/semantic_compiler_v3.py`
- `app/agent/prompts.py`
- `test_semantic_compiler_v3.py`

规则：

- `fact_expression` 义务由其冻结的 fact/value target 对应表达式落实。
- expression target 仍由 FactBlueprint 冻结，LLM不能增加。
- `join` 义务由已验证 join + match_mode 落实。
- `result_filter` 义务必须对应 boolean result_filter。
- `contract_only` 不生成 SQL，但进入覆盖矩阵。

不新增让 LLM重复填写 policy_key 的字段。

编译器生成：

```python
policy_coverage = [
    {
        "obligation_id": "...",
        "policy_key": "...",
        "kind": "...",
        "target": "...",
        "implemented_by": "...",
        "status": "covered",
    }
]
```

稳定错误码：

- `OBLIGATION_IMPLEMENTATION_MISSING`
- `OBLIGATION_IMPLEMENTATION_EXTRA`
- `OBLIGATION_TARGET_CHANGED`
- `OBLIGATION_EXPRESSION_TYPE_MISMATCH`
- `OBLIGATION_RESULT_FILTER_MISSING`

## 9. SemanticCompiler 清理

修改：

- `app/services/semantic_compiler_v3.py`
- `app/services/semantic_symbols.py`
- `test_semantic_compiler_v3.py`

删除：

- 基于 `FactBlueprintItem.filter_policy_keys` 的 expected/actual 比较。
- 基于 `ResultContract.business_policies` 字典的旧消费检查。
- 任何根据 policy 名字推断作用域的逻辑。

新增：

- obligation set 哈希校验。
- SourceRequirements 与 obligation set 的一一对应校验。
- expression/join/result_filter 覆盖证明。
- 所有 ConfirmedDecision key 均进入 policy coverage。
- coverage 不完整时禁止构造 SemanticContract。

## 10. 提示词、错误协议与 UI

修改：

- `app/agent/prompts.py`
- `app/routes/chat.py`
- `app/templates/index.html`
- `app/static/style.css`
- `test_chat_assumption_payload.py`
- `test_semantic_design_graph_v3.py`

提示词只负责：

- ResultContract：政策 effect 分类。
- FactBlueprint：政策作用目标选择。
- SourceRequirements：填写冻结义务的物理无关实现需求。
- ExpressionDesign：填写冻结事实值表达式。

错误展示必须包含：

- policy key 和已确认值。
- effect。
- obligation kind。
- 目标事实、事实值或 join。
- 缺失或冲突证据。
- 系统已停止的阶段。
- 用户应补充的业务信息。

不得只显示：

```text
FACT_FILTER_POLICY_MISMATCH
```

用户界面示例：

```text
业务政策：comparison_scope
作用方式：来源范围过滤
目标事实：sales_invoice_fact
当前问题：该义务没有可绑定的来源字段实现
系统动作：已停止，未进入表达式设计和 Schema
```

## 11. 测试实施顺序

### 11.1 先写失败测试

1. 强类型 BusinessPolicySpec。
2. effect 与 PolicyBinding 不匹配。
3. policy 未绑定。
4. obligation 编译确定性。
5. 动态 policy_filters 全部 required。
6. 漏槽位结构校验失败。
7. 修改冻结目标失败。
8. checkpoint 级联失效。
9. coverage 不完整禁止 SemanticContract。

### 11.2 针对最新真实错误的通用回归

构造：

```text
comparison_scope:
  effect=source_population
  binding=fact_filter(sales_invoice_fact)

unmatched_records_handling:
  effect=matching
  binding=join(join_sales_journal, include_unmatched)
```

验证：

- SourceRequirements 只需要销售事实的 comparison_scope 槽位。
- 不要求凭证事实伪造同名来源过滤。
- unmatched policy 不出现在 SourceRequirements。
- full join 覆盖 unmatched policy。

再构造日期政策同时作用两个事实，验证动态 Schema 生成两个不同必填槽位。

### 11.3 完整离线回归

```powershell
.venv\Scripts\python.exe -m pytest -q `
  --basetemp D:\ai_projects\sp_generator\.pytest_tmp_policy_obligations
```

要求：

- 新增测试全部通过。
- 原有通过数不得减少。
- `git diff --check` 无错误。

## 12. 真实数据库 E2E

仅在离线回归通过后运行，继续使用 guarded 脚本和测试数据库 Snapshot 保护。

场景：

1. 销售收入统计与财务凭证比对。
2. 应收发票明细查询。
3. 多事实汇总对账。
4. 仅输出差异记录。
5. 包含单边未匹配记录。

每个场景连续通过三次，必须真实完成：

```text
ResultContract
→ FactBlueprint
→ SemanticObligationSet
→ SourceRequirements
→ ExpressionDesign
→ SemanticContract
→ SchemaBinding
→ Reference Facts
→ Procedure
→ SQL Server 编译
→ 测试执行
→ 结果比较
→ validated
```

每次记录：

- 每阶段修复次数。
- obligation coverage。
- Schema revision 次数。
- Reference / Procedure 编译证据。
- Validation 结果。
- Snapshot 原值、测试值、恢复值。

## 13. 停止条件

出现以下任一情况必须停止并报告，不继续补丁：

- 同一 obligation 根因修复后三次连续复发。
- 动态 Schema 仍允许遗漏 required 槽位。
- 需要根据 SAP 表名或字段名决定业务政策作用域。
- 需要自动修改用户确认的业务口径才能通过。
- Reference 必须读取 SP SQL 才能完成校验。
- 需要关闭确定性校验或伪造验证结果。
- Snapshot 无法恢复。

## 14. 完成定义

只有同时满足以下条件才能宣布本计划完成：

- 旧政策字段和旧消费路径已删除。
- 强类型政策、绑定、义务集均持久化并可审计。
- 动态 SourceRequirements Schema 确实包含全部必填义务。
- 编译器生成完整 coverage，且无法强行变绿。
- 错误信息能够指出具体政策、作用域、目标和系统动作。
- 完整离线回归通过。
- 五类真实 E2E 各连续三次成功。
- Schema 到最终校验全部正确通过。
