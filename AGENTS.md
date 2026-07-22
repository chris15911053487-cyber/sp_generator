# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.



## 5. Python 环境

虚拟环境位于项目根目录的 `.venv/`。所有包管理操作必须针对该环境：

- 激活：`source .venv/Scripts/activate` (Windows Git Bash) 或 `.venv\Scripts\activate` (CMD)
- pip：`.venv/Scripts/pip.exe install <package>`
- 运行：`.venv/Scripts/python.exe <script>`

禁止使用全局 `pip install` 或安装到其他环境。

## 6. 自动测试方法

Codex 在修改代码后应主动执行与改动相匹配的测试，不必等待用户再次要求。测试失败时先定位并修复，再重复测试；不得只报告失败后停止。

### Linux / Docker 环境

当前 Linux 工作区没有宿主机 Python，项目的可靠测试环境是 Docker。 `pytest==8.4.1` 已写入 `requirements.txt`。不要在宿主机全局安装 Python 包。

测试文件被 `.dockerignore` 的 `test_*.py` 排除，因此运行测试时需把项目目录只读挂载到 `/workspace`。禁用 pytest 缓存可避免只读目录警告。

每次代码改动至少依次执行：

1. 检查补丁格式：

```bash
git diff --check
```

2. 如果应用代码或依赖有变化，构建最新测试镜像：

```bash
docker compose build sp-generator
```

3. 检查应用代码语法：

```bash
docker compose run --rm --no-deps sp-generator python -m compileall -q app
```

4. 优先运行与改动直接相关的测试。例如方案确认流程：

```bash
docker compose run --rm --no-deps \
  -v "$PWD:/workspace:ro" \
  sp-generator pytest -q -p no:cacheprovider \
  /workspace/test_design_confirmation.py
```

5. 运行当前不依赖真实 LLM、SQL Server 或已启动服务的单元测试集合：

```bash
docker compose run --rm --no-deps \
  -v "$PWD:/workspace:ro" \
  sp-generator pytest -q -p no:cacheprovider \
  /workspace/test_clarify.py \
  /workspace/test_invoke_mock.py \
  /workspace/test_design_confirmation.py \
  /workspace/test_verify_autofix.py
```

上述单元测试命令已验证可用，当前结果为 `12 passed`。

### 服务部署后的检查

只有任务包含部署或要求更新正在运行的服务时，才执行：

```bash
docker compose up -d --force-recreate sp-generator
docker compose ps
docker compose logs --tail=80 sp-generator
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8000/
```

成功标准：容器状态为 `Up`、启动日志包含 `Application startup complete`、首页返回 `HTTP 200`。服务刚启动不足一秒时首次请求可能被重置，应查看日志后再重试一次。

### 集成测试与 E2E 限制

- `test_improvements.py` 会访问 `127.0.0.1:8000` 并创建测试会话，不属于纯 pytest 单元测试，不能混入默认测试集合。
- `test_e2e.py` 当前使用 Windows `.venv/Scripts/python.exe`、`netstat/findstr` 和 `taskkill`，并会调用真实 LLM、SQL Server、生成及校验 SP。仅在 Windows 测试环境、配置齐全且用户明确要求 E2E 时运行。
- 不要为了让测试通过而连接、修改或部署到真实业务数据库，除非该外部操作明确属于用户授权范围。


---

Always reply in Chinese. 请始终使用中文回复。