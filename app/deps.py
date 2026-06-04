#!/usr/bin/env python3
"""FastAPI 依赖 — 本期只放 get_db；身份层（current_user）下一波再加。"""
from app.core import db


def get_db():
    """请求级 DB 连接：yield conn，finally close（配 Depends 用）。"""
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()
