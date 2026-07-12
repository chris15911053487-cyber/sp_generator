# SAP B1 存储过程智能体 — 设计文档

**日期**: 2026-07-12
**状态**: 已确认

---

## 一、概述

开发一个独立的 SAP Business One 存储过程智能体，用户通过对话式交互描述需求，智能体经过头脑风暴 → 方案设计 → 代码生成 → 校验 → 部署的完整流程，生成符合要求的存储过程并部署到 SQL Server。

**验收标准**:
1. 数据库连接正常
2. 用户输入「我要做销售收入统计和比对的存储过程」→ 完成完整流程 → 一键部署 → 存储过程出现在 SQL Server 中
3. 交付能通过验收测试的完整代码

---

## 二、技术选型

| 维度 | 选择 |
|------|------|
| 前端 | FastAPI + Jinja2 + HTMX + CodeMirror 6 CDN |
| 智能体框架 | LangChain + LangGraph (StateGraph) |
| LLM | 可配置，默认 deepseek-v4-pro（兼容 OpenAI 接口） |
| B1 领域知识 | 混合方式：核心 B1 标准表结构预置在 system prompt + 自定义表实时查询 |
| 业务校验 | 混合：对话区简要结果 + 右侧面板详细 SQL 和比对结果 |
| 持久化 | SQLite 本地存储 |
| 部署 | 两步走：预检（全部校验通过）→ 部署（CREATE OR ALTER PROCEDURE） |
| 虚拟环境 | 项目内 `.venv` 独立虚拟环境 |

---

## 三、系统架构

### 3.1 分层架构

```
┌─────────────────────────────────────────────────┐
│                  前端层                           │
│  FastAPI + Jinja2 + HTMX + CodeMirror            │
│  左右分栏: 左侧对话 | 右侧 SP/校验列表            │
├─────────────────────────────────────────────────┤
│                  API 层 (FastAPI)                 │
│  /chat, /session, /sp, /verify, /deploy, /config │
├─────────────────────────────────────────────────┤
│              智能体层 (LangGraph)                  │
│  StateGraph: 需求澄清 → 方案设计 → 代码生成        │
│  → 校验 → 用户修改 → 部署                         │
├─────────────────────────────────────────────────┤
│              工具层 (LangChain Tools)              │
│  DB 查询 | B1 知识检索 | 语法校验 | 业务校验       │
│  | 部署执行                                       │
├─────────────────────────────────────────────────┤
│              数据层 (SQLite + SQL Server)          │
│  会话/SP/校验记录(本地) | B1 数据库(远程)         │
└─────────────────────────────────────────────────┘
```

### 3.2 核心模块

1. **Chat 模块** — 处理对话、管理会话，驱动 LangGraph 状态机
2. **Agent 模块** — LangGraph 定义"需求分析→方案→生成→校验→部署"流程
3. **SP Manager 模块** — 存储过程的 CRUD、编辑、配置修改
4. **Verify 模块** — 语法校验 + 业务数据正确性校验
5. **Deploy 模块** — 两步部署（预检 + 批量部署到 SQL Server）
6. **Config 模块** — 数据库连接、LLM 配置管理

---

## 四、LangGraph 状态流转

```
用户输入 → [需求澄清节点] ↔ 用户回答（人机循环）
        → [方案设计节点] → 展示方案给用户
        → [代码生成节点] → 生成存储过程 + 校验 SQL
        → [自动校验节点] → 语法 + 业务数据校验
        → [用户修改节点] ↔ 用户编辑/手动校验（人机循环）
        → [部署预检节点] → 最终校验
        → [部署执行节点] → CREATE PROCEDURE 写入 SQL Server
```

每个节点间通过 `AgentState` 共享上下文，包含：对话历史、用户需求摘要、方案内容、SP 列表、校验结果。

---

## 五、模块详设

### 5.1 Chat 模块
- 管理会话生命周期（新建、删除、切换）
- 接收用户消息，驱动 LangGraph Agent，流式返回响应
- 消息通过 SSE (Server-Sent Events) 流式推送到前端；HTMX 通过 `hx-sse` 扩展接收 SSE 事件流，将 LLM token 逐字渲染到对话区
- LangGraph 通过 `interrupt` 机制暂停等待用户输入：当节点需要用户回答或确认时，抛出 `interrupt` 暂停图执行，用户提交响应后通过 `Command(resume=...)` 继续执行

### 5.2 Agent 模块
- 用 `StateGraph` 实现有状态的人机交替流程
- 关键节点：
  - `clarify`: 根据用户需求提出必要问题（1 个/次），直到信息充分
  - `design`: 基于澄清后的需求生成方案，等待用户确认或修改
  - `generate`: 生成存储过程代码和对应的校验 SQL
  - `verify`: 自动执行语法校验和业务校验，输出结果
  - `deploy_check`: 部署前最终预检
  - `deploy`: 执行部署到 SQL Server

### 5.3 Verify 模块
- **语法校验**: 用 `SET PARSEONLY ON` 在 SQL Server 上检查语法
- **业务校验**: 执行 SP 获取结果 → 执行等价 SQL（模型生成）查询源表 → 比对金额/数量等关键数据。差异以对比表格展示

### 5.4 Deploy 模块
- **预检**: 遍历所有 SP 做最终校验（语法 + 业务），任一失败则阻止部署并高亮问题
- **部署**: 通过后逐个执行 `CREATE OR ALTER PROCEDURE`，结果反馈到前端

### 5.5 Config 模块
- 数据库连接：IP、端口、用户名、密码、账套
- LLM 配置：API Key、Base URL、Model Name
- 存储在 SQLite 中，界面提供配置表单

---

## 六、前端界面设计

### 6.1 整体布局

```
┌─────────────────────────────────────────────────────────────┐
│  顶部导航栏: [SP Generator]  [会话选择▼] [新建+] [设置⚙]     │
├────────────────────────────┬────────────────────────────────┤
│  左侧：对话区 (60%)         │  存储过程列表                    │
│                            │  ┌─────────────────────────┐   │
│  ┌──────────────────────┐  │  │ SP_1: 销售收入汇总  ✅   │   │
│  │ 用户: 我要做销售收入  │  │  │  ▸ 代码 | ✎ 编辑 | ✓ 校验│   │
│  │ 统计和比对...         │  │  └─────────────────────────┘   │
│  └──────────────────────┘  │  ┌─────────────────────────┐   │
│  ┌──────────────────────┐  │  │ SP_2: 收入比对分析  ✅   │   │
│  │ Agent: 好的, 请问...  │  │  │  ▸ 代码 | ✎ 编辑 | ✓ 校验│   │
│  └──────────────────────┘  │  └─────────────────────────┘   │
│                            │  校验SQL列表                     │
│  ┌──────────────────────┐  │  ┌─────────────────────────┐   │
│  │ 输入框...       [发送]│  │  │ Verify_1: 汇总金额比对   │   │
│  └──────────────────────┘  │  │ ▸ SQL | 📊 结果          │   │
│                            │  └─────────────────────────┘   │
├────────────────────────────┴────────────────────────────────┤
│  状态栏: 🟢 数据库已连接 | LLM: deepseek-v4-pro             │
└─────────────────────────────────────────────────────────────┘
```

### 6.2 核心交互
- **对话流**: 发送消息 → HTMX SSE 流式返回 → 消息逐字显示
- **右侧面板**: SP/校验项可展开，展开后内嵌 CodeMirror 编辑器（语法高亮）
- **SP 操作**: 代码（展开查看）| 编辑（可修改内容）| 校验（单文件重校验）
- **预检/部署**: 右下角两个按钮，预检通过后启用部署
- **配置页**: 点击设置图标进入配置表单

### 6.3 技术实现
- **HTML**: Jinja2 模板，单页面
- **交互**: HTMX 局部刷新 + SSE 流式对话
- **代码高亮**: CodeMirror 6 CDN
- **样式**: 内联 CSS，简洁干净

---

## 七、数据模型 (SQLite)

```sql
-- 会话表
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 消息表
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- 存储过程表
CREATE TABLE stored_procedures (
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
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- 校验 SQL 表
CREATE TABLE verify_queries (
    id TEXT PRIMARY KEY,
    sp_id TEXT NOT NULL,
    name TEXT NOT NULL,
    sql_code TEXT NOT NULL,
    compare_columns TEXT,
    status TEXT DEFAULT 'pending',
    result_detail TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (sp_id) REFERENCES stored_procedures(id)
);

-- 配置表
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 八、项目目录结构

```
sp_generator_aug/
├── .venv/                     # 项目虚拟环境
├── main.py                    # FastAPI 入口
├── config.py                  # 全局配置加载（从 SQLite 读取 DB/LLM 配置）
├── requirements.txt
├── app/
│   ├── __init__.py
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── graph.py           # LangGraph StateGraph 定义
│   │   ├── nodes.py           # 节点实现
│   │   ├── tools.py           # LangChain Tools
│   │   └── prompts.py         # System prompts + B1 核心表结构（OINV/INV1/OJDT 等）
│   ├── db/
│   │   ├── __init__.py
│   │   ├── sqlite.py          # SQLite 操作
│   │   └── sqlserver.py       # SQL Server 连接
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── chat.py
│   │   ├── sp.py
│   │   ├── verify.py
│   │   ├── deploy.py
│   │   ├── session.py
│   │   └── config_routes.py
│   ├── templates/
│   │   └── index.html
│   └── static/
│       └── style.css
├── docs/
│   └── superpowers/
│       └── specs/
│           └── 2026-07-12-sp-generator-design.md
└── CLAUDE.md
```

---

## 九、错误处理策略

- **数据库连接失败**: 状态栏红色提示，禁止发送消息
- **LLM 调用失败**: 对话中显示错误，支持重试
- **语法校验失败**: 右侧面板高亮错误行，对话中简要提示
- **业务校验不通过**: 差异表格在右侧面板展示，阻止部署
- **部署失败**: 逐个显示失败原因

---

## 十、关键依赖

```
fastapi
uvicorn
langchain
langgraph
pyodbc              # SQL Server 连接
httpx               # LLM API 调用
jinja2              # 模板引擎
python-multipart    # 表单处理
```
