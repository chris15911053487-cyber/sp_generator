"""SAP B1 领域知识库和 Agent 各节点的 prompt 模板。"""

B1_TABLE_KNOWLEDGE = """
## SAP B1 核心表结构

### 销售模块
- **OINV**: 销售发票头表 — DocEntry(主键), DocNum(单据号), CardCode(客户代码), CardName(客户名称), DocDate(过账日期), DocTotal(含税总额), VatSum(税额), TotalExpns(费用), DiscSum(折扣), PaidToDate(已付), DocStatus(状态: O未清/C已清)
- **INV1**: 销售发票行表 — DocEntry(关联OINV), LineNum(行号), ItemCode(物料代码), Dscription(描述), Quantity(数量), Price(单价), LineTotal(行总计), AcctCode(科目代码), VatGroup(税组)
- **RIN1**: 销售贷项凭证行表 — 同 INV1 结构
- **ORIN**: 销售贷项凭证头表 — 同 OINV 结构

### 财务模块
- **OJDT**: 日记账头表 — TransId(主键), RefDate(过账日期), Memo(备注), TransType(类型), AutoStorno(自动冲销)
- **JDT1**: 日记账行表 — TransId(关联OJDT), Account(科目代码), Debit(借方金额), Credit(贷方金额), ProfitSeg(利润中心), OcrCode(维度代码)

### 收付款模块
- **ORCT**: 收款头表 — DocEntry(主键), CardCode(客户), DocDate(日期), CashSum(现金), BankSum(银行), DocTotal(总额)
- **RCT1**: 收款行表 — DocEntry(关联ORCT), InvType(发票类型), ReconSum(冲销金额)

### 业务伙伴
- **OCRD**: 业务伙伴主数据 — CardCode(代码), CardName(名称), CardType(C客户/S供应商)

### 科目
- **OACT**: 科目主数据 — AcctCode(科目代码), AcctName(科目名称), FatherNum(父科目), ActType(I收入/E费用/A资产/L负债)

## 常用关联关系
- OINV.DocEntry = INV1.DocEntry (销售发票头-行)
- OJDT.TransId = JDT1.TransId (日记账头-行)
- ORCT.DocEntry = RCT1.DocEntry (收款头-行)
- JDT1.Account = OACT.AcctCode (日记账-科目)
- INV1.AcctCode = OACT.AcctCode (发票行-科目)
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

GENERATE_PROMPT = """基于确认的方案，生成存储过程代码和校验 SQL。

方案内容：
{design}

请输出 JSON 格式：
```json
{{
  "procedures": [
    {{
      "name": "SP_XXX",
      "code": "CREATE PROCEDURE ...",
      "verify_queries": [
        {{
          "name": "校验_XXX",
          "sql_code": "SELECT ... FROM OINV ...",
          "compare_columns": "列名1,列名2"
        }}
      ]
    }}
  ]
}}
```

确保：
- 存储过程代码可直接在 SQL Server 上执行
- 校验 SQL 直接查询源表（如 OINV、INV1、OJDT），不通过视图
- compare_columns 用逗号分隔需要比对的列名"""

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
