# 业务政策义务编译器实施审查

日期：2026-07-26

对照计划：
`2026-07-26-business-policy-obligation-compiler-root-redesign.md`

## 结论

计划中的代码结构与离线验收项已经实现。真实数据库验收已证明：

- 一条应收发票明细链路从 Schema 到最终业务比较全部通过；
- 销售收入与凭证对账的自然语言链路已通过语义编译并生成完整政策覆盖矩阵；
- 该自然语言链路在 Schema 物理歧义处正确停止，没有猜测字段或强行变绿。

计划要求的“五类场景各连续通过三次”尚未完成，因此不能把整份计划标记为最终验收完成。

## 逐项审查

| 计划项 | 状态 | 代码证据 |
|---|---|---|
| 强类型 `BusinessPolicySpec` | 已完成 | `app/contracts/semantic_design.py` |
| 删除 `filter_policy_keys` | 已完成 | 全仓无旧字段引用 |
| 判别式 `PolicyBinding` | 已完成 | `app/contracts/semantic_design.py` |
| 决策 key/value 冻结 | 已完成 | `app/agent/nodes.py::_validate_result_contract_stage` |
| 确定性义务 ID 与槽位 | 已完成 | `app/services/semantic_obligation_compiler.py` |
| 义务 checkpoint 阶段 | 已完成 | `app/agent/graph.py`、`semantic_design_state.py` |
| 动态 Fact 政策目标 Schema | 已完成（强化项） | `app/services/fact_policy_schema.py` |
| 动态 Source 必填 Schema | 已完成 | `app/services/source_obligation_schema.py` |
| 确定性物化 policy key/target | 已完成 | 两个动态 Schema 服务 |
| 编译覆盖矩阵 | 已完成 | `app/services/semantic_compiler_v3.py` |
| 缺失、额外、目标变化阻断 | 已完成 | `OBLIGATION_IMPLEMENTATION_*`、`OBLIGATION_TARGET_CHANGED` |
| 下游阻断 | 已完成 | Agent 条件边与失败 checkpoint |
| 提示词同步 | 已完成 | `app/agent/prompts.py` |
| 错误信息与 UI 同步 | 已完成 | `app/agent/nodes.py`、`app/templates/index.html` |
| 完整离线回归 | 已完成 | 206 passed，8 skipped |
| 五类真实 E2E 各三次 | 未完成 | 一类完整通过；一类正确停在用户选择型 Schema 歧义 |

## E2E 发现并完成的架构强化

最初实现仍允许 FactBlueprint 的 LLM 自由填写 binding kind。真实用户链路中，
模型把 matching 政策写成 contract_only，证明事后校验仍然太晚。

现已改为：

```text
ResultContract.business_policies
→ 程序生成动态 policy_targets 必填 Schema
→ LLM 只填写目标
→ 程序写入 policy_key 与 binding kind
→ SemanticObligationCompiler
```

因此 LLM 已无法遗漏政策、修改 key，或把 matching 政策写成 contract_only。

## 尚未关闭的验收项

销售收入与凭证对账场景在真实 SAP 测试库发现两项物理歧义：

1. 不含税收入可映射到发票行净额等多个候选字段；
2. “主营业务收入”没有统一布尔字段，需要用户确认 Series、DocSubType、Project
   或其他业务分类规则。

这是正确的用户选择型阻断。除非用户提供业务映射，系统不得自行选择，因此该场景
当前不能宣称 validated。

此外，旧 `test_v3_sqlserver_e2e.py` 启用真实库后，三组中一组已通过，另外两组
仍使用手写关系计划，与当前“facts 合同必须由确定性 fact compiler 生成计划”的规则冲突。
生产校验没有为旧夹具降级；这两组夹具需要改为使用编译后的事实计划和
`source_fact` Reference 角色后再继续验收。
