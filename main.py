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


@app.get("/help")
async def help_page():
    readme_path = os.path.join(os.path.dirname(__file__), "README.md")
    try:
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_content = f.read()
    except FileNotFoundError:
        return HTMLResponse(content="<h2>帮助文件未找到</h2><p>README.md 不存在，请重新构建容器。</p><a href='/'>← 返回</a>", status_code=200)
    # 转义反引号避免 JS 模板字符串冲突
    readme_escaped = readme_content.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    help_html = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>帮助 — SP Generator</title>
        <link rel="stylesheet" href="/static/style.css">
        <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/14.1.0/marked.min.js"></script>
        <style>
            .help-page {{
                max-width: 900px;
                margin: 0 auto;
                padding: 24px 32px;
                background: #fff;
                min-height: 100vh;
            }}
            .help-page img {{ max-width: 100%; }}
            .help-page h1 {{ font-size: 28px; margin-bottom: 16px; border-bottom: 2px solid #6c5ce7; padding-bottom: 12px; }}
            .help-page h2 {{ font-size: 20px; margin-top: 32px; margin-bottom: 12px; color: #6c5ce7; }}
            .help-page h3 {{ font-size: 16px; margin-top: 20px; margin-bottom: 8px; }}
            .help-page p {{ margin: 8px 0; line-height: 1.8; color: #2d3436; }}
            .help-page ul, .help-page ol {{ padding-left: 24px; margin: 8px 0; }}
            .help-page li {{ margin: 4px 0; line-height: 1.7; }}
            .help-page code {{ background: #f0f0f5; padding: 2px 6px; border-radius: 3px; font-size: 13px; }}
            .help-page pre {{ background: #2d3436; color: #dfe6e9; padding: 14px 18px; border-radius: 8px; overflow-x: auto; margin: 12px 0; }}
            .help-page pre code {{ background: none; color: inherit; padding: 0; }}
            .help-page blockquote {{ border-left: 3px solid #6c5ce7; padding: 8px 16px; margin: 12px 0; background: #f8f9fc; border-radius: 0 4px 4px 0; }}
            .help-page table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
            .help-page th, .help-page td {{ border: 1px solid #dfe6e9; padding: 8px 12px; text-align: left; }}
            .help-page th {{ background: #f5f6fa; }}
            .help-page strong {{ color: #6c5ce7; }}
            .help-back {{ display: inline-block; margin-bottom: 16px; padding: 6px 16px; background: #6c5ce7; color: #fff; text-decoration: none; border-radius: 6px; font-size: 14px; }}
            .help-back:hover {{ background: #5b4cdb; }}
        </style>
    </head>
    <body>
        <div class="help-page">
            <a href="/" class="help-back">← 返回主界面</a>
            <div id="help-content"></div>
        </div>
        <script>
            const raw = `{readme_escaped}`;
            document.getElementById('help-content').innerHTML = marked.parse(raw);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=help_html)


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
    async function loadConfig() {
        const r = await fetch('/api/config');
        const d = await r.json();
        document.getElementById('cfg-db-server').value = d.db.server || '';
        document.getElementById('cfg-db-port').value = d.db.port || '';
        document.getElementById('cfg-db-user').value = d.db.user || '';
        document.getElementById('cfg-db-password').value = d.db.password || '';
        document.getElementById('cfg-db-database').value = d.db.database || '';
        document.getElementById('cfg-llm-key').value = d.llm.api_key || '';
        document.getElementById('cfg-llm-url').value = d.llm.base_url || '';
        document.getElementById('cfg-llm-model').value = d.llm.model_name || '';
    }
    async function testDbConnection() {
        document.getElementById('db-test-result').textContent = '⏳ 连接中...';
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
    window.onload = loadConfig;
    </script>
    </body></html>
    """
    return HTMLResponse(content=config_html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
