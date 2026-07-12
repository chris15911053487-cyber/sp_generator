# SAP B1 存储过程智能体 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个对话式 SAP B1 存储过程生成智能体，支持需求澄清→方案设计→代码生成→语法+业务校验→部署的完整流程。

**Architecture:** FastAPI + Jinja2 + HTMX 全栈应用。LangGraph StateGraph 编排 Agent 流程，通过 `interrupt` 实现人机交替。SQLite 本地持久化会话/SP/校验记录。pyodbc 连接 SQL Server 执行校验和部署。

**Tech Stack:** Python 3.11+, FastAPI, LangChain + LangGraph, pyodbc, HTMX, CodeMirror 6 (CDN), SQLite, SQL Server (remote)

## Global Constraints

- 虚拟环境：项目内 `.venv`，禁止使用全局 pip
- LLM：通过 httpx 调用兼容 OpenAI 接口的 API，默认 deepseek-v4-pro
- 数据库：IP 139.199.221.230，端口 1400，用户 sa，密码 <YourStrong@Passw0rd>，账套 B1UP_DEMO
- 简洁优先：无过度抽象，单文件职责清晰
- 每个任务末尾 commit

---

### Task 1: 项目脚手架和虚拟环境

**Files:**
- Create: `requirements.txt`
- Create: `app/__init__.py`
- Create: `app/agent/__init__.py`
- Create: `app/db/__init__.py`
- Create: `app/routes/__init__.py`
- Create: `app/templates/.gitkeep`
- Create: `app/static/.gitkeep`
- Modify: `CLAUDE.md` (更新虚拟环境说明)

**Interfaces:**
- Consumes: nothing
- Produces: 项目目录结构，虚拟环境，所有依赖已安装

- [ ] **Step 1: 创建项目目录结构**

```bash
mkdir -p app/agent app/db app/routes app/templates app/static
```

- [ ] **Step 2: 写入 requirements.txt**

```
fastapi==0.115.6
uvicorn[standard]==0.34.0
langchain==0.3.14
langgraph==0.2.61
pyodbc==5.2.0
httpx==0.28.1
jinja2==3.1.4
python-multipart==0.0.19
```

- [ ] **Step 3: 创建项目级 __init__.py 文件**

内容均为空文件：
`app/__init__.py`、`app/agent/__init__.py`、`app/db/__init__.py`、`app/routes/__init__.py`

- [ ] **Step 4: 创建虚拟环境并安装依赖**

```bash
python -m venv .venv
source .venv/Scripts/activate  # Windows
pip install -r requirements.txt
pip install -r requirements.txt --require-virtualenv
```

验证：`python -c "import fastapi, langchain, langgraph, pyodbc, httpx; print('OK')"`
期望输出：`OK`

- [ ] **Step 5: 创建 placeholder 文件**

```bash
touch app/templates/.gitkeep app/static/.gitkeep
```

- [ ] **Step 6: 更新 CLAUDE.md 虚拟环境说明**

在 `CLAUDE.md` 底部修改第 5 节为：

```markdown
## 5. Python 环境

虚拟环境位于项目根目录的 `.venv/`。所有包管理操作必须针对该环境：

- 激活：`source .venv/Scripts/activate` (Windows Git Bash) 或 `.venv\Scripts\activate` (CMD)
- pip：`.venv/Scripts/pip.exe install <package>`
- 运行：`.venv/Scripts/python.exe <script>`

禁止使用全局 `pip install` 或安装到其他环境。
```

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: project scaffolding with virtual environment and dependencies"
```

---

### Task 2: 配置管理模块 (config.py)

**Files:**
- Create: `config.py`

**Interfaces:**
- Consumes: Task 1 (目录结构)
- Produces:
  - `init_config()` → None
  - `get_config(key: str, default: str = "")` → str
  - `set_config(key: str, value: str)` → None
  - `get_db_config()` → dict (keys: server, port, user, password, database)
  - `get_llm_config()` → dict (keys: api_key, base_url, model_name)

- [ ] **Step 1: 写 config.py 完整代码**

```python
"""全局配置管理 — 从 SQLite 读取 DB/LLM 配置。"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "app.db")

DEFAULT_CONFIG = {
    "db_server": "139.199.221.230",
    "db_port": "1400",
    "db_user": "sa",
    "db_password": "<YourStrong@Passw0rd>",
    "db_database": "B1UP_DEMO",
    "llm_api_key": "",
    "llm_base_url": "https://api.deepseek.com/v1",
    "llm_model_name": "deepseek-v4-pro",
}


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.commit()


def init_config() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    _ensure_table(conn)
    for key, value in DEFAULT_CONFIG.items():
        conn.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()
    conn.close()


def _get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def get_config(key: str, default: str = "") -> str:
    conn = _get_conn()
    row = conn.execute(
        "SELECT value FROM config WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    return row[0] if row else default


def set_config(key: str, value: str) -> None:
    conn = _get_conn()
    _ensure_table(conn)
    conn.execute(
        """INSERT INTO config (key, value, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
           updated_at=CURRENT_TIMESTAMP""",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_db_config() -> dict:
    return {
        "server": get_config("db_server"),
        "port": int(get_config("db_port", "1433")),
        "user": get_config("db_user"),
        "password": get_config("db_password"),
        "database": get_config("db_database"),
    }


def get_llm_config() -> dict:
    return {
        "api_key": get_config("llm_api_key"),
        "base_url": get_config("llm_base_url"),
        "model_name": get_config("llm_model_name"),
    }
```

- [ ] **Step 2: 验证 config 模块**

```bash
.venv/Scripts/python.exe -c "
from config import init_config, get_db_config, set_config, get_config
init_config()
cfg = get_db_config()
print('DB config:', cfg['server'], cfg['database'])
set_config('test_key', 'test_value')
print('test_key:', get_config('test_key'))
print('OK')
"
```

期望输出：
```
DB config: 139.199.221.230 B1UP_DEMO
test_key: test_value
OK
```

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add config module with SQLite-backed configuration"
```

---

### Task 3: SQLite 数据层 (app/db/sqlite.py)

**Files:**
- Create: `app/db/sqlite.py`

**Interfaces:**
- Consumes: Task 2 (config.DB_PATH 路径约定)
- Produces:
  - `init_db()` → None
  - `create_session(name: str)` → dict
  - `get_sessions()` → list[dict]
  - `delete_session(session_id: str)` → None
  - `save_message(session_id: str, role: str, content: str)` → dict
  - `get_messages(session_id: str)` → list[dict]
  - `save_sp(session_id: str, name: str, code: str)` → dict
  - `get_sps(session_id: str)` → list[dict]
  - `update_sp(sp_id: str, **kwargs)` → None
  - `delete_sp(sp_id: str)` → None
  - `save_verify_query(sp_id: str, name: str, sql_code: str, compare_columns: str)` → dict
  - `get_verify_queries(sp_id: str)` → list[dict]
  - `update_verify_query(query_id: str, **kwargs)` → None

- [ ] **Step 1: 写 app/db/sqlite.py 完整代码**

```python
"""SQLite 持久化层 — 会话、消息、存储过程、校验 SQL。"""
import sqlite3
import uuid
from config import DB_PATH


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS stored_procedures (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            name TEXT NOT NULL,
            code TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            syntax_valid INTEGER DEFAULT 0,
            business_valid INTEGER DEFAULT 0,
            verify_result TEXT,
            deployed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS verify_queries (
            id TEXT PRIMARY KEY,
            sp_id TEXT NOT NULL,
            name TEXT NOT NULL,
            sql_code TEXT NOT NULL,
            compare_columns TEXT,
            status TEXT DEFAULT 'pending',
            result_detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (sp_id) REFERENCES stored_procedures(id) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()


# --- Sessions ---

def create_session(name: str) -> dict:
    conn = _get_conn()
    sid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (id, name) VALUES (?, ?)", (sid, name)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    conn.close()
    return dict(row)


def get_sessions() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_session(session_id: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()


# --- Messages ---

def save_message(session_id: str, role: str, content: str) -> dict:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
        (session_id, role, content),
    )
    conn.execute(
        "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (session_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM messages WHERE id = last_insert_rowid()"
    ).fetchone()
    conn.close()
    return dict(row)


def get_messages(session_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Stored Procedures ---

def save_sp(session_id: str, name: str, code: str) -> dict:
    conn = _get_conn()
    sp_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO stored_procedures (id, session_id, name, code)
           VALUES (?, ?, ?, ?)""",
        (sp_id, session_id, name, code),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM stored_procedures WHERE id = ?", (sp_id,)
    ).fetchone()
    conn.close()
    return dict(row)


def get_sps(session_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM stored_procedures WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_sp(sp_id: str, **kwargs) -> None:
    allowed = {"name", "code", "status", "syntax_valid",
               "business_valid", "verify_result", "deployed_at"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = "CURRENT_TIMESTAMP"
    set_clause = ", ".join(
        f"{k} = {v}" if k == "updated_at" else f"{k} = ?"
        for k in ["updated_at"] + list(updates.keys())
        if k != "updated_at"
    )
    # simpler approach:
    set_parts = []
    params = []
    for k, v in updates.items():
        if k == "updated_at":
            set_parts.append("updated_at = CURRENT_TIMESTAMP")
        else:
            set_parts.append(f"{k} = ?")
            params.append(v)
    params.append(sp_id)
    conn = _get_conn()
    conn.execute(
        f"UPDATE stored_procedures SET {', '.join(set_parts)} WHERE id = ?",
        params,
    )
    conn.commit()
    conn.close()


def delete_sp(sp_id: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM stored_procedures WHERE id = ?", (sp_id,))
    conn.commit()
    conn.close()


# --- Verify Queries ---

def save_verify_query(sp_id: str, name: str, sql_code: str,
                      compare_columns: str = "") -> dict:
    conn = _get_conn()
    vq_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO verify_queries (id, sp_id, name, sql_code, compare_columns)
           VALUES (?, ?, ?, ?, ?)""",
        (vq_id, sp_id, name, sql_code, compare_columns),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM verify_queries WHERE id = ?", (vq_id,)
    ).fetchone()
    conn.close()
    return dict(row)


def get_verify_queries(sp_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM verify_queries WHERE sp_id = ? ORDER BY created_at",
        (sp_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_verify_query(query_id: str, **kwargs) -> None:
    allowed = {"name", "sql_code", "compare_columns", "status", "result_detail"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    set_parts = []
    params = []
    for k, v in updates.items():
        set_parts.append(f"{k} = ?")
        params.append(v)
    params.append(query_id)
    conn = _get_conn()
    conn.execute(
        f"UPDATE verify_queries SET {', '.join(set_parts)} WHERE id = ?",
        params,
    )
    conn.commit()
    conn.close()
```

- [ ] **Step 2: 验证 SQLite 数据层**

```bash
.venv/Scripts/python.exe -c "
from config import init_config
init_config()
from app.db.sqlite import init_db, create_session, save_message, get_messages, save_sp

init_db()
s = create_session('测试会话')
print('Session:', s['name'], s['id'])
save_message(s['id'], 'user', '你好')
save_message(s['id'], 'assistant', '你好！')
msgs = get_messages(s['id'])
print('Messages:', len(msgs))
sp = save_sp(s['id'], 'test_sp', 'SELECT 1')
print('SP:', sp['name'], sp['status'])
print('OK')
"
```

期望输出：
```
Session: 测试会话 <uuid>
Messages: 2
SP: test_sp draft
OK
```

- [ ] **Step 3: Commit**

```bash
git add app/db/__init__.py app/db/sqlite.py
git commit -m "feat: add SQLite persistence layer for sessions, messages, SPs, and verify queries"
```

---

### Task 4: SQL Server 连接层 (app/db/sqlserver.py)

**Files:**
- Create: `app/db/sqlserver.py`

**Interfaces:**
- Consumes: Task 2 (config.get_db_config)
- Produces:
  - `get_connection()` → pyodbc.Connection
  - `execute_query(sql: str)` → list[dict]
  - `check_syntax(sql: str)` → tuple[bool, str]
  - `deploy_procedure(name: str, code: str)` → tuple[bool, str]
  - `get_table_columns(table_name: str)` → list[dict]
  - `get_tables()` → list[str]

- [ ] **Step 1: 写 app/db/sqlserver.py 完整代码**

```python
"""SQL Server 连接和操作 — 语法校验、查询执行、存储过程部署。"""
import pyodbc
from config import get_db_config


def _build_conn_str() -> str:
    cfg = get_db_config()
    return (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={cfg['server']},{cfg['port']};"
        f"DATABASE={cfg['database']};"
        f"UID={cfg['user']};"
        f"PWD={cfg['password']};"
        "Encrypt=no;TrustServerCertificate=yes;"
        "Connection Timeout=10;"
    )


def get_connection() -> pyodbc.Connection:
    return pyodbc.connect(_build_conn_str(), autocommit=True)


def execute_query(sql: str) -> list[dict]:
    """执行 SQL 查询，返回字典列表。"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql)
    columns = [col[0] for col in cursor.description] if cursor.description else []
    rows = []
    for row in cursor.fetchall():
        rows.append(dict(zip(columns, row)))
    conn.close()
    return rows


def check_syntax(sql: str) -> tuple[bool, str]:
    """用 SET PARSEONLY ON 检查 SQL 语法。返回 (通过, 错误信息)。"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SET PARSEONLY ON")
        cursor.execute(sql)
        cursor.execute("SET PARSEONLY OFF")
        conn.close()
        return True, ""
    except Exception as e:
        try:
            cursor.execute("SET PARSEONLY OFF")
        except Exception:
            pass
        conn.close()
        return False, str(e)


def deploy_procedure(name: str, code: str) -> tuple[bool, str]:
    """部署存储过程到 SQL Server。返回 (成功, 错误信息)。"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # 如果存在则先删除再创建，确保幂等
        cursor.execute(f"DROP PROCEDURE IF EXISTS [{name}]")
        cursor.execute(code)
        conn.close()
        return True, ""
    except Exception as e:
        conn.close()
        return False, str(e)


def get_table_columns(table_name: str) -> list[dict]:
    """查询表的列信息。"""
    cfg = get_db_config()
    sql = f"""
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = '{table_name}'
        ORDER BY ORDINAL_POSITION
    """
    return execute_query(sql)


def get_tables() -> list[str]:
    """获取当前数据库中的用户表列表。"""
    rows = execute_query(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'"
    )
    return [r["TABLE_NAME"] for r in rows]
```

- [ ] **Step 2: 验证 SQL Server 连接和基本操作**

```bash
.venv/Scripts/python.exe -c "
from config import init_config
init_config()
from app.db.sqlserver import get_connection, execute_query, get_tables, get_table_columns

conn = get_connection()
print('Connected OK')
tables = get_tables()
print('Tables:', len(tables), 'found, first 5:', tables[:5])
if 'TABLE1' in tables:
    cols = get_table_columns('TABLE1')
    print('TABLE1 columns:', [c['COLUMN_NAME'] for c in cols])
conn.close()
print('OK')
"
```

- [ ] **Step 3: Commit**

```bash
git add app/db/sqlserver.py
git commit -m "feat: add SQL Server connection layer with syntax check, deploy, and schema queries"
```

---

### Task 5: B1 知识库和 Prompt (app/agent/prompts.py)

**Files:**
- Create: `app/agent/prompts.py`

**Interfaces:**
- Consumes: nothing standalone
- Produces:
  - `B1_TABLE_KNOWLEDGE: str` — B1 核心表结构描述
  - `SYSTEM_PROMPT: str` — Agent 系统提示词
  - `CLARIFY_PROMPT: str` — 需求澄清节点提示词
  - `DESIGN_PROMPT: str` — 方案设计节点提示词
  - `GENERATE_PROMPT: str` — 代码生成节点提示词
  - `VERIFY_PROMPT: str` — 校验分析提示词

- [ ] **Step 1: 写 app/agent/prompts.py**

```python
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
```

- [ ] **Step 2: 验证 prompt 模块**

```bash
.venv/Scripts/python.exe -c "
from app.agent.prompts import SYSTEM_PROMPT, CLARIFY_PROMPT
print('SYSTEM_PROMPT length:', len(SYSTEM_PROMPT))
print('CLARIFY_PROMPT template has user_input:', '{user_input}' in CLARIFY_PROMPT)
print('OK')
"
```

期望输出：
```
SYSTEM_PROMPT length: <数字>
CLARIFY_PROMPT template has user_input: True
OK
```

- [ ] **Step 3: Commit**

```bash
git add app/agent/prompts.py
git commit -m "feat: add B1 table knowledge base and agent prompt templates"
```

---

### Task 6: Agent 工具层 (app/agent/tools.py)

**Files:**
- Create: `app/agent/tools.py`

**Interfaces:**
- Consumes: Task 4 (app.db.sqlserver), Task 5 (prompts)
- Produces:
  - `create_tools()` → list[BaseTool] (LangChain tools)
  - 包含: `check_syntax_tool`, `run_sql_tool`, `get_table_info_tool`, `get_table_list_tool`

- [ ] **Step 1: 写 app/agent/tools.py**

```python
"""LangChain Tools — 包装 SQL Server 操作为 Agent 可调用的工具。"""
from langchain.tools import tool
from app.db.sqlserver import check_syntax, execute_query, get_table_columns, get_tables


@tool
def run_sql_tool(sql: str) -> str:
    """在 SQL Server 上执行只读查询，返回结果集。仅用于 SELECT 语句。
    参数 sql: 要执行的 SELECT 语句。"""
    upper = sql.strip().upper()
    if not upper.startswith("SELECT") and not upper.startswith("WITH"):
        return "错误：仅允许执行 SELECT 查询"
    try:
        rows = execute_query(sql)
        if not rows:
            return "查询返回空结果集"
        return str(rows[:50])  # 限制返回行数
    except Exception as e:
        return f"查询执行失败: {e}"


@tool
def get_table_info_tool(table_name: str) -> str:
    """获取指定表的列信息（列名、数据类型、长度）。
    参数 table_name: 表名，如 'OINV', 'INV1'。"""
    try:
        cols = get_table_columns(table_name)
        if not cols:
            return f"未找到表 {table_name} 的列信息"
        lines = [f"{c['COLUMN_NAME']}: {c['DATA_TYPE']}" for c in cols]
        return "\n".join(lines)
    except Exception as e:
        return f"查询失败: {e}"


@tool
def get_table_list_tool(_dummy: str = "") -> str:
    """获取当前数据库中所有用户表列表。"""
    try:
        tables = get_tables()
        return "\n".join(tables)
    except Exception as e:
        return f"查询失败: {e}"


def create_tools() -> list:
    return [run_sql_tool, get_table_info_tool, get_table_list_tool]
```

- [ ] **Step 2: 验证 tools 加载**

```bash
.venv/Scripts/python.exe -c "
from config import init_config
init_config()
from app.agent.tools import create_tools
tools = create_tools()
for t in tools:
    print(f'{t.name}: {t.description[:50]}...')
print(f'Total tools: {len(tools)}')
print('OK')
"
```

- [ ] **Step 3: Commit**

```bash
git add app/agent/tools.py
git commit -m "feat: add LangChain tools wrapping SQL Server operations"
```

---

### Task 7: Agent 状态图节点 (app/agent/nodes.py)

**Files:**
- Create: `app/agent/nodes.py`

**Interfaces:**
- Consumes: Task 5 (prompts), Task 6 (tools)
- Produces:
  - `AgentState` (TypedDict): session_id, messages, requirements, design, sp_list, status, error
  - `clarify_node(state, config) → dict`
  - `design_node(state, config) → dict`
  - `generate_node(state, config) → dict`
  - `verify_node(state, config) → dict`
  - `deploy_check_node(state, config) → dict`
  - `deploy_node(state, config) → dict`

- [ ] **Step 1: 写 app/agent/nodes.py**

```python
"""LangGraph 节点实现 — 需求澄清、方案设计、代码生成、校验、部署。"""
import json
import re
from typing import TypedDict
from langgraph.types import interrupt, Command
from langchain_openai import ChatOpenAI
from app.agent.prompts import (
    SYSTEM_PROMPT, CLARIFY_PROMPT, DESIGN_PROMPT,
    GENERATE_PROMPT, VERIFY_PROMPT,
)
from app.agent.tools import create_tools
from app.db.sqlserver import check_syntax, execute_query, deploy_procedure
from app.db.sqlite import save_sp, save_verify_query, save_message, update_sp
from config import get_llm_config


class AgentState(TypedDict):
    session_id: str
    user_input: str
    mode: str          # "clarify" | "design" | "generate" | "verify" | "deploy"
    requirements: str
    design: str
    sp_list: list
    status: str
    error: str


def _get_llm() -> ChatOpenAI:
    cfg = get_llm_config()
    return ChatOpenAI(
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        model=cfg["model_name"],
        temperature=0.1,
    )


def _build_chat_history(session_id: str, max_msgs: int = 10) -> str:
    from app.db.sqlite import get_messages
    msgs = get_messages(session_id)
    lines = []
    for m in msgs[-max_msgs:]:
        role = "用户" if m["role"] == "user" else "助手"
        lines.append(f"{role}: {m['content']}")
    return "\n".join(lines)


def clarify_node(state: AgentState, config: dict = None) -> dict:
    """需求澄清节点 — 一次最多问一个问题，直到信息充分。"""
    llm = _get_llm()
    chat_history = _build_chat_history(state["session_id"])
    clarified = state.get("requirements", "")

    prompt = CLARIFY_PROMPT.format(
        user_input=state["user_input"],
        chat_history=chat_history,
        clarified_info=clarified or "暂无",
    )
    response = llm.invoke([("system", SYSTEM_PROMPT), ("user", prompt)])

    if "INFO_SUFFICIENT" in response.content:
        return {
            "requirements": response.content.replace("INFO_SUFFICIENT", "").strip(),
            "mode": "design",
            "status": "clarified",
        }

    # 向用户展示问题，等待回答
    question = response.content
    answer = interrupt({"type": "clarify", "question": question})

    # 用户已回答，累积需求信息
    new_requirements = clarified + f"\nQ: {question}\nA: {answer}\n" if clarified else f"Q: {question}\nA: {answer}\n"
    return {
        "user_input": state["user_input"],
        "requirements": new_requirements,
        "mode": "clarify",  # 继续循环
        "status": "clarifying",
    }


def design_node(state: AgentState, config: dict = None) -> dict:
    """方案设计节点 — 基于需求生成方案，等待用户确认。"""
    llm = _get_llm()
    prompt = DESIGN_PROMPT.format(requirements=state["requirements"])
    response = llm.invoke([("system", SYSTEM_PROMPT), ("user", prompt)])
    design = response.content

    # 展示方案给用户，等待确认或修改
    decision = interrupt({"type": "design", "content": design})

    if isinstance(decision, dict) and decision.get("action") == "modify":
        # 用户要求修改方案
        design = decision.get("design", design)

    return {
        "design": design,
        "mode": "generate",
        "status": "designed",
    }


def generate_node(state: AgentState, config: dict = None) -> dict:
    """代码生成节点 — 生成存储过程和校验 SQL。"""
    llm = _get_llm()
    prompt = GENERATE_PROMPT.format(design=state["design"])
    response = llm.invoke([("system", SYSTEM_PROMPT), ("user", prompt)])

    # 尝试提取 JSON
    content = response.content
    json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
    if json_match:
        data = json.loads(json_match.group(1))
    else:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # 尝试提取最外层 JSON
            bracket_match = re.search(r'\{.*\}', content, re.DOTALL)
            if bracket_match:
                data = json.loads(bracket_match.group(0))
            else:
                return {"error": f"无法解析 LLM 响应为 JSON: {content[:500]}"}

    sp_list = []
    for proc in data.get("procedures", []):
        sp = save_sp(state["session_id"], proc["name"], proc["code"])
        sp_row = dict(sp) if not isinstance(sp, dict) else sp
        for vq in proc.get("verify_queries", []):
            save_verify_query(
                sp_row["id"],
                vq["name"],
                vq["sql_code"],
                vq.get("compare_columns", ""),
            )
        sp_list.append(sp_row)

    return {
        "sp_list": sp_list,
        "mode": "verify",
        "status": "generated",
    }


def verify_node(state: AgentState, config: dict = None) -> dict:
    """校验节点 — 对每个 SP 执行语法校验和业务校验。"""
    results = []
    all_pass = True

    for sp in state.get("sp_list", []):
        from app.db.sqlite import get_verify_queries, update_sp as db_update_sp, update_verify_query
        sp_result = {"sp_id": sp["id"], "syntax_ok": False, "business_ok": False, "details": []}

        # 语法校验
        ok, err = check_syntax(sp["code"])
        sp_result["syntax_ok"] = ok
        if not ok:
            sp_result["details"].append({"type": "syntax", "pass": False, "error": err})
            all_pass = False
            db_update_sp(sp["id"], syntax_valid=0, verify_result=str(sp_result))
        else:
            db_update_sp(sp["id"], syntax_valid=1)

        # 业务校验
        vqs = get_verify_queries(sp["id"])
        for vq in vqs:
            try:
                verify_rows = execute_query(vq["sql_code"])
                db_update_sp(sp["id"], business_valid=1)
                update_verify_query(vq["id"], status="pass", result_detail=str(verify_rows[:20]))
                sp_result["details"].append(
                    {"type": "business", "pass": True, "query": vq["name"], "data": verify_rows[:10]}
                )
            except Exception as e:
                all_pass = False
                update_verify_query(vq["id"], status="fail", result_detail=str(e))
                sp_result["details"].append(
                    {"type": "business", "pass": False, "query": vq["name"], "error": str(e)}
                )
                sp_result["business_ok"] = False

        sp_result["business_ok"] = all(
            d["pass"] for d in sp_result["details"] if d["type"] == "business"
        )
        results.append(sp_result)

    return {
        "status": "verified" if all_pass else "verify_failed",
        "verify_results": results,
    }


def deploy_check_node(state: AgentState, config: dict = None) -> dict:
    """部署预检节点 — 最终校验所有 SP。"""
    # 重新运行语法校验
    all_pass = True
    results = []
    for sp in state.get("sp_list", []):
        ok, err = check_syntax(sp["code"])
        results.append({"sp_id": sp["id"], "name": sp["name"], "syntax_ok": ok, "error": err})
        if not ok:
            all_pass = False

    if not all_pass:
        interrupt({"type": "deploy_check", "pass": False, "results": results})

    return {"status": "ready_to_deploy", "precheck_results": results}


def deploy_node(state: AgentState, config: dict = None) -> dict:
    """部署节点 — 执行 CREATE OR ALTER PROCEDURE。"""
    from app.db.sqlite import update_sp as db_update_sp
    import datetime

    results = []
    for sp in state.get("sp_list", []):
        ok, err = deploy_procedure(sp["name"], sp["code"])
        if ok:
            db_update_sp(sp["id"], status="deployed", deployed_at=datetime.datetime.now().isoformat())
        results.append({"sp_id": sp["id"], "name": sp["name"], "success": ok, "error": err})

    all_ok = all(r["success"] for r in results)
    return {"status": "deployed" if all_ok else "deploy_failed", "deploy_results": results}
```

- [ ] **Step 2: 验证 nodes 模块导入**

```bash
.venv/Scripts/python.exe -c "
from config import init_config
init_config()
from app.agent.nodes import AgentState, clarify_node
print('AgentState keys:', list(AgentState.__annotations__.keys()))
print('clarify_node imported OK')
print('OK')
"
```

- [ ] **Step 3: Commit**

```bash
git add app/agent/nodes.py
git commit -m "feat: add LangGraph node implementations for clarify, design, generate, verify, deploy"
```

---

### Task 8: LangGraph 状态图组装 (app/agent/graph.py)

**Files:**
- Create: `app/agent/graph.py`

**Interfaces:**
- Consumes: Task 7 (nodes)
- Produces:
  - `create_graph()` → CompiledStateGraph

- [ ] **Step 1: 写 app/agent/graph.py**

```python
"""LangGraph StateGraph 组装 — 定义节点和条件边的完整流程。"""
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from app.agent.nodes import (
    AgentState, clarify_node, design_node, generate_node,
    verify_node, deploy_check_node, deploy_node,
)


def _after_clarify(state: AgentState) -> str:
    if state.get("mode") == "design":
        return "design"
    return "clarify"  # 继续问问题


def _after_verify(state: AgentState) -> str:
    if state.get("status") == "verified":
        return END
    return END  # 等用户决定: 修改 or 部署


def create_graph() -> StateGraph:
    builder = StateGraph(AgentState)

    builder.add_node("clarify", clarify_node)
    builder.add_node("design", design_node)
    builder.add_node("generate", generate_node)
    builder.add_node("verify", verify_node)
    builder.add_node("deploy_check", deploy_check_node)
    builder.add_node("deploy", deploy_node)

    builder.set_entry_point("clarify")

    # 主流程边
    builder.add_conditional_edges("clarify", _after_clarify, {
        "clarify": "clarify",
        "design": "design",
    })
    builder.add_edge("design", "generate")
    builder.add_edge("generate", "verify")
    builder.add_edge("verify", END)       # 校验后结束，等待用户动作
    builder.add_edge("deploy_check", "deploy")
    builder.add_edge("deploy", END)

    memory = MemorySaver()
    return builder.compile(checkpointer=memory)
```

- [ ] **Step 2: 验证 graph 编译**

```bash
.venv/Scripts/python.exe -c "
from config import init_config
init_config()
from app.agent.graph import create_graph
graph = create_graph()
print('Graph compiled OK')
print('Nodes:', list(graph.get_graph().nodes.keys()))
print('OK')
"
```

- [ ] **Step 3: Commit**

```bash
git add app/agent/graph.py
git commit -m "feat: assemble LangGraph StateGraph with clarify→design→generate→verify→deploy flow"
```

---

### Task 9: 会话管理路由 (app/routes/session.py)

**Files:**
- Create: `app/routes/session.py`

**Interfaces:**
- Consumes: Task 3 (sqlite)
- Produces: FastAPI APIRouter with `/api/sessions` endpoints

- [ ] **Step 1: 写 app/routes/session.py**

```python
"""会话管理 API — 新建、列表、删除。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db.sqlite import create_session, get_sessions, delete_session, get_messages

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    name: str = "新会话"


@router.post("")
def api_create_session(req: CreateSessionRequest):
    session = create_session(req.name)
    return {"ok": True, "session": session}


@router.get("")
def api_get_sessions():
    return {"sessions": get_sessions()}


@router.delete("/{session_id}")
def api_delete_session(session_id: str):
    delete_session(session_id)
    return {"ok": True}


@router.get("/{session_id}/messages")
def api_get_messages(session_id: str):
    return {"messages": get_messages(session_id)}
```

- [ ] **Step 2: 验证路由可用性**

路由将在 main.py 集成后统一验证，此处先确保导入无误：

```bash
.venv/Scripts/python.exe -c "
from app.routes.session import router
print('Session router loaded, routes:', [r.path for r in router.routes])
print('OK')
"
```

- [ ] **Step 3: Commit**

```bash
git add app/routes/session.py
git commit -m "feat: add session management API routes"
```

---

### Task 10: 配置管理路由 (app/routes/config_routes.py)

**Files:**
- Create: `app/routes/config_routes.py`

**Interfaces:**
- Consumes: Task 2 (config)
- Produces: FastAPI APIRouter with `/api/config` endpoints

- [ ] **Step 1: 写 app/routes/config_routes.py**

```python
"""配置管理 API — 数据库连接、LLM 配置。"""
from fastapi import APIRouter
from pydantic import BaseModel
from config import get_config, set_config, get_db_config, get_llm_config

router = APIRouter(prefix="/api/config", tags=["config"])


class SetConfigRequest(BaseModel):
    key: str
    value: str


@router.get("")
def api_get_all_config():
    return {
        "db": get_db_config(),
        "llm": get_llm_config(),
    }


@router.post("")
def api_set_config(req: SetConfigRequest):
    set_config(req.key, req.value)
    return {"ok": True}


@router.get("/test-db")
def api_test_db_connection():
    from app.db.sqlserver import get_connection
    try:
        conn = get_connection()
        conn.close()
        return {"ok": True, "message": "数据库连接成功"}
    except Exception as e:
        return {"ok": False, "message": str(e)}
```

- [ ] **Step 2: Commit**

```bash
git add app/routes/config_routes.py
git commit -m "feat: add config management API routes with DB test endpoint"
```

---

### Task 11: 对话路由 (app/routes/chat.py)

**Files:**
- Create: `app/routes/chat.py`

**Interfaces:**
- Consumes: Task 8 (create_graph), Task 3 (sqlite)
- Produces: FastAPI APIRouter with `/api/chat` SSE streaming endpoint

- [ ] **Step 1: 写 app/routes/chat.py**

```python
"""对话路由 — SSE 流式对话，驱动 Agent 状态图。"""
import json
import asyncio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from app.db.sqlite import save_message, get_messages
from app.agent.graph import create_graph

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    session_id: str
    message: str
    action: str = "send"  # "send" | "approve" | "modify"


@router.post("/stream")
async def api_chat_stream(req: ChatRequest):
    """SSE 流式对话端点。"""
    # 保存用户消息
    save_message(req.session_id, "user", req.message)

    async def event_stream():
        graph = create_graph()
        thread_id = req.session_id
        config = {"configurable": {"thread_id": thread_id}}

        try:
            # 根据 session 上次状态决定模式
            state = graph.get_state(config)
            if state and state.values:
                current_mode = state.values.get("mode", "clarify")
            else:
                current_mode = "clarify"

            # 执行图
            input_state = {
                "session_id": req.session_id,
                "user_input": req.message,
                "mode": current_mode,
                "requirements": state.values.get("requirements", "") if state and state.values else "",
                "sp_list": state.values.get("sp_list", []) if state and state.values else [],
            }

            from langgraph.types import Command
            try:
                # 如果有 interrupt，resume
                if state and state.interrupts:
                    graph.update_state(config, {"user_input": req.message})
                    events = graph.stream(None, config)
                else:
                    events = graph.stream(input_state, config)
            except Exception:
                events = graph.stream(input_state, config)

            assistant_response = ""

            for event in events:
                for node_name, node_output in event.items():
                    if isinstance(node_output, dict):
                        # 检查是否有 interrupt 需要用户输入
                        if node_output.get("status") == "generated":
                            sp_list = node_output.get("sp_list", [])
                            assistant_response = f"已生成 {len(sp_list)} 个存储过程，校验完毕。请查看右侧面板。\n"
                            for sp in sp_list:
                                assistant_response += f"- {sp['name']}\n"

                        yield f"data: {json.dumps({'node': node_name, 'data': node_output, 'type': 'update'})}\n\n"

            if not assistant_response:
                assistant_response = "处理完成"

            save_message(req.session_id, "assistant", assistant_response)

            # 发送最终响应
            yield f"data: {json.dumps({'type': 'done', 'content': assistant_response})}\n\n"

        except Exception as e:
            error_msg = f"处理出错: {str(e)}"
            save_message(req.session_id, "assistant", error_msg)
            yield f"data: {json.dumps({'type': 'error', 'content': error_msg})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/messages/{session_id}")
def api_get_messages(session_id: str):
    return {"messages": get_messages(session_id)}
```

- [ ] **Step 2: Commit**

```bash
git add app/routes/chat.py
git commit -m "feat: add chat SSE streaming route with LangGraph agent integration"
```

---

### Task 12: 存储过程管理路由 (app/routes/sp.py)

**Files:**
- Create: `app/routes/sp.py`

**Interfaces:**
- Consumes: Task 3 (sqlite)
- Produces: FastAPI APIRouter with `/api/sp` endpoints

- [ ] **Step 1: 写 app/routes/sp.py**

```python
"""存储过程管理 API — 列表、更新、删除、获取代码。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db.sqlite import get_sps, update_sp, delete_sp

router = APIRouter(prefix="/api/sp", tags=["stored_procedures"])


class UpdateSpRequest(BaseModel):
    name: str | None = None
    code: str | None = None


@router.get("/{session_id}")
def api_get_sps(session_id: str):
    return {"procedures": get_sps(session_id)}


@router.put("/{sp_id}")
def api_update_sp(sp_id: str, req: UpdateSpRequest):
    kwargs = {}
    if req.name is not None:
        kwargs["name"] = req.name
    if req.code is not None:
        kwargs["code"] = req.code
    if not kwargs:
        raise HTTPException(400, "没有可更新的字段")
    update_sp(sp_id, **kwargs)
    return {"ok": True}


@router.delete("/{sp_id}")
def api_delete_sp(sp_id: str):
    delete_sp(sp_id)
    return {"ok": True}
```

- [ ] **Step 2: Commit**

```bash
git add app/routes/sp.py
git commit -m "feat: add stored procedure management API routes"
```

---

### Task 13: 校验管理路由 (app/routes/verify.py)

**Files:**
- Create: `app/routes/verify.py`

**Interfaces:**
- Consumes: Task 3 (sqlite), Task 4 (sqlserver)
- Produces: FastAPI APIRouter with `/api/verify` endpoints

- [ ] **Step 1: 写 app/routes/verify.py**

```python
"""校验管理 API — 单 SP 语法校验、业务校验、获取校验列表。"""
from fastapi import APIRouter
from app.db.sqlite import get_sps, get_verify_queries, update_sp, update_verify_query
from app.db.sqlserver import check_syntax, execute_query

router = APIRouter(prefix="/api/verify", tags=["verify"])


@router.post("/syntax/{session_id}/{sp_id}")
def api_check_syntax(session_id: str, sp_id: str):
    """对单个 SP 执行语法校验。"""
    sps = get_sps(session_id)
    target = next((s for s in sps if s["id"] == sp_id), None)
    if not target:
        return {"ok": False, "message": "SP 不存在"}
    ok, err = check_syntax(target["code"])
    update_sp(sp_id, syntax_valid=1 if ok else 0)
    return {"ok": ok, "error": err}


@router.post("/business/{sp_id}")
def api_check_business(sp_id: str):
    """对单个 SP 执行所有关联校验 SQL 的业务校验。"""
    vqs = get_verify_queries(sp_id)
    results = []
    for vq in vqs:
        try:
            rows = execute_query(vq["sql_code"])
            update_verify_query(vq["id"], status="pass", result_detail=str(rows[:20]))
            results.append({"query_id": vq["id"], "name": vq["name"], "pass": True, "data": rows[:10]})
        except Exception as e:
            update_verify_query(vq["id"], status="fail", result_detail=str(e))
            results.append({"query_id": vq["id"], "name": vq["name"], "pass": False, "error": str(e)})
    return {"results": results}


@router.get("/{session_id}/sp/{sp_id}")
def api_get_verify_for_sp(session_id: str, sp_id: str):
    """获取指定 SP 的校验查询列表。"""
    return {"verify_queries": get_verify_queries(sp_id)}


@router.post("/all/{session_id}")
def api_verify_all(session_id: str):
    """对会话下所有 SP 执行完整校验。"""
    sps = get_sps(session_id)
    all_results = []
    for sp in sps:
        syntax_ok, syntax_err = check_syntax(sp["code"])
        update_sp(sp["id"], syntax_valid=1 if syntax_ok else 0)
        sp_result = {"sp_id": sp["id"], "name": sp["name"], "syntax_ok": syntax_ok, "syntax_err": syntax_err}

        vqs = get_verify_queries(sp["id"])
        biz_results = []
        for vq in vqs:
            try:
                rows = execute_query(vq["sql_code"])
                update_verify_query(vq["id"], status="pass", result_detail=str(rows[:20]))
                biz_results.append({"query_id": vq["id"], "name": vq["name"], "pass": True})
            except Exception as e:
                update_verify_query(vq["id"], status="fail", result_detail=str(e))
                biz_results.append({"query_id": vq["id"], "name": vq["name"], "pass": False, "error": str(e)})
        sp_result["business"] = biz_results
        all_results.append(sp_result)

    return {"results": all_results}
```

- [ ] **Step 2: Commit**

```bash
git add app/routes/verify.py
git commit -m "feat: add verify API routes for syntax and business validation"
```

---

### Task 14: 部署路由 (app/routes/deploy.py)

**Files:**
- Create: `app/routes/deploy.py`

**Interfaces:**
- Consumes: Task 3 (sqlite), Task 4 (sqlserver)
- Produces: FastAPI APIRouter with `/api/deploy` endpoints

- [ ] **Step 1: 写 app/routes/deploy.py**

```python
"""部署管理 API — 预检 + 一键部署。"""
import datetime
from fastapi import APIRouter
from app.db.sqlite import get_sps, update_sp
from app.db.sqlserver import check_syntax, deploy_procedure

router = APIRouter(prefix="/api/deploy", tags=["deploy"])


@router.post("/precheck/{session_id}")
def api_precheck(session_id: str):
    """部署前预检 — 对所有 SP 执行语法校验。"""
    sps = get_sps(session_id)
    if not sps:
        return {"ok": False, "message": "没有可部署的存储过程"}

    results = []
    all_pass = True
    for sp in sps:
        ok, err = check_syntax(sp["code"])
        update_sp(sp["id"], syntax_valid=1 if ok else 0)
        if not ok:
            all_pass = False
        results.append({"sp_id": sp["id"], "name": sp["name"], "syntax_ok": ok, "error": err})

    return {"ok": all_pass, "results": results}


@router.post("/{session_id}")
def api_deploy(session_id: str):
    """一键部署 — 预检通过后执行 CREATE PROCEDURE。"""
    sps = get_sps(session_id)
    if not sps:
        return {"ok": False, "message": "没有可部署的存储过程"}

    # 先预检
    all_syntax_ok = True
    for sp in sps:
        ok, err = check_syntax(sp["code"])
        if not ok:
            all_syntax_ok = False
    
    if not all_syntax_ok:
        return {"ok": False, "message": "预检未通过，请先解决语法错误后再部署"}

    # 逐个部署
    results = []
    for sp in sps:
        ok, err = deploy_procedure(sp["name"], sp["code"])
        if ok:
            update_sp(sp["id"], status="deployed", deployed_at=datetime.datetime.now().isoformat())
        results.append({"sp_id": sp["id"], "name": sp["name"], "success": ok, "error": err})

    all_ok = all(r["success"] for r in results)
    return {"ok": all_ok, "results": results}
```

- [ ] **Step 2: Commit**

```bash
git add app/routes/deploy.py
git commit -m "feat: add deploy precheck and deploy API routes"
```

---

### Task 15: 前端界面 (app/templates/index.html)

**Files:**
- Create: `app/templates/index.html`

**Interfaces:**
- Consumes: all route modules (via HTMX/SSE)
- Produces: 单页面应用 — 左右分栏布局，对话+SP列表

- [ ] **Step 1: 写 app/templates/index.html**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SP Generator — SAP B1 存储过程智能体</title>
    <script src="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/6.65.7/codemirror.min.css">
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
    <!-- 顶部导航栏 -->
    <header id="topbar">
        <span id="logo">🗄️ SP Generator</span>
        <nav>
            <select id="session-select" name="session_id" hx-get="/api/sessions" hx-trigger="load" hx-swap="innerHTML">
                <option value="">加载中...</option>
            </select>
            <button hx-post="/api/sessions" hx-vals='{"name":"新会话"}' hx-target="#session-select" hx-swap="innerHTML">➕ 新建</button>
            <button hx-delete="" hx-include="#session-select" hx-confirm="确定删除当前会话？">🗑️ 删除</button>
            <a href="/config" id="settings-link">⚙️ 设置</a>
        </nav>
        <div id="status-bar">
            <span id="db-status">🔍 检测中...</span>
            <span id="llm-status"></span>
        </div>
    </header>

    <!-- 主区域 -->
    <main>
        <!-- 左侧对话区 -->
        <section id="chat-panel">
            <div id="chat-messages">
                <!-- 消息由 JS 通过 EventSource 动态填充 -->
            </div>
            <form id="chat-form" onsubmit="sendMessage(event)">
                <textarea id="chat-input" name="message" placeholder="描述你的存储过程需求..." rows="2"></textarea>
                <button type="submit">发送</button>
            </form>
        </section>

        <!-- 右侧面板 -->
        <section id="right-panel">
            <h2>存储过程列表</h2>
            <div id="sp-list">
                <!-- JS 动态填充 SP 列表 -->
            </div>

            <h2>校验 SQL 列表</h2>
            <div id="verify-list">
                <!-- 展开 SP 时动态加载 -->
            </div>

            <div id="deploy-actions">
                <button id="btn-precheck" onclick="precheckDeploy()">
                    🔍 一键预检
                </button>
                <button id="btn-deploy" onclick="deployAll()" disabled>
                    🚀 一键部署
                </button>
                <div id="deploy-result"></div>
            </div>
        </section>
    </main>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/6.65.7/codemirror.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/6.65.7/mode/sql/sql.min.js"></script>
    <script>
    // === 全局状态 ===
    let currentSessionId = '';

    // 初始化: 加载会话列表
    async function loadSessions() {
        const r = await fetch('/api/sessions');
        const data = await r.json();
        const sel = document.getElementById('session-select');
        sel.innerHTML = '';
        for (const s of data.sessions) {
            const opt = document.createElement('option');
            opt.value = s.id;
            opt.textContent = s.name;
            sel.appendChild(opt);
        }
        if (data.sessions.length > 0) {
            currentSessionId = data.sessions[0].id;
            sel.value = currentSessionId;
            loadMessages();
            loadSpList();
        }
    }

    // 切换会话
    function switchSession() {
        currentSessionId = document.getElementById('session-select').value;
        document.getElementById('chat-messages').innerHTML = '';
        loadMessages();
        loadSpList();
    }
    document.getElementById('session-select').addEventListener('change', switchSession);

    // 加载历史消息
    async function loadMessages() {
        if (!currentSessionId) return;
        const r = await fetch(`/api/chat/messages/${currentSessionId}`);
        const data = await r.json();
        const container = document.getElementById('chat-messages');
        container.innerHTML = '';
        for (const m of data.messages) {
            const div = document.createElement('div');
            div.className = `message ${m.role}`;
            div.textContent = m.content;
            container.appendChild(div);
        }
        container.scrollTop = container.scrollHeight;
    }

    // 发送消息 (通过 EventSource SSE)
    async function sendMessage(event) {
        event.preventDefault();
        const input = document.getElementById('chat-input');
        const message = input.value.trim();
        if (!message || !currentSessionId) return;

        // 添加用户消息到界面
        const container = document.getElementById('chat-messages');
        const userDiv = document.createElement('div');
        userDiv.className = 'message user';
        userDiv.textContent = message;
        container.appendChild(userDiv);
        container.scrollTop = container.scrollHeight;
        input.value = '';

        // POST 触发 SSE 流
        const r = await fetch('/api/chat/stream', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({session_id: currentSessionId, message: message}),
        });

        // 读取 SSE 流
        const reader = r.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, {stream: true});
            const lines = buffer.split('\n');
            buffer = lines.pop();
            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    try {
                        const data = JSON.parse(line.slice(6));
                        if (data.type === 'done') {
                            const div = document.createElement('div');
                            div.className = 'message assistant';
                            div.textContent = data.content;
                            container.appendChild(div);
                            loadSpList();
                        } else if (data.type === 'error') {
                            const div = document.createElement('div');
                            div.className = 'message error';
                            div.textContent = data.content;
                            container.appendChild(div);
                        }
                    } catch(e) {}
                }
            }
            container.scrollTop = container.scrollHeight;
        }
    }

    // 加载 SP 列表
    async function loadSpList() {
        if (!currentSessionId) return;
        const r = await fetch(`/api/sp/${currentSessionId}`);
        const data = await r.json();
        const container = document.getElementById('sp-list');
        if (data.procedures.length === 0) {
            container.innerHTML = '<p style="color:#636e72;font-size:13px;">暂无存储过程</p>';
            return;
        }
        container.innerHTML = data.procedures.map(sp => `
            <div class="sp-card">
                <div class="sp-header" onclick="toggleSpCode('${sp.id}')">
                    <span class="sp-name">${sp.name}</span>
                    <span class="sp-status ${sp.status || 'draft'}">${sp.status || 'draft'}</span>
                </div>
                <div class="sp-actions">
                    <button onclick="toggleSpCode('${sp.id}')">▸ 代码</button>
                    <button onclick="editSp('${sp.id}')">✎ 编辑</button>
                    <button onclick="verifySp('${sp.id}')">✓ 校验</button>
                </div>
                <div class="sp-code" id="sp-code-${sp.id}">
                    <textarea id="sp-editor-${sp.id}" class="sp-code-editor" readonly>${escapeHtml(sp.code)}</textarea>
                </div>
            </div>
        `).join('');
    }

    function escapeHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

    function toggleSpCode(spId) {
        const el = document.getElementById(`sp-code-${spId}`);
        el.classList.toggle('open');
        if (el.classList.contains('open')) {
            initCodeMirror(`sp-editor-${spId}`);
        }
    }

    function editSp(spId) {
        const ta = document.getElementById(`sp-editor-${spId}`);
        ta.readOnly = !ta.readOnly;
        if (!ta.readOnly) {
            ta.style.background = '#fffbe6';
            const btn = document.createElement('button');
            btn.textContent = '保存';
            btn.onclick = async () => {
                const code = ta.value;
                const name = prompt('存储过程名称:', '');
                if (name) {
                    await fetch(`/api/sp/${spId}`, {
                        method: 'PUT',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({name, code}),
                    });
                    ta.readOnly = true;
                    ta.style.background = '';
                    btn.remove();
                    loadSpList();
                }
            };
            ta.parentElement.appendChild(btn);
        }
    }

    async function verifySp(spId) {
        const r = await fetch(`/api/verify/business/${spId}`, {method: 'POST'});
        const data = await r.json();
        alert('校验结果: ' + JSON.stringify(data.results, null, 2));
    }

    // 部署
    async function precheckDeploy() {
        if (!currentSessionId) return;
        const r = await fetch(`/api/deploy/precheck/${currentSessionId}`, {method: 'POST'});
        const data = await r.json();
        const resultDiv = document.getElementById('deploy-result');
        if (data.ok) {
            resultDiv.innerHTML = '✅ 预检通过，可以部署';
            document.getElementById('btn-deploy').disabled = false;
        } else {
            resultDiv.innerHTML = data.results.map(r => r.syntax_ok ? `✅ ${r.name}` : `❌ ${r.name}: ${r.error}`).join('<br>');
            document.getElementById('btn-deploy').disabled = true;
        }
    }

    async function deployAll() {
        if (!currentSessionId) return;
        const r = await fetch(`/api/deploy/${currentSessionId}`, {method: 'POST'});
        const data = await r.json();
        const resultDiv = document.getElementById('deploy-result');
        if (data.ok) {
            resultDiv.innerHTML = '🚀 全部部署成功！';
        } else {
            resultDiv.innerHTML = data.results.map(r => r.success ? `✅ ${r.name}` : `❌ ${r.name}: ${r.error}`).join('<br>');
        }
    }

    // CodeMirror 初始化
    function initCodeMirror(textareaId) {
        const ta = document.getElementById(textareaId);
        if (!ta || ta.nextSibling?.classList?.contains('CodeMirror')) return;
        const cm = CodeMirror.fromTextArea(ta, {
            mode: 'text/x-sql',
            lineNumbers: true,
            readOnly: ta.readOnly,
            theme: 'default',
        });
        ta._cm = cm;
    }

    // 页面加载时初始化
    loadSessions();
    </script>
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add app/templates/index.html
git commit -m "feat: add frontend template with HTMX SSE chat, SP list, and deploy actions"
```

---

### Task 16: 样式 (app/static/style.css)

**Files:**
- Create: `app/static/style.css`

- [ ] **Step 1: 写 app/static/style.css**

```css
/* 全局 */
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f6fa; color: #2d3436; height: 100vh; display: flex; flex-direction: column; }

/* 顶部栏 */
#topbar { display: flex; align-items: center; gap: 12px; padding: 8px 16px; background: #2d3436; color: #fff; }
#logo { font-weight: 700; font-size: 16px; }
#topbar nav { display: flex; gap: 8px; align-items: center; }
#topbar select { padding: 4px 8px; border-radius: 4px; border: none; }
#topbar button, #settings-link { padding: 4px 12px; border: none; border-radius: 4px; background: #6c5ce7; color: #fff; cursor: pointer; font-size: 13px; text-decoration: none; }
#settings-link { background: #636e72; }
#status-bar { margin-left: auto; display: flex; gap: 16px; font-size: 12px; }

/* 主区域 */
main { display: flex; flex: 1; overflow: hidden; }

/* 左侧对话区 */
#chat-panel { flex: 6; display: flex; flex-direction: column; border-right: 1px solid #dfe6e9; }
#chat-messages { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
#chat-messages .message { max-width: 80%; padding: 10px 14px; border-radius: 12px; line-height: 1.5; font-size: 14px; }
#chat-messages .user { align-self: flex-end; background: #6c5ce7; color: #fff; }
#chat-messages .assistant { align-self: flex-start; background: #fff; border: 1px solid #dfe6e9; }
#chat-messages .system { align-self: center; font-size: 12px; color: #636e72; }
#chat-messages .error { align-self: flex-start; background: #ff7675; color: #fff; }
#chat-form { display: flex; padding: 12px; gap: 8px; border-top: 1px solid #dfe6e9; background: #fff; }
#chat-form textarea { flex: 1; padding: 8px; border: 1px solid #dfe6e9; border-radius: 8px; resize: none; font-size: 14px; }
#chat-form button { padding: 8px 16px; background: #6c5ce7; color: #fff; border: none; border-radius: 8px; cursor: pointer; }

/* 右侧面板 */
#right-panel { flex: 4; padding: 16px; overflow-y: auto; }
#right-panel h2 { font-size: 14px; color: #636e72; margin-bottom: 8px; padding-bottom: 4px; border-bottom: 1px solid #dfe6e9; }
.sp-card { background: #fff; border: 1px solid #dfe6e9; border-radius: 8px; padding: 12px; margin-bottom: 8px; }
.sp-card .sp-header { display: flex; justify-content: space-between; align-items: center; cursor: pointer; }
.sp-card .sp-name { font-weight: 600; font-size: 14px; }
.sp-card .sp-status { font-size: 12px; padding: 2px 8px; border-radius: 12px; }
.sp-status.draft { background: #ffeaa7; color: #d63031; }
.sp-status.verified { background: #55efc4; color: #00b894; }
.sp-status.deployed { background: #74b9ff; color: #0984e3; }
.sp-card .sp-actions { display: flex; gap: 8px; margin-top: 8px; }
.sp-card .sp-actions button { padding: 4px 10px; border: 1px solid #dfe6e9; border-radius: 4px; background: #fff; cursor: pointer; font-size: 12px; }
.sp-card .sp-code { display: none; margin-top: 8px; }
.sp-card .sp-code.open { display: block; }
.sp-card textarea.sp-code-editor { width: 100%; min-height: 150px; font-family: "Fira Code", monospace; font-size: 13px; }

/* 部署按钮 */
#deploy-actions { margin-top: 16px; display: flex; gap: 8px; }
#deploy-actions button { flex: 1; padding: 10px; border: none; border-radius: 8px; font-size: 14px; cursor: pointer; }
#btn-precheck { background: #fdcb6e; color: #2d3436; }
#btn-deploy { background: #00b894; color: #fff; }
#btn-deploy:disabled { background: #b2bec3; cursor: not-allowed; }
#deploy-result { margin-top: 8px; font-size: 13px; }

/* 配置页面 */
.config-page { max-width: 600px; margin: 40px auto; }
.config-page h1 { margin-bottom: 24px; }
.config-section { background: #fff; border-radius: 8px; padding: 20px; margin-bottom: 16px; }
.config-section h2 { font-size: 16px; margin-bottom: 12px; }
.config-section label { display: block; margin-bottom: 8px; font-size: 13px; color: #636e72; }
.config-section input { width: 100%; padding: 8px; border: 1px solid #dfe6e9; border-radius: 4px; margin-bottom: 12px; }
.config-section button { padding: 8px 16px; background: #6c5ce7; color: #fff; border: none; border-radius: 4px; cursor: pointer; }
```

- [ ] **Step 2: Commit**

```bash
git add app/static/style.css
git commit -m "feat: add frontend stylesheet with chat, SP cards, and deploy button styles"
```

---

### Task 17: FastAPI 入口 (main.py)

**Files:**
- Create: `main.py`

**Interfaces:**
- Consumes: all route modules, config, sqlite
- Produces: 可运行的 FastAPI 应用

- [ ] **Step 1: 写 main.py**

```python
"""FastAPI 入口 — SP Generator 应用启动。"""
import os
import sys
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(__file__))

from config import init_config
from app.db.sqlite import init_db
from app.routes import session, config_routes, chat, sp, verify, deploy

# 初始化配置和数据库
init_config()
init_db()

app = FastAPI(title="SP Generator", version="1.0.0")

# 静态文件
static_dir = os.path.join(os.path.dirname(__file__), "app", "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# 路由注册
app.include_router(session.router)
app.include_router(config_routes.router)
app.include_router(chat.router)
app.include_router(sp.router)
app.include_router(verify.router)
app.include_router(deploy.router)

# 模板
templates_dir = os.path.join(os.path.dirname(__file__), "app", "templates")
templates = Jinja2Templates(directory=templates_dir)


@app.get("/")
async def home(request):
    """主页面。"""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/config")
async def config_page(request):
    """配置页面 — 内嵌在 index.html 中通过 HTMX 切换，此处返回配置片段。"""
    config_html = """
    <div class="config-page">
        <h1>⚙️ 系统配置</h1>
        <div class="config-section">
            <h2>数据库连接</h2>
            <label>服务器地址 <input id="cfg-db-server" placeholder="139.199.221.230"></label>
            <label>端口 <input id="cfg-db-port" placeholder="1400"></label>
            <label>用户名 <input id="cfg-db-user" placeholder="sa"></label>
            <label>密码 <input id="cfg-db-password" type="password" placeholder="密码"></label>
            <label>账套 <input id="cfg-db-database" placeholder="B1UP_DEMO"></label>
            <button onclick="testDbConnection()">测试连接</button>
            <span id="db-test-result"></span>
        </div>
        <div class="config-section">
            <h2>LLM 配置</h2>
            <label>API Key <input id="cfg-llm-key" type="password" placeholder="sk-..."></label>
            <label>Base URL <input id="cfg-llm-url" placeholder="https://api.deepseek.com/v1"></label>
            <label>Model Name <input id="cfg-llm-model" placeholder="deepseek-v4-pro"></label>
        </div>
        <button onclick="saveConfig()">💾 保存配置</button>
        <a href="/">← 返回主界面</a>
    </div>
    <script>
    async function testDbConnection() {
        const r = await fetch('/api/config/test-db');
        const d = await r.json();
        document.getElementById('db-test-result').textContent = d.ok ? '✅ 连接成功' : '❌ ' + d.message;
    }
    async function saveConfig() {
        const items = [
            ['db_server', document.getElementById('cfg-db-server').value],
            ['db_port', document.getElementById('cfg-db-port').value],
            ['db_user', document.getElementById('cfg-db-user').value],
            ['db_password', document.getElementById('cfg-db-password').value],
            ['db_database', document.getElementById('cfg-db-database').value],
            ['llm_api_key', document.getElementById('cfg-llm-key').value],
            ['llm_base_url', document.getElementById('cfg-llm-url').value],
            ['llm_model_name', document.getElementById('cfg-llm-model').value],
        ];
        for (const [k, v] of items) {
            if (v) await fetch('/api/config', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k,value:v})});
        }
        alert('配置已保存');
    }
    </script>
    """
    return HTMLResponse(content=config_html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
```

- [ ] **Step 2: 启动应用并验证基础功能**

```bash
.venv/Scripts/python.exe main.py
```

验证：
1. 浏览器打开 `http://127.0.0.1:8000`，看到主界面
2. 打开 `http://127.0.0.1:8000/config`，看到配置页面
3. 测试数据库连接

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add FastAPI entry point with all routers, templates, and static files"
```

---

### Task 18: 验收测试

**目的:** 验证完整流程：对话 → 生成 → 校验 → 部署

- [ ] **Step 1: 确保应用运行中**

```bash
.venv/Scripts/python.exe main.py
```

- [ ] **Step 2: 数据库连接测试**

```bash
curl http://127.0.0.1:8000/api/config/test-db
```

期望输出：`{"ok":true,"message":"数据库连接成功"}`

- [ ] **Step 3: 新建会话**

```bash
curl -X POST http://127.0.0.1:8000/api/sessions -H "Content-Type: application/json" -d '{"name":"验收测试"}'
```

期望输出包含 `"ok":true` 和 session 对象

- [ ] **Step 4: 发送需求对话**

在浏览器中打开 `http://127.0.0.1:8000`，在输入框输入：
```
我要做销售收入统计和比对的存储过程
```
观察 Agent 返回澄清问题。

- [ ] **Step 5: 完整流程测试**

逐一回答 Agent 的问题直到生成存储过程，检查：
- 右侧面板是否显示生成的 SP
- SP 代码是否带语法高亮显示
- 校验结果是否正确显示

- [ ] **Step 6: 部署测试**

点击「一键预检」→ 确认通过 → 点击「一键部署」

验证 SQL Server 中存储过程已创建：

```bash
.venv/Scripts/python.exe -c "
from config import init_config
init_config()
from app.db.sqlserver import execute_query
rows = execute_query(\"SELECT name FROM sys.procedures\")
print('Deployed procedures:', [r['name'] for r in rows])
"
```

- [ ] **Step 7: Commit final changes**

```bash
git add -A
git commit -m "test: acceptance test complete — full flow verified"
```
