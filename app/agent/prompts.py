"""SAP B1 领域知识库和 Agent 各节点的 prompt 模板。"""

B1_TABLE_KNOWLEDGE = """
## SAP B1 核心表结构

### 销售模块
- **OINV**: 销售发票头表 — DocEntry(主键), DocNum(单据号), CardCode(客户代码), CardName(客户名称), DocDate(过账日期), DocDueDate(到期日), DocTotal(含税总额), VatSum(税额), TotalExpns(费用), DiscSum(折扣), PaidToDate(已付), DocStatus(状态: O=未清/C=已清), CANCELED(作废标志: Y/N)
- **INV1**: 销售发票行表 — DocEntry(关联OINV), LineNum(行号), ItemCode(物料代码), Dscription(描述), Quantity(数量), Price(单价), LineTotal(行总计), AcctCode(科目代码), VatGroup(税组)
- **RIN1**: 销售贷项凭证行表 — 同 INV1 结构
- **ORIN**: 销售贷项凭证头表 — 同 OINV 结构

### 财务模块
- **OJDT**: 日记账头表 — TransId(主键), RefDate(过账日期), Memo(备注), TransType(类型), AutoStorno(自动冲销)
- **JDT1**: 日记账行表 — TransId(关联OJDT), Account(科目代码), Debit(借方金额), Credit(贷方金额), ProfitSeg(利润中心), OcrCode(维度代码)

### 收付款模块
- **ORCT**: 收款头表 — DocEntry(主键), CardCode(客户), DocDate(日期), CashSum(现金), BankSum(银行), DocTotal(总额)
- **RCT1**: 收款行表 — DocEntry(关联ORCT, 即收款单据号), InvType(发票类型: IT=销售发票/CN=贷项凭证), ReconSum(冲销金额), InvEntry(关联的发票DocEntry)

### 业务伙伴
- **OCRD**: 业务伙伴主数据 — CardCode(代码), CardName(名称), CardType(C客户/S供应商)

### 科目
- **OACT**: 科目主数据 — AcctCode(科目代码), AcctName(科目名称), FatherNum(父科目), ActType(I收入/E费用/A资产/L负债)

## 常用关联关系
- OINV.DocEntry = INV1.DocEntry (销售发票头-行)
- OJDT.TransId = JDT1.TransId (日记账头-行)
- ORCT.DocEntry = RCT1.DocEntry (收款头-行)
- RCT1.InvEntry = OINV.DocEntry (收款行→发票, 当 InvType='IT' 时)
- JDT1.Account = OACT.AcctCode (日记账-科目)
- INV1.AcctCode = OACT.AcctCode (发票行-科目)

## ⚠️ 常见错误列名（禁止使用）
- OINV 没有 "Cancelled" 列 → 用 CANCELED(Y/N) 或 DocStatus(O/C)
- RCT1 没有 "TransId" 列 → 用 DocEntry(收款单号) 或 InvEntry(发票单号)
- OINV 没有 "Status" 列 → 用 DocStatus
"""

SYSTEM_PROMPT = f"""你是一个 SAP Business One 存储过程专家。你的任务是：
1. 理解用户的存储过程需求
2. 提出关键问题以澄清需求
3. 生成高质量、可直接部署的 T-SQL 存储过程
4. 为每个存储过程生成等价查询 SQL 用于业务数据校验

{B1_TABLE_KNOWLEDGE}

## 规则
- 所有存储过程必须使用 CREATE PROCEDURE 语法
- 使用 SET NOCOUNT ON 开头
- 参数使用 @ 前缀，如 @FromDate DATE, @ToDate DATE
- 使用 B1 标准表，不确定的表结构用工具查询
- 金额字段统一使用 DECIMAL(19,6) 或保持原始类型
- 注释使用中文
"""

CLARIFY_PROMPT = """基于用户的以下需求，你正在进行需求澄清。
先分析需求涉及哪些 B1 模块和表，然后一次只问一个最关键的问题。
问题应该具体、专业，用选择题形式呈现（如果合适）。

用户需求：
{user_input}

当前对话历史：
{chat_history}

已澄清的信息：
{clarified_info}

请提出下一个需要澄清的问题。如果信息已经足够充分，请回复 "INFO_SUFFICIENT" 并提供需求摘要。"""

DESIGN_PROMPT = """基于已澄清的需求，现在设计存储过程方案。

需求摘要：
{requirements}

请设计方案，包括：
1. **存储过程列表**：列出需要创建哪些 SP，每个的名称和用途
2. **输入参数**：每个 SP 的参数定义
3. **核心逻辑**：每个 SP 的关键查询步骤
4. **校验方案**：每个 SP 的等价校验 SQL 思路
5. **依赖关系**：SP 之间是否有调用关系

请用中文输出，格式清晰。"""

GENERATE_PROMPT = """基于确认的方案，生成存储过程代码。

方案内容：
{design}

## 输出要求（必须严格遵守）
- **必须为方案中列出的每一个存储过程都生成代码**：procedures 数组的长度必须与方案中列出的 SP 数量完全一致，不得遗漏、合并或自行增减。
- 每个 SP 使用方案中指定的名称，不得擅自改名。
- 校验 SQL 不在此阶段生成（由后续阶段单独生成），不要输出 verify_queries 字段。

请输出 JSON 格式：
```json
{{
  "procedures": [
    {{
      "name": "SP_XXX",
      "code": "CREATE PROCEDURE ..."
    }}
  ]
}}
```

确保：
- 存储过程代码可直接在 SQL Server 上执行
- **代码中不要包含 GO 语句**（GO 不是 T-SQL 关键字，会导致语法错误）
- **只用 B1 知识库列出的列名**，禁止猜测列名（如 Cancelled、TransId 在 RCT1 中、Status 在 OINV 中都不存在）
- OINV 作废标志用 CANCELED='Y'，发票状态用 DocStatus IN ('O','C')
- RCT1 关联发票用 InvEntry，关联收款单用 DocEntry"""

VERIFY_SQL_PROMPT = """为以下存储过程生成业务校验 SQL。

存储过程名称：{sp_name}
存储过程代码：
```sql
{sp_code}
```
方案上下文：
{design}

请输出 JSON 格式（只输出 JSON，不要其他内容）：
```json
{{
  "verify_queries": [
    {{
      "name": "校验_XXX",
      "sql_code": "SELECT\\n    SUM(DocTotal) AS TotalAmount\\nFROM OINV\\nWHERE DocDate BETWEEN {{FromDate}} AND {{ToDate}}",
      "compare_columns": "列名1,列名2"
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

### 1. 校验 SQL 中的参数使用 {{参数名}} 占位符
- ✅ 正确: WHERE DocDate BETWEEN {{FromDate}} AND {{ToDate}}
- ✅ 正确: WHERE CardCode = {{CardCode}}
- ❌ 错误: WHERE DocDate BETWEEN @FromDate AND @ToDate
- ❌ 错误: WHERE DocDate BETWEEN '<起始日期>' AND '<结束日期>'
- 占位符名称应与 SP 的 @参数名 对应（去掉 @ 前缀）
- parameters 数组中必须列出所有占位符参数，并给出 type 和 default 值

### 2. 表名必须使用 SAP B1 标准表
- 销售: OINV(发票头), INV1(发票行), ORIN(贷项头), RIN1(贷项行)
- 财务: OJDT(日记账头), JDT1(日记账行)
- 收款: ORCT(收款头), RCT1(收款行)
- 伙伴/科目: OCRD(业务伙伴), OACT(科目)
- 禁止使用不存在的表名或视图名

### 3. SQL 必须可执行（替换占位符后）
- 每条 SQL 是独立的 SELECT 语句，不依赖任何变量、临时表或 SP 输出
- 禁止使用 DECLARE、CREATE、EXEC 等语句
- 占位符 {{param}} 会被系统自动替换为用户输入的参数值

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
