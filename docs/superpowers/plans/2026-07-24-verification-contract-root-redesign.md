# 校验契约根因重构方案

日期：2026-07-24

## 1. 问题定义

最新会话暴露的“Oracle SQL 返回 `cnt`，契约却要求返回业务输出列”不是单条
SQL 的偶发错误，而是当前系统存在三套互不一致的校验语义：

1. QuerySpec 只声明 `mode + required_columns`。
2. Oracle 生成层把 `required_columns` 无条件注入为 `compare_columns`。
3. 运行时比较器实际还需要 `actual`、`key_columns`、`column_mapping`、
   `tolerance`、`snapshot_sql` 等模式专属信息。

这会导致同一字段被同时解释为：

- 规则依赖的 SP 输出列；
- Actual 与 Expected 的比较列；
- Oracle SQL 必须返回的列。

三者并不总是相同。例如：

- `zero_rows` 的 Oracle 应返回异常明细，甚至可以只返回主键；它不需要返回
  SP 的全部业务输出列。
- 明细 SP 与 `COUNT/SUM` Oracle 对账时，应使用 `aggregate`，Oracle 返回的是
  指标列，不是被聚合的 SP 输出列。
- `scalar` 只适用于 SP 和 Oracle 都恰好返回一行的场景，不能用于任意明细 SP。

此外，当前 `VerificationRuleSpec.mode` 不允许 `aggregate`，而运行时比较器和旧
生成提示又支持 `aggregate`。这意味着“明细结果与独立汇总对账”在设计契约中
无法被完整表达。

因此，继续给 `oracle_output` 增加例外、针对 `cnt` 改别名，或者增强修复提示，
都只能修当前个例，换一个存储过程仍会以其他形式失败。

## 2. 根因

### 2.1 QuerySpec 不是完整的可执行校验契约

QuerySpec 记录了校验名称、模式和一个通用列列表，但没有记录：

- SP 结果是一行还是多行；
- Actual 侧如何取值或聚合；
- Oracle 侧应返回什么结构；
- 多行结果按键、按多重集合还是按聚合值比较；
- 哪些规则是直接对账，哪些只是补充不变量；
- 各模式允许和必须出现哪些字段。

因此后续层只能猜测。

### 2.2 通用字段掩盖了模式差异

`required_columns` 和通用 `validation_spec` 让不同模式看起来具有相同结构，
但实际每种模式是不同协议。无条件执行：

```text
required_columns -> compare_columns -> Oracle 必须输出这些列
```

在 `scalar` 的同名列对账中可能碰巧成立，在 `aggregate`、`zero_rows` 和
`change_set` 中并不成立。

### 2.3 契约字段存在多个事实源

当前数据同时存在于：

- QuerySpec 的 `verification_rules`；
- `verify_queries.compare_columns` 字符串；
- `verify_queries.validation_spec` JSON；
- 模型生成或修复返回值；
- 运行时根据缺失字段推断出的默认值。

同一规则在不同阶段可以被解释成不同模式。模型修复又被禁止修改 QuerySpec，
遇到设计契约本身不完整时只能反复重写 SQL，无法真正修复。

### 2.4 设计错误发现得太晚

模式适用性直到 Oracle SQL 已生成、已编译后才间接暴露。比如明细 SP 配
`scalar`，本应在用户确认设计前失败，却在 contract gate 或 business gate
才失败，浪费生成和修复轮次。

### 2.5 “直接对账”和“补充断言”没有类型隔离

`zero_rows` 只检查数据库中是否存在异常记录，不能证明 SP 实现与独立 Oracle
一致。当前结构允许它与直接结果对账混在同一个模式列表中，容易生成“结果非空”
这类数据依赖、且不能验证实现正确性的规则。

## 3. 目标与非目标

### 3.1 目标

- 任意存储过程都先形成完整、可确定性校验的验证契约，再生成 SQL。
- 每种比较模式有独立结构，不再共享含义模糊的 `required_columns`。
- QuerySpec 是业务事实源，编译后的 VerificationPlan 是唯一运行时事实源。
- Oracle 模型只生成 SQL 实现，不决定比较协议。
- 契约编译、SQL 元数据检查和业务比较使用同一份列映射与结果形状。
- 设计契约错误不进入 SQL 自动修复循环。
- 查询型 SP 至少有一条直接对账规则；补充断言不能替代直接对账。
- 写入型 SP 的每个写目标都有对应的变化集对账。
- 旧会话可读、不可因兼容转换而错误获得部署资格。

### 3.2 非目标

- 不针对表名、列名、SAP B1 或会话编号写特判。
- 不通过放宽 contract gate 让错误候选通过。
- 不让模型自行决定安全边界、写入范围或最大影响行数。
- 不在默认单元测试中访问真实 LLM 或业务数据库。
- 不要求本次一次性迁移并重写全部历史校验 SQL。

## 4. 总体架构

新链路固定为：

```text
已确认业务决策
  -> QuerySpec V2
  -> Design Contract Validator
  -> Verification Contract Compiler
  -> VerificationPlan
  -> SP SQL / Oracle SQL 独立生成
  -> SQL 编译与结果元数据检查
  -> Contract Gate
  -> Business Comparator
  -> 部署资格
```

职责边界：

- QuerySpec V2：描述业务结果和校验意图。
- Design Contract Validator：检查引用、模式适用性和覆盖强度。
- Verification Contract Compiler：确定性生成可执行比较协议。
- 模型：只实现 SP SQL 和 Oracle SQL。
- SQL 编译器：返回真实参数和结果集元数据。
- Contract Gate：比较 SQL 制品与 VerificationPlan，不重新解释 QuerySpec。
- Business Comparator：只按 VerificationPlan 执行，不推断默认模式。

## 5. QuerySpec V2

### 5.1 显式结果形状

每个 ProcedureSpec 增加：

```json
{
  "result_contract": {
    "cardinality": "one | many",
    "allow_empty": true,
    "key_columns": ["发票号"]
  }
}
```

规则：

- `cardinality=one` 才允许 `scalar_equal`。
- `cardinality=many` 可使用 `keyed_rows_equal`、`multiset_rows_equal` 或
  `aggregate_equal`。
- `key_columns` 只能引用输出列；无法提供稳定业务键时为空，并使用多重集合对账。
- `allow_empty` 是结果形状，不等于“必须返回数据”。不得默认生成非空断言。
- 写入型过程的结果集不是主要验证对象时，可明确声明无结果集。

### 5.2 使用带判别字段的规则联合类型

废弃一个通用 `VerificationRuleSpec` 承载所有模式。改为以下联合类型。

#### scalar_equal

用于 Actual 和 Expected 都恰好返回一行：

```json
{
  "name": "汇总金额对账",
  "kind": "scalar_equal",
  "role": "direct",
  "columns": [
    {
      "actual": "总金额",
      "expected": "总金额",
      "tolerance": 0.01
    }
  ],
  "description": "核对单行汇总结果"
}
```

#### aggregate_equal

用于多行 SP 结果与 Oracle 单行指标对账：

```json
{
  "name": "明细总额对账",
  "kind": "aggregate_equal",
  "role": "direct",
  "metrics": [
    {
      "operation": "sum",
      "actual_column": "未收金额",
      "expected_column": "未收金额合计",
      "tolerance": 0.01
    },
    {
      "operation": "count_rows",
      "actual_column": null,
      "expected_column": "明细行数",
      "tolerance": 0
    }
  ],
  "description": "独立核对明细行数和未收金额合计"
}
```

允许的聚合操作固定为：

- `sum`
- `count_rows`
- `count_distinct`
- `min`
- `max`
- `avg`

除 `count_rows` 外必须声明 `actual_column`。

#### keyed_rows_equal

用于具有稳定输出业务键的明细结果全量对账：

```json
{
  "name": "发票明细逐行对账",
  "kind": "keyed_rows_equal",
  "role": "direct",
  "key_columns": ["发票号"],
  "compare_columns": [
    "客户代码",
    "客户名称",
    "发票日期",
    "到期日",
    "总金额",
    "已收金额",
    "未收金额",
    "币种",
    "状态"
  ],
  "tolerance": {
    "总金额": 0.01,
    "已收金额": 0.01,
    "未收金额": 0.01
  },
  "description": "按发票号逐行核对全部业务输出"
}
```

Oracle 必须使用 SP 输出别名返回相同列。新契约不再允许模型自由发明
`column_mapping`；物理字段到业务输出名的映射由 QuerySpec.outputs 决定。

#### multiset_rows_equal

用于没有稳定业务键、但需要完整行集对账的场景：

```json
{
  "name": "明细多重集合对账",
  "kind": "multiset_rows_equal",
  "role": "direct",
  "compare_columns": ["类别", "金额"],
  "tolerance": {"金额": 0.01},
  "description": "保留重复行次数进行无序结果对账"
}
```

比较器必须按“规范化行值 + 重复次数”比较，不能用普通集合丢失重复行。

#### invariant_zero_rows

用于补充业务不变量，Oracle 返回违反规则的异常行：

```json
{
  "name": "未收金额非负",
  "kind": "invariant_zero_rows",
  "role": "supplemental",
  "evidence_columns": ["发票号", "未收金额"],
  "description": "返回未收金额小于零的异常发票"
}
```

规则：

- 它不比较 SP Actual 结果。
- Oracle 返回零行即通过。
- `evidence_columns` 是异常详情结构，不是 `compare_columns`。
- `COUNT(*)` 不符合该协议；Oracle 应返回异常行。
- 它不能满足查询型过程的直接对账覆盖要求。

#### change_set_equal

用于写入型过程：

```json
{
  "name": "发票状态变更对账",
  "kind": "change_set_equal",
  "role": "direct",
  "target": {
    "table": "dbo.OINV",
    "operation": "update",
    "key_columns": ["DocEntry"],
    "compare_columns": ["DocStatus"],
    "max_affected_rows": 100
  },
  "description": "核对事务内实际变化集与独立预期变化集"
}
```

目标表、操作、键、比较列和最大行数必须由 QuerySpec.writes 确定性派生。

### 5.3 删除含义模糊的字段

QuerySpec V2 不再使用：

- `required_columns`
- 通用 `mode`
- 通用 `compare_columns`

这些字段由各规则类型的专属字段替代。`required` 也不再由模型生成：

- `role=direct` 默认必需；
- `role=supplemental` 是否阻断部署由显式策略决定，首期统一阻断，避免静默忽略。

## 6. Design Contract Validator

设计契约在用户确认前执行，不依赖 SQL 文本。

### 6.1 通用检查

- 规则名称唯一。
- 所有输出、键和比较列引用必须存在。
- 查询型过程至少有一条 `role=direct` 规则。
- `invariant_zero_rows` 不能作为唯一验证规则。
- 写入型过程的每个 `writes` 目标恰好对应一条 `change_set_equal`。
- reporting 过程不得声明 change set；controlled_write 不得缺少 change set。

### 6.2 模式适用性

| 规则 | one | many | 无结果集 |
|---|---:|---:|---:|
| scalar_equal | 允许 | 禁止 | 禁止 |
| aggregate_equal | 可选 | 允许 | 禁止 |
| keyed_rows_equal | 禁止 | 允许 | 禁止 |
| multiset_rows_equal | 禁止 | 允许 | 禁止 |
| invariant_zero_rows | 补充 | 补充 | 补充 |
| change_set_equal | 禁止 | 禁止 | 写入型允许 |

### 6.3 语义拒绝

以下情况直接返回 `design_contract_invalid`，不得进入 SQL 生成：

- 明细结果使用 `scalar_equal`。
- `aggregate_equal` 缺少指标或引用不存在的 Actual 输出。
- `keyed_rows_equal` 的键不唯一或未出现在输出中。
- `invariant_zero_rows` 被描述为“结果必须非空”。
- Oracle 输出结构无法从规则确定性推导。
- 规则只能检查源数据，却被标记为直接对账。

若修正会改变用户确认的业务口径，则重新展示设计并要求确认；若只是模式的
确定性规范化且不改变业务含义，可自动重编译并记录诊断。

## 7. Verification Contract Compiler

### 7.1 编译产物

QuerySpec V2 经确定性编译后生成 VerificationPlan：

```json
{
  "version": 2,
  "procedure": "GetARInvoiceDetail",
  "result_contract": {
    "cardinality": "many",
    "columns": [
      {"name": "发票号", "type_family": "INT"},
      {"name": "未收金额", "type_family": "DECIMAL"}
    ]
  },
  "rules": [
    {
      "name": "发票明细逐行对账",
      "kind": "keyed_rows_equal",
      "role": "direct",
      "actual_schema": ["发票号", "未收金额"],
      "expected_schema": ["发票号", "未收金额"],
      "comparator": {
        "key_columns": ["发票号"],
        "compare_columns": ["未收金额"],
        "tolerance": {"未收金额": 0.01}
      }
    }
  ]
}
```

VerificationPlan 必须具备：

- 版本号；
- QuerySpec 哈希；
- ProcedureSpec 哈希；
- 每条规则的 Actual 形状；
- 每条规则的 Expected 形状；
- 比较器配置；
- 是否直接对账；
- 是否阻断部署。

它是 contract gate 和 business comparator 的唯一输入，不再从
`verify_queries` 的字符串字段反推协议。

### 7.2 OracleSqlTask

每条规则向 Oracle 模型提供一个确定性任务：

```json
{
  "name": "发票明细逐行对账",
  "kind": "keyed_rows_equal",
  "expected_output": [
    {"name": "发票号", "type_family": "INT"},
    {"name": "未收金额", "type_family": "DECIMAL"}
  ],
  "business_description": "...",
  "allowed_sources": ["dbo.OINV", "dbo.OCRD"],
  "parameters": ["@FromDate", "@ToDate", "@CardCode", "@DocNum"]
}
```

模型只返回：

```json
{"name": "发票明细逐行对账", "sql_code": "SELECT ..."}
```

不得返回或覆盖 kind、比较列、容差、写入范围等契约字段。

## 8. Contract Gate

Contract Gate 改为按 VerificationPlan 分模式检查。

### 8.1 SP 检查

- 过程名、参数、参数使用、对象范围符合 ProcedureSpec。
- SQL Server 描述的输出列、顺序和类型族符合 result_contract。
- 声明 `cardinality=one` 时，设计必须具有确定性单行语义；首期通过聚合无
  GROUP BY、明确单行投影等保守规则检查，无法证明则要求人工修正设计。

### 8.2 Oracle 检查

- 规则集合与 VerificationPlan 一一对应。
- 参数和对象引用在允许范围内。
- SQL Server 编译结果列名、顺序和类型族与 `expected_schema` 一致。
- 不再执行“所有规则的 Oracle 都必须输出 required_columns”。

模式专属检查：

- scalar：Oracle 必须返回一行形状；列与 expected_schema 一致。
- aggregate：Oracle 返回一行、每个指标列恰好一次。
- keyed rows：键和比较列全部存在。
- multiset rows：比较列全部存在，不要求键。
- zero rows：输出 evidence_columns；禁止聚合成单行计数。
- change set：返回固定的 ChangeType、键、Before/After 列。

### 8.3 错误分类

- `design_contract_error`：VerificationPlan 无法形成，回到设计阶段，不调用
  SQL 修复模型。
- `sql_contract_error`：计划合法但 SQL 输出不符，可定向修复对应制品。
- `compile_error`：SQL Server 无法编译，可定向修复 SQL。
- `business_mismatch`：两边可执行但结果不同，不得默认认定任一方正确。
- `needs_review`：无法确定 SP 或 Oracle 哪一侧违反业务语义。

## 9. Business Comparator

运行时删除缺省模式推断，必须接收 VerificationPlan。

### 9.1 比较算法

- scalar：一行对一行，按显式列对和容差比较。
- aggregate：在内存中从 SP Actual 计算指标，再与 Oracle 单行指标比较。
- keyed rows：按业务键建索引，检查重复键、缺失、多余和字段差异。
- multiset rows：对规范化行做计数，保留重复次数。
- zero rows：只执行 Oracle，零异常行通过；不读取 Actual。
- change set：事务内 before/after 快照生成实际变化集，与 Expected Change Set
  比较，最终回滚并验证恢复。

### 9.2 执行限制

- 明细结果和 Oracle 结果继续受最大行数、超时和内存上限约束。
- 超限不得截断后判定通过，应返回 `inconclusive/needs_review`。
- 数值、日期、NULL、字符串排序规则采用统一规范化函数。
- 所有必需规则通过且没有 inconclusive，业务阶段才是 passed。

## 10. 自动修复策略

修复前先判断错误归属：

1. VerificationPlan 编译失败：修 QuerySpec 设计，不消耗 SQL 修复次数。
2. SP SQL 与计划不符：只修 SP。
3. Oracle SQL 与 expected_schema 不符：只修对应 Oracle。
4. 两边编译通过但业务结果不同：能由确定性契约定位时修违规侧，否则
   `needs_review`。

修复提示必须包含：

- 不可变的 VerificationPlan 规则；
- 当前 SQL；
- SQL Server 实际结果元数据；
- 预期结果元数据；
- 精确错误码；
- 最小 SchemaEvidence。

禁止模型修改 QuerySpec、VerificationPlan、规则名称、比较模式、输出契约和
写入范围。

## 11. 持久化与兼容

### 11.1 新持久化字段

为当前设计或候选持久化：

- `query_spec_version`
- `verification_plan_json`
- `verification_plan_hash`
- `contract_diagnostics_json`

`verify_queries` 新记录只持久化：

- 规则标识；
- Oracle SQL；
- 编译元数据；
- 执行结果。

不再把 `compare_columns` 字符串作为新记录的事实源。

### 11.2 旧数据适配

建立只读 LegacyVerificationAdapter：

- 能无歧义映射的旧 scalar/keyed/change_set 生成 V2 兼容计划。
- 旧 zero_rows 不把 `compare_columns` 当成 Oracle 输出契约。
- 缺少 aggregate.actual、键、Expected 输出等关键信息时标记
  `legacy_contract_incomplete`。
- 不完整旧契约可以展示和编辑，但重新校验前必须重新生成/确认 V2 设计。
- 旧验证结果不能仅凭历史布尔值获得新的部署资格。

兼容逻辑集中在适配器，不散落到比较器和 contract gate。

## 12. 实施任务

### Task 0：锁定当前缺陷

新增不访问数据库的回归测试，固定以下事实：

- QuerySpec 无法表达 aggregate。
- zero_rows + `COUNT(*) AS cnt` 被错误地按业务输出列检查。
- 明细 SP + scalar 能通过设计模型却会在运行时失败。
- 自动修复两轮无法改变不完整设计契约。

### Task 1：新增 QuerySpec V2 类型

涉及：

- `app/services/generation_harness.py`
- `app/agent/prompts.py`

工作：

- 增加 result_contract。
- 将 verification rule 改为带判别字段的联合类型。
- 删除新模型中的 required_columns。
- 增加模式专属字段和交叉校验。
- QuerySpec canonical JSON 纳入版本号。

### Task 2：实现 Design Contract Validator

涉及：

- 新增 `app/services/verification_contract.py`
- `app/agent/nodes.py`

工作：

- 在方案展示前执行模式适用性和覆盖检查。
- 输出结构化设计诊断。
- 区分可自动规范化与需要重新确认的修改。

### Task 3：实现 Verification Contract Compiler

涉及：

- `app/services/verification_contract.py`

工作：

- 将每种 V2 规则编译为统一 VerificationPlan。
- 确定 actual_schema、expected_schema 和 comparator。
- 生成稳定哈希。
- 同一输入必须产生字节级稳定的 canonical JSON。

### Task 4：收窄 Oracle 生成协议

涉及：

- `app/agent/prompts.py`
- `app/agent/nodes.py`

工作：

- 按 OracleSqlTask 逐条生成 SQL。
- 明确给出 Expected 输出列及类型族。
- 模型输出只接收 name 和 sql_code。
- 初次生成与修复共用同一入口。

### Task 5：重写模式化 Contract Gate

涉及：

- `app/services/candidate_pipeline.py`
- `app/db/sqlserver.py`

工作：

- 删除 required_columns 的通用 Oracle 输出检查。
- 按 VerificationPlan.expected_schema 校验编译元数据。
- 增加模式专属错误码。
- 设计错误不得标记为 SQL 可修复。

建议错误码：

- `verification_plan_invalid`
- `verification_direct_rule_missing`
- `verification_mode_incompatible`
- `oracle_result_shape_mismatch`
- `oracle_expected_column_missing`
- `oracle_unexpected_column`
- `zero_rows_must_return_evidence`
- `aggregate_metric_mismatch`
- `row_key_missing`
- `row_key_not_unique`

### Task 6：让运行时只执行 VerificationPlan

涉及：

- `app/services/validation.py`

工作：

- 删除根据 compare_columns 猜 scalar/zero_rows 的新数据路径。
- 增加 multiset rows 比较器。
- zero rows 不再传入 Actual。
- 所有比较器接收已编译配置。
- 超限返回 inconclusive，不得判成功。

### Task 7：修复归因与循环

涉及：

- `app/services/candidate_pipeline.py`
- `app/agent/nodes.py`
- `app/agent/prompts.py`

工作：

- 设计、SP、Oracle、业务不一致分别处理。
- 仅 sql_contract_error 和 compile_error 进入模型修复。
- 每轮修复后从安全、Schema、编译、契约重新执行。
- 记录每轮输入错误码和输出哈希，防止重复无效修复。

### Task 8：持久化和旧会话适配

涉及：

- `app/db/sqlite.py`
- `app/routes/verify.py`
- `app/routes/deploy.py`

工作：

- 保存 VerificationPlan 和哈希。
- 新候选部署资格绑定 QuerySpec、Plan、SP、Oracle 和 Schema 指纹。
- 增加集中式 LegacyVerificationAdapter。
- 旧的不完整契约重新校验时回到设计阶段。

### Task 9：前端诊断

涉及：

- `app/routes/chat.py`
- `app/templates/index.html`

工作：

- 区分“设计契约错误”“SQL 实现违约”“业务结果不一致”。
- 展示规则类型、预期列和实际列。
- 设计错误提供“返回方案修改”，不显示为 SQL 编译错误。

## 13. 测试矩阵

### 13.1 模式单元测试

| SP 形状 | 规则 | 预期 |
|---|---|---|
| 单行汇总 | scalar_equal | 通过 |
| 多行明细 | scalar_equal | 设计阶段拒绝 |
| 多行明细 | aggregate_equal/count_rows | 通过 |
| 多行明细 | aggregate_equal/sum | 通过 |
| 有稳定键明细 | keyed_rows_equal | 通过 |
| 重复键明细 | keyed_rows_equal | 业务失败 |
| 无稳定键明细 | multiset_rows_equal | 通过且保留重复数 |
| 任意查询 | invariant_zero_rows 返回零行 | 补充规则通过 |
| 任意查询 | invariant_zero_rows 返回异常行 | 业务失败 |
| 任意查询 | invariant_zero_rows 使用 COUNT(*) | 契约失败 |
| 写入过程 | change_set_equal | 事务内校验并回滚 |

### 13.2 列协议测试

- SP 与 Oracle 使用相同业务别名。
- Oracle 缺列、多列、错序、错类型。
- aggregate 的 Actual 列与 Expected 指标列不同名。
- 数值容差、NULL、日期和 Unicode 字符串。
- 大小写不同但目标排序规则不区分大小写。
- 物理源列到多个输出名时禁止猜测映射。

### 13.3 通用场景回归

至少覆盖四类互不相关的过程，避免对当前个例过拟合：

1. 单行销售汇总过程：scalar。
2. 应收发票明细过程：keyed rows + aggregate + zero rows。
3. 无自然键的分类明细过程：multiset rows。
4. 测试库中的受控更新过程：change set。

每类用例替换过程名、表名、参数名和输出别名后仍应通过，证明实现没有会话或
业务对象特判。

### 13.4 变异测试

对每个正确候选分别注入：

- 漏过滤条件；
- 错 JOIN；
- 少输出列；
- Oracle 错别名；
- Oracle 错聚合；
- 参数未使用；
- 多余来源表；
- 写入范围扩大。

验证 gate 能在正确阶段拒绝，并且不会通过修改契约来迁就错误 SQL。

### 13.5 兼容测试

- 旧 scalar 可无歧义读取。
- 旧 zero_rows 不再要求 Oracle 返回 SP 输出列。
- 旧不完整 aggregate 被标记为需重新设计。
- 新旧会话同时存在时前端可加载。
- 旧结果不会错误获得 deployment_eligible。

### 13.6 测试执行边界

- 默认执行纯单元测试和本地 SQLite 测试。
- `test_improvements.py` 不混入默认测试。
- SQL Server 集成测试只连接显式隔离测试库。
- `test_e2e.py` 仅在用户明确要求且真实 LLM、SQL Server 配置齐全时运行。

## 14. 实施顺序与提交边界

建议分阶段实施，每一步保持主分支可验证：

1. 缺陷字符化测试。
2. QuerySpec V2 与 Design Contract Validator。
3. Verification Contract Compiler。
4. OracleSqlTask 与生成协议。
5. 模式化 Contract Gate。
6. VerificationPlan 驱动的 Business Comparator。
7. 修复归因。
8. 持久化和旧数据适配。
9. 前端诊断与完整回归。

不要先删除旧字段。先完成 V2 双读、新写 V2，再停止旧格式写入，最后评估移除
旧字段。

## 15. 完成标准

全部满足才算根因解决：

1. QuerySpec 能完整表达 scalar、aggregate、keyed rows、multiset rows、
   zero rows 和 change set。
2. 不再存在 `required_columns` 到 Oracle 输出列的无条件映射。
3. 明细 SP 配 scalar 在 SQL 生成前被拒绝。
4. zero rows 与直接结果对账在类型和覆盖规则上明确分离。
5. Contract Gate 和 Business Comparator 使用同一 VerificationPlan。
6. 模型不能修改比较模式、列协议、容差和写入范围。
7. 设计契约错误不消耗 SQL 修复轮次。
8. 换用至少四类不同存储过程均能按相同链路生成并通过正确校验。
9. 所有变异候选都在预期阶段被拒绝。
10. 旧会话可读，旧不完整契约不可直接部署。
11. 默认单元测试全部通过。
12. 未经明确授权不访问或修改真实业务数据库。
