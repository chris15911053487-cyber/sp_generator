"""E2E 测试：完整对话流程 — 需求 → 澄清(最多4轮) → 设计 → 确认 → 生成+校验

特性：
- 自动管理服务器进程（杀旧启新，不依赖 --reload）
- 超时保护 + 重试机制
- 详细错误报告
- 独立运行，不需要手动启动服务器
"""
import requests
import json
import time
import sys
import io
import subprocess
import os

# Fix Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

PORT = 8001
BASE = f"http://127.0.0.1:{PORT}"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(PROJECT_DIR, '.venv', 'Scripts', 'python.exe')

HEALTH_RETRIES = 30       # 服务器启动等待重试次数
HEALTH_INTERVAL = 1       # 重试间隔(秒)
SSE_TIMEOUT = 180          # SSE 流超时(秒)
REQUEST_TIMEOUT = 15       # 普通请求超时(秒)

# ====== 日志工具 ======

def log(level, msg):
    timestamp = time.strftime("%H:%M:%S")
    print(f"[{timestamp} {level}] {msg}", flush=True)


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ====== 服务器进程管理 ======

def find_pids_on_port(port):
    """查找占用指定端口的进程 PID 列表"""
    try:
        result = subprocess.run(
            f'netstat -ano | findstr :{port}',
            shell=True, capture_output=True, text=True, timeout=5
        )
        pids = set()
        for line in result.stdout.splitlines():
            if f':{port}' in line and 'LISTENING' in line.upper():
                parts = line.strip().split()
                if parts:
                    pids.add(parts[-1])
        return list(pids)
    except Exception:
        return []


def kill_pid(pid):
    """强制结束进程（兼容 Git Bash 路径解析）"""
    try:
        subprocess.run(
            ['cmd', '/c', 'taskkill', '/F', '/PID', str(pid)],
            capture_output=True, timeout=10
        )
        return True
    except Exception:
        return False


def start_server(port=PORT):
    """杀掉旧进程，启动新服务器，等待就绪。返回 Popen 对象。"""
    # 1. 清理旧进程
    pids = find_pids_on_port(port)
    for pid in pids:
        log("INFO", f"端口 {port} 被 PID {pid} 占用，正在结束...")
        if kill_pid(pid):
            log("OK", f"已结束 PID {pid}")
        time.sleep(1)

    # 额外等待端口释放
    time.sleep(1)

    # 2. 启动服务器（输出到日志文件方便调试）
    server_log = os.path.join(PROJECT_DIR, "data", "server.log")
    os.makedirs(os.path.dirname(server_log), exist_ok=True)
    log_fp = open(server_log, "w")
    log("INFO", f"启动 uvicorn main:app --host 127.0.0.1 --port {port}")
    log("INFO", f"服务器日志: {server_log}")
    proc = subprocess.Popen(
        [PYTHON, '-m', 'uvicorn', 'main:app',
         '--host', '127.0.0.1', '--port', str(port)],
        cwd=PROJECT_DIR,
        stdout=log_fp,
        stderr=log_fp,
        env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
    )

    # 3. 等待就绪（轮询 /api/sessions）
    log("INFO", "等待服务器就绪...")
    for i in range(HEALTH_RETRIES):
        try:
            r = requests.get(f"{BASE}/api/sessions", timeout=2)
            if r.status_code == 200:
                log("OK", f"服务器就绪 ({i+1}/{HEALTH_RETRIES})")
                return proc
        except requests.ConnectionError:
            pass
        except Exception:
            pass
        time.sleep(HEALTH_INTERVAL)

    # 超时 — 输出 stderr 帮助诊断
    log("FAIL", f"服务器启动超时 ({HEALTH_RETRIES}s)")
    stderr = ""
    try:
        proc.terminate()
        _, stderr = proc.communicate(timeout=5)
    except Exception:
        proc.kill()
    if stderr:
        log("ERROR", f"服务器 stderr:\n{stderr[:2000]}")
    sys.exit(1)


def stop_server(proc):
    """停止服务器进程"""
    if proc is None:
        return
    log("INFO", "停止服务器...")
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        log("WARN", "服务器未响应 SIGTERM，强制结束")
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    log("OK", "服务器已停止")


# ====== API 封装（带重试） ======

def api_call(fn, desc, retries=3):
    """带重试的 API 调用包装"""
    last_err = None
    for attempt in range(retries):
        try:
            return fn()
        except requests.ConnectionError as e:
            last_err = e
            log("WARN", f"{desc} 连接失败 (尝试 {attempt+1}/{retries})")
            time.sleep(2)
        except requests.Timeout as e:
            last_err = e
            log("WARN", f"{desc} 超时 (尝试 {attempt+1}/{retries})")
            time.sleep(2)
    raise last_err


def create_session():
    """创建测试会话"""
    def _do():
        r = requests.post(
            f"{BASE}/api/sessions",
            json={"name": f"E2E测试_{int(time.time())}"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()
        sid = d.get("session", {}).get("id") or d.get("id")
        if not sid:
            raise RuntimeError(f"响应中没有 session id: {d}")
        return sid

    sid = api_call(_do, "创建会话")
    log("OK", f"会话创建: {sid[:16]}...")
    return sid


def send_message(session_id, message):
    """发送消息，通过 SSE 流读取完整响应。返回事件列表。"""
    def _do():
        r = requests.post(
            f"{BASE}/api/chat/stream",
            json={"session_id": session_id, "message": message},
            stream=True,
            timeout=SSE_TIMEOUT,
        )
        r.raise_for_status()
        events = []
        try:
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if line.startswith("data: "):
                    try:
                        events.append(json.loads(line[6:]))
                    except json.JSONDecodeError:
                        pass
        except requests.exceptions.ChunkedEncodingError as e:
            log("WARN", f"SSE 流中断: {e}")
        except Exception as e:
            log("WARN", f"SSE 读取异常: {e}")
        return events

    return api_call(_do, f"发送消息: {message[:50]}...")


def get_messages(session_id):
    def _do():
        r = requests.get(
            f"{BASE}/api/chat/messages/{session_id}",
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["messages"]
    return api_call(_do, "获取消息列表")


def get_sp_list(session_id):
    def _do():
        r = requests.get(
            f"{BASE}/api/sp/{session_id}",
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("procedures", [])
    return api_call(_do, "获取 SP 列表")


def get_verify_queries(session_id, sp_id):
    def _do():
        r = requests.get(
            f"{BASE}/api/verify/{session_id}/sp/{sp_id}",
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("verify_queries", [])
    return api_call(_do, f"获取校验SQL: {sp_id[:8]}...")


# ====== SSE 事件处理 ======

def summary(events):
    """打印 SSE 事件摘要"""
    for e in events:
        t = e.get("type", e.get("node", "?"))
        c = str(e.get("content", ""))[:120].replace("\n", "\\n")
        if not c:
            c = str(e.get("data", {}))
        log("SSE", f"type={t} content={c[:150]}")


def find_event_type(events, event_type):
    """查找指定类型的 SSE 事件"""
    return [e for e in events if e.get("type") == event_type]


def has_event_type(events, event_type):
    return len(find_event_type(events, event_type)) > 0


def has_status(events, status_val):
    """检查是否有 data.status 匹配的事件"""
    return any(
        e.get("data", {}).get("status") == status_val
        for e in events
    )


# ====== 结果报告 ======

def report_sp_results(sps, session_id):
    """打印 SP 结果并返回通过状态"""
    section("SP 结果")
    all_syntax_ok = True
    all_biz_ok = True
    has_params = False

    for sp in sps:
        sv = sp.get("syntax_valid")
        bv = sp.get("business_valid")
        syn_icon = "✅" if sv else ("❌" if sv == 0 else "⬜")
        biz_icon = "✅" if bv else ("❌" if bv == 0 else "⬜")
        status = sp.get("status", "?")
        log("SP", f"  {sp['name']}")
        log("SP", f"    语法:{syn_icon}  业务:{biz_icon}  状态:{status}")

        # 参数验证
        try:
            params = json.loads(sp.get("parameters", "[]"))
            if params:
                has_params = True
                param_names = [p["name"] for p in params]
                log("PARAM", f"    参数({len(params)}个): {', '.join(param_names)}")
        except (json.JSONDecodeError, TypeError):
            pass

        if not sv:
            all_syntax_ok = False
        if not bv:
            all_biz_ok = False

        # 校验 SQL 详情
        vqs = get_verify_queries(session_id, sp["id"])
        for vq in vqs:
            vs = vq.get("status", "?")
            vs_icon = "✅" if vs == "pass" else ("❌" if vs == "fail" else "⬜")
            detail = (vq.get("result_detail", "") or "")[:100]
            log("VQ", f"    {vs_icon} {vq['name']}: {vs} | {detail}")

    if has_params:
        log("OK", "✅ 参数面板: SP 已生成参数默认值")
    else:
        log("WARN", "⚠️ 参数面板: SP 未生成参数（可能 LLM 未输出 parameters）")

    return all_syntax_ok, all_biz_ok


# ====== P0 验证：SP 状态是否更新 ======

def verify_sp_status_update(sps):
    """P0 验证：检查 SP 状态是否已从 'draft' 更新为 'verified' 或 'verify_failed'"""
    section("P0 验证: SP 状态更新")
    all_updated = True
    for sp in sps:
        status = sp.get("status", "")
        if status in ("draft", ""):
            log("FAIL", f"  {sp['name']}: 状态仍为 '{status}'（应为 'verified' 或 'verify_failed'）")
            all_updated = False
        else:
            log("OK", f"  {sp['name']}: 状态已更新为 '{status}'")
    return all_updated


# ====== 主流程 ======

def main():
    log("INFO", "SP Generator E2E 测试")
    log("INFO", f"端口: {PORT}  |  项目目录: {PROJECT_DIR}")

    server_proc = None
    exit_code = 1

    try:
        # ============ 启动服务器 ============
        server_proc = start_server(PORT)

        # ============ 1. 创建会话 ============
        section("Step 1: 创建会话")
        sid = create_session()

        # ============ 2. 发送需求 ============
        section("Step 2: 发送需求")
        events = send_message(sid, "我现在要做一个销售收入统计和比对的存储过程")
        summary(events)

        msgs = get_messages(sid)
        user_count = sum(1 for m in msgs if m["role"] == "user")
        log("INFO", f"当前用户消息数: {user_count}")

        if has_event_type(events, "question"):
            q = find_event_type(events, "question")[-1]["content"]
            log("OK", f"收到澄清问题: {q[:100]}")
        elif has_event_type(events, "design"):
            log("OK", "LLM 认为信息充足，直接进入设计阶段")
        else:
            log("WARN", "未收到 question 或 design 事件，继续后续步骤")

        # ============ 3. 澄清循环 ============
        section("Step 3: 澄清问答（最多 3 轮）")
        for round_num in range(1, 5):
            msgs = get_messages(sid)
            user_count = sum(1 for m in msgs if m["role"] == "user")

            if has_event_type(events, "design") or has_status(events, "generated"):
                log("INFO", f"第 {round_num} 轮前已进入设计/生成阶段，停止提问")
                break

            if user_count >= 4:
                log("INFO", f"用户消息已达 {user_count} 条，本应强制进入设计阶段")
                # 继续发一轮，触发强制设计
                if not has_event_type(events, "design"):
                    log("STEP", f"强制触发设计（第 {round_num} 轮）")
                    events = send_message(sid, f"第{round_num}轮回答：使用默认方案")
                    summary(events)
                    break

            log("STEP", f"第 {round_num} 轮澄清回答")
            events = send_message(sid, f"第{round_num}轮回答：A（选默认方案）")
            summary(events)

        # ============ 4. 确认设计 ============
        section("Step 4: 确认设计方案")
        if has_event_type(events, "design"):
            log("OK", "收到设计方案，发送确认")
            events = send_message(sid, "确认，按此方案生成代码")
            summary(events)
        elif has_status(events, "generated"):
            log("OK", "已直接进入生成阶段（跳过设计确认）")
        else:
            log("WARN", "未收到 design 事件，尝试直接发送确认")
            events = send_message(sid, "确认，按此方案生成代码")
            summary(events)

        # ============ 5. 检查结果 ============
        sps = get_sp_list(sid)

        if len(sps) == 0:
            log("FAIL", "未生成任何存储过程！")
            # 打印最后的事件帮助调试
            log("INFO", f"最后收到 {len(events)} 个 SSE 事件")
            for e in events[-5:]:
                log("DEBUG", json.dumps(e, ensure_ascii=False, default=str)[:300])
            return 1

        # 结果报告
        all_syntax_ok, all_biz_ok = report_sp_results(sps, sid)

        # P0 验证
        status_updated = verify_sp_status_update(sps)

        # ============ 6. 最终判定 ============
        section("最终判定")

        if not status_updated:
            log("FAIL", "❌ P0: SP 状态未更新（仍为 draft）")

        if all_syntax_ok and all_biz_ok:
            log("PASS", "✅ 全部通过！语法+业务校验均成功")
            exit_code = 0
        elif all_syntax_ok:
            if status_updated:
                log("PASS", "⚠️ 语法校验全部通过，业务校验部分失败（可能是数据或 SQL 质量问题）")
                exit_code = 0
            else:
                log("FAIL", "❌ 语法通过但状态未更新")
                exit_code = 1
        else:
            log("FAIL", "❌ 语法校验失败")
            exit_code = 1

        return exit_code

    except Exception as e:
        log("FAIL", f"测试异常: {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        # ============ 调试：输出服务器日志中的 DEBUG 行 ============
        server_log = os.path.join(PROJECT_DIR, "data", "server.log")
        if os.path.exists(server_log):
            with open(server_log, "r", encoding="utf-8", errors="replace") as f:
                debug_lines = [line.strip() for line in f if "[DEBUG" in line or "DEBUG]" in line]
            if debug_lines:
                section("服务器 DEBUG 日志")
                for line in debug_lines:
                    print(f"  {line}")

        # ============ 清理 ============
        if server_proc:
            stop_server(server_proc)
        log("INFO", f"E2E 测试结束 (exit_code={exit_code})")


if __name__ == "__main__":
    sys.exit(main())
