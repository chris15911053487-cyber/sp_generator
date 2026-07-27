# Verification Contract V2 实施计划

日期：2026-07-24

依据：

- `docs/superpowers/plans/2026-07-24-verification-contract-root-redesign.md`

## 1. 实施原则

本计划只实现通用验证契约，不为会话编号、表名、字段名或 `COUNT(*) AS cnt`
增加特判。

每个任务都遵循：

1. 先增加一个能稳定复现目标行为的失败测试。
2. 只写使该测试通过的最小实现。
3. 运行本任务测试和受影响的回归测试。
4. 检查差异，只提交与本任务直接相关的文件。

所有 Python 命令使用项目虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m pytest ...
```

默认不运行：

- `test_improvements.py`
- `test_e2e.py`
- 真实 LLM 调用
- 真实业务 SQL Server 写入

## 2. 最终交付结构

新增：

- `app/services/verification_contract.py`
- `test_verification_contract.py`

主要修改：

- `app/services/generation_harness.py`
- `app/services/candidate_pipeline.py`
- `app/services/validation.py`
- `app/agent/prompts.py`
- `app/agent/nodes.py`
- `app/db/sqlite.py`
- `app/routes/verify.py`
- `app/routes/deploy.py`
- `app/routes/chat.py`
- `app/templates/index.html`
- `test_generation_harness.py`
- `test_validation_service.py`
- `test_verify_autofix.py`
- `test_deploy_validation.py`

## 3. 目标数据流

```text
DecisionPlan
  -> QuerySpec V2
  -> validate_design_contract()
  -> compile_verification_plan()
  -> VerificationPlan
  -> SP SQL + Oracle SQL
  -> compile_candidate()
  -> validate_artifacts_against_plan()
  -> execute_verification_plan()
  -> deployment_eligible
```

核心约束：

- QuerySpec V2 是业务验证意图的唯一来源。
- VerificationPlan 是 contract gate 和业务执行的唯一来源。
- `verify_queries.compare_columns` 与旧 `validation_spec` 只用于历史兼容。
- 新链路不得根据字段缺失猜测比较模式。

---

## Task 0：建立基线并字符化当前缺陷

### 目标

在改模型前锁定当前错误，证明后续修复来自通用协议，而不是放宽检查。

### 文件

- 修改 `test_generation_harness.py`
- 修改 `test_validation_service.py`

### Step 1：增加当前行为字符化测试

在 `test_generation_harness.py` 增加：

1. `test_legacy_query_spec_rejects_aggregate_rule`
2. `test_legacy_zero_rows_count_alias_hits_oracle_output_contract_failure`
3. `test_legacy_detail_scalar_reaches_late_contract_or_business_stage`

在 `test_validation_service.py` 增加：

4. `test_legacy_zero_rows_count_result_is_not_zero_rows`

这些测试记录旧链路当前行为，不把它声明为 V2 的正确行为，不连接数据库。
编译元数据通过 stub 提供。测试名必须带 `legacy`，防止后续误认为新链路仍应
保留该语义。

### Step 2：确认字符化测试通过

```powershell
.\.venv\Scripts\python.exe -m pytest test_generation_harness.py test_validation_service.py -q
```

预期：

- 新测试稳定复现 aggregate 不可表达、zero_rows 输出含义冲突和 scalar
  适用性检查过晚；
- 现有测试继续通过。

### Step 3：记录基线

运行当前相关回归：

```powershell
.\.venv\Scripts\python.exe -m pytest test_generation_harness.py test_validation_service.py test_verify_autofix.py test_deploy_validation.py -q
```

不得在本任务修改 `_contract_errors()`。

### 提交边界

只提交可通过的字符化测试。

---

## Task 1：新增 QuerySpec V2 规则类型

### 目标

用带判别字段的联合类型替代通用 `mode + required_columns`。

### 文件

- 修改 `app/services/generation_harness.py`
- 新增 `test_verification_contract.py`

### Step 1：编写模型测试

覆盖：

1. `scalar_equal` 接受显式列对和容差。
2. `aggregate_equal` 接受 `sum/count_rows/count_distinct/min/max/avg`。
3. 非 `count_rows` 指标缺少 `actual_column` 时拒绝。
4. `keyed_rows_equal` 必须有键和比较列。
5. `multiset_rows_equal` 必须有比较列。
6. `invariant_zero_rows` 使用 `evidence_columns`，不存在 `compare_columns`。
7. `change_set_equal` 目标必须完整。
8. V2 规则出现 `required_columns` 或通用 `mode` 时因 `extra=forbid` 拒绝。
9. QuerySpec V2 canonical JSON 包含稳定版本号。

### Step 2：实现最小模型

在 `generation_harness.py` 增加：

- `ResultContract`
- `ScalarColumnPair`
- `AggregateMetric`
- `ScalarEqualRule`
- `AggregateEqualRule`
- `KeyedRowsEqualRule`
- `MultisetRowsEqualRule`
- `InvariantZeroRowsRule`
- `ChangeSetTarget`
- `ChangeSetEqualRule`
- `VerificationRuleV2` 判别联合

新增 V2 ProcedureSpec：

```python
class ProcedureSpecV2(...):
    ...
result_contract: ResultContract
verification_rules: list[VerificationRuleV2]
```

新增 V2 QuerySpec：

```python
class QuerySpecV2(...):
    ...
contract_version: Literal[2]
```

### Step 3：保留旧模型但隔离

不要让 V2 模型兼容接收旧字段。当前 `VerificationRuleSpec`、`ProcedureSpec`、
`QuerySpec` 暂时保留原名和行为，供现有链路继续运行；新增类型使用明确的 V2
名称：

- `VerificationRuleV2`
- `ProcedureSpecV2`
- `QuerySpecV2`

到 Task 4 切换新设计入口时，再把旧类型重命名为 `Legacy*` 并集中修正导入。
Task 1 完成时所有现有测试必须仍然通过。

### Step 4：验证

```powershell
.\.venv\Scripts\python.exe -m pytest test_verification_contract.py -q
```

### 提交边界

只提交领域模型和模型测试，不接入 Agent、候选流水线或数据库。

---

## Task 2：实现 Design Contract Validator

### 目标

在生成任何 SQL 之前拒绝不适用或覆盖不足的规则。

### 文件

- 新增 `app/services/verification_contract.py`
- 修改 `test_verification_contract.py`

### Step 1：编写失败测试

新增：

- many + scalar_equal：拒绝。
- one + keyed_rows_equal：拒绝。
- one + multiset_rows_equal：拒绝。
- 查询型过程只有 invariant_zero_rows：拒绝。
- aggregate 指标引用不存在输出：拒绝。
- keyed rows 的键或比较列不存在：拒绝。
- multiset rows 比较列不存在：拒绝。
- invariant evidence 列不存在：拒绝。
- reporting 使用 change_set：拒绝。
- controlled_write 缺少 writes 对应 change_set：拒绝。
- change_set 目标超出 writes：拒绝。

新增通过测试：

- 单行汇总 + scalar。
- 多行明细 + keyed rows。
- 多行无键结果 + multiset rows。
- 多行明细 + aggregate。
- direct 对账 + supplemental zero rows。
- controlled write 的每个目标各有一条 change set。

### Step 2：实现

在 `verification_contract.py` 增加：

```python
class DesignContractIssue(...)
class DesignContractResult(...)

def validate_design_contract(
    procedure: ProcedureSpec,
) -> DesignContractResult:
    ...
```

错误至少包含：

- `code`
- `rule_name`
- `path`
- `message`
- `requires_user_confirmation`

建议错误码：

- `verification_direct_rule_missing`
- `verification_mode_incompatible`
- `verification_output_reference_missing`
- `verification_change_target_missing`
- `verification_change_target_extra`
- `verification_zero_rows_not_direct`

### Step 3：验证

```powershell
.\.venv\Scripts\python.exe -m pytest test_verification_contract.py -q
```

### 提交边界

只提交纯函数 validator 和单元测试。

---

## Task 3：实现 VerificationPlan 编译器

### 目标

把合法 QuerySpec V2 确定性编译为唯一可执行协议。

### 文件

- 修改 `app/services/verification_contract.py`
- 修改 `test_verification_contract.py`

### Step 1：编写测试

覆盖每种规则的：

- `actual_schema`
- `expected_schema`
- comparator 配置
- direct/supplemental 角色
- blocking 策略

特别覆盖：

- aggregate 的 Actual 列和 Expected 指标列可以不同名。
- zero rows 的 expected schema 来自 `evidence_columns`。
- zero rows 没有 Actual schema。
- keyed rows 的 Oracle expected schema 使用业务输出别名。
- change set 自动产生 `ChangeType`、键、Before/After 列。
- 同一 QuerySpec 两次编译产生完全相同 canonical JSON 和哈希。
- 只改变规则或结果契约时哈希变化。

### Step 2：实现模型

增加：

- `ResultColumnContract`
- `RuleExecutionPlan`
- `VerificationPlan`
- `VerificationPlanCompileError`

增加函数：

```python
def compile_verification_plan(
    query_spec: QuerySpec,
    procedure: ProcedureSpec,
) -> VerificationPlan:
    ...
```

VerificationPlan 必须记录：

- `version`
- `query_spec_hash`
- `procedure_name`
- `result_contract`
- `rules`
- canonical JSON
- plan hash

### Step 3：实现 OracleSqlTask

增加：

```python
class OracleSqlTask(...)

def oracle_sql_tasks(
    plan: VerificationPlan,
    procedure: ProcedureSpec,
) -> list[OracleSqlTask]:
    ...
```

OracleSqlTask 只包含模型实现 SQL 所需的业务描述、允许对象、参数和 Expected
输出形状。

### Step 4：验证

```powershell
.\.venv\Scripts\python.exe -m pytest test_verification_contract.py -q
```

### 提交边界

只提交确定性编译器和测试。

---

## Task 4：将设计节点切换到 QuerySpec V2

### 目标

新会话只生成、展示和确认 V2；不完整设计在 SQL 生成前失败。

### 文件

- 修改 `app/agent/prompts.py`
- 修改 `app/agent/nodes.py`
- 修改 `test_design_confirmation.py`
- 修改 `test_generation_harness.py`

### Step 1：更新测试

覆盖：

- `_generate_query_spec_design()` 使用 V2 JSON Schema。
- 设计模型返回 many + scalar 时，设计状态为 `contract_invalid`。
- 只有 zero rows 时，设计状态为 `contract_invalid`。
- 合法 V2 的渲染文本与实际确认的 canonical JSON 一致。
- 修改意见重生成后重新编译 plan。
- 用户确认的 decision hash 仍不可漂移。

### Step 2：更新设计提示

在 `QUERY_SPEC_DESIGN_PROMPT` 和 repair prompt 中：

- 解释 `result_contract`。
- 明确六类规则的适用性。
- 禁止默认生成“结果必须非空”。
- 明确 zero rows 只是补充不变量。
- 明确多行 SP 不得使用 scalar。
- 明确 aggregate 的 Actual 与 Expected 列职责。

提示只帮助模型正确生成；最终合法性仍由 validator 决定。

### Step 3：接入 validator/compiler

在方案节点中：

1. Pydantic 校验 V2。
2. `validate_design_contract()`。
3. `compile_verification_plan()`。
4. 保存 QuerySpec 草稿、结构化诊断和 plan。
5. 只有三步都通过才进入 `ready_for_confirmation`。

### Step 4：更新方案展示

`_render_query_spec()` 按模式展示：

- 结果形状；
- 直接对账或补充断言；
- Actual 与 Expected 列；
- 键、聚合、容差、异常证据或写入范围。

不得重新从自然语言解释规则。

### Step 5：验证

```powershell
.\.venv\Scripts\python.exe -m pytest test_design_confirmation.py test_generation_harness.py test_verification_contract.py -q
```

### 提交边界

Task 4 与 Task 5 是一个完整集成边界：如果 Task 4 结束时确认后的 V2 还不能
进入候选生成，就不要单独提交 Task 4。旧会话读取暂时仍走旧入口。

---

## Task 5：让候选和 Oracle 生成携带 VerificationPlan

### 目标

模型只写 SQL，契约字段完全来自 plan。

### 文件

- 修改 `app/services/candidate_pipeline.py`
- 修改 `app/agent/prompts.py`
- 修改 `app/agent/nodes.py`
- 修改 `test_generation_harness.py`

### Step 1：增加测试

覆盖：

- `CandidateBundle` 必须包含 `verification_plan`。
- plan hash 与 QuerySpec 不匹配时拒绝。
- Oracle 模型只返回 `name + sql_code` 即可。
- 模型返回 mode、compare_columns、tolerance 等额外字段时拒绝或忽略并记录
  `model_contract_field_ignored`，但不能改变 plan。
- Oracle 缺规则、重复规则、未知规则时明确失败。
- 每条 Oracle prompt 包含 expected schema。

### Step 2：修改 CandidateBundle

增加：

```python
verification_plan: VerificationPlan
```

不可变契约哈希必须同时覆盖：

- QuerySpec
- VerificationPlan
- SchemaEvidence

### Step 3：替换 `_normalize_oracle_candidates`

删除新链路中的：

```text
required_columns -> compare_columns
```

改为：

```python
def bind_oracle_sql_to_plan(
    raw_queries: list[dict],
    plan: VerificationPlan,
) -> list[VerifyQueryCandidate]:
    ...
```

`VerifyQueryCandidate` 新链路只需要：

- `rule_name`
- `sql_code`
- `rule_plan_hash`

数据库兼容字段在持久化边界派生，不作为内存事实源。

### Step 4：更新 Oracle Prompt

按 `OracleSqlTask` 逐规则或整批生成，必须给出：

- 规则 kind；
- Expected 输出列名、顺序、类型族；
- 业务描述；
- 允许来源和参数。

### Step 5：验证

```powershell
.\.venv\Scripts\python.exe -m pytest test_generation_harness.py test_verification_contract.py -q
```

### 提交边界

提交候选模型和 Oracle 生成边界，不改运行时比较器。

---

## Task 6：按 VerificationPlan 重写 Contract Gate

### 目标

Contract Gate 只比较 SQL Server 实际元数据与 plan，不再使用通用列检查。

### 文件

- 修改 `app/services/candidate_pipeline.py`
- 修改 `test_generation_harness.py`

### Step 1：增加模式矩阵测试

覆盖：

- scalar：Expected 缺列、多列、错序、错类型。
- aggregate：指标列缺失或多余。
- keyed rows：键或比较列缺失。
- multiset rows：比较列缺失。
- zero rows：evidence 列正确时通过。
- zero rows：`COUNT(*) AS cnt` 时失败为
  `zero_rows_must_return_evidence`，而不是当前通用 `oracle_output`。
- change set：固定结果形状和写目标一致。
- SP 结果列仍与 result_contract 一致。

### Step 2：拆分函数

将 `_contract_errors()` 拆为：

```python
def _procedure_contract_errors(...): ...
def _oracle_contract_errors(...): ...
def _rule_result_shape_errors(...): ...
```

Oracle 检查依据：

```python
rule_plan.expected_schema
```

不得再读取 `rule.required_columns` 或从 `validation_spec` 推断模式。

### Step 3：修复错误归属

错误分为：

- `design_contract_error`：plan 本身无效，`repairable=False`
- `sql_contract_error`：SQL 元数据不符，`repairable=True`

### Step 4：验证

```powershell
.\.venv\Scripts\python.exe -m pytest test_generation_harness.py test_verification_contract.py -q
```

### 提交边界

只提交 contract gate 和对应测试。

---

## Task 7：让 Business Comparator 只执行 VerificationPlan

### 目标

移除新链路的默认模式推断，并补齐 multiset 与正确 zero rows 语义。

### 文件

- 修改 `app/services/validation.py`
- 修改 `test_validation_service.py`
- 修改 `test_verification_contract.py`

### Step 1：测试比较器

新增：

- scalar 一行对一行。
- scalar Actual 多行时配置失败。
- aggregate 从 Actual 明细计算多个指标。
- keyed rows 检测重复键、缺失、多余和字段差异。
- multiset rows 保留重复行次数。
- zero rows 只执行 Oracle，不传入 Actual。
- zero rows 的 Oracle 返回异常明细时失败。
- change set 继续事务回滚。
- 结果超过上限返回 inconclusive，不能判 passed。

### Step 2：新增执行入口

新增：

```python
def execute_verification_plan(
    sp: dict,
    oracle_queries: list[dict],
    plan: VerificationPlan,
    params: dict | None = None,
) -> dict:
    ...
```

新入口直接按 `rule.kind` 分派。

### Step 3：保留旧入口为适配层

`validate_sp_bundle()` 暂时保留，但：

- V2 候选必须调用 `execute_verification_plan()`。
- 旧候选调用 Legacy Adapter。
- 不允许 V2 回退到 `normalize_verify_queries()` 的默认模式猜测。

### Step 4：实现 multiset

规范化每行的比较列后使用计数映射：

```text
canonical_row -> occurrence_count
```

不得使用 set。

### Step 5：修正 zero rows

zero rows：

- 不读取 SP Actual。
- Oracle 零行通过。
- Oracle 任意行失败并展示前 20 条 evidence。
- SQL 返回 `COUNT(*)` 已在 contract gate 拒绝，不在运行时添加特判放行。

### Step 6：验证

```powershell
.\.venv\Scripts\python.exe -m pytest test_validation_service.py test_verification_contract.py -q
```

### 提交边界

提交运行时比较器和测试，不动持久化。

---

## Task 8：重构自动修复归因

### 目标

设计错误不再消耗 SQL 修复轮次，SQL 错误只修对应制品。

### 文件

- 修改 `app/services/candidate_pipeline.py`
- 修改 `app/agent/nodes.py`
- 修改 `app/agent/prompts.py`
- 修改 `test_verify_autofix.py`
- 修改 `test_generation_harness.py`

### Step 1：增加测试

覆盖：

- plan invalid：不调用 SP/Oracle 修复模型。
- Oracle Expected 列不符：只调用 Oracle 修复。
- SP 输出列不符：只调用 SP 修复。
- 修复不得改变 QuerySpec hash 或 plan hash。
- 相同 SQL hash 连续两轮不变时提前停止，错误为 `repair_no_progress`。
- business mismatch 无法归因时转 `needs_review`。

### Step 2：实现分类

增加：

```python
def classify_repair_action(
    errors: list[GateError],
) -> Literal[
    "none", "redesign", "repair_procedure", "repair_oracle", "needs_review"
]:
    ...
```

### Step 3：更新修复 Prompt

Oracle 修复提示包含：

- 对应 RuleExecutionPlan；
- Expected 元数据；
- Actual 编译元数据；
- 当前 SQL；
- 最小 SchemaEvidence。

禁止模型返回或修改契约字段。

### Step 4：验证

```powershell
.\.venv\Scripts\python.exe -m pytest test_verify_autofix.py test_generation_harness.py -q
```

### 提交边界

提交错误归因和修复循环。

---

## Task 9：持久化 VerificationPlan 和稳定哈希

### 目标

V2 候选重启后仍使用同一份 plan，部署资格绑定完整契约版本。

### 文件

- 修改 `app/db/sqlite.py`
- 修改 `app/services/candidate_pipeline.py`
- 修改 `test_generation_harness.py`
- 修改 `test_deploy_validation.py`

### Step 1：增加迁移测试

在临时 SQLite 数据库验证：

- 旧数据库自动新增 plan 字段。
- 保存和读取 canonical plan JSON。
- plan hash 可恢复。
- 修改 QuerySpec、plan、SP、Oracle 或 Schema 指纹都会使 bundle hash 变化。
- 编辑任意 SQL 后清除 validated hash。

### Step 2：增加字段

`session_designs`：

- `query_spec_version`
- `verification_plan_json`
- `verification_plan_hash`

`stored_procedures`：

- `verification_plan_json`
- `verification_plan_hash`

迁移只做可重复的 `ALTER TABLE ADD COLUMN`。

### Step 3：更新事务保存

整批候选保存必须在一个事务中写入：

- QuerySpec
- VerificationPlan
- Schema fingerprint
- SP
- Oracle SQL
- bundle hash
- 阶段结果

任一失败整体回滚。

### Step 4：更新部署哈希

部署资格至少绑定：

```text
query_spec_hash
+ verification_plan_hash
+ schema_fingerprint
+ procedure_sql_hash
+ oracle_sql_hashes
```

### Step 5：验证

```powershell
.\.venv\Scripts\python.exe -m pytest test_generation_harness.py test_deploy_validation.py -q
```

### 提交边界

提交数据库迁移、原子保存和哈希测试。

---

## Task 10：实现旧会话只读适配

### 目标

旧会话可查看；能无歧义转换的可以重新校验，不能转换的必须重新设计。

### 文件

- 修改 `app/services/verification_contract.py`
- 修改 `app/routes/verify.py`
- 修改 `test_verification_contract.py`
- 修改 `test_validation_service.py`

### Step 1：编写适配测试

覆盖：

- 旧 scalar 同名一行规则可转换。
- 旧 keyed rows 具有完整键和比较列时可转换。
- 旧 change set 完整时可转换。
- 旧 zero rows 不把 compare_columns 当成 Oracle 必须输出列。
- 旧 aggregate 缺少 actual 配置时不能猜测。
- 明细 scalar 不能因兼容而继续执行。
- 不完整转换返回 `legacy_contract_incomplete`。

### Step 2：实现

增加：

```python
class LegacyAdaptationResult(...)

def adapt_legacy_verification_contract(
    legacy_query_spec: LegacyQuerySpec,
    verify_queries: list[dict],
) -> LegacyAdaptationResult:
    ...
```

规则：

- 只转换唯一、无歧义信息。
- 不根据 SQL 文本猜业务模式。
- 不完整旧契约可展示但不可部署。

### Step 3：更新 verify 路由

路由分派：

```text
contract_version=2 -> V2 plan
旧版本 -> Legacy Adapter
无法适配 -> 返回重新设计诊断
```

### Step 4：验证

```powershell
.\.venv\Scripts\python.exe -m pytest test_verification_contract.py test_validation_service.py -q
```

### 提交边界

提交兼容适配器和路由分派。

---

## Task 11：更新部署门禁和前端诊断

### 目标

用户能区分设计契约错误、SQL 实现违约和业务不一致；部署只信任 V2 完整哈希。

### 文件

- 修改 `app/routes/deploy.py`
- 修改 `app/routes/chat.py`
- 修改 `app/templates/index.html`
- 修改 `test_deploy_validation.py`
- 修改 `test_generation_harness.py`

### Step 1：部署测试

覆盖：

- 缺 plan 拒绝部署。
- plan hash 与保存内容不符时拒绝。
- 旧不完整契约拒绝。
- 全部 gate 通过且 bundle hash 一致时允许。
- 重新编辑后立即失去部署资格。

### Step 2：聊天与前端测试

至少验证输出数据包含：

- `design_contract_invalid`
- `sql_contract_error`
- `business_mismatch`
- `needs_review`

前端展示：

- 规则名称和 kind；
- Expected 列与实际列；
- 建议动作；
- “返回方案修改”或“修复 SQL”。

不得把设计契约错误显示为 SQL 编译错误。

### Step 3：验证

```powershell
.\.venv\Scripts\python.exe -m pytest test_deploy_validation.py test_generation_harness.py -q
```

### 提交边界

提交部署门禁和展示。

---

## Task 12：跨存储过程通用回归

### 目标

证明实现没有针对会话20、OINV、`cnt` 或中文输出列硬编码。

### 文件

- 修改 `test_verification_contract.py`
- 修改 `test_generation_harness.py`
- 修改 `test_validation_service.py`
- 可新增 `tests/fixtures/verification_contract_v2/`

### Step 1：增加四类 fixture

1. 单行销售汇总：
   - scalar_equal
2. 应收发票明细：
   - keyed_rows_equal
   - aggregate_equal
   - invariant_zero_rows
3. 无自然键分类明细：
   - multiset_rows_equal
4. 隔离测试库受控更新：
   - change_set_equal

fixture 使用不同的：

- 过程名
- 表名
- 参数名
- 输出别名
- 规则名

### Step 2：参数化重命名测试

把上述名称全部替换后重新构建 QuerySpec 和 plan，断言仍能：

- 设计校验通过；
- plan 编译通过；
- contract gate 识别正确元数据；
- 比较器产生正确结果。

### Step 3：变异测试

分别注入：

- 漏过滤；
- 错 JOIN；
- SP 少列；
- Oracle 错别名；
- Oracle 错聚合；
- 参数未使用；
- 多余来源表；
- 写入范围扩大。

断言错误发生在预期阶段，且不会修改 plan 来迁就 SQL。

### Step 4：运行完整默认回归

```powershell
.\.venv\Scripts\python.exe -m pytest test_clarify.py test_design_confirmation.py test_generation_harness.py test_sql_artifact_compiler.py test_sqlserver_compile_integration.py test_validation_service.py test_verify_autofix.py test_deploy_validation.py test_verification_contract.py -q
```

若 `test_sqlserver_compile_integration.py` 的用例需要真实 SQL Server 配置，则只运行
其中使用 mock/fake 的默认可执行用例；不得擅自连接真实业务数据库。

### Step 5：静态检查

```powershell
git diff --check
```

检查代码中不应再出现新链路依赖：

```powershell
rg -n "required_columns|normalize_verify_queries" app
```

允许命中：

- Legacy 模型；
- Legacy Adapter；
- 有明确注释的历史兼容代码。

不允许命中：

- QuerySpec V2；
- VerificationPlan compiler；
- V2 contract gate；
- V2 business comparator。

### 提交边界

提交通用 fixture、参数化回归和变异测试。

---

## 4. 推荐提交顺序

1. `test: characterize verification contract gaps`
2. `feat: add query spec v2 verification rule models`
3. `feat: validate query spec verification semantics`
4. `feat: compile deterministic verification plans`
5. `feat: wire v2 designs and oracle sql to verification plans`
6. `feat: validate sql result shapes against plans`
7. `feat: execute business checks from verification plans`
8. `fix: classify contract repair ownership`
9. `feat: persist verification plans and hashes`
10. `feat: adapt legacy verification contracts safely`
11. `feat: enforce v2 deployment and diagnostics`
12. `test: add cross-procedure contract regressions`

不要在同一个提交里同时重构模型、数据库、前端和比较器。

## 5. 完成检查表

- [ ] QuerySpec V2 可表达六类验证规则。
- [ ] many + scalar 在 SQL 生成前失败。
- [ ] 查询型过程缺少 direct 规则时失败。
- [ ] zero rows 不再复用 compare_columns。
- [ ] aggregate 的 Actual 与 Expected 列职责分离。
- [ ] Contract Gate 使用 plan.expected_schema。
- [ ] Business Comparator 使用同一 VerificationPlan。
- [ ] V2 新链路不推断默认 mode。
- [ ] 设计错误不调用 SQL 修复模型。
- [ ] SQL 修复不能改变 QuerySpec/plan hash。
- [ ] plan 已持久化并纳入部署哈希。
- [ ] 旧不完整契约不能获得部署资格。
- [ ] 四类不同存储过程全部通过通用回归。
- [ ] 变异候选都在预期阶段被拒绝。
- [ ] 默认单元测试全部通过。
- [ ] 未运行未经授权的真实 E2E 或业务数据库写入。

## 6. 停止条件

实施中遇到以下情况必须停止当前任务并先修设计，不得继续打补丁：

- 某种规则无法从 QuerySpec 确定 Expected 输出形状。
- Contract Gate 和 Business Comparator 需要不同的字段解释。
- 需要读取 SQL 文本才能猜测规则模式。
- 需要按表名、字段名或规则名写业务特判。
- 为兼容旧会话必须放宽新会话部署门禁。
- 测试只能连接真实业务数据库才能通过。
