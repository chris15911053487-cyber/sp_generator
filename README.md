# 🗄️ SP Generator — SAP B1 存储过程智能体

一个基于 LLM 的智能化 SAP Business One 存储过程生成系统，通过自然语言交互自动设计、生成、校验和部署 SQL Server 存储过程。

## ✨ 核心功能

- **需求澄清**：通过多轮对话澄清用户需求，提出关键问题
- **智能设计**：基于需求自动生成简洁高效的 SP 设计方案
- **代码生成**：从设计方案自动生成可部署的 T-SQL 存储过程
- **双层校验**：同时进行语法校验和业务数据校验，支持自动修复
- **一键部署**：通过 Web 界面安全部署到 SQL Server
- **实时对话**：流式输出设计方案和修改建议，支持在线编辑

## 🚀 快速开始

### 前置需求

- Docker & Docker Compose
- SQL Server（与 SAP B1 相同实例）
- DeepSeek API Key（或其他兼容 OpenAI 的 LLM）

### 部署方式

```bash
# 进入项目目录
cd sp_generator

# 构建并启动
docker compose up -d --build

# 查看日志
docker compose logs -f
```

启动后访问：`http://<服务器IP>:8000`

## ⚙️ 配置说明

首次使用需要在 Web 界面配置数据库和 LLM：

### 数据库配置
1. 点击右上角 ⚙️ 进入系统配置
2. 填写 SQL Server 连接信息：
   - **服务器地址**：SQL Server 所在主机 IP
   - **端口**：通常 1433（B1 用 1400 等非标端口则相应调整）
   - **用户名**：sa 或有权限的账户
   - **密码**：对应账户密码
   - **账套**：B1 所在数据库名
3. 点击"测试连接"验证
4. 保存配置

### LLM 配置
- **API Key**：DeepSeek 或其他 LLM 的 API Key
- **Base URL**：LLM 服务的 API 端点（默认 `https://api.deepseek.com/v1`）
- **Model Name**：使用的模型名称（默认 `deepseek-v4-pro`）

## 📖 使用流程

### 1. 创建新会话
点击左上角"➕ 新建"，为本次需求创建独立会话

### 2. 描述需求
在底部输入框描述你的存储过程需求，按 `Enter` 发送（`Shift+Enter` 换行）

### 3. 澄清阶段 (Clarify)
AI 会提出关键问题帮助理解需求，最多 5 个问题
- 点击"⏭️ 跳过澄清"可直接进入设计阶段
- 或输入回答继续澄清

### 4. 设计方案 (Design)
AI 生成简洁的 SP 设计方案，包括：
- 存储过程列表（名称 + 用途）
- 参数定义
- 需确认的关键假设

确认方案或提出修改意见：
- 点击"✅ 确认方案，开始生成"执行代码生成
- 点击"🔄 重新设计"重新调整方案
- 或直接输入修改意见

### 5. 代码生成 (Generate)
系统自动生成存储过程代码，显示在右侧面板

### 6. 校验 (Verify)
- **语法校验**：检查 T-SQL 语法
- **业务校验**：运行等价 SQL 验证业务逻辑

校验结果：
- ✅ 通过：显示"✅ 一键部署"按钮
- ❌ 失败：显示"🔧 自动修复"按钮，或"🔄 重新生成"

### 7. 部署 (Deploy)
- 点击"🚀 一键部署"将存储过程写入 SQL Server
- 或点击右侧"🔍 一键预检"进行部署前最后检查

## 🎮 快捷操作

### 对话快捷键
- **Enter**：发送消息
- **Shift+Enter**：换行
- **⏹ 停止**：中断长时间生成（发送期间显示）

### 快捷按钮
在 AI 回复后出现的快捷按钮：
- **澄清阶段**：⏭️ 跳过澄清，直接设计
- **设计方案**：✅ 确认方案 / 🔄 重新设计
- **校验通过**：🚀 一键部署 / 🔄 重新生成
- **校验失败**：🔧 自动修复 / 🔄 重新生成
- **出错**：🔄 重试

### 面板调整
中间的分割条可拖拽调整左右面板宽度（范围 25%-75%）

## 📋 常见问题

### Q: Docker 容器连不上 SQL Server？
A: 
- 检查 SQL Server 是否允许远程连接
- 确认防火墙允许 1433（或自定义端口）出入
- 如果 SQL Server 在宿主机本地，使用 `host.docker.internal` 或宿主机实际 IP 而非 `localhost`

### Q: 生成的 SP 代码过于复杂？
A: 
- AI 设计时遵循"最简方案优先"原则
- 生成时会检查"简洁高效"约束
- 如果仍不满意可在校验后反馈具体修改意见

### Q: 校验失败提示"INNER JOIN 导致数据缺失"？
A: 这是常见的数据关联问题，修改意见后系统会自动重新生成并采用 LEFT JOIN 等合适的方式

### Q: 怎样保存当前的 SP 设计方案？
A: 确认后代码自动保存在数据库，可在右侧面板查看历史版本，也可部署后在 SQL Server 中查看源代码

## 🏗️ 项目结构

```
sp_generator/
├── Dockerfile              # Docker 镜像配置
├── docker-compose.yml      # 容器编排
├── requirements.txt        # Python 依赖
├── main.py                 # FastAPI 应用入口
├── config.py               # 全局配置管理
├── app/
│   ├── main.py             # 应用初始化
│   ├── routes/             # FastAPI 路由
│   │   ├── chat.py         # 对话接口（SSE 流式）
│   │   ├── sp.py           # 存储过程管理
│   │   ├── verify.py       # 校验接口
│   │   ├── deploy.py       # 部署接口
│   │   ├── session.py      # 会话管理
│   │   └── config_routes.py # 系统配置接口
│   ├── agent/              # LangGraph AI 智能体
│   │   ├── graph.py        # 状态图定义
│   │   ├── nodes.py        # 各阶段节点实现
│   │   ├── prompts.py      # LLM Prompt 模板
│   │   └── tools.py        # AI 工具函数
│   ├── db/                 # 数据库操作
│   │   ├── sqlite.py       # SQLite（配置+会话）
│   │   └── sqlserver.py    # SQL Server（业务 DB）
│   ├── static/             # 前端静态文件
│   └── templates/          # HTML 模板
└── data/                   # 数据目录（SQLite）
```

## 🔧 技术栈

- **后端框架**：FastAPI + LangGraph
- **LLM**：LangChain (DeepSeek/OpenAI)
- **数据库**：SQL Server (业务) + SQLite (配置)
- **前端**：原生 HTML/CSS/JS + Marked (Markdown) + CodeMirror (代码编辑)
- **容器化**：Docker + docker-compose
- **并发**：异步 Python (async/await) + ThreadPoolExecutor

## 🛠️ 开发相关

### 本地开发运行
```bash
# 安装依赖
pip install -r requirements.txt

# 本地运行（需要 Python 3.11+）
python main.py
# 访问 http://localhost:8000
```

### 调试
容器日志可通过以下查看：
```bash
docker compose logs -f sp-generator
```

SQL 相关调试可在数据库中查看：
```bash
# SQLite 配置数据库
sqlite3 data/app.db

# 查看会话记录
SELECT * FROM chat_messages WHERE session_id = '...';

# 查看生成的 SP
SELECT * FROM stored_procedures WHERE session_id = '...';
```

## 📝 Prompt 设计理念

系统采用三阶段 Prompt 策略：

1. **SYSTEM_PROMPT**：定义 AI 角色、原则和 B1 领域知识
2. **DESIGN_PROMPT**：精简设计输出，强调"最简方案优先"，避免过度设计
3. **GENERATE_PROMPT**：添加代码简洁性约束，生成高效代码

## 🤝 贡献指南

欢迎 PR 和 Issue 反馈。主要改进方向：
- 支持更多 LLM 模型（Claude, Llama 等）
- 增强 B1 知识库的表结构覆盖
- UI 优化和国际化
- 性能优化（缓存、并发限制）

## 📄 许可证

MIT License
