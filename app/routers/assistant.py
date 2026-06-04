#!/usr/bin/env python3
"""AI 助手路由 — 本期只做 /assistant/ping（设置页「测试连接」）。

聊天端点 /assistant/chat 下一波由别人加（SSE + 上下文注入），文件结构留好扩展空间：
  · ping 已演示「按用户解析 cfg + 取 provider」的标准路径，chat 可复用。
全 POST + payload: dict = Body(...)，同步 def。
"""
import json

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from app.auth import service
from app.deps import get_db, current_user

router = APIRouter()


@router.post("/assistant/ping")
def ping(payload: dict = Body(...), user: dict = Depends(current_user), conn=Depends(get_db)):
    """测试 AI 连接。表单传入的 config 优先；api_key 为空则用该用户已存的解密 key。
    cli 通道且可用 → 直接判可用（无网络测试）；否则发一句极短 complete 探活。"""
    try:
        form = payload.get("config") or {}
        # 以用户已解析的 cfg 为底，用表单字段覆盖（表单 api_key 空 → 沿用已存的）
        base = service.resolve_ai_cfg(user)
        cfg = dict(base)
        for k in ("channel", "base_url", "model"):
            v = (form.get(k) or "").strip()
            if v:
                cfg[k] = v
        form_key = (form.get("api_key") or "").strip()
        if form_key:
            cfg["api_key"] = form_key
        # 若用户切到了表单选的通道，但表单没填 key 且该通道 base 没存 key，保持 base 的 key

        from app.llm.base import get_provider
        provider = get_provider(cfg)
        if (cfg.get("channel") or "cli").lower() == "cli":
            if provider.available:
                return {"ok": True, "reply": "CLI 模式可用，无需网络测试"}
            return {"ok": False, "error": "本机未找到 claude CLI（请改用 API 通道或配置 CLAUDE_BIN）"}
        reply = provider.complete("你是连接测试", "回复：连接成功", max_tokens=20)
        return {"ok": True, "reply": (reply or "").strip()[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)}
