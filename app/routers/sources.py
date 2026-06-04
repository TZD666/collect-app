#!/usr/bin/env python3
"""监控源路由 — /mon-source（CRUD）+ /mon-run（手动抓取）。

从 server.py 的 handle_mon_source / handle_mon_run 改写为 FastAPI 路由，
请求/响应 JSON 契约与状态码严格保持不变。同步 def（内部有阻塞网络调用，
FastAPI 自动丢线程池）。
"""
from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from app.core import crawler
from app.core import db as cdb
from app.agent_tasks import agent_available

router = APIRouter()


@router.post("/mon-source")
def mon_source(payload: dict = Body(...)):
    """监控源 CRUD；add 先试抓校验（结构化源优先，HTML 列表主路）。"""
    try:
        action = payload.get("action")
        conn = cdb.connect()
        cdb.init_db(conn)
        try:
            if action == "list":
                return {"ok": True, "sources": cdb.source_list(conn),
                        "stats": cdb.stats(conn)}
            elif action == "add":
                url = (payload.get("url") or "").strip()
                if not url:
                    return JSONResponse(status_code=400, content={"error": "缺少 url"})
                try:
                    kind, items = crawler.discover(url)
                except Exception as e:
                    return {"ok": False, "agent_available": agent_available(),
                            "error": f"试抓失败：{e}"}
                if not items:
                    return {"ok": False, "agent_available": agent_available(),
                            "error": "该页未解析出文章链接（换个栏目列表页，或详情页日期规律更清晰的源）"}
                src = cdb.source_add(conn, url, payload.get("title") or url, kind,
                                     payload.get("freq", "daily"), payload.get("grp", ""),
                                     payload.get("run_times", "08:00"),
                                     payload.get("backfill_days", 7))
                return {"ok": True, "source": src,
                        "preview": items[:5], "count": len(items)}
            elif action == "delete":
                cdb.source_delete(conn, payload["id"])
                return {"ok": True}
            elif action == "toggle":
                cdb.source_toggle(conn, payload["id"], payload.get("enabled", True))
                return {"ok": True}
            else:
                return JSONResponse(status_code=400, content={"error": f"未知 action: {action}"})
        finally:
            conn.close()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/mon-run")
def mon_run(payload: dict = Body(...)):
    """手动触发抓取 = 对所有启用源强制「首抓」：无视冷却、按各源回溯天数重拉，
    清理过的窗口内条目也会找回（定时调度仍走增量，不受影响）。"""
    try:
        crawler.run(only_source_id=payload.get("sourceId"), force=payload.get("force", True))
        conn = cdb.connect()
        st = cdb.stats(conn)
        conn.close()
        return {"ok": True, "stats": st}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
