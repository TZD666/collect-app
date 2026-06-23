#!/usr/bin/env python3
"""FastAPI 应用入口 — 收集App。

职责：
  · lifespan：启动 init_db + 建数据/全文目录 + 起后台调度线程；退出优雅停线程。
  · 注册路由（统一前缀 /api）。
  · GET /api/info 探活。
  · 同源静态托管 web/（单文件全中文前端）。

调度线程迁自 server.py 的 _collect_scheduler：每轮 crawler.run() 增量抓取，
每晚 2:00 全清资讯流（无星删除、打星归档进重点新闻；幂等一天一次），
用 threading.Event().wait(60) 替代 sleep——退出时 stop.set() 可即时唤醒优雅退出。
"""
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.core import db
from app.routers import sources, feed, agent, auth, assistant

# 调度线程停止信号（lifespan 退出时 set）
_stop_event = threading.Event()


def _scheduler_loop():
    """后台调度：每 60s 增量抓取一轮 + 每晚 2:00 全清资讯流（无星删、打星归档；幂等）。
    异常打印中文继续，不让单次失败拖垮整个调度。"""
    from app.core import crawler
    while not _stop_event.is_set():
        try:
            crawler.run()
        except Exception as e:
            print(f"[收集调度] 抓取异常：{e}", flush=True)
        try:
            conn = db.connect()
            db.init_db(conn)
            today = time.strftime("%Y-%m-%d")
            # last_read_clean 用全局态（user_id=''）：一天一次幂等，不分用户。
            # 补偿式触发：不再死等「恰好 tm_hour==2」那一刻——只要当天还没清过且已过 2:00 就清。
            # 个人 Mac 半夜睡眠会错过 2 点窗口（进程被挂起），改这样后醒来当天首次跑到这里即补清。
            # before_date=today：只清「今天之前」的条目，避免补偿时误删当天还没来得及筛选的新条目。
            if time.localtime().tm_hour >= 2 and db.meta_get(conn, "last_read_clean") != today:
                n_del, n_arc = db.feed_daily_clear(conn, before_date=today)  # 无星删除、打星归档进重点新闻
                db.session_gc(conn)          # 顺手清过期会话
                db.meta_set(conn, "last_read_clean", today)
                print(f"[收集调度] 资讯流日清（今天之前·补偿式）：删除 {n_del} 条 / 归档 {n_arc} 条", flush=True)
            conn.close()
        except Exception as e:
            print(f"[收集调度] 清理异常：{e}", flush=True)
        _stop_event.wait(60)


def _lan_ips():
    """探测本机局域网 IP（多来源候选 + 私网段过滤）。

    只用出口路由法会被代理/VPN 的虚拟网卡（如 198.18.0.0/15 隧道段）骗到，
    所以再叠加主机名解析候选，统一过滤：仅保留真私网段，优先 192.168.*。
    """
    import socket
    cands = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))   # 不真发包，仅取出口网卡 IP
        cands.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        cands += socket.gethostbyname_ex(socket.gethostname())[2]
    except Exception:
        pass
    private = [ip for ip in dict.fromkeys(cands)   # 去重保序
               if ip.startswith(("192.168.", "10."))
               or any(ip.startswith(f"172.{i}.") for i in range(16, 32))]
    private.sort(key=lambda ip: not ip.startswith("192.168."))  # 家用 WiFi 段排最前
    return private[:2]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：建目录 + 初始化库 + 起调度
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    os.makedirs(settings.FULLTEXT_DIR, exist_ok=True)
    db.init_db()
    # 幂等建默认用户：single 模式直接用它，存量 source/feed 也归属它
    conn = db.connect()
    db.ensure_default_user(conn)
    conn.close()
    if settings.AUTH_MODE == "multi" and not settings.SECRET_KEY:
        print("[⚠ 安全提醒] multi 模式未配置 SECRET_KEY：用户 API Key 将明文存库，"
              "强烈建议在 .env 中设置", flush=True)
    _stop_event.clear()
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()
    print(f"✓ 收集App 已启动 → http://{settings.HOST}:{settings.PORT}")
    print(f"  身份模式：{settings.AUTH_MODE}　AI 通道：{settings.DEFAULT_AI_CHANNEL}")
    print("  收集调度：每 60s 增量抓取 · 每晚 2:00 全清资讯流（打星归档进重点新闻）")
    # HOST=0.0.0.0 时探测局域网 IP，打印手机可访问的地址（探测失败静默跳过）
    if settings.HOST == "0.0.0.0":
        for ip in _lan_ips():
            print(f"  📱 手机同 WiFi 访问：http://{ip}:{settings.PORT}"
                  "（浏览器菜单「添加到主屏幕」即可装成 App 图标）", flush=True)
    try:
        yield
    finally:
        # 退出：唤醒并停止调度线程
        _stop_event.set()


app = FastAPI(title="收集App", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def _http_exc_handler(request, exc: HTTPException):
    """FastAPI 默认把 HTTPException 包成 {"detail": ...}，但前端契约要 {"error": 中文}。
    在此统一转换（401 未登录、404 不存在等都走这里）。"""
    detail = exc.detail
    if isinstance(detail, dict):
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(status_code=exc.status_code, content={"error": str(detail)})


# 业务路由（统一 /api 前缀）
app.include_router(auth.router, prefix="/api")
app.include_router(assistant.router, prefix="/api")
app.include_router(sources.router, prefix="/api")
app.include_router(feed.router, prefix="/api")
app.include_router(agent.router, prefix="/api")


@app.get("/api/info")
def info():
    """服务探活（不要求登录）：身份模式 / 是否开放注册 / 实例默认是否走 CLI。
    multi 模式未登录也要能拿到这些基本信息（前端据此决定显示登录页/注册按钮）。"""
    return {"ok": True, "app": "收集App", "auth_mode": settings.AUTH_MODE,
            "allow_register": settings.ALLOW_REGISTER,
            "cli_mode": (settings.DEFAULT_AI_CHANNEL or "cli").lower() == "cli"}


# 同源静态托管 web/（前端单文件）。web/ 此刻可能尚未建好——先建空目录防 mount 崩。
os.makedirs(settings.WEB_DIR, exist_ok=True)
app.mount("/", StaticFiles(directory=settings.WEB_DIR, html=True), name="web")
