"""FastAPI 入口 — SP Generator 应用启动。"""
import os
import sys
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

sys.path.insert(0, os.path.dirname(__file__))

from config import init_config
from app.db.sqlite import init_db
from app.routes import session, config_routes, chat, sp, verify, deploy

init_config()
init_db()

app = FastAPI(title="SP Generator", version="1.0.0")

static_dir = os.path.join(os.path.dirname(__file__), "app", "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.include_router(session.router)
app.include_router(config_routes.router)
app.include_router(chat.router)
app.include_router(sp.router)
app.include_router(verify.router)
app.include_router(deploy.router)

templates_dir = os.path.join(os.path.dirname(__file__), "app", "templates")
templates = Jinja2Templates(directory=templates_dir)


@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/config")
async def config_page():
    config_html = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head><meta charset="UTF-8"><title>配置 — SP Generator</title>
    <link rel="stylesheet" href="/static/style.css"></head>
    <body>
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
    </body></html>
    """
    return HTMLResponse(content=config_html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
