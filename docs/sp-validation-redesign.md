# SP 语法、业务校验与部署重设计

> 日期：2026-07-20
> 状态：已实施（首期最小改造）
> 适用范围：SQL Server 测试数据库；生成的 SP 可以是查询型，也可以包含 INSERT、UPDATE、DELETE

## 2026-07-21 Harness 加固说明

首期“逐个保存草稿后校验”的生成语义已被生成 harness 取代：已确认设计先编译为一个 QuerySpec，再绑定实时 SchemaEvidence；SP 与独立 Oracle 只作为内存 CandidateBundle 生成。安全、Schema、编译、契约和回滚业务闸门全部通过后，整批制品才在一个 SQLite 事务中替换。

因此，第 4.1 节保留为首期历史说明，不再代表当前 Agent 生成主路径。当前规则是：

- 候选生成和修复期间不写 `stored_procedures` 或 `verify_queries`；
- 任一 SP 失败或进入 `needs_review`，旧整套制品保持不变；
- 自动修复最多两轮且不得改变 QuerySpec 不变量；
- 保存的 QuerySpec、Schema 指纹和 bundle 哈希共同绑定验证版本；
- 部署前重新捕获 Schema 指纹并核对 bundle 哈希。

SQL Server 静态编译保证与真实集成探针边界见 `docs/sqlserver-validation-capabilities.md`。

## 1. 目标与原则

本次改造把“保存草稿”“完整校验”“正式部署”明确分离。

1. 生成阶段只把 SP、校验 SQL、参数和校验规格保存到 SQLite 草稿，不向 SQL Server 持久化部署 SP。
2. 校验必须同时包含语法/执行校验和业务校验。
3. 校验使用会话级本地临时过程，不创建或覆盖持久化 SP。
4. 查询型 SP 比较返回结果；写入型 SP 比较执行前后的数据变化集。
5. 写入型校验在测试数据库事务内执行，完成后始终回滚。
6. 只有用户点击“一键部署”时，系统才允许创建或修改持久化 SP。
7. 部署内容必须与最近一次完整校验通过的内容完全一致。

## 2. SP 类型

每个 SP 具有 `operation_type`：

| 类型 | 含义 | 校验重点 |
| --- | --- | --- |
| `query` | 只返回查询结果 | SP Actual 与独立 SQL Expected 对账 |
| `insert` | 新增数据 | 新增键、行数和字段值 |
| `update` | 修改数据 | 被修改键、旧值和新值 |
| `delete` | 删除数据 | 被删除键、数量和范围 |
| `mixed` | 同时包含多种 DML | 每个声明目标表的完整变化集 |

无法确定类型时不得显示业务通过。

## 3. 生成与独立性

### 3.1 设计方案

设计方案必须确认：

- 业务口径、参数和输出粒度；
- 输出字段与业务键；
- 权威数据源、筛选条件和容差；
- SP 类型及可能影响的表；
- 至少一条直接结果/变化集对账规则；
- 必要的业务不变量。

### 3.2 SP 与校验 SQL 分开生成

SP 与校验 SQL 使用两次独立模型调用。生成校验 SQL 时只提供已确认业务规则、真实元数据、参数契约、输出契约和比较规格，不提供 SP 主体，避免简单复制 SP 的错误实现。

模型生成的 Oracle 校验 SQL始终只允许单条 `SELECT` 或 `WITH ... SELECT`。

## 4. 保存语义

### 4.1 模型生成完成

SP、校验 SQL、参数和校验规格保存为 SQLite 草稿；生成过程中按单个制品落库，生成完成后才替换旧 SP。用户执行“校验+保存”时才要求整个 SP 校验包原子保存：

```text
status = draft
syntax_valid = 0
business_valid = 0
validated_hash = NULL
```

### 4.2 “校验”

校验页面当前 SP、校验 SQL、参数和规格，不保存代码。若当前内容与已保存草稿不同，即使校验通过也提示“尚未保存，不可部署”。

### 4.3 “校验+保存”

```text
原子保存 SP + 校验 SQL + 参数 + 校验规格
→ 清除旧校验状态
→ 执行完整校验
→ 通过后记录 validated_hash
→ 失败则保留草稿/失败状态，但不可部署
```

保存接口失败时不得继续校验。

## 5. 校验流程

### 5.1 公共步骤

```text
安全与结构检查
→ SET PARSEONLY 基础语法检查
→ 将过程名改写为 #verify_<随机ID>
→ 在同一 SQL Server 连接创建本地临时过程
→ 使用参数绑定执行
→ 执行业务 Oracle SQL和不变量
→ 比较并记录结果
→ 回滚/关闭连接
```

`PARSEONLY` 仅作为快速语法检查；临时过程的实际创建和执行负责发现对象、参数、权限和运行期错误。

### 5.2 查询型 SP

优先使用 `SNAPSHOT` 隔离，在同一事务、同一连接、同一参数下获取 Actual 和 Expected。

必须至少存在一条 Actual/Expected 直接对账规则。业务不变量不能替代 SP 输出对账。

### 5.3 写入型 SP

使用框架控制的外层事务，推荐 `SERIALIZABLE` 并设置 `XACT_ABORT ON`：

```text
记录 Before
→ 计算 Expected Change Set
→ 执行临时 SP
→ 查询 After
→ 计算 Actual Change Set
→ 比较变化键、字段和数量
→ 检查业务不变量
→ ROLLBACK
→ 确认数据恢复
```

待测 SP 不允许自行执行 `BEGIN TRANSACTION`、`COMMIT`、`ROLLBACK` 或 `SAVE TRANSACTION`。

## 6. 比较模式

校验规格存储在 `verify_queries.validation_spec` JSON 中，首期支持四种模式：

### 6.1 `scalar`

总体金额、数量、单据数等单行指标，按列级容差比较。

### 6.2 `keyed_rows`

按稳定业务键比较分组或明细结果：检测重复键、缺失行、多余行和字段差异，不依赖结果顺序。

### 6.3 `zero_rows`

用于业务不变量和异常查询，返回零行通过，返回任意记录失败。

### 6.4 `change_set`

用于写入型 SP，比较 INSERT、UPDATE、DELETE 的预期变化与实际变化。每个目标表必须声明操作、业务键、比较字段和最大影响行数。

示例：

```json
{
  "operation_type": "delete",
  "affected_tables": [
    {
      "table": "dbo.TestOrders",
      "operation": "delete",
      "key_columns": ["DocEntry"],
      "compare_columns": [],
      "max_affected_rows": 1000
    }
  ],
  "mode": "change_set",
  "required": true
}
```

## 7. 业务通过标准

只有以下条件全部满足，才能设置 `syntax_valid=1`、`business_valid=1`、`status=verified`：

1. SP 安全和结构检查通过；
2. `PARSEONLY` 通过；
3. 临时 SP 创建与执行成功；
4. 至少存在一条必选校验 SQL；
5. 查询型 SP 至少存在一条 `scalar` 或 `keyed_rows` 直接结果对账；
6. 写入型 SP 至少存在一条 `change_set` 变化集对账；
7. 全部必选 Oracle SQL执行成功；
8. Actual 与 Expected 使用相同参数和一致数据视图；
9. 所有必选规则通过；
10. 写入型校验已成功回滚。

没有校验 SQL、SP 未执行、SP 执行失败或只运行了业务不变量时，均不得显示业务通过。

## 8. SQL 安全边界

### 8.1 Oracle 校验 SQL

仅允许单条 `SELECT` 或 `WITH ... SELECT`；禁止 DDL、DML、`EXEC`、动态 SQL、临时表和多结果集。

### 8.2 待测 SP

可按照声明类型使用 `INSERT`、`UPDATE`、`DELETE`；首期不支持 `MERGE`。DML 目标必须属于 `affected_tables`。

允许本地 `#临时表`操作；禁止全局 `##` 临时表。

始终禁止：

- 数据库级 DDL、永久表 DDL、`TRUNCATE`；
- 动态 SQL、`sp_executesql`；
- `xp_cmdshell`、外部数据源、链接服务器和跨库写入；
- 权限、用户和登录修改；
- 事务控制语句；
- SQL Agent、邮件、文件、网络等不可回滚外部副作用。

### 8.3 数据库与账号

写入型校验仅允许配置为 `test` 的指定数据库。执行前检查 `DB_NAME()` 与配置完全一致。

权限上建议区分 validation 与 deployment 两个数据库账号。首期因仅连接指定测试数据库并控制最小改造，保留现有单账号配置，但在应用层强制 `environment=test`、校验 `DB_NAME()`、阻止校验路径持久化 DDL，并只保留一个部署 API。账号级拆分列为后续部署加固项。

所有参数使用 pyodbc 绑定，不拼接参数值。执行增加超时、最大结果行数和最大影响行数。

## 9. 状态与版本绑定

最小新增字段：

```text
stored_procedures.validated_hash
stored_procedures.deployed_hash
stored_procedures.operation_type
verify_queries.validation_spec
```

`validated_hash` 覆盖 SP 名称、代码、参数、操作类型、全部校验 SQL和校验规格。

```text
current_hash == validated_hash  → 当前保存版本已校验
current_hash != validated_hash  → 校验已过期，禁止部署
deployed_hash == validated_hash → 已部署版本与校验版本一致
```

任何相关内容修改后，旧校验立即失效。

## 10. 部署检查与一键部署

原“一键预检”改为“部署检查”，检查完整校验状态、版本哈希、安全规则、过程名、未保存修改和部署连接，不重复昂贵业务对账，也不部署。

一键部署是唯一允许持久化 SP 的入口。服务端重新检查资格后，在一个 SQL Server 事务中对全部 SP 执行 `CREATE OR ALTER PROCEDURE`；任一失败则整体回滚，全部成功后记录 `deployed_hash` 和 `deployed_at`。

## 11. 普通执行

普通“执行”只执行已部署版本。查询型直接展示结果；写入型必须显示操作类型、影响表和参数并要求明确确认，提示本次执行会永久修改测试数据库。

## 12. 最小实施范围

新增 `app/services/validation.py`，集中负责安全检查、临时过程执行、参数绑定、四种比较模式和哈希计算。

修改：

- `app/agent/nodes.py`：移除自动正式部署和校验时自动修改；
- `app/agent/prompts.py`：独立生成 Oracle SQL和校验规格；
- `app/routes/verify.py`：统一调用校验服务；
- `app/routes/deploy.py`：增加版本门禁和事务部署；
- `app/db/sqlserver.py`：支持受控事务、参数化、超时和原子部署；
- `app/db/sqlite.py`：迁移字段和原子保存；
- `app/templates/index.html`：提交当前全部编辑内容并展示准确状态。

暂不建设独立验证数据库、完整版本历史、运行记录表、外部数据质量框架和月结规则引擎。

## 13. 验收标准

1. 生成和校验流程不会创建或修改持久化 SP；
2. 只有一键部署能持久化部署；
3. 没有直接结果/变化集对账时业务校验失败；
4. 查询结果支持 scalar、按键明细和零行不变量比较；
5. INSERT、UPDATE、DELETE 校验后数据完整恢复；
6. 未声明表写入、跨库写入、危险 DDL和事务控制被阻止；
7. 超时、异常或比较失败均回滚；
8. 修改 SP 或校验 SQL后旧校验立即失效；
9. 未校验或校验过期版本不能部署；
10. 批量部署任一失败时整体回滚；
11. 未展开编辑器不会保存空代码；
12. 所有参数均不可注入 SQL。
