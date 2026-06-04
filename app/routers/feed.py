#!/usr/bin/env python3
"""feed 路由 — /feed（读取 / 打星(触发或删全文) / 批量已读 / GC）。

从 server.py 的 handle_feed 改写为 FastAPI 路由，请求/响应 JSON 契约与状态码不变。
打星时的全文抓取改调 app.core.fulltext.fetch_fulltext，落盘 {title,text,images,source_url}。
同步 def（内部有阻塞抓取/IO，FastAPI 自动丢线程池）。
"""
import json
import os
import time

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from app.core import db as cdb
from app.core.fulltext import fetch_fulltext
from app.deps import current_user

router = APIRouter()


@router.post("/feed")
def feed(payload: dict = Body(...), user: dict = Depends(current_user)):
    """feed 读取 / 打星(触发或删全文) / GC（按用户隔离）。"""
    try:
        action = payload.get("action")
        uid = user["id"]
        conn = cdb.connect()
        cdb.init_db(conn)
        try:
            if action == "list":
                return {"ok": True,
                        "items": cdb.feed_list(conn, uid, only_starred=payload.get("starred", False),
                                               source_id=payload.get("sourceId")),
                        "stats": cdb.stats(conn, uid)}
            elif action == "star":
                fid = payload["id"]
                star = int(payload.get("star", 0))
                f = cdb.feed_get(conn, fid, uid)
                if not f:
                    return JSONResponse(status_code=404, content={"error": "feed 不存在"})
                cdb.feed_set_star(conn, fid, star)
                if star > 0 and not f["fulltext"]:
                    # 打星 → 惰性抓全文
                    try:
                        doc = fetch_fulltext(f["url"])
                        dest = cdb.fulltext_path(fid)
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with open(dest, "w", encoding="utf-8") as wf:
                            json.dump({"title": doc.get("title", ""), "text": doc.get("text", ""),
                                       "images": doc.get("images", []), "source_url": doc.get("source_url", f["url"])},
                                      wf, ensure_ascii=False, indent=2)
                        cdb.feed_set_fulltext(conn, fid, True)
                        return {"ok": True, "fulltext": True,
                                "title": doc.get("title", ""), "chars": len(doc.get("text", ""))}
                    except Exception as e:
                        return {"ok": True, "fulltext": False, "warn": f"全文抓取失败：{e}"}
                elif star == 0:
                    # 取消星 → 删全文；窗口外旧文直接从资讯流移除（不再赖在 feed 里）
                    if f["fulltext"]:
                        cdb.fulltext_delete(fid)
                        cdb.feed_set_fulltext(conn, fid, False)
                    cutoff = time.strftime(
                        "%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
                    removed = False
                    if f.get("pub_date") and f["pub_date"] < cutoff:
                        conn.execute("DELETE FROM feed WHERE id=?", (fid,))
                        conn.commit()
                        removed = True
                    return {"ok": True, "fulltext": False, "removed": removed}
                else:
                    return {"ok": True}
            elif action == "read":
                # 批量标已读（前端入屏≥3s / 点开链接时上报）
                cdb.feed_mark_read(conn, payload.get("ids", []), uid)
                return {"ok": True}
            elif action == "fulltext":
                # 读取已落盘的全文（总结页阅读用）。先校验 feed 归属，防越权读他人全文
                fid = payload["id"]
                if not cdb.feed_get(conn, fid, uid):
                    return JSONResponse(status_code=404, content={"error": "feed 不存在"})
                fp = cdb.fulltext_path(fid)
                if not os.path.isfile(fp):
                    return JSONResponse(status_code=404, content={"error": "全文未保存或已删除"})
                with open(fp, encoding="utf-8") as rf:
                    return {"ok": True, "doc": json.load(rf)}
            elif action == "gc":
                # 手动清理：默认 0 = 立即清本用户所有无星
                n = cdb.gc(conn, user_id=uid, keep_unstarred_hours=int(payload.get("hours", 0)))
                return {"ok": True, "removed": n}
            else:
                return JSONResponse(status_code=400, content={"error": f"未知 action: {action}"})
        finally:
            conn.close()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
