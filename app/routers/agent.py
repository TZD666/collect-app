#!/usr/bin/env python3
"""接入助手路由 — /agent-crack（任务管理）。

从 server.py 的 handle_agent_crack 改写为 FastAPI 路由，请求/响应 JSON 契约不变。
CLI_AVAILABLE 判断改用 agent_tasks.agent_available()（多模型化后统一走 provider）。
同步 def（start/say 会起后台线程，本体不阻塞）。
"""
import threading
import time

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from app import agent_tasks
from app.agent_tasks import AGENT_TASKS, _alog, _agent_attempts, agent_available

router = APIRouter()


@router.post("/agent-crack")
def agent_crack(payload: dict = Body(...)):
    """接入助手任务：start 起线程分析 / status 轮询日志 / say 递线索续跑。"""
    try:
        action = payload.get("action")
        if action == "start":
            if not agent_available():
                return {"ok": False, "error": "本机无可用 AI 通道，接入助手不可用"}
            url = (payload.get("url") or "").strip()
            if not url:
                return JSONResponse(status_code=400, content={"error": "缺少 url"})
            tid = f"ac_{int(time.time()*1000)}"
            AGENT_TASKS[tid] = {
                "id": tid, "url": url, "state": "running",
                "log": [], "hints": [], "history": [], "html": "",
                "params": {k: payload.get(k) for k in ("title", "grp", "freq", "run_times", "backfill_days")},
                "source": None, "preview": [],
            }
            _alog(tid, "sys", f"任务开始：{url}")
            if not payload.get("external"):  # external=外部工作者（如终端 Claude）接管，不起内置线程
                threading.Thread(target=_agent_attempts, args=(tid,), daemon=True).start()
            return {"ok": True, "id": tid}
        elif action == "log":
            # 外部工作者实时回报：往任务日志追加一行，可顺带改状态/挂结果
            t = AGENT_TASKS.get(payload.get("id"))
            if not t:
                return JSONResponse(status_code=404, content={"error": "任务不存在"})
            _alog(t["id"], payload.get("who", "agent"), payload.get("text", ""))
            if payload.get("state") in ("running", "waiting", "success"):
                t["state"] = payload["state"]
            if payload.get("source"):
                t["source"] = payload["source"]
            return {"ok": True}
        elif action == "latest":
            # 页面启动时自动接上最近的活跃任务（服务重启/刷新后续命）
            live = [t for t in AGENT_TASKS.values() if t["state"] in ("running", "waiting")]
            live.sort(key=lambda t: t["id"], reverse=True)
            if live:
                return {"ok": True, "id": live[0]["id"], "state": live[0]["state"]}
            else:
                return {"ok": True, "id": None}
        elif action == "status":
            t = AGENT_TASKS.get(payload.get("id"))
            if not t:
                return JSONResponse(status_code=404, content={"error": "任务不存在（服务可能重启过）"})
            return {"ok": True, "state": t["state"], "log": t["log"],
                    "source": t.get("source"), "preview": t.get("preview", [])}
        elif action == "say":
            t = AGENT_TASKS.get(payload.get("id"))
            if not t:
                return JSONResponse(status_code=404, content={"error": "任务不存在（服务可能重启过）"})
            text = (payload.get("text") or "").strip()
            if not text:
                return JSONResponse(status_code=400, content={"error": "空消息"})
            _alog(t["id"], "user", text)
            t["hints"].append(text)
            if t["state"] in ("waiting",):
                t["state"] = "running"
                threading.Thread(target=_agent_attempts, args=(t["id"],), daemon=True).start()
            return {"ok": True}
        else:
            return JSONResponse(status_code=400, content={"error": f"未知 action: {action}"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
