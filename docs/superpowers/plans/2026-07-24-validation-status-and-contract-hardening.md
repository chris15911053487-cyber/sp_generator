# 校验状态与契约鲁棒性改造计划

日期：2026-07-24

## 1. 背景

当前校验流水线已经包含 QuerySpec、Schema、安全、编译、契约和业务等多个阶段，但对外结果主要压缩为：

- `syntax_ok`
- `business_ok`

这会造成以下问题：

1. Schema、契约或安全检查失败时，界面也可能显示“语法错误”。
2. 前置阶段失败后，后续阶段实际上没有执行，但界面仍显示为失败，而不是“未执行”。
3. `OINV`、`dbo.OINV`、`[dbo].[OINV]` 等等价对象引用通过字符串比较时可能被误判。
4. `mode`、`compare_columns`、`affected_tables` 等确定性契约字段由模型重复生成，容易产生无业务含义的格式漂移。
5. 自动修复同时承担格式修复和 SQL 逻辑修复，结果不稳定，且难以解释失败原因。

本计划将“生成结果是否保存”和“生成结果是否可部署”分离，并建立可解释、可扩展的分阶段校验模型。

## 2. 目标

### 2.1 用户体验目标

- 只有 SQL Server 编译探针失败时，界面才显示“SQL 编译错误”。
- Schema、契约、安全、业务错误分别显示，不再归入“语法错误”。
- 未执行的阶段显示“未执行”，而不是错误。
- 失败候选仍保存到右侧，可编辑、可重新校验。
- 未通过全部必需阶段的候选不能部署。
- 错误信息能够说明失败阶段、失败对象、原因和建议动作。

### 2.2 工程目标

- QuerySpec 作为唯一业务契约来源。
- 确定性字段由代码注入，不让模型重复解释。
- 数据库对象按解析后的身份比较，不按原始 SQL 字符串比较。
- 确定性修复优先于模型修复。
- 每轮模型修复后从第一道门重新校验。
- 保持整批候选保存的原子性。

## 3. 非目标

- 不放宽危险 SQL、安全边界或部署门禁。
- 不自动选择存在歧义的同名表。
- 不通过修改真实业务数据库来让测试通过。
- 不在本计划中引入 SQL AST 第三方依赖；优先使用现有解析与 SQL Server 元数据能力。
- 不改变用户已经确认的业务口径、参数、输出或校验规则。

## 4. 总体设计

### 4.1 分阶段三态校验

每个阶段统一返回：

```json
{
  "status": "passed | failed | not_run",
  "errors": [],
  "details": {}
}
```

阶段固定为：

1. `query_spec`
2. `schema`
3. `safety`
4. `compile`
5. `contract`
6. `business`

规则：

- 阶段成功：`passed`
- 阶段实际执行且失败：`failed`
- 因前置失败而未执行：`not_run`
- 不允许用 `False` 同时表示“失败”和“未执行”

候选整体状态继续使用：

- `candidate_generated`
- `validated`
- `needs_review`
- `failed`

持久化状态使用：

- `draft`
- `persisted`
- `verify_failed`
- `needs_review`
- `deployed`

### 4.2 对外校验结果

新增结构化字段：

```json
{
  "sp_id": "...",
  "sp_name": "...",
  "overall_status": "verify_failed",
  "stages": {
    "query_spec": {"status": "passed", "errors": [], "details": {}},
    "schema": {"status": "passed", "errors": [], "details": {}},
    "safety": {"status": "passed", "errors": [], "details": {}},
    "compile": {"status": "passed", "errors": [], "details": {}},
    "contract": {"status": "failed", "errors": [], "details": {}},
    "business": {"status": "not_run", "errors": [], "details": {}}
  },
  "deployment_eligible": false
}
```

兼容期保留：

- `syntax_ok`
- `business_ok`

兼容字段的新语义：

- `syntax_ok=true`：`compile=passed`
- `syntax_ok=false`：`compile=failed`
- `syntax_ok=null`：`compile=not_run`
- `business_ok` 同理映射业务阶段

待前端和测试完成迁移后，再评估是否删除兼容字段。

### 4.3 Schema 对象身份解析

建立统一的数据库对象引用解析函数，输入 SQL 中的对象引用，输出：

```json
{
  "database": "当前数据库",
  "schema": "dbo",
  "name": "OINV",
  "object_type": "table",
  "resolution": "exact | default_schema | unique_name",
  "ambiguous": false
}
```

解析规则：

1. `dbo.OINV` 和 `[dbo].[OINV]` 规范化为同一对象。
2. 非限定名称 `OINV`：
   - 优先按当前连接用户的默认 Schema 解析。
   - 默认 Schema 未命中时，仅在 QuerySpec 允许范围内唯一匹配时解析。
   - 出现多个同名候选时返回歧义错误，不自动选择。
3. 已解析对象必须属于 QuerySpec 允许的来源表或写入表。
4. 真实存在但未声明的对象属于契约错误。
5. 无法在实时 Schema 中解析的对象属于 Schema 错误。
6. 标识符大小写规则应与目标数据库排序规则保持一致；无法取得排序规则时使用保守策略。

### 4.4 确定性契约注入

以下字段从已确认的 QuerySpec 直接生成：

- 校验规则名称
- `mode`
- `required`
- `compare_columns`
- `affected_tables`
- 写入操作类型
- 主键列
- 最大影响行数
- 参数名称、顺序、类型、必填性和默认值
- 预期输出列、顺序和类型族

模型只负责：

- SP SQL 主体
- 每条独立 Oracle 的 SQL 主体

模型输出中的确定性字段处理方式：

- 缺失：由程序补齐。
- 与 QuerySpec 等价但格式不同：规范化。
- 与 QuerySpec 冲突：忽略模型值并记录结构化诊断，不改变已确认契约。
- 规则名称未知或规则数量不一致：契约失败，不自动猜测映射。

### 4.5 编译与契约职责分离

SQL Server 编译阶段负责：

- SQL 语法
- 表和字段是否可由 SQL Server 解析
- 参数声明是否合法
- 结果集元数据是否可描述

契约阶段负责：

- 是否只使用已声明对象
- 参数签名是否符合 QuerySpec
- 输出列及类型是否符合 QuerySpec
- 校验规则集合是否完整
- 校验方式和比较列是否符合 QuerySpec
- 写入范围是否符合 QuerySpec

静态正则检查不得再决定 `syntax_ok`。

### 4.6 修复策略

修复顺序：

1. 确定性规范化
2. 重新运行全部阶段
3. 对可修复的 SQL 实现错误调用模型
4. 模型修复后重新运行全部阶段
5. 达到修复上限后保存失败草稿

确定性修复包括：

- 标识符括号格式规范化。
- 唯一可解析对象的 Schema 限定。
- QuerySpec 契约字段注入。
- `compare_columns` 字符串与列表格式统一。
- SQL 类型同义词规范化，例如 `NUMERIC` 与 `DECIMAL`。

禁止确定性修复：

- 在多个同名表之间自动选择。
- 猜测不存在字段的替代字段。
- 修改业务过滤条件。
- 增删输出列。
- 改变写入范围。

模型只接收：

- 当前失败阶段。
- 结构化错误。
- 当前目标制品。
- 与错误相关的最小 Schema 子集。
- 不可修改的 QuerySpec 约束。

## 5. 实施步骤

### Task 1：建立分阶段三态结果模型

涉及文件：

- `app/services/candidate_pipeline.py`
- `app/agent/nodes.py`
- `app/services/validation.py`

工作内容：

1. 为每个候选初始化完整的六阶段结果。
2. 执行到的阶段更新为 `passed` 或 `failed`。
3. 未执行阶段保持 `not_run`。
4. 重构 `_candidate_result`，直接序列化阶段结果。
5. `syntax_ok` 仅从 `compile` 阶段派生。
6. `business_ok` 仅从 `business` 阶段派生。
7. 增加 `deployment_eligible`。

验证：

- 契约失败时 `compile=passed`、`contract=failed`、`syntax_ok=true`。
- Schema 失败时 `compile=not_run`、`syntax_ok=null`。
- 编译失败时 `compile=failed`、`syntax_ok=false`。

### Task 2：统一 Schema 对象解析

涉及文件：

- `app/services/schema_evidence.py`
- `app/services/candidate_pipeline.py`
- `app/db/sqlserver.py`

工作内容：

1. SchemaEvidence 增加对象身份解析所需信息。
2. 从 SQL Server 获取当前数据库、默认 Schema 和必要的对象元数据。
3. 新增对象引用规范化与解析函数。
4. 用解析结果替换 `_table_references(sql) - allowed_tables` 字符串差集。
5. 为歧义、未解析、越权引用提供不同错误码。

建议错误码：

- `schema_object_not_found`
- `schema_object_ambiguous`
- `contract_object_not_allowed`

验证：

- `OINV`、`dbo.OINV`、`[dbo].[OINV]` 唯一解析时等价。
- `dbo.X` 和 `custom.X` 同时存在时，`X` 不得自动通过。
- 未声明但真实存在的表归类为契约错误。
- 不存在的表归类为 Schema 或编译错误，不标记为普通语法错误。

### Task 3：确定性生成 Oracle 契约外壳

涉及文件：

- `app/agent/nodes.py`
- `app/services/candidate_pipeline.py`
- `app/agent/prompts.py`

工作内容：

1. 根据每条 `verification_rule` 建立 Oracle 外壳。
2. 模型只返回规则名称和 SQL 主体。
3. 程序注入 `mode`、`required`、`compare_columns`。
4. 写入型规则由程序注入 `affected_tables`。
5. 初次生成和修复路径共用同一个规范化函数。
6. 删除 Prompt 中要求模型重复输出确定性字段的内容。

验证：

- 模型省略确定性字段仍能生成合法候选。
- 模型返回冲突字段不会改变 QuerySpec。
- 未知规则名、重复规则或缺少规则会明确进入契约失败。

### Task 4：拆分编译状态与契约状态

涉及文件：

- `app/services/candidate_pipeline.py`
- `app/db/sqlserver.py`
- `app/routes/chat.py`

工作内容：

1. 编译探针结果只写入 `compile` 阶段。
2. 契约检查结果只写入 `contract` 阶段。
3. Schema 和权限相关错误不得改写为语法错误。
4. 聊天摘要按失败阶段生成。
5. 对多个失败阶段按固定优先级展示。

展示优先级：

1. 安全
2. Schema
3. 编译
4. 契约
5. 业务

验证：

- 同一次校验可同时保留已执行阶段的结果。
- 聊天区和右侧使用同一份结构化数据，不各自猜测错误类型。

### Task 5：前端展示分阶段状态

涉及文件：

- `app/templates/index.html`
- `app/static/style.css`

工作内容：

1. 将“语法”徽标改为“SQL 编译”。
2. 增加 Schema、契约和业务状态。
3. 三态显示：
   - 通过：绿色
   - 失败：红色
   - 未执行：灰色
4. 错误详情按阶段分组。
5. 失败 SP 和校验 SQL继续使用现有编辑器。
6. 部署按钮只读取 `deployment_eligible`，不自行推导。

验证：

- 契约失败不显示“SQL 编译失败”。
- 前置失败时后续阶段显示“未执行”。
- 刷新页面后仍能从 SQLite 恢复失败草稿及阶段详情。

### Task 6：持久化结构化阶段结果

涉及文件：

- `app/db/sqlite.py`
- `app/routes/verify.py`
- `app/routes/deploy.py`

工作内容：

1. 将完整校验结果以 JSON 保存到 `verify_result`。
2. 保存失败候选、待复核候选和成功候选。
3. 仅全部必需阶段通过时写入 `validated_hash`。
4. 编辑 SP 或校验 SQL 后清除 `validated_hash`，阶段状态重置为待校验。
5. 部署资格以当前内容哈希和 `validated_hash` 一致为准。

验证：

- 失败候选可编辑且重启后仍存在。
- 编辑成功候选后立即失去部署资格。
- 修改失败候选不会影响其他会话。
- 整批保存失败时事务完整回滚。

### Task 7：重构自动修复

涉及文件：

- `app/services/candidate_pipeline.py`
- `app/agent/nodes.py`
- `app/agent/prompts.py`

工作内容：

1. 增加确定性修复阶段。
2. 只有标记为 `repairable=true` 的 SQL 实现错误进入模型修复。
3. 模型不得修改 QuerySpec。
4. 每次修复后清空旧阶段结果并从头校验。
5. 修复次数和每轮错误写入结果详情。
6. Schema 歧义、业务差异无法归因时停止自动修复。

验证：

- 格式等价问题不调用模型。
- 安全错误不调用模型。
- Schema 歧义不调用模型。
- SQL 编译错误最多修复规定轮数。
- 每轮修复都重新执行前置门禁。

### Task 8：迁移与兼容

涉及文件：

- `app/db/sqlite.py`
- `app/routes/verify.py`
- `app/templates/index.html`

工作内容：

1. 旧 `verify_result` 缺少 `stages` 时生成兼容视图。
2. 旧布尔字段仅用于展示历史记录，不赋予新的部署资格。
3. 新保存结果统一使用阶段结构。
4. 不要求修改历史 SP SQL。

验证：

- 旧会话可以正常打开。
- 旧失败记录不会因为迁移被错误标记为可部署。
- 新旧数据同时存在时前端不报错。

## 6. 测试计划

### 6.1 单元测试

主要文件：

- `test_generation_harness.py`
- `test_validation_service.py`
- `test_deploy_validation.py`
- `test_verify_autofix.py`

覆盖矩阵：

| 场景 | Schema | 编译 | 契约 | 业务 | 部署 |
|---|---|---|---|---|---|
| 对象不存在 | failed | not_run | not_run | not_run | 禁止 |
| 安全违规 | passed | not_run | not_run | not_run | 禁止 |
| SQL 语法错误 | passed | failed | not_run | not_run | 禁止 |
| 参数契约漂移 | passed | passed | failed | not_run | 禁止 |
| 业务结果不同 | passed | passed | passed | failed/needs_review | 禁止 |
| 全部通过 | passed | passed | passed | passed | 允许 |

对象解析用例：

- `OINV`
- `dbo.OINV`
- `[dbo].[OINV]`
- 大小写差异
- 默认 Schema
- 跨 Schema 同名歧义
- 临时表
- CTE
- SAP B1 用户表，例如 `[@CUSTOM]`

契约用例：

- 模型缺少 `compare_columns`
- 模型返回错误 `mode`
- 模型改变 `affected_tables`
- 规则缺失、重复、未知
- 输出列顺序变化
- SQL 类型同义词
- 参数默认值漂移

### 6.2 API 测试

验证：

- 校验失败响应包含完整 `stages`。
- 失败候选已分配 `sp_id`。
- `/api/sp/{session_id}` 可读取失败候选。
- 编辑失败候选后状态重置。
- 部署接口拒绝没有有效 `validated_hash` 的候选。

### 6.3 前端验证

验证：

- 各阶段三态图标正确。
- 契约失败不显示语法错误。
- 错误详情分组正确。
- 失败候选可展开和编辑。
- 刷新后失败详情仍存在。
- 部署按钮状态与后端一致。

### 6.4 不自动运行的测试

- `test_improvements.py` 需要本地服务，不混入默认单元测试。
- `test_e2e.py` 会调用真实 LLM 和 SQL Server，仅在配置完整且用户明确要求时运行。
- SQL Server 集成测试不得连接或修改真实业务数据库。

## 7. 实施顺序

推荐按以下顺序提交，避免前后端中间状态互相误解：

1. 分阶段三态结果模型及单元测试。
2. Schema 对象身份解析及解析测试。
3. QuerySpec 确定性契约注入。
4. 编译、契约和业务结果拆分。
5. SQLite 持久化与部署门禁兼容。
6. 前端分阶段展示。
7. 自动修复重构。
8. 旧数据兼容和完整回归。

每一步都必须保持现有安全门禁和部署哈希检查可用。

## 8. 完成标准

满足以下条件才算改造完成：

1. 非编译错误不再显示为“语法错误”。
2. 所有阶段都支持 `passed/failed/not_run`。
3. 等价对象名基于实际对象身份判断。
4. 歧义对象不会被自动放行。
5. 确定性契约字段不再依赖模型输出。
6. 失败候选始终保存、显示、可编辑。
7. 失败或编辑后的候选始终不可部署。
8. 错误信息能明确指出阶段和原因。
9. 相关单元测试全部通过。
10. 未经明确授权不运行真实 LLM/SQL Server E2E。

