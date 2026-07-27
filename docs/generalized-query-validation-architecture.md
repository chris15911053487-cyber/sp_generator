# 通用查询型存储过程生成与验证架构

## 目标范围

同一条主链必须覆盖：

1. 单实体或已确认关联的明细查询；
2. 按维度汇总的统计查询；
3. 多来源业务事实对账；
4. 异常集合查询。

不允许用某个案例的表名、列名或 SQL 形状作为通用规则。

## 冻结语义

用户确认后必须同时冻结：

- 输出、参数、粒度、过滤、空值和币种口径；
- 源事实及其业务实体；
- 每个事实的维度、指标和聚合方式；
- 多事实之间的匹配键及连接类型；
- 最终结果如何由事实值计算；
- 金额容差和合法空结果语义。

多实体或汇总需求缺少上述任一结构时，必须停在设计阶段，禁止让后续 LLM
在物理关系计划中补猜业务含义。

## 三类 Expected

### 明细

独立 Reference 返回与最终结果同粒度的完整 Expected 行集。

### 汇总

独立 Reference 按冻结维度和聚合方式生成 Expected。聚合类型必须是合同字段，
不能从输出名称或自然语言临时推断。

### 多来源对账

每个来源分别生成最小 Reference：

- 业务侧事实；
- 财务侧事实；
- 其他已确认来源事实。

验证程序按照冻结的事实匹配键在内存中组合这些来源，随后计算冻结结果表达式，
形成最终 Expected。禁止生成第三条“最终对账 SQL”，也禁止参考 SP SQL。

## 用例状态真值表

| 用例 | Expected | Actual | 结论 |
|---|---:|---:|---|
| coverage | 非空 | 一致非空 | passed |
| coverage | 空 | 空 | inconclusive |
| boundary | 非空 | 一致非空 | passed |
| boundary | 空 | 空 | inconclusive |
| empty，允许空 | 空 | 空 | passed，但不贡献覆盖 |
| empty | 非空 | 非空且一致 | inconclusive，用例选择无效 |
| 任意 | 不一致 | 任意 | failed |

候选只有在至少一个 coverage 有效、所有必需 boundary 通过、所有 empty 用例符合预期，
并且所有结果比较一致时，才可部署。

## 生成职责

- LLM：生成纯业务语义候选和 Schema 候选；
- 合同校验器：拒绝缺失、冲突或歧义语义；
- 确定性编译器：把冻结事实编译为受限关系计划；
- SQL Renderer：把受限关系计划渲染为 SQL Server SQL；
- Reference Composer：在内存中组合多来源 Expected；
- Validation Runner：同快照执行、比较并产生唯一结论。

常规明细、结构化聚合和已声明事实连接不得调用 LLM 生成物理关系计划。

## 通用验收矩阵

| 类型 | 正确实现 | 删除过滤 | 改聚合 | 改匹配键 | 改金额字段 |
|---|---|---|---|---|---|
| 明细 | validated | failed | 不适用 | failed | failed |
| 汇总 | validated | failed | failed | failed | failed |
| 多来源对账 | validated | failed | failed | failed | failed |
| 异常集合 | validated | failed | failed | failed | failed |

任何 Schema 歧义、环境不一致、无有效覆盖或证据缺失均不得变绿。
