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
