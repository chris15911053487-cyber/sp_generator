# 语义计算蓝图与输入义务编译器实施审查

日期：2026-07-26  
对应计划：`2026-07-26-semantic-computation-input-compiler-root-redesign.md`

## 审查结论

核心架构已经按计划切换：

```text
ResultContract
→ FactBlueprint
→ ComputationBlueprint
→ SemanticObligationSet
→ SemanticInputObligationSet
→ Dynamic SourceRequirements
→ deterministic ExpressionDesign
→ SemanticCompiler
```

旧的 LLM `ExpressionDesign` 节点、提示词和图路由已经删除。Source 的
LLM 响应不再包含自由 `fields` 列表，只能实现程序生成的动态输入槽位。

离线回归通过，真实库存用户流已经通过语义链，在测试库确实缺少库存收发存、
总账库存余额和物料主数据对象时停在 Schema 能力缺口，没有伪造绑定。

本轮尚不能宣布整个计划全部完成：计划要求的五类真实数据库场景各连续成功三次
尚未全部执行。

## 逐项核查

| 计划项 | 状态 | 实施证据 |
|---|---|---|
| ComputationBlueprint | 已完成 | `app/contracts/computation_blueprint.py` |
| 事实、结果、过滤表达式上下文隔离 | 已完成 | 三组判别式 AST；事实公式无 parameter kind |
| 计算目标动态必填槽位 | 已完成 | `app/services/computation_blueprint_schema.py` |
| 目标、类型、聚合由程序写入 | 已完成 | `materialize_computation_blueprint` |
| 公式输入完整性和类型推导 | 已完成 | `computation_blueprint_validator.py` |
| 输出引用和依赖环校验 | 已完成 | ComputationBlueprint 合同和确定性验证器 |
| SemanticInputContractCompiler | 已完成 | `semantic_input_compiler.py` |
| 输入 ID、slot、usage path 稳定 | 已完成 | 输入义务合同及确定性 hash |
| 同事实输入复用 | 已完成 | 按 `(fact_symbol, input_symbol)` 合并 |
| 跨事实同名输入隔离 | 已完成 | slot 包含 fact symbol |
| Source 动态输入槽位 | 已完成 | `source_obligation_schema.py` |
| 禁止自由来源字段 | 已完成 | LLM Source Schema 不再暴露 `fields` |
| 输入实体所有权 | 已完成 | Source 物化器和语义编译器双重校验 |
| 政策过滤槽位 | 已完成 | 动态 policy filter 槽位和程序写入 policy target |
| 删除 LLM ExpressionDesign | 已完成 | 图中改为 `expression_materialize` |
| 确定性表达式物化 | 已完成 | `expression_materializer.py` |
| 公式与物化表达式等价 | 已完成 | SemanticCompiler 比较 canonical hash |
| 政策公式/输入覆盖证据 | 已完成 | policy coverage 包含公式、计算 hash、必需和实现输入 |
| 无义务来源字段拒绝 | 已完成 | 动态 Schema + `SOURCE_FIELD_UNUSED` |
| checkpoint v3 | 已完成 | 新阶段、新字段、数据库列和旧 v2 直接失效 |
| 级联失效 | 已完成 | checkpoint 阶段顺序驱动全部下游清空 |
| 错误码和证据 | 已完成 | 稳定码归一化、失败阶段、冻结公式、输入和 blocked downstream |
| UI 展示 | 已完成 | 新阶段标签和结构化证据字段 |
| 完整离线回归 | 已完成 | 227 passed，8 skipped（E2E 迁移前记录） |
| 库存自然语言 E2E | 正确停止 | 语义链 confirmed；Schema 目录无真实库存/总账对象 |
| 隔离 SQL Server E2E | 已完成 | 明细/头行、分组统计、双事实对账连续三轮均 3/3 通过 |
| 五类自然语言 E2E 各连续三次 | 未完成 | 库存场景仍需真实物理对象或用户绑定选择 |

## 与原计划的一处有意差异

原计划示例把输入义务建模为单个 `value_symbol`。实现采用
`value_symbols: list[Symbol]`，因为同一事实输入可能被多个事实值公式复用。
这样一个底层业务输入只产生一个稳定 slot，不会要求 Source 重复实现同一字段；
所有使用位置仍由 `usage_paths` 完整记录。这是对计划“同一事实内可复用同一输入”
要求的收敛实现，不是兼容分支。

## 真实 E2E 证据

自然需求：

```text
帮我查询一个业务端库存余额与财务端库存余额的对比情况的存储过程
```

结果：

- 结果模式、事实蓝图、计算蓝图、政策义务、输入义务、Source 和语义编译均通过；
- checkpoint 状态到达 `confirmed / semantic_compile`；
- Schema 解析发现测试库目录不含库存收发存、总账库存余额和物料主数据对象；
- 状态机停在 `awaiting_design_reconfirmation`；
- 没有生成 SchemaBinding、Reference、SP 或伪造验证结果；
- Snapshot Isolation 原值为 ON，结束后仍为 ON。

仓库隔离测试套件随后迁移为：

- 结构化事实合同的 SP 计划只使用确定性 `compile_contract_plan`；
- 双事实 Reference 分别使用 `compile_fact_plan` 生成 `source_fact`；
- Reference 不读取或复制 SP SQL；
- 程序从独立事实结果组合最终期望结果后再与 SP 比较。

该套件连续运行三轮，每轮明细/头行、分组统计、双事实对账均为 `3 passed`。

## 完成剩余项所需条件

要完成五类真实 E2E，各场景的测试库必须存在可绑定的真实物理对象，或者由用户在
Schema 选择中明确决定允许的物理映射。没有该条件时继续自动重试只会重复同一个
真实能力缺口，不应通过字段名猜测或场景专用规则绕过。
