"""SAP B1 领域知识库和 Agent 各节点的 prompt 模板。"""

B1_TABLE_KNOWLEDGE = """
## SAP Business One 表结构知识

SAP Business One 官方知识只用于理解业务语义和寻找候选表。当前客户数据库的实时 schema
才是表名、字段名和数据类型的最高事实源；模型记忆与实时 schema 冲突时必须以实时 schema 为准。

以下仅为快速参考（常见易错点），不能替代对当前客户数据库的验证：

### 常见易错提醒
- OINV 没有 "Cancelled" 列 → 用 CANCELED(Y/N) 或 DocStatus(O/C)
- RCT1 没有 "TransId" 列 → 用 DocEntry(收款单号) 或 InvEntry(发票单号)
- OINV 没有 "Status" 列 → 用 DocStatus
- PCH1（采购发票行）与 PDN1（采购收货行）通过 BaseEntry/BaseLine 关联，不要错误关联到 IGN1（库存收发行）
- B1 头行表通用关联模式：头表.DocEntry = 行表.DocEntry
- 行表之间的单据流关联通常通过 BaseEntry + BaseLine + BaseType 字段

### 工具使用策略
- **所有最终会进入方案的表和字段**：必须调用 get_table_info_tool 验证
- **自定义字段（UDF，U_ 前缀）和自定义表（@ 前缀）**：必须以工具返回的实际名称为准
- **工具未返回的字段**：禁止猜测或编造
- **不确定表间关联关系**：调用 get_table_relations_tool 确认
- SAP B1 常见关联模式只能作为候选，不能覆盖现场 schema 证据
"""

SYSTEM_PROMPT = f"""你是一个 SAP Business One 存储过程专家。你的任务是：
1. 理解用户的存储过程需求
2. 提出关键问题以澄清需求
3. 生成高质量、可直接部署的 T-SQL 存储过程
4. 为每个存储过程生成基于同一业务契约、但实现独立的 Reference SQL 用于业务数据校验

{B1_TABLE_KNOWLEDGE}

## 核心原则
- **简洁优先**：用最简单的方式满足需求，不做过度设计
- 能用 1 个 SP 解决的不要拆成多个
- 能用 1 个 SELECT 解决的不要用临时表+多步
- 只关联需求必需的表，不"顺便"加额外逻辑

## 规则
- 所有存储过程必须使用 CREATE PROCEDURE 语法
- 使用 SET NOCOUNT ON 开头
- 参数使用 @ 前缀，如 @FromDate DATE, @ToDate DATE
- 所有最终使用的表和字段必须经过当前数据库 schema 验证
- 金额字段统一使用 DECIMAL(19,6) 或保持原始类型
- 注释使用中文
"""

CLARIFY_PROMPT = """基于用户需求生成下一项结构化决策（当前最多还能询问第 {q_num} 项，总上限 5 项）。

## 决策分级
- blocking：缺少答案就无法定义用户要求的功能、输入或输出。只有此类问题可以在当前阶段逐项询问。
- defaultable：存在合理且可解释的建议默认值，可在后续“关键项确认”阶段一次性确认。不要把它伪装成 blocking。
- 如果没有新的 blocking 决策，返回 sufficient。

不得依据“金额、状态、过滤、计算”等主题词机械分级；应判断答案是否真正阻塞功能定义。
不要询问 SP 划分、参数实现等设计细节。
`decision_key` 描述决策本身，必须是稳定的小写英文 snake_case；不得包含问题编号。
defaultable 的第一个选项必须是建议默认值，供关键项确认阶段兜底使用。

用户需求：
{user_input}

当前对话历史：
{chat_history}

已确认的信息：
{clarified_info}

已确认的结构化决策：
{clarify_decisions}

{last_question_hint}

只输出 JSON，不要输出 Markdown 或解释。二选一：

{{
  "action": "ask",
  "decision_key": "stable_decision_key",
  "decision_type": "blocking 或 defaultable",
  "question": "一个完整且单一的问题",
  "options": [
    {{"id": "A", "value": "完整选项含义"}},
    {{"id": "B", "value": "完整选项含义"}}
  ],
  "reason": "为什么该决策会阻塞，或为什么可以延后"
}}

或：

{{
  "action": "sufficient",
  "summary": "包含已确认决策完整语义的需求摘要"
}}"""

DECISION_PLAN_PROMPT = """一次性分析用户需求，生成完整的业务决策清单。

用户需求：
{user_input}

当前对话历史：
{chat_history}

规则：
- 最多 5 个决策，每个决策只表达一个问题。
- 本阶段只讨论业务语义。不得出现数据库、schema、表名、字段名、SQL、
  `表.字段` 或 SAP 技术对象名；物理绑定必须等用户确认业务方案后进行。
- 输出列使用面向业务的稳定名称，不得复用模型记忆中的 SAP 物理字段名。
- blocking：没有答案就无法确定功能、输入或输出。
- defaultable：存在合理建议值，可在关键项确认阶段集中确认。
- 不按“金额、状态、过滤”等主题词机械分类。
- 不询问 SP 拆分、SQL 写法等实现细节。
- 当前阶段每个存储过程只支持一个结果集；不得提供或推荐“汇总结果集 +
  明细结果集”等多结果集选项。需要汇总与明细时，必须定义为同一结果集的
  单一稳定形态，否则作为阻塞决策要求用户二选一。
- decision_key 必须稳定、使用小写英文 snake_case。
- defaultable 必须给 recommended_option_id。
- 如果信息已经充分，decisions 可以为空。
- 只输出 JSON，不要 Markdown。

格式：
{{
  "action": "plan",
  "requirements_summary": "保留用户原意的需求摘要",
  "decisions": [
    {{
      "decision_key": "stable_key",
      "decision_type": "blocking 或 defaultable",
      "question": "单一问题",
      "options": [
        {{"id": "A", "value": "完整选项含义"}},
        {{"id": "B", "value": "完整选项含义"}}
      ],
      "reason": "为什么需要该决策",
      "recommended_option_id": null,
      "contract_relevant": true
    }}
  ]
}}"""

ASSUMPTIONS_PROMPT = """基于已确认的需求，列出所有影响最终结果的关键业务假设，供用户逐项确认。

需求摘要：
{requirements}

澄清阶段已确认的决策（不得再次生成相同 key）：
{clarify_decisions}

澄清阶段识别并延后的可默认决策（必须优先纳入）：
{deferred_decisions}

## 输出要求
- 列出所有需要用户确认的关键项（通常 3~8 项）
- 只讨论业务口径，不得出现数据库、schema、表名、字段名、SQL 或
  `表.字段`；例如应问“税额采用发票税额”，不能问“选择哪个物理字段”。
- 每个关键项包含：标题（简短）、默认值/建议值、说明（为什么需要确认）
- 关键项应覆盖：过滤条件、计算口径、数据范围、状态判断、特殊处理逻辑等
- 涉及金额统计或跨来源对账时，必须逐项检查尚未确认的含税/不含税口径、
  币种口径、借贷方向与符号、作废/冲销处理、匹配粒度；有合理默认值也必须
  作为关键项列出，不能静默假设。
- 只列影响结果的项，不要列显而易见的内容
- 延后决策沿用其 decision_key 作为 key
- 不得重复已确认决策中的 decision_key
- 当前阶段每个存储过程只支持一个结果集；不得建议同时返回彼此独立的汇总
  与明细结果集。

## 输出 JSON 格式（只输出 JSON）
```json
{{
  "assumptions": [
    {{
      "key": "exclude_cancelled",
      "title": "排除已作废单据",
      "value": "是，排除 CANCELED='Y' 的单据",
      "reason": "作废单据通常不参与统计，需确认是否排除"
    }},
    {{
      "key": "amount_type",
      "title": "金额口径",
      "value": "不含税销售收入",
      "reason": "含税价款与不含税收入的业务含义不同，会直接影响对账结果"
    }}
  ]
}}
```"""

DESIGN_PROMPT = """基于已确认的需求和关键项，设计存储过程方案。

需求摘要：
{requirements}

用户确认的关键项：
{confirmed_assumptions}

## 设计原则（必须遵守）
- **最简方案优先**：能用 1 个 SP 解决的，绝不拆成多个。只有当需求明确包含多个独立功能时才拆分。
- **避免过度设计**：不要添加需求中没有提到的功能（如额外的汇总、明细拆分、错误处理分支）。
- **严格遵循用户确认的关键项**：按用户确认的过滤条件、计算口径等来设计。

## 表结构与关联关系（必须遵守）
- SAP B1 知识只用于寻找候选；必须用工具验证方案中的每一张表及其实际字段。
- **不要猜测关联关系**。如果数据库未声明外键，应把常见关联模式作为业务规则明确写入方案，而不是伪装成数据库事实。
- 常见关联模式：头行表通过 DocEntry 关联；行表之间的单据流通过 BaseEntry + BaseLine + BaseType 关联。
- 自定义字段（U_ 前缀）必须通过工具验证存在性。

## 输出内容（需要以下 3 项）
1. **存储过程列表**：
   - 名称 + 一句话用途
   - 操作类型：query、insert、update、delete 或 mixed
   - 写入型 SP 必须明确列出会修改的正式表；query 不得修改正式表
   - 参数定义
   - 业务逻辑描述（做什么、涉及哪些表、核心计算/过滤逻辑）

2. **校验逻辑描述**（必须使用以下结构化格式，每个SP对应一组校验）：

<!-- VERIFY_LOGIC_START -->
- SP名称: sp_XXX
  - 校验1: (校验名称) | (校验方式描述，如：直接查询OINV按日期汇总DocTotal) | (对比列，如：TotalAmount)
  - 校验2: (校验名称) | (校验方式描述) | (对比列)
- SP名称: sp_YYY
  - 校验1: (校验名称) | (校验方式描述) | (对比列)
<!-- VERIFY_LOGIC_END -->

示例：
<!-- VERIFY_LOGIC_START -->
- SP名称: sp_InvoiceSummary
  - 校验1: 验证发票总金额 | 直接查询OINV按DocDate汇总DocTotal，WHERE条件与SP一致（排除CANCELED='Y'） | TotalAmount
  - 校验2: 验证发票数量 | 直接COUNT OINV满足条件的记录数 | InvoiceCount
<!-- VERIFY_LOGIC_END -->

3. **注意事项**（如有）：特殊处理逻辑或边界情况的说明

请用中文输出，简洁明了。不要输出"需确认的假设"（已在上一步确认完毕）。"""

QUERY_SPEC_PROMPT = """把下面的已确认设计编译为 QuerySpec JSON。

已确认设计：
{design}

QuerySpec JSON Schema：
{schema}

严格要求：
- 只能结构化上面的已有内容，不得补充、推测或更改业务规则。
- 信息不足时不要猜测；返回的 JSON 应因缺少必填字段而被拒绝。
- 只输出 JSON，不要 Markdown。
- 字段、类型、嵌套结构和枚举值必须严格遵循上面的 JSON Schema。
- 所有引用必须使用已声明的 alias、参数和输出；不得输出任何额外字段。
"""

QUERY_SPEC_REPAIR_PROMPT = """上一次 QuerySpec 输出未通过严格校验。请只修正列出的结构错误。

已确认设计：
{design}

QuerySpec JSON Schema：
{schema}

上一次输出：
{response}

校验错误：
{errors}

严格要求：
- 只修正校验错误，不得补充、推测或更改业务规则。
- 返回完整的 QuerySpec JSON，而不是局部片段或补丁。
- 字段、类型、嵌套结构和枚举值必须严格遵循 JSON Schema。
- 所有引用必须使用已声明的 alias、参数和输出；不得输出任何额外字段。
- 只输出 JSON，不要 Markdown。
"""

QUERY_SPEC_DESIGN_PROMPT = """根据已确认需求直接生成设计契约，不要先生成 Markdown 方案。

需求：
{requirements}

已确认的澄清决策：
{clarify_decisions}

已确认的关键项：
{confirmed_assumptions}

现有契约与用户修改要求（首次设计为空）：
{revision_context}

QuerySpec JSON Schema：
{schema}

严格要求：
- 可以调用 Schema 工具核对表和字段；最终只输出 JSON。
- 最终格式必须是 {{"summary":"中文方案摘要","query_spec":{{...}}}}。
- query_spec 必须严格符合 JSON Schema。
- contract_version 必须为 3。
- 每个参数必须声明 boundary：普通参数用 none；日期起点用 inclusive；
  日期终点若覆盖整个自然日必须用 inclusive_full_day。
- 每条 filters 必须是原子业务条件并声明 operator；常量条件写入 literal_values，
  参数条件写入 parameter_refs。日期自然日范围用 full_day_range 和两个日期参数。
- 只能使用当前数据库中已核对的表和字段。
- 每个 procedure 必须声明 result_contract.cardinality（one/many/none）。
- one 结果使用 scalar；many 结果使用 keyed_rows、multiset_rows 或 aggregate。
- aggregate 必须声明 metrics，区分 actual_column 与 expected_column。
- keyed_rows 必须声明 key_columns 和 compare_columns。
- multiset_rows 用于没有稳定业务键的多行结果。
- zero_rows 只能作为 supplemental 不变量，使用 evidence_columns 返回异常明细，
  不能替代直接结果对账，也不要生成“结果必须非空”规则。
- change_set 必须声明 writes 中唯一对应的 target_table 和目标表物理 compare_columns；
  多个写入目标必须各有一条规则。
- 查询型过程至少有一条 direct 结果对账规则。
- 所有规则引用的业务列只能使用 outputs.name。
- 数据库物理字段只能出现在 source_columns、joins、filters 或 grain。
- 用户已经确认的业务决策不得被改写。
- 不得增加用户没有要求的输出、过滤、写入或存储过程。
"""

QUERY_SPEC_DESIGN_REPAIR_PROMPT = """下面的设计契约未通过严格校验，请只修正列出的契约问题。

已确认需求：
{requirements}

已确认决策：
{decisions}

上一次 DesignEnvelope：
{response}

结构化错误：
{errors}

QuerySpec JSON Schema：
{schema}

严格要求：
- 返回完整 {{"summary":"...","query_spec":{{...}}}} JSON。
- contract_version 必须为 3，参数必须显式声明 boundary。
- filters 必须保留 operator、literal_values、column_refs 和 parameter_refs。
- 不得修改用户确认的业务口径。
- 修正 result_contract 与验证模式的适用性；many 不得使用 scalar。
- aggregate 使用 metrics；keyed_rows 使用 key_columns/compare_columns；
  zero_rows 使用 evidence_columns 且只能作为 supplemental。
- 查询型过程必须保留至少一条 direct 对账规则。
- 所有规则列引用只能使用 outputs.name。
- 物理字段与输出名只有唯一对应时才可改为输出名；有歧义时不得猜测。
- 只输出 JSON，不要 Markdown。
"""


PROCEDURE_CANDIDATE_PROMPT = """根据下面唯一的业务契约和实时 Schema 证据，生成一个 SQL Server 存储过程候选。

完整 QuerySpec：
{query_spec}

本次 ProcedureSpec：
{procedure_spec}

SchemaEvidence（fingerprint={schema_fingerprint}）：
{schema_evidence}

要求：
- QuerySpec 决定业务语义，SchemaEvidence 决定物理表、字段和类型。
- 只能使用 ProcedureSpec 声明的来源表和写表，必须使用 schema-qualified 名称。
- 不得增加、删除或更改参数、输出、筛选、粒度、写入范围和校验规则。
- 禁止动态 SQL、GO 和未声明副作用。
- 只输出 JSON：{{"name":"过程名","code":"完整 CREATE PROCEDURE SQL"}}。
"""


ORACLE_CANDIDATE_PROMPT = """根据下面唯一的业务契约和实时 Schema 证据，独立生成业务校验 Oracle SQL 候选。

完整 QuerySpec：
{query_spec}

本次 ProcedureSpec：
{procedure_spec}

SchemaEvidence（fingerprint={schema_fingerprint}）：
{schema_evidence}

程序确定性编译的 Oracle 任务（输出结构必须精确匹配）：
{oracle_tasks}

独立性要求：
- 你没有也不得请求存储过程源码；不得从 SP 实现推导 SQL。
- QuerySpec 决定业务语义，SchemaEvidence 决定物理表、字段和类型。
- 每条 verification_rule 必须恰好生成一条规则，名称必须一致。
- SQL 仅允许单条 SELECT 或 WITH...SELECT，必须使用 schema-qualified 名称。
- 参数必须使用 ProcedureSpec 中声明的 SQL Server 原生名称（例如 @FromDate）；
  不得使用花括号占位符、问号占位符或拼接固定值。
- mode、role、比较列、键、聚合指标、容差和写入范围由程序从 QuerySpec
  编译，不要重复解释或修改。
- scalar/aggregate Oracle 必须返回任务要求的单行指标列。
- keyed_rows/multiset_rows Oracle 必须返回任务要求的业务输出别名。
- zero_rows 必须返回异常明细证据；禁止用 COUNT(*) 聚合成一行。
- 只输出 JSON：{{"verify_queries":[{{"name":"规则名","sql_code":"SELECT..."}}]}}。
"""


REPAIR_PROCEDURE_CANDIDATE_PROMPT = """只修复下面存储过程候选的确定性错误。

ProcedureSpec：
{procedure_spec}

SchemaEvidence（fingerprint={schema_fingerprint}）：
{schema_evidence}

结构化错误：
{errors}

当前 SQL：
{sql}

不得改变过程名、参数签名、来源表、写表、输出、筛选、粒度或业务含义。
只输出 JSON：{{"fixed_sql":"完整 CREATE PROCEDURE SQL"}}。
"""


REPAIR_ORACLE_CANDIDATE_PROMPT = """只修复下面独立 Oracle 候选的确定性错误。

ProcedureSpec：
{procedure_spec}

不可变 VerificationPlan：
{verification_plan}

SchemaEvidence（fingerprint={schema_fingerprint}）：
{schema_evidence}

结构化错误：
{errors}

当前 Oracle 候选：
{verify_queries}

不得改变规则名称、mode、输出形状、筛选、粒度或业务含义。不得参考存储过程源码。
参数必须使用 ProcedureSpec 中声明的 SQL Server 原生名称（例如 @FromDate）。
zero_rows 必须返回 VerificationPlan 的异常证据列，禁止用 COUNT(*) 聚合成一行。
其他模式必须精确返回 VerificationPlan 的 expected_schema。
契约字段由程序从 QuerySpec 注入，不要输出或修改。
只输出 JSON：{{"verify_queries":[{{"name":"规则名","sql_code":"SELECT..."}}]}}。
"""


RELATIONAL_PLAN_V3_PROMPT = """把业务合同编译成受限 RelationalPlan JSON。

生成角色：{role}

SemanticContract（唯一业务语义来源）：
{semantic_contract}

SchemaBinding（唯一物理对象来源）：
{schema_binding}

RelationalPlan JSON Schema：
{plan_schema}

严格要求：
- 只输出完整 RelationalPlan JSON，不要 Markdown。
- 只能使用 SchemaBinding 中的 entity_id 和 field_binding_id。
- SemanticContract.filters.field_ids 对应 SchemaBinding.fields.semantic_id；
  每条结构化过滤必须逐条落实 operator、parameter_ids 和 literal_values，不能只参考自然语言。
- 输出列的名称、顺序、类型必须与消息末尾给出的确定性 result_schema 完全一致；
  Procedure 使用完整输出，Reference Fact 可以只使用其独立事实投影。
- 只能使用 scan/join/filter/project/aggregate/union_all/sort。
- 表达式只能使用 column/output/parameter/literal/binary/unary/function/case/cast。
- binary 只允许 =、<>、>、>=、<、<=、AND、OR、+、-、*、/、LIKE；
  unary 只允许 NOT、IS NULL、IS NOT NULL、NEGATE；
  function 只允许 ABS、AVG、COALESCE、CONCAT、COUNT、DATEADD、DATEDIFF、
  DATEFROMPARTS、EOMONTH、LOWER、LTRIM、MAX、MIN、MONTH、NULLIF、RTRIM、
  SUM、UPPER、YEAR。
- SQL Server 编译结果与 result_schema 类型不一致时，只能使用受控
  cast 表达式：{{"kind":"cast","target_type":"date","args":[一个表达式]}}。
  禁止把 CAST/CONVERT 写成 function，禁止任意 SQL 类型文本。
- 派生输出可以按 SemanticContract 公式引用其他输出名；系统会确定性编译为嵌套
  project，禁止改写或重复业务公式。
- 禁止输出 SQL 文本、表名、列名、临时表、动态 SQL 或存储过程。
- 日期终点 boundary=inclusive_full_day 时，必须表达为
  column < DATEADD(day, 1, @结束参数)，不能使用 <= @结束参数。
- join 必须遵循 SchemaBinding.joins，禁止添加未经确认的关系。
- 聚合只在业务合同明确要求时使用；不得增加输出或过滤。
- sort 只能引用其输入已经输出的名称，不得在聚合查询中偷偷加入非输出列。
- role=reference 时独立表达业务事实；role=procedure 时同样只依据本消息中的
  SemanticContract 与 SchemaBinding，不会提供也不得请求 Reference SQL。
"""


GENERATE_PROMPT = """基于确认的方案，生成存储过程代码。

方案内容：
{design}

当前客户数据库实时 schema：
{schema_context}

## 代码风格（必须遵守）
- **简洁高效**：用最少的代码实现需求，能一个 SELECT 解决的不要拆成临时表+多步查询。
- **避免过度工程**：不加需求之外的错误处理、不加多余的 NULL 判断、不加未要求的输出列。
- **最少 JOIN**：只关联需求必需的表，不要"顺便"加入额外的关联。
- 写入型 SP 的 INSERT/UPDATE/DELETE 目标必须直接使用完整表名，不使用目标表别名。

## 输出要求
- **必须为方案中列出的每一个存储过程都生成代码**，不得遗漏、合并或增减。
- 每个 SP 使用方案中指定的名称。
- 校验 SQL 不在此阶段生成，不要输出 verify_queries 字段。

请输出 JSON 格式：
```json
{{
  "procedures": [
    {{
      "name": "SP_XXX",
      "operation_type": "query",
      "code": "CREATE PROCEDURE ..."
    }}
  ]
}}
```

确保：
- 存储过程代码可直接在 SQL Server 上执行
- **代码中不要包含 GO 语句**（GO 不是 T-SQL 关键字，会导致语法错误）
- 只能使用上方实时 schema 中存在的表和字段；schema 未列出的标识符不得猜测
- 注意常见易错列名：OINV 作废标志用 CANCELED='Y'（非 Cancelled），发票状态用 DocStatus（非 Status）
- RCT1 关联发票用 InvEntry，关联收款单用 DocEntry
- 自定义字段（U_ 前缀）如未出现在实时 schema 中，不要使用"""

VERIFY_SQL_PROMPT = """为以下存储过程生成业务校验 SQL。

存储过程名称：{sp_name}

请仅依据已确认的业务方案、参数和输出契约生成独立 Oracle SQL。
不要假设或复制存储过程内部实现。

SP 输出投影（仅用于识别实际输出列名和别名）：
{sp_output_projection}

不得从该投影推断或复制数据来源、筛选条件和业务实现。

方案上下文：
{design}

当前客户数据库实时 schema：
{schema_context}

## ⚠️ 必须遵循的校验逻辑（来自设计方案，不可自行发挥）
{verify_logic}

**重要**：上面列出的校验逻辑是设计方案中明确指定的，你必须严格按照这些描述来生成校验 SQL。
- 校验数量必须与上面列出的一致
- 校验名称必须与上面列出的一致
- 校验方式必须与上面描述的一致
- 对比列必须与上面指定的一致
- 不要增加额外的校验项，不要遗漏指定的校验项
- 查询型 SP 至少生成一条 scalar、aggregate 或 keyed_rows 直接结果对账
- scalar 仅用于 SP 本身和校验 SQL 都恰好返回一行的情况
- SP 返回明细、校验 SQL 返回 SUM/COUNT/AVG 等单行汇总时，必须使用 aggregate，禁止使用 scalar
- aggregate 必须提供 actual.operation；除 count_rows 外还必须提供 actual.column，列名必须与 SP 输出别名完全一致
- aggregate.actual.operation 只允许 sum、count_rows、count_distinct、min、max、avg
- keyed_rows 的 key_columns 和 compare_columns 必须与两边真实输出列一致；列名不同时必须提供 column_mapping（SP输出列 → 校验SQL列）
- 禁止把数据库源列名当作 SP 输出列名；必须以已确认方案中的 SP 输出契约为准
- 写入型 SP 的每个“目标表 + 操作”必须各生成一条 change_set 规则
- 每条 change_set 只能声明一个 affected_tables 对象，并包含 table、operation、key_columns、compare_columns 和 max_affected_rows
- change_set 的 snapshot_sql 返回业务键和比较字段；sql_code 返回 Expected Change Set
- Expected Change Set 必须返回 ChangeType、业务键、Before_字段和 After_字段；INSERT 的 Before_字段为 NULL，DELETE 的 After_字段为 NULL
- zero_rows 规则只能补充业务不变量，不能替代直接对账

请输出 JSON 格式（只输出 JSON，不要其他内容）：
```json
{{
  "verify_queries": [
    {{
      "name": "校验_XXX",
      "sql_code": "SELECT\\n    SUM(DocTotal) AS TotalAmount\\nFROM OINV\\nWHERE DocDate BETWEEN @FromDate AND @ToDate",
      "compare_columns": "TotalAmount",
      "validation_spec": {{
        "mode": "aggregate",
        "required": true,
        "actual": {{
          "operation": "sum",
          "column": "SP输出金额列",
          "output_column": "TotalAmount"
        }},
        "compare_columns": ["TotalAmount"],
        "tolerance": {{"TotalAmount": 0.01}}
      }}
    }}
  ],
  "parameters": [
    {{
      "name": "FromDate",
      "type": "DATE",
      "default": "2024-01-01"
    }},
    {{
      "name": "ToDate",
      "type": "DATE",
      "default": "2024-12-31"
    }}
  ]
}}
```

## 关键要求（必须严格遵守）

### 1. 校验 SQL 使用 SQL Server 原生命名参数
- change_set 示例：affected_tables 为 [{{"table":"dbo.TestOrders","operation":"delete","key_columns":["DocEntry"],"compare_columns":["DocTotal"],"max_affected_rows":1}}]
- 对应 sql_code 返回 ChangeType、DocEntry、Before_DocTotal、After_DocTotal，snapshot_sql 返回 DocEntry、DocTotal
- ✅ 正确: WHERE DocDate BETWEEN @FromDate AND @ToDate
- ✅ 正确: WHERE CardCode = @CardCode
- ❌ 错误: WHERE DocDate BETWEEN {{FromDate}} AND {{ToDate}}
- ❌ 错误: WHERE DocDate BETWEEN '<起始日期>' AND '<结束日期>'
- 参数名称必须与 SP 的 @参数名完全对应
- parameters 数组中必须列出所有参数，并给出 type 和 default 值

### 2. 表名和列名必须准确
- 只能使用上方实时 schema 中存在的表和字段
- 注意常见易错点：OINV 用 CANCELED(Y/N) 而非 Cancelled，用 DocStatus 而非 Status
- 禁止使用不存在的表名、视图名或列名

### 3. SQL 必须可参数化执行
- 每条 SQL 是独立的 SELECT 语句，不依赖任何变量、临时表或 SP 输出
- 禁止使用 DECLARE、CREATE、EXEC 等语句
- @参数由系统按 QuerySpec 类型安全绑定，不得自行拼接或改写参数值

### 4. 校验逻辑简单明确
- 优先用 SUM/COUNT/AVG 等聚合做总量校验
- 每个校验只验证一个指标，不要多个指标混在一起
- 避免复杂的多层嵌套子查询

### 5. 参数默认值要合理
- 日期类型：使用具体日期如 "2024-01-01"，或相对日期如最近 30 天的范围
- 字符串类型：使用有代表性的示例值（如客户代码 "C001"）
- 数值类型：使用合理的数值

### 6. SQL 格式化（重要！）
- sql_code 必须像手写 SQL 一样格式化，每个子句独占一行
- SELECT / FROM / WHERE / GROUP BY / ORDER BY / HAVING 等关键字都换行
- 字段列表用缩进对齐
- 正确示例：
  SELECT\\n    Col1,\\n    SUM(Col2) AS Total\\nFROM TableName\\nWHERE Condition1\\n    AND Condition2\\nGROUP BY Col1\\nORDER BY Col1
- 错误示例：
  SELECT Col1, SUM(Col2) FROM TableName WHERE Condition1 AND Condition2 (禁止单行)"""

VERIFY_PROMPT = """分析以下校验结果。
存储过程输出：
{sp_result}

校验 SQL 输出：
{verify_result}

要校验的列：{compare_columns}

请判断：
1. 数据是否一致
2. 如果有差异，分析可能原因
3. 是否需要修正存储过程逻辑"""

DESIGN_FEEDBACK_PROMPT = """你是 SAP B1 存储过程方案设计助理。用户对以下设计方案给出了反馈，请分析用户意图。

## 当前设计方案
{design}

## 用户反馈
{user_feedback}

## 你的任务
判断用户意图，从以下三种情况中选择一种：

1. **CONFIRM** — 用户表示同意、确认、可以继续生成代码。
   常见表达：可以、确认、好的、行、没问题、ok、yes、生成、继续、开始、就这样、没意见、不错、挺好、就这样吧、同意、没问题了、往下走

2. **MODIFY** — 用户提出了具体的修改意见、疑问或调整要求。
   常见表达：能不能...、修改...、减少/增加SP数量、字段不对、换个逻辑、不要某个SP、参数不对、有问题、这个字段在表里吗、太多了/太少了、加一个/去掉

3. **IRRELEVANT** — 用户说的内容与当前设计方案完全无关，或者是模糊无法操作的反馈。

请输出 JSON 格式（只输出 JSON）：
```json
{{
  "intent": "CONFIRM",
  "reply": "（给用户的回复。CONFIRM/IRRELEVANT 时简短回复；MODIFY 时说明修改了什么，方案已更新）",
  "new_design": "（MODIFY 时输出修改后的完整新方案，保持原有格式；CONFIRM/IRRELEVANT 时为空字符串）"
}}
```"""

FIX_SP_PROMPT = """以下存储过程校验失败，请根据错误信息修复代码。

## 存储过程名称
{sp_name}

## 当前代码
```sql
{sp_code}
```

## 校验错误
{errors}

## 当前客户数据库实时 schema
{schema_context}

## 修复要求
- 保持存储过程的整体功能和业务逻辑不变
- **只修复导致校验失败的问题**
- 输出完整的 CREATE PROCEDURE 代码
- 不要包含 GO 语句
- 只能使用实时 schema 中存在的表和字段；不能用近似名称静默替换业务含义

请输出 JSON 格式（只输出 JSON）：
```json
{{
  "fixed_code": "修复后的完整 CREATE PROCEDURE 代码"
}}
```"""


FIX_VERIFY_SQL_PROMPT = """以下校验 SQL 执行失败，请只修复校验 SQL 本身。

## 校验名称
{query_name}

## 当前校验 SQL
```sql
{sql_code}
```

## 执行错误
{error}

## 当前客户数据库实时 schema
{schema_context}

## 修复要求
- 保持原校验目的、输出列和筛选逻辑不变
- 只修复 SQL Server 语法、保留字或字段引用错误
- 保留 SQL Server 原生 @参数名，不要改成固定值或花括号占位符
- 只输出 JSON，不要包含 Markdown

请输出：
{{
  "fixed_sql": "修复后的完整校验 SQL"
}}
"""
RESULT_CONTRACT_PROMPT = """你正在构建存储过程的“最终结果契约”。

只回答用户最终要得到什么，不回答数据从哪里来、如何查询或如何计算。
禁止出现数据库、schema、表、列、SQL、实体、事实、源字段和表达式。
输出必须严格符合给定 JSON Schema，且只输出 JSON。

要求：
- 只输出差异、异常、未匹配记录的业务口径必须使用 result_mode=exception_rows，
  并声明 effect=result_selection 的业务政策；不得与 full_rows 同时出现。
- full_rows 表示输出全部结果，不得声明“仅输出差异/异常”的 result_selection 政策。
- 输出列逐一对应用户需求，不增添实现辅助列。
- 输出 name 必须使用业务名称（如 DocumentId、DocumentNumber、DocumentAmount），
  禁止使用 DocEntry、DocNum、DocTotal 等数据库物理字段名。
- 非 scalar_summary 结果必须从已声明 outputs.symbol 中选择 grain_output_symbols。
- business_policies 必须与所有 contract_relevant 的已确认 decision key 一一对应，
  不得遗漏，也不得新增未经确认的政策。
- business_policies.value 必须逐字复制已确认 decision 的 value，不得概括、改写或缩短。
- 每项政策必须填写 key、value、meaning 和 effect。effect 只能按政策实际影响选择：
  source_population=限定某个业务事实的数据总体；
  calculation=改变某个事实维度或指标的计算；
  matching=改变事实之间如何关联、以及保留左侧/匹配/双侧记录；
  result_selection=决定最终保留哪些结果，包括差异阈值或金额容差；
  presentation=只影响契约展示，不改变数据计算。
- money_tolerance 是金额比较容差；没有金额也保留合理的非负值。
- 单一“截止日期/截至日期”参数的 boundary 使用 inclusive，后续采用 lte 过滤；
  inclusive_full_day 只用于同时存在开始、结束两个参数的自然日区间结束参数，
  开始参数必须为 inclusive。
"""


FACT_BLUEPRINT_PROMPT = """你正在构建存储过程的“业务事实蓝图”。

只描述哪些相互独立的业务事实能够证明最终结果，以及事实之间如何匹配。
不要描述数据库、schema、物理表列、SQL 或最终表达式。
输出必须严格符合给定 JSON Schema，且只输出 JSON。

要求：
- 每个事实代表可从底层业务记录独立证明的数据来源。
- 禁止创建 final_result、sp_result、procedure_result 一类伪事实。
- 每个事实的 grain 只能引用本事实已经声明的 dimension symbol。
- 每个维度必须冻结 logical_type；若映射结果输出，类型必须完全一致。
- 事实关联两侧维度的 logical_type 必须一致。
- 多事实必须用 joins 形成连通图。
- entity_symbols 是下一阶段必须落实的业务实体符号。
- result_output_symbol 只能引用结果契约已经声明的 output symbol。
- 每个最终 output 必须恰好归属一次：
  直接来自事实值的，在对应 dimension 或 measure 填 result_output_symbol；
  需要跨事实计算、差额或状态公式的，写入 derived_output_symbols。
- 不允许遗漏输出，也不允许同一输出同时绑定多个事实值或又声明为派生输出。
- policy_targets 的字段由程序根据 ResultContract 的每项政策动态生成，全部必填，
  不得删除、改名或增加字段。
- source_population 槽位填写一个或多个目标事实；
  calculation 槽位填写一个或多个目标事实值；
  matching 槽位填写一个或多个目标 join 及其匹配模式。
- result_selection 和 presentation 槽位只填写空对象；其 binding kind 和 policy key
  由程序写入，模型不得重复填写或改变。
- matching 表示改变事实关联及保留哪侧记录，必须指向 joins；
  不能因为它也影响最终展示就把它当作 presentation。
- 日期参数等普通查询条件不是业务政策，不要创建政策目标。
"""


COMPUTATION_BLUEPRINT_PROMPT = """你正在构建存储过程的“业务计算蓝图”。

业务公式必须在底层来源字段之前冻结。你只能填写程序动态生成的事实值槽位、
结果输出槽位和结果过滤槽位；不能填写或修改 fact、value、output 的目标 ID，
不能出现数据库、schema、物理表列或 SQL。输出必须严格符合给定 JSON Schema，
且只输出 JSON。

要求：
- 每个非 count_rows 事实值必须声明业务输入，并用结构化 expression 消费全部输入。
- input 只表示底层业务含义，例如数量、单位成本、借方金额、贷方金额；
  不得把净额、差额、库存金额等公式结果伪装成 input。
- 事实公式只能引用本槽位声明的 input，不允许引用参数、输出或其他事实值。
- 结果公式只能引用已冻结 fact_value、其他输出、参数和 literal。
- 结果过滤只能引用输出、参数和 literal。
- 数量乘成本、贷方减借方、含税额减税额、外币额乘汇率、两端金额相减等，
  必须完整表达为结构化公式，不能用一个派生 input 绕过。
- exception_rows 必须填写 boolean result_filter；其他模式不得填写。
"""


SOURCE_REQUIREMENTS_PROMPT = """你正在构建存储过程的“底层业务源需求”。

只描述实现已冻结输入所需的单粒度业务实体和业务过滤。
不要猜测数据库、schema、物理表名、列名或 SQL，也不要把派生金额、期间、
净额、差额等计算结果伪装成底层字段。
输出必须严格符合给定 JSON Schema，且只输出 JSON。

要求：
- 单据头、单据行、凭证头、凭证明细、科目分类等不同粒度必须拆成独立实体。
- required_inputs 的每个字段都是输入义务编译器生成的必填槽位；只能补充实体归属、
  业务含义和已冻结的可空性，不能删除、改名或增加槽位。
- required_inputs.entity_symbol 必须引用本阶段声明的实体。
- 必须完整实现事实蓝图中出现的 entity_symbols。
- ordinary_filters 只用于参数或普通业务条件，不能填写或发明 policy_key。
- policy_filters 的每个字段都是上游义务编译器生成的必填槽位，必须逐项实现；
  不能删除、改名、增加槽位，也不能修改其政策 key 和目标事实。
- ordinary_filters 的 source_symbol 只能引用 required_inputs 的动态槽位；
  参数只能引用结果契约参数。
- 单一截止日期使用 lte 且只引用一个 inclusive 参数；between 和
  full_day_range 必须引用开始、结束两个参数，其中 full_day_range 的边界必须是
  inclusive / inclusive_full_day。
- 当过滤使用 required=false 且 default=null 的单个可选参数时，必须设置
  skip_when_parameter_null=true，表示参数为空时不应用该过滤；必填参数不得设置。
"""
