#!/usr/bin/env python3
"""FastAPI 依赖 — get_db（请求级连接）+ current_user（身份解析）。"""
from fastapi import Depends, HTTPException, Request

from app.config import settings
from app.core import db


def get_db():
    """请求级 DB 连接：yield conn，finally close（配 Depends 用）。"""
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request, conn=Depends(get_db)) -> dict:
    """解析当前用户：
      single 模式 → 直返 u_default（启动已 ensure_default_user）。
      multi 模式  → 读 cookie 'sid' → session_user；无效抛 401。
    401 的 detail 在 main.py 的 exception_handler 里转成 {"error": detail}（前端契约）。"""
    if settings.AUTH_MODE == "single":
        user = db.user_get(conn, "u_default")
        if not user:
            user = db.ensure_default_user(conn)
        return user
    token = request.cookies.get("sid", "")
    user = db.session_user(conn, token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="未登录，请先登录")
    return user
