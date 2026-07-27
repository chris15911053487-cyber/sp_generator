# 语义计算蓝图与输入义务编译器根因重构实施计划

日期：2026-07-26  
状态：待实施  
适用范围：V3 查询型、统计型、对账型 SQL Server 存储过程 Agent  
当前离线基线：`206 passed, 8 skipped`  
兼容策略：所有历史会话均为测试数据；不兼容旧 checkpoint、旧 LLM
`ExpressionDesign` 和旧自由字段列表，不编写迁移分支

## 1. 执行结论

当前链路已经能够冻结政策、事实目标和来源过滤，但计算仍然采用错误顺序：

```text
FactBlueprint
→ SourceRequirements
→ LLM ExpressionDesign
```

这允许 SourceRequirements 在公式尚未冻结时同时声明基础输入和派生结果，例如：

```text
on_hand_quantity
unit_cost
business_amount
```

随后 ExpressionDesign 可以直接使用 `business_amount`，绕过用户确认的
`on_hand_quantity * unit_cost`，并把两个基础字段留成未消费字段。

本次不得继续通过提示词要求模型“记得使用基础字段”。必须将链路改为：

```text
ResultContract
→ FactBlueprint
→ ComputationBlueprint
→ SemanticObligationSet
→ SemanticInputContractCompiler
→ SemanticInputObligationSet
→ 动态 SourceRequirements Schema
→ SourceRequirements
→ 程序物化 ExpressionDesign
→ SemanticCompiler
→ Schema
→ Reference
→ SP
→ Validation
```

业务公式必须先于来源字段冻结。Source 阶段只能实现已冻结的业务输入，不能创建
未声明的派生值。

## 2. 成功标准

实施完成必须同时满足：

- 每个事实维度、事实指标、结果输出和结果过滤都有冻结的计算定义。
- 每个计算定义明确声明业务输入、输入类型和结构化公式。
- 业务输入 ID、所属事实、目标值和类型由程序冻结，LLM 不能修改。
- SourceRequirements 只能逐项实现冻结输入，不能增加派生业务值。
- 旧 LLM `ExpressionDesign` 节点和提示词被删除。
- canonical `ExpressionDesign` 仅由程序从计算蓝图和来源输入物化。
- 参数不能进入不允许参数的事实表达式上下文。
- 每个来源字段必须能追溯到计算、过滤、关联或分组义务。
- 每个计算政策必须证明目标、公式、输入和来源实现全部一致。
- 任一输入或计算义务未覆盖时，Schema、Reference 和 SP 均不得运行。
- 错误信息包含政策、目标、公式、缺失或冗余输入及停止阶段。
- 完整离线回归不少于当前基线。
- 库存余额、销售收入与凭证、应收发票明细真实库 E2E 正确通过或停在真实用户选择。

## 3. 明确不做

- 不按 `business_amount`、`journal_amount` 等具体名字编写规则。
- 不根据 SAP 表名、列名推断公式。
- 不使用文本正则猜测“数量 × 成本”。
- 不自动删除未消费字段来强行通过。
- 不允许 Source 阶段声明未在输入义务中的额外字段。
- 不保留 LLM 直接生成完整 `ExpressionDesign` 的兼容路径。
- 不放宽 `SOURCE_FIELD_UNUSED`、类型、结果结构或业务比较校验。
- Reference SQL 不读取、复制或解析 SP SQL。

## 4. 合同设计

### 4.1 新增 ComputationBlueprint

新增：

- `app/contracts/computation_blueprint.py`
- `test_computation_blueprint_v3.py`

核心类型：

```python
class ComputationInputSpec(StrictContract):
    symbol: Symbol
    meaning: str
    logical_type: LogicalType
    nullable: bool
    parameter_symbol: Symbol | None = None


class ComputationInputExpression(StrictContract):
    kind: Literal["input"]
    symbol: Symbol


class ComputationFactValueExpression(StrictContract):
    kind: Literal["fact_value"]
    fact_symbol: Symbol
    value_symbol: Symbol


class ComputationOutputExpression(StrictContract):
    kind: Literal["output"]
    symbol: Symbol
```

表达式继续使用判别式 AST，分成严格上下文：

- `FactComputationExpression`：只允许 `input`、literal、合法一元/二元运算、函数和 case。
- `ResultComputationExpression`：只允许 `fact_value`、output、parameter、literal 及结构化运算。
- `FilterComputationExpression`：只允许结果输出、参数、literal 和布尔运算。

禁止用一个包含所有 kind 的 `SymbolExpression` 同时服务三个上下文。

事实计算：

```python
class FactValueComputation(StrictContract):
    fact_symbol: Symbol
    value_symbol: Symbol
    inputs: list[ComputationInputSpec]
    expression: FactComputationExpression | None
    aggregation: Aggregation
    logical_type: LogicalType
```

规则：

- `count_rows` 不允许 inputs 和 expression。
- 其他指标和维度必须声明 expression。
- expression 只能引用本计算声明的 inputs。
- 每个 input 必须在 expression 中至少使用一次。
- 不允许未声明 input。
- 聚合只由目标事实值声明一次。

结果计算：

```python
class ResultValueComputation(StrictContract):
    output_symbol: Symbol
    expression: ResultComputationExpression


class ComputationBlueprint(StrictContract):
    version: Literal[1]
    result_contract_hash: str
    fact_blueprint_hash: str
    fact_values: list[FactValueComputation]
    results: list[ResultValueComputation]
    result_filter: FilterComputationExpression | None
```

确定性校验：

- 每个 FactBlueprint 维度和指标恰好有一个计算定义。
- 每个 ResultContract output 恰好有一个结果计算。
- `exception_rows` 必须有 boolean result_filter。
- 其他结果模式不得声明 result_filter。
- 事实目标、结果目标、类型和 aggregation 不得修改上游合同。
- 所有事实、值、输出、参数引用必须存在。
- 结果输出依赖图不得循环。

### 4.2 FactBlueprint 清理

修改：

- `app/contracts/semantic_design.py`
- `app/services/fact_policy_schema.py`

FactBlueprint 只负责：

- 事实及业务含义；
- 事实粒度；
- 维度和指标目标；
- 事实关联；
- 政策目标绑定；
- 最终输出归属分类。

FactBlueprint 不再描述来源字段，也不描述具体公式。

保留并强化：

- `FactMeasureNeed.result_output_symbol`
- `FactBlueprint.derived_output_symbols`

要求每个结果输出恰好归属一次，ComputationBlueprint 必须与该归属一致。

### 4.3 计算蓝图动态响应 Schema

新增：

- `app/services/computation_blueprint_schema.py`

程序根据 ResultContract 和 FactBlueprint 创建动态必填槽位：

```text
fact_value_<fact>_<value>
result_<output>
result_filter
```

LLM 只填写每个槽位的业务输入和公式，不能填写目标 fact/value/output ID。

物化时由程序写入：

- fact_symbol
- value_symbol
- output_symbol
- aggregation
- logical_type
- 上游 hash

这样模型无法遗漏目标、增加目标或把公式写到错误事实值。

## 5. SemanticInputContractCompiler

新增：

- `app/contracts/semantic_input_obligations.py`
- `app/services/semantic_input_compiler.py`
- `test_semantic_input_compiler_v3.py`

输入义务：

```python
class SemanticInputObligation(StrictContract):
    obligation_id: str
    slot_name: Symbol
    fact_symbol: Symbol
    value_symbol: Symbol
    input_symbol: Symbol
    meaning: str
    logical_type: LogicalType
    nullable: bool
    usage_paths: list[str]


class SemanticInputObligationSet(StrictContract):
    version: Literal[1]
    result_contract_hash: str
    fact_blueprint_hash: str
    computation_blueprint_hash: str
    inputs: list[SemanticInputObligation]
```

义务 ID：

```text
sha256(
  result_hash
  + fact_hash
  + computation_hash
  + fact_symbol
  + value_symbol
  + input_symbol
)
```

要求：

- 相同输入产生完全一致的 ID 和 slot。
- 同一事实内可复用同一个业务输入，但类型和含义必须一致。
- 跨事实同名输入不自动合并。
- 参数输入不生成普通来源字段义务。
- usage_paths 必须覆盖输入出现的所有公式路径。

## 6. SourceRequirements 重构

修改：

- `app/contracts/source_requirements_draft.py`
- `app/services/source_obligation_schema.py`
- `app/contracts/semantic_design.py`
- `app/agent/prompts.py`

删除 LLM 可自由填写的：

```python
fields: list[SourceFieldRequirement]
```

新的 LLM 草稿：

```python
class SourceInputImplementation(StrictContract):
    entity_symbol: Symbol
    meaning: str
    nullable: bool


class SourceRequirementsDraft(StrictContract):
    entities: list[EntityRequirement]
    required_inputs: DynamicRequiredInputSlots
    ordinary_filters: list[OrdinaryFilterRequirement]
    policy_filters: DynamicRequiredPolicyFilters
```

动态 Schema 必须包含：

- 每个 SemanticInputObligation 的必填输入槽位；
- 每个 fact_filter 政策义务的必填过滤槽位；
- 不允许额外输入槽位；
- 不把 input ID、fact/value target 或 policy key 交给 LLM 填写。

确定性物化：

- input obligation 写入稳定 source field symbol；
- input obligation 写入 logical_type；
- policy obligation 写入 policy_key 和 fact_symbols；
- LLM 只能选择业务实体归属并补充物理无关含义；
- 输出 canonical `SourceRequirements`。

编译器拒绝：

- 缺失输入；
- 多余输入；
- 输入类型变化；
- 输入归属到未声明实体；
- 普通过滤伪装成政策过滤；
- 来源字段没有义务所有者。

## 7. 删除 LLM ExpressionDesign

删除或停止使用：

- `app/agent/nodes.py::expression_design_node`
- `EXPRESSION_DESIGN_PROMPT`
- LLM-facing `ExpressionDesign` JSON Schema

保留 canonical `ExpressionDesign` 仅作为内部编译产物。

新增：

- `app/services/expression_materializer.py`
- `test_expression_materializer_v3.py`

输入：

```text
ComputationBlueprint
+ SemanticInputObligationSet
+ SourceRequirements
```

输出：

```text
canonical ExpressionDesign
```

物化规则：

- `input` 节点确定性转换为 source symbol。
- `fact_value`、output、parameter 引用保持冻结目标。
- aggregation 和 logical_type 来自 FactBlueprint。
- 结果过滤来自 ComputationBlueprint。
- 不调用 LLM。

## 8. 政策义务覆盖升级

修改：

- `app/services/semantic_obligation_compiler.py`
- `app/services/semantic_compiler_v3.py`
- `app/contracts/semantic_obligations.py`

`fact_expression` 政策覆盖不能再以“目标表达式存在”为通过条件。

新的覆盖证明必须包含：

```python
{
    "policy_key": "...",
    "policy_value": "...",
    "effect": "calculation",
    "target": "fact.value",
    "computation_hash": "...",
    "formula": {...},
    "required_inputs": [...],
    "implemented_inputs": [...],
    "status": "covered",
}
```

通过条件：

- policy binding 目标与 computation target 一致；
- computation hash 未变化；
- 每个公式输入均有冻结输入义务；
- 每个输入义务均有且只有一个 Source 实现；
- materialized expression 与 computation expression 等价；
- 类型推导与目标类型一致；
- 没有额外来源字段或未消费输入。

## 9. Agent 图与 checkpoint

修改：

- `app/agent/graph.py`
- `app/agent/nodes.py`
- `app/contracts/semantic_design_state.py`
- `app/services/semantic_design_checkpoints.py`
- `app/db/sqlite.py`

新主链：

```text
result_contract
→ fact_blueprint
→ computation_blueprint
→ semantic_obligations
→ semantic_inputs
→ source_requirements
→ expression_materialize
→ semantic_compile
```

checkpoint 版本提升为 3。

新增字段：

```python
computation_blueprint: ComputationBlueprint | None
semantic_inputs: SemanticInputObligationSet | None
```

`expression_design` 保留为程序物化产物，不再允许 LLM 修复计数。

级联失效：

- Result 变化：其后全部失效。
- Fact 变化：Computation 及其后全部失效。
- Computation 变化：Policy/Input obligation、Source、Expression、Compile 全失效。
- Policy/Input obligation 变化：Source、Expression、Compile 全失效。
- Source 变化：Expression、Compile 失效。

任何确定性编译节点失败后必须立即停止，不运行下游。

## 10. 错误协议和 UI

修改：

- `app/agent/nodes.py`
- `app/routes/chat.py`
- `app/templates/index.html`
- `app/static/style.css`

新增稳定错误码：

- `COMPUTATION_TARGET_MISSING`
- `COMPUTATION_TARGET_DUPLICATE`
- `COMPUTATION_TARGET_CHANGED`
- `COMPUTATION_INPUT_UNKNOWN`
- `COMPUTATION_INPUT_MISSING`
- `COMPUTATION_INPUT_EXTRA`
- `COMPUTATION_INPUT_UNUSED`
- `COMPUTATION_INPUT_TYPE_MISMATCH`
- `COMPUTATION_RESULT_TYPE_MISMATCH`
- `COMPUTATION_DEPENDENCY_CYCLE`
- `PARAMETER_CONTEXT_INVALID`
- `SOURCE_INPUT_IMPLEMENTATION_MISSING`
- `SOURCE_INPUT_IMPLEMENTATION_EXTRA`
- `SOURCE_INPUT_OWNER_UNKNOWN`
- `POLICY_COMPUTATION_NOT_COVERED`

错误 evidence 必须包含：

- policy key/value/effect；
- fact/value 或 result output；
- 冻结公式；
- required inputs；
- actual inputs；
- 缺失、额外或未消费输入；
- 失败阶段；
- 已阻止的下游阶段。

本阶段重试必须收到完整 `code + evidence`，不能只收到概括消息。

## 11. 测试实施顺序

### 11.1 合同失败测试

先编写：

1. 事实计算目标遗漏。
2. 同一事实值重复计算。
3. 公式引用未声明输入。
4. 声明输入未被公式消费。
5. input 类型与操作符不兼容。
6. 参数进入不允许的事实表达式。
7. 结果输出遗漏或重复。
8. 输出依赖循环。

### 11.2 输入义务测试

覆盖：

1. 输入 ID 和 slot 确定性。
2. 同事实输入复用。
3. 跨事实同名输入隔离。
4. 动态 Source Schema 所有输入均 required。
5. LLM 无法修改 input ID、类型和目标。
6. 额外派生来源字段被拒绝。

### 11.3 公式案例

至少覆盖：

- 库存金额：`quantity * unit_cost`
- 凭证净额：`credit - debit`
- 不含税收入：`gross_amount - tax_amount`
- 汇率换算：`foreign_amount * exchange_rate`
- 差异金额：`business_amount - financial_amount`
- 容差过滤：`ABS(difference) > tolerance`
- 日期过滤参数不能混入事实值公式

### 11.4 图与 checkpoint

覆盖：

- 新阶段顺序；
- 每一级变化的下游失效；
- computation 失败不进入 input/source；
- source 失败不物化 expression；
- expression_materialize 不调用 LLM；
- 旧 version=2 checkpoint 直接失效。

### 11.5 完整离线回归

```powershell
.venv\Scripts\python.exe -m pytest -q `
  --basetemp D:\ai_projects\sp_generator\.pytest_tmp_computation_inputs

.venv\Scripts\python.exe -m compileall -q app

git diff --check
```

要求：

- 新测试全部通过；
- 当前 `206 passed` 基线不得减少；
- 8 个真实环境测试可以继续按授权条件 skip；
- 全仓无旧 LLM ExpressionDesign 调用；
- 全仓无自由来源 fields 生成路径。

## 12. 真实数据库 E2E

离线回归通过后再运行。

### 场景一：公司整体库存金额对比

冻结口径：

```text
业务端：SUM(截止日期在手数量 × 单位成本)
财务端：SUM(截止日期库存评估金额)
粒度：公司整体
容差：0
```

验收：

- Source 不得声明派生 business_amount。
- quantity 和 unit_cost 必须全部被业务金额公式消费。
- 财务评估金额使用独立来源事实。
- SP 与独立 Reference 结果一致。

### 场景二：销售收入与凭证

验证：

```text
销售净收入公式
凭证贷方减借方公式
差异与容差公式
```

### 场景三：应收发票明细

验证无计算或简单 identity 计算不会被过度复杂化。

### 场景四：汇率换算

验证同一金额输入和汇率输入被准确消费。

### 场景五：单边未匹配记录

验证 join、result_filter 和计算义务同时覆盖。

每类场景连续通过三次，并记录：

- computation hash；
- input obligation coverage；
- Source 实现；
- Schema revision；
- Reference/SP compile evidence；
- comparison evidence；
- Snapshot 原值、测试值和恢复值。

## 13. 停止条件

出现以下任一情况必须停止并报告，不继续打补丁：

- 仍需根据字段名猜测公式。
- Source 仍可添加没有输入义务的字段。
- LLM 仍能修改 fact/value/output target。
- 计算输入已经冻结但表达式可以绕过它。
- 需要关闭未消费字段校验才能通过。
- Reference 必须读取 SP SQL 才能构建。
- 同一根因在修复后连续三次复发。
- 测试数据库 Snapshot 无法恢复。

## 14. 完成定义

只有同时满足以下条件才可标记 Plan 完成：

- ComputationBlueprint、输入义务和动态 Source Schema 已持久化且可审计。
- 业务公式在来源字段之前冻结。
- LLM ExpressionDesign 路径已删除。
- canonical ExpressionDesign 由程序确定性生成。
- 政策覆盖能够证明目标、公式、输入和来源实现。
- Schema 到最终业务比较全部通过。
- 完整离线回归通过。
- 五类真实 E2E 各连续三次成功。
- 代码审查确认没有旧路径、兼容分支或场景专用规则。
