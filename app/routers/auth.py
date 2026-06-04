#!/usr/bin/env python3
"""身份路由 — /auth/...（注册 / 登录 / 登出 / me / ai-config）。

前端契约（必须严格匹配，见 web/login.html、web/settings.html）：
  成功统一 HTTP 200 + {ok:true, ...}；失败 200 + {ok:false, error:中文}。
  登录/注册成功种 HttpOnly cookie 'sid'。
全 POST + payload: dict = Body(...)，同步 def（内部 scrypt/DB 阻塞，FastAPI 自动丢线程池）。
"""
import json

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from app.auth import service
from app.config import settings
from app.core import db as cdb
from app.deps import get_db, current_user

router = APIRouter()

_COOKIE = "sid"
_TTL_DAYS = 30
_MAX_AGE = _TTL_DAYS * 86400


def _set_cookie(resp: JSONResponse, token: str):
    resp.set_cookie(_COOKIE, token, max_age=_MAX_AGE, httponly=True,
                    samesite="lax", path="/")


@router.post("/auth/register")
def register(payload: dict = Body(...), conn=Depends(get_db)):
    try:
        token = service.register(conn, payload.get("username", ""), payload.get("password", ""))
    except ValueError as e:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(e)})
    resp = JSONResponse(content={"ok": True})
    _set_cookie(resp, token)
    return resp


@router.post("/auth/login")
def login(payload: dict = Body(...), conn=Depends(get_db)):
    try:
        token = service.login(conn, payload.get("username", ""), payload.get("password", ""))
    except ValueError as e:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(e)})
    resp = JSONResponse(content={"ok": True})
    _set_cookie(resp, token)
    return resp


@router.post("/auth/logout")
def logout(request: Request, conn=Depends(get_db)):
    token = request.cookies.get(_COOKIE, "")
    if token:
        cdb.session_delete(conn, token)
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(_COOKIE, path="/")
    return resp


@router.post("/auth/me")
def me(user: dict = Depends(current_user)):
    return {"ok": True, "username": user.get("username", ""), "auth_mode": settings.AUTH_MODE}


def _mask(plain: str) -> str:
    """掩码：有 key → "sk-***"+后4位（不足4位用「已配置」）；无 key → 空串。"""
    if not plain:
        return ""
    if len(plain) >= 4:
        return "sk-***" + plain[-4:]
    return "已配置"


@router.post("/auth/ai-config")
def ai_config(payload: dict = Body(...), user: dict = Depends(current_user), conn=Depends(get_db)):
    action = payload.get("action")
    try:
        ucfg = json.loads(user.get("ai_config") or "{}")
    except (ValueError, TypeError):
        ucfg = {}

    if action == "get":
        return {"ok": True, "config": {
            "channel": ucfg.get("channel") or settings.DEFAULT_AI_CHANNEL,
            "base_url": ucfg.get("base_url") or "",
            "api_key_mask": _mask(service.dec_secret(ucfg.get("api_key") or "")),
            "model": ucfg.get("model") or "",
        }}
    elif action == "save":
        cfg = payload.get("config") or {}
        new = {
            "channel": (cfg.get("channel") or "").strip(),
            "base_url": (cfg.get("base_url") or "").strip(),
            "model": (cfg.get("model") or "").strip(),
        }
        # api_key 为空字符串 = 不覆盖已存的；有值才 enc 后覆盖
        incoming_key = (cfg.get("api_key") or "").strip()
        if incoming_key:
            new["api_key"] = service.enc_secret(incoming_key)
        elif ucfg.get("api_key"):
            new["api_key"] = ucfg["api_key"]  # 保留旧密文
        cdb.user_set_ai_config(conn, user["id"], json.dumps(new, ensure_ascii=False))
        return {"ok": True}
    else:
        return JSONResponse(status_code=400, content={"error": f"未知 action: {action}"})
