# SQL / SP 生成 Harness 加固实施计划

日期：2026-07-21

状态：离线实施完成；真实 SQL Server 隔离探针待显式授权

## 1. 目标

解决模型生成的 SQL Server 存储过程与独立校验 SQL 不一致、引用不存在字段或表、自动修复改变业务语义，以及校验失败后覆盖旧版本的问题。

本计划的核心不是继续堆叠提示词，而是在模型外建立确定性的生成 harness：

    已确认的业务设计
      -> 结构化 QuerySpec
      -> 实时数据库 SchemaEvidence
      -> SP 与独立 Oracle 候选
      -> 安全 / Schema / 编译 / 契约 / 业务校验
      -> 全部通过后原子替换

完成后，类似 Invalid column name 'DocCurrency' 的错误应在候选阶段被捕获；失败候选不得进入当前有效版本，更不得影响已有可用 SP。

## 2. 本次范围

### 必须完成

- 用一个结构化 QuerySpec 作为 SP 与独立校验 SQL 的共同业务契约。
- 从目标 SQL Server 实例读取并精确绑定本次生成涉及的表、字段和类型。
- 对候选 SP 与校验 SQL 执行分层、确定性的闸门校验。
- 自动修复必须受不可变约束保护，并限制次数。
- 所有候选只保存在内存中；全部通过后，再用一个 SQLite 事务替换当前会话的整套产物。
- Schema 证据和指纹可审计，并在部署前检测过期。
- 增加覆盖错误字段、错误表、契约漂移、部分失败和原子回滚的测试。

### 明确不做

- 不把 SAP 官方文档、社区资料或向量检索作为正确性的主来源。
- 不引入通用 SQL AST 重写器或复杂工作流框架。
- 不允许动态 SQL 绕过静态校验。
- 不在默认测试中访问真实业务数据库、真实 LLM 或部署服务。
- 不在实施过程中顺手重构无关代码。

SAP 文档、客户术语表、CUFD/UFD1/OUTB 等知识可作为后续 P1 的语义增强，但实时物理 Schema 始终是字段和表是否存在的最高事实来源。UFD1 等业务值默认不得发送给模型，避免数据泄露。

## 3. 实施前约束

新会话开始后必须先完整阅读：

- 项目根目录 AGENTS.md 和 CLAUDE.md。
- 本计划。
- docs/sp-validation-redesign.md。
- docs/verification-and-deployment-audit.md。
- 当前工作树差异。

当前工作树已有未提交修改，至少涉及：

- app/agent/graph.py
- app/agent/nodes.py
- app/agent/prompts.py
- app/db/sqlserver.py
- app/templates/index.html
- test_verify_autofix.py

这些修改属于当前工作成果或用户工作。实施者不得使用 git reset、git checkout 或覆盖式还原；每一步都要基于实际 diff 做最小增量修改。除非用户明确要求，不创建提交、不部署服务。

## 4. 设计原则与不可变条件

### 4.1 单一业务契约

QuerySpec 是生成阶段唯一的业务语义输入。SP 和独立 Oracle SQL 必须由同一个 QuerySpec、同一份 SchemaEvidence 分别生成。

独立 Oracle 的含义是实现独立，不是契约独立：

- Oracle 不得复制、改写或从 SP SQL 推导。
- Oracle 与 SP 共享参数、来源、过滤、粒度、输出和校验规则。
- 两者发生结果差异时，不能默认任意一方正确；确定性契约校验能定位时才修复，否则转人工复核。

### 4.2 Schema 优先

- 所有物理标识符都必须精确绑定到当前目标数据库。
- 必须使用 schema-qualified 名称，避免 dbo 与其他 schema 同名歧义。
- 不存在的表或字段必须在生成保存前失败。
- 模糊匹配只能给出候选提示，绝不能自动替换字段。
- 用户自定义表和字段必须与标准表字段同等处理，例如 @CUSTOM、U_Color。
- 不再以设计文本中碰巧出现的表名和最多 12 张表作为 Schema 上下文边界。

### 4.3 候选先行、原子发布

- 生成、修复、校验期间不得修改 stored_procedures 和 verify_queries 的当前有效记录。
- 多个 SP 中任意一个失败，整批候选失败。
- 只有整批通过全部闸门时，才允许在一个 SQLite 事务中替换。
- 事务异常必须回滚，旧产物、旧校验状态和旧查询保持完全不变。

### 4.4 修复受约束

自动修复不得改变：

- 存储过程名和 operation_type。
- 参数名、顺序、SQL 类型、必填性和默认值。
- 允许读取或写入的表集合。
- 写操作类型和影响范围约束。
- 输出列名、类型、含义及粒度。
- 独立校验规则的名称、模式和所需列。

语法、Schema、编译或契约错误最多修复两轮。每次修复后必须从第一道闸门重新执行全部校验。安全违规默认直接失败；无法确定责任方的业务不一致转 needs_review。

## 5. 核心数据结构

优先使用项目现有 Pydantic 版本，不增加新的运行时依赖。

### 5.1 QuerySpec

建议在 app/services/generation_harness.py 中定义最小模型：

    QuerySpec
      design_version: str
      procedures: list[ProcedureSpec]

    ProcedureSpec
      name: str
      purpose: str
      operation_type: reporting | controlled_write
      parameters: list[ParameterSpec]
      sources: list[SourceSpec]
      joins: list[JoinSpec]
      filters: list[FilterSpec]
      grain: list[ColumnRef]
      outputs: list[OutputSpec]
      writes: list[WriteSpec]
      verification_rules: list[VerificationRuleSpec]

最小字段语义：

- ParameterSpec：name、sql_type、required、default、meaning。
- SourceSpec：schema、table、alias、role。
- JoinSpec：join_type、左右 ColumnRef、reason。
- FilterSpec：description、column_refs、parameter_refs。
- OutputSpec：name、meaning、source_columns、aggregation、sql_type。
- WriteSpec：table、operation、key_columns、max_affected_rows。
- VerificationRuleSpec：name、mode、required_columns、description。

QuerySpec 从已确认的自由文本设计编译一次。编译提示词只能结构化已有内容，不得补充新业务规则。模型返回后由 Pydantic 校验；缺少决定性信息时失败并要求用户补充，不静默猜测。

### 5.2 SchemaEvidence

    SchemaEvidence
      database_name: str
      captured_at: datetime
      objects: list[TableEvidence]
      unresolved: list[UnresolvedIdentifier]
      fingerprint: str

    TableEvidence
      schema: str
      name: str
      object_type: str
      columns: list[ColumnEvidence]

    ColumnEvidence
      name: str
      sql_type: str
      max_length: int | null
      precision: int | null
      scale: int | null
      nullable: bool
      description: str | null

fingerprint 使用排序后的规范 JSON 计算 SHA-256。只包含 QuerySpec 实际引用对象的结构，不包含业务行数据。

### 5.3 CandidateBundle

    CandidateBundle
      procedure_spec: ProcedureSpec
      procedure_sql: str
      verify_queries: list[VerifyQueryCandidate]
      schema_evidence: SchemaEvidence
      gate_results: list[GateResult]
      repair_count: int
      bundle_hash: str

候选对象只存在于 AgentState 或函数调用链中，校验完成前不写入现有持久化表。

### 5.4 GateError

    GateError
      artifact: query_spec | procedure | oracle | bundle
      category: safety | schema | compile | contract | business
      code: str
      message: str
      schema_subset: object | null
      repairable: bool

错误必须结构化后再传给模型，避免把完整数据库 Schema、异常堆栈或无关上下文重复发送。

## 6. 闸门顺序

每个候选必须按固定顺序执行：

1. QuerySpec 结构校验。
2. 实时 Schema 精确绑定。
3. SQL 安全校验。
4. SQL Server 编译和元数据校验。
5. QuerySpec 契约一致性校验。
6. 回滚事务中的业务校验。
7. 整批候选原子持久化。

前一道失败时，不执行有副作用的后续步骤。

### 6.1 安全闸门

复用并收紧 app/services/validation.py 中：

- validate_reporting_procedure。
- validate_readonly_query。

安全校验必须在任何 save_sp、save_verify_query、delete_sps_except 或批量替换之前执行。禁止动态 SQL、危险 DDL、未声明写表和越权写操作。

### 6.2 编译与元数据闸门

校验 SQL 使用 sys.sp_describe_first_result_set，捕获错误字段、错误表和结果元数据问题。

SP 不能只依赖临时 CREATE PROCEDURE 成功，因为 SQL Server 的 deferred name resolution 可能允许不存在的对象或分支通过。实施时先写真实 SQL Server 集成探针，验证以下方案：

- 在回滚隔离中创建唯一临时过程。
- 使用 SET SHOWPLAN_XML ON 对临时过程 EXEC，传入由参数规格生成的代表值。
- 确认不会执行写操作。
- 确认错误 207 和 208 能被捕获。
- 确认分支和存储过程内部引用的覆盖边界。

若探针证明 SHOWPLAN 不能可靠绑定所有引用，不得把它包装成强保证。应采用以下回退之一，并在代码和文档中明确能力边界：

- 在授权的隔离验证数据库执行回滚式编译/验证。
- 对静态 SQL 做受限标识符提取，加实时 Schema 精确绑定。

不要为此引入完整 SQL 解析器，除非现有受限方法经过测试仍无法满足本项目 SQL 子集。

### 6.3 契约闸门

至少验证：

- SP 名称与 ProcedureSpec 一致。
- 参数签名完全一致。
- 只引用允许的来源表和写表。
- operation_type 与实际语句类型一致。
- 结果集列名和兼容类型与 outputs 一致。
- Oracle 校验规则、模式和所需列与 verification_rules 一致。
- bundle_hash 覆盖 QuerySpec、Schema 指纹、SP 和 Oracle。

优先利用 SQL Server 返回的参数和结果集元数据、SHOWPLAN 引用对象；仅对项目允许的静态 SQL 子集做最小文本检查。

### 6.4 业务闸门

继续复用 validate_sp_bundle 的回滚执行和 SP / Oracle 独立比较，但输入改为内存 CandidateBundle。

业务差异处理：

- 若契约闸门能确定某个产物违反 QuerySpec，只修复该产物。
- 若只是两组结果不同而无法判断责任方，状态设为 needs_review。
- needs_review 不保存、不覆盖旧版本，并把差异摘要返回用户。

## 7. 分阶段实施任务

每个任务都应先补失败测试，再做最小实现，并在任务结束时运行直接相关测试。

### Task 0：锁定基线和现有行为

涉及文件：

- test_generation_harness.py，新建。
- test_validation_service.py，按需补充。
- test_verify_autofix.py，按需补充。

步骤：

1. 记录 git status --short 和 git diff --stat，不修改或清理现有变更。
2. 运行现有默认单元测试，确认基线。
3. 增加字符化测试，证明当前流程会在完整校验前保存或删除产物。
4. 增加失败测试：任一候选安全失败时，旧 SP 和旧 Oracle 必须保持字节级不变。

验收：

- 新测试准确复现当前风险。
- 不修改生产代码时，风险测试按预期失败。

### Task 1：引入 QuerySpec

涉及文件：

- app/services/generation_harness.py，新建。
- app/agent/prompts.py。
- app/agent/state.py 或当前 AgentState 定义文件。
- test_generation_harness.py。

步骤：

1. 定义最小 Pydantic 模型和规范化序列化方法。
2. 增加 compile_query_spec(design)；只接收已确认设计。
3. 严格解析模型 JSON，拒绝额外字段、重复参数、重复输出、未声明别名和非法引用。
4. AgentState 增加 query_spec 和 candidate_bundles；保持现有状态字段兼容。
5. 测试同一设计只编译一次，后续 SP 和 Oracle 都引用同一对象。

验收：

- 无法从设计得到完整契约时明确失败。
- QuerySpec 的规范 JSON 在相同输入下稳定。
- 不改变现有确认设计的人机流程。

### Task 2：实时 SchemaEvidence 与精确绑定

涉及文件：

- app/db/sqlserver.py。
- app/services/schema_evidence.py，新建；若逻辑很小可并入 generation_harness.py。
- test_generation_harness.py。
- 可选的真实 SQL Server 集成测试文件。

步骤：

1. 从 sys.schemas、sys.objects、sys.columns、sys.types 和扩展属性读取准确结构。
2. 按 QuerySpec 中的所有来源、连接、过滤、粒度、输出和写入引用绑定，不设 12 表上限。
3. 支持 dbo、非 dbo、@ 开头表和 U_ 开头字段。
4. 对 schema 同名表要求显式限定。
5. 返回结构化 unresolved，不自动采用模糊候选。
6. 计算稳定 fingerprint。
7. 当 SQL Server 返回 207 或 208 时只刷新一次 SchemaEvidence，再决定是否允许修复。

验收：

- DocCurrency 不存在而 DocCur 存在时，DocCurrency 必须失败，不能自动替换。
- 不存在表、字段大小写/排序规则差异、schema 冲突均有测试。
- 超过 12 张引用表仍完整收集。
- 不查询和发送业务行数据。

### Task 3：验证 SQL Server 编译方案

涉及文件：

- app/db/sqlserver.py。
- test_sqlserver_compile_integration.py，新建，默认跳过。
- docs/sqlserver-validation-capabilities.md，新建或更新现有审计文档。

步骤：

1. 先写最小集成探针，验证临时过程、SHOWPLAN_XML 和 sp_describe_first_result_set 行为。
2. 覆盖错误 207、208、过程参数、只读 SQL、写过程不实际执行。
3. 记录 deferred name resolution 和条件分支的真实边界。
4. 根据证据选择 SHOWPLAN 或受限静态绑定回退方案。
5. 实现统一 compile_candidate 接口，确保清理临时对象。

验收：

- 代码声明的保证与集成实验一致。
- 无论成功失败，都不在数据库留下临时对象或提交业务写入。
- 默认单元测试不要求真实 SQL Server。

真实 SQL Server 测试只在 environment=test、目标为隔离测试库且用户明确授权时运行。禁止用真实业务库做探针。

### Task 4：生成纯候选，不提前持久化

涉及文件：

- app/agent/nodes.py。
- app/agent/graph.py。
- app/agent/prompts.py。
- app/services/generation_harness.py。
- test_generation_harness.py。
- test_invoke_mock.py。

步骤：

1. generate_node 在确认设计后编译 QuerySpec、绑定 Schema、生成 CandidateBundle。
2. SP 生成函数只接收对应 ProcedureSpec 和 SchemaEvidence。
3. 重构 _generate_verify_sql_for_sp 为纯函数，返回候选 SQL 和参数，不调用 save_verify_query。
4. Oracle 提示词不得包含 SP SQL，只包含同一个 QuerySpec、SchemaEvidence 和独立验证职责。
5. 移除生成中间阶段对 save_sp、save_verify_query 和 delete_sps_except 的调用。
6. 调整图状态，使 candidate_generated 不被 UI 表述为已保存或已可部署。

验收：

- 生成候选后 SQLite 当前有效产物没有变化。
- 测试能证明 SP 与 Oracle 接收到完全相同的 QuerySpec 和 Schema 指纹。
- Oracle 的输入中不存在 SP 源码。

### Task 5：闸门流水线与安全修复

涉及文件：

- app/services/generation_harness.py。
- app/services/validation.py。
- app/agent/nodes.py。
- app/agent/prompts.py。
- test_generation_harness.py。
- test_validation_service.py。
- test_verify_autofix.py。

步骤：

1. 实现固定顺序 run_candidate_gates。
2. 将现有安全、编译和业务验证适配为接收内存候选。
3. 生成 GateError，只发送必要错误与最小 Schema 子集给修复模型。
4. 保存修复前 invariant snapshot。
5. 修复后重新解析、重新绑定并从首道闸门执行。
6. 超过两轮或 invariant 改变时失败。
7. 无法确定责任方的业务差异返回 needs_review。

验收：

- 修复 DocCurrency 为真实字段后可继续校验。
- 修复若偷偷改变参数、来源表、写表、输出或规则，必须被拒绝。
- 安全失败不会触发保存。
- 每轮修复均重新运行全部闸门。

### Task 6：整批原子替换

涉及文件：

- app/db/sqlite.py。
- app/agent/nodes.py。
- app/agent/graph.py。
- test_generation_harness.py。
- test_invoke_mock.py。

新增接口建议：

    replace_session_sp_bundles_atomically(
        session_id: str,
        bundles: list[ValidatedCandidateBundle],
    ) -> list[int]

步骤：

1. 为 stored_procedures 增加 query_spec_json、schema_fingerprint、bundle_hash 等必要审计字段；使用项目现有迁移方式。
2. 在一个 SQLite 事务中写入全部 SP、Oracle、参数、验证状态和哈希。
3. 全部插入成功后再删除本会话旧产物；或使用 staging 后切换，确保失败可回滚。
4. verify_node 只有在全部 CandidateBundle 通过后调用该接口。
5. 保留 save_sp_bundle 供兼容路径使用，但生成主流程不得逐个替换。

验收：

- 第二个 SP 失败时，第一个也不得被保存。
- SQLite 写入中途异常时旧整套产物不变。
- 成功时只出现一套新产物，SP 与 Oracle 外键关系正确。
- 保存的 bundle_hash 与实际内容一致。

### Task 7：修正手工验证保存与部署前检查

涉及文件：

- app/routes/verify.py。
- app/services/deploy.py 或当前部署实现。
- app/db/sqlite.py。
- test_validation_service.py。
- test_deploy_validation.py。

步骤：

1. 手工 verify 请求在 req.save=true 时先校验候选，成功后才保存。
2. 若产品确实需要保存无效内容，必须使用显式 draft 状态，不能覆盖当前有效版本；没有现有需求则不新增 draft 功能。
3. 部署前重新读取目标 Schema 指纹。
4. 指纹变化时返回 revalidation_required，不自动部署旧校验结果。
5. 部署前同时验证 bundle_hash，避免校验后 SQL 被修改。

验收：

- 无效手工 SQL 不覆盖当前版本。
- Schema 变化或 bundle 内容变化后不能沿用旧 verified 状态。
- 本任务只实现检查，不部署实际服务或业务数据库对象。

### Task 8：提示词、状态和 UI 兼容

涉及文件：

- app/agent/prompts.py。
- app/agent/graph.py。
- app/templates/index.html。
- 相应测试。

步骤：

1. 明确提示词中的事实优先级：QuerySpec 决定业务语义，SchemaEvidence 决定物理标识符。
2. UI 区分 candidate_generated、validated、needs_review、persisted。
3. 只有 persisted 后才刷新可部署 SP 列表。
4. 保留现有设计确认和错误展示行为，避免无关界面重构。

验收：

- 用户不会把候选生成误认为已经保存。
- needs_review 有清晰差异摘要和后续动作。
- 现有确认流程测试继续通过。

### Task 9：回归、文档和清理

涉及文件：

- AGENTS.md，仅在命令发生真实变化时更新。
- docs/sp-validation-redesign.md 或新增能力说明。
- 本计划中涉及的测试。

步骤：

1. 删除仅由本次改动产生的未使用函数、导入和状态字段。
2. 更新架构文档，记录强保证和已知限制。
3. 运行完整的离线测试矩阵。
4. 检查最终 diff，仅保留能追溯到本计划的修改。

验收：

- 默认测试不依赖真实 LLM、SQL Server 或已启动服务。
- 无格式错误、语法错误和新增未解释失败。
- 不部署、不连接或修改真实业务数据库。

## 8. 测试矩阵

### QuerySpec 和 Schema

- 缺少必填业务信息。
- 重复参数、输出或 alias。
- 未声明 alias、表和字段。
- DocCurrency 错误、DocCur 正确。
- dbo 与自定义 schema 同名表。
- @CUSTOM 与 U_Color。
- 超过 12 张表。
- fingerprint 确定性及结构变化敏感性。

### 候选和契约

- SP 与 Oracle 使用同一 QuerySpec / SchemaEvidence。
- Oracle 输入不含 SP SQL。
- 参数、输出、来源表、写表和 operation_type 漂移。
- 动态 SQL 或危险语句。
- 修复次数上限和全闸门重跑。
- 无法归因的业务差异进入 needs_review。

### 原子性

- 安全失败保留旧产物。
- 编译失败保留旧产物。
- 契约失败保留旧产物。
- 业务失败保留旧产物。
- 多 SP 部分失败保留全部旧产物。
- SQLite 中途异常完整回滚。
- 全部成功一次性替换。

### SQL Server 集成

- 错误字段返回 207。
- 错误对象返回 208。
- 参数和结果元数据。
- SHOWPLAN 不执行写入。
- 临时对象始终清理。
- deferred name resolution 与条件分支边界。

## 9. 每次应用代码修改后的验证命令

按 AGENTS.md 顺序执行。

补丁格式：

    git diff --check

应用代码或依赖变化后构建：

    docker compose build sp-generator

语法检查：

    docker compose run --rm --no-deps sp-generator python -m compileall -q app

直接相关测试：

    docker compose run --rm --no-deps \
      -v "$PWD:/workspace:ro" \
      sp-generator pytest -q -p no:cacheprovider \
      /workspace/test_generation_harness.py \
      /workspace/test_validation_service.py \
      /workspace/test_verify_autofix.py

当前离线默认集合：

    docker compose run --rm --no-deps \
      -v "$PWD:/workspace:ro" \
      sp-generator pytest -q -p no:cacheprovider \
      /workspace/test_clarify.py \
      /workspace/test_invoke_mock.py \
      /workspace/test_design_confirmation.py \
      /workspace/test_verify_autofix.py

部署相关代码变更时追加：

    docker compose run --rm --no-deps \
      -v "$PWD:/workspace:ro" \
      sp-generator pytest -q -p no:cacheprovider \
      /workspace/test_validation_service.py \
      /workspace/test_deploy_validation.py

test_improvements.py 不属于默认单元测试。test_e2e.py 只在 Windows、配置完整、用户明确授权真实 E2E 时运行。

## 10. 最终验收标准

以下条件必须全部成立：

- 已确认设计被编译为一个可审计 QuerySpec。
- SP 和独立 Oracle 均由同一 QuerySpec 与 SchemaEvidence 生成。
- 所有物理表、字段和类型在保存前由目标数据库精确绑定。
- Invalid column / object 在候选阶段阻断。
- 安全、Schema、编译、契约和业务闸门顺序固定。
- 自动修复不改变业务不变量，最多两轮。
- 候选校验期间不修改当前有效产物。
- 多 SP 整批成功后才在一个 SQLite 事务中替换。
- Schema 或 bundle 变化会使旧 verified 状态失效。
- 默认离线测试全部通过。
- 未部署服务，未修改真实业务数据库。

## 11. 新会话执行顺序

新会话应从 Task 0 开始，按 Task 1 至 Task 9 顺序实施，不跳过字符化测试和 SQL Server 编译能力探针。

推荐首轮只完成 Task 0 至 Task 2，并提供：

- 实际变更文件清单。
- 新增失败测试与通过结果。
- QuerySpec / SchemaEvidence 的最终字段定义。
- 与当前 dirty worktree 的冲突处理说明。

第二轮再完成编译探针、候选流水线和原子替换。若真实 SQL Server 探针需要网络或外部数据库权限，应先停在默认跳过的集成测试和明确接口处，向用户申请授权，不得用猜测替代验证。

本方案不需要创建 Codex Skill 才能保证 SQL 正确性。Skill 或 SAP 知识包适合后续提升术语理解和表字段推荐；正确性底座必须由 QuerySpec、实时 Schema、确定性闸门和原子发布共同提供。
