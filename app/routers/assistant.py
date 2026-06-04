#!/usr/bin/env python3
"""AI 助手路由 — 本期只做 /assistant/ping（设置页「测试连接」）。

聊天端点 /assistant/chat 下一波由别人加（SSE + 上下文注入），文件结构留好扩展空间：
  · ping 已演示「按用户解析 cfg + 取 provider」的标准路径，chat 可复用。
全 POST + payload: dict = Body(...)，同步 def。
"""
import json

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import service
from app.core import db
from app.deps import get_db, current_user

router = APIRouter()


# ── 收集管家系统提示 ─────────────────────────────────────────────
_SYSTEM_HEAD = """你是「收集管家」，是新闻收集工作台的内置助手。用户用这个工作台监控新闻源、用打星筛选文章（★1=存档候选 ★2=今天要用 ★3=爆点核心选题）。
你的职责：基于下方资讯快照回答用户问题（如今天有什么新闻、某主题有哪些报道）、给出打星建议（哪些值得★2/★3 及理由）、帮用户梳理选题。
注意：你只能给建议，不能直接操作打星。回答用中文，简洁直接，引用文章时给标题。

【当前资讯快照】
"""


def _build_context(conn, user_id: str) -> str:
    """同步构建资讯快照（stats 摘要行 + 近 7 天最多 60 条 feed 列表行）。

    StreamingResponse 在 handler 返回后才迭代生成器，那时 Depends 的连接已关闭，
    故上下文必须在 handler 内同步取完，拼成纯字符串带进生成器。
    """
    import time
    st = db.stats(conn, user_id)
    summary = (f"监控源 {st['sources']} 个 · 资讯共 {st['feed_total']} 条 · "
               f"未读 {st['unread']} 条 · 已打星 {st['starred']} 条"
               f"（其中爆点★3 {st['star3']} 条）· 已存全文 {st['fulltext']} 条")

    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    rows = db.feed_list(conn, user_id)
    lines = []
    for f in rows:
        date = (f.get("pub_date") or "").strip() or (f.get("fetched_at") or "")[:10]
        # 近 7 天过滤：用展示日期（pub_date 优先，无则抓取日）
        if date and date < cutoff:
            continue
        star = int(f.get("star") or 0)
        star_tag = f"★{star}" if star > 0 else "无星"
        read_tag = "已读" if (f.get("read_at") or "").strip() else "未读"
        src = (f.get("source_title") or "-").strip() or "-"
        title = (f.get("title") or "").strip()
        lines.append(f"- [{star_tag}|{read_tag}] {title}（{src}·{date}）")
        if len(lines) >= 60:
            break

    body = "\n".join(lines) if lines else "（近 7 天暂无资讯）"
    return _SYSTEM_HEAD + summary + "\n" + body


def _compose_user_text(messages: list) -> str:
    """把多轮历史拼成单段 user 文本（llm 层是 system/user 二段式）。
    最后一条为本轮用户问题，前面的轮次作为「此前对话」附在前面。"""
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    if not msgs:
        return ""
    last = msgs[-1]
    cur = (last.get("content") or "").strip()
    prior = msgs[:-1]
    if not prior:
        return cur
    history_lines = []
    for m in prior:
        role = m.get("role") or "user"
        who = "user" if role == "user" else "assistant"
        content = (m.get("content") or "").strip()
        if content:
            history_lines.append(f"{who}: {content}")
    if not history_lines:
        return cur
    return "此前对话：\n" + "\n".join(history_lines) + "\n\n本轮问题：" + cur


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


@router.post("/assistant/chat")
def chat(payload: dict = Body(...), user: dict = Depends(current_user), conn=Depends(get_db)):
    """收集管家聊天 — SSE 流式。

    请求体 {messages:[{role,content},...]}，最后一条是本轮用户消息。
    上下文（资讯快照）由服务端注入，不信任前端传值。
    生成器内部不依赖 Depends 的连接（handler 返回即关闭），故上下文同步取完、
    LLM 调用所需的 cfg 也在 handler 内解析好，生成器只管流式吐 delta。
    """
    messages = payload.get("messages") or []
    cfg = service.resolve_ai_cfg(user)
    system = _build_context(conn, user["id"])
    user_text = _compose_user_text(messages)

    def gen():
        if not user_text:
            yield 'data: {"error": "消息为空"}\n\n'
            yield 'data: {"done": true}\n\n'
            return
        try:
            from app.llm.base import get_provider
            provider = get_provider(cfg)
            if not provider.available:
                yield ('data: ' + json.dumps(
                    {"error": "AI 通道不可用（本机未找到 claude CLI，或未配置 API Key，请到设置页检查）"},
                    ensure_ascii=False) + '\n\n')
                yield 'data: {"done": true}\n\n'
                return
            for chunk in provider.stream(system, user_text):
                if not chunk:
                    continue
                yield 'data: ' + json.dumps({"delta": chunk}, ensure_ascii=False) + '\n\n'
            yield 'data: {"done": true}\n\n'
        except Exception as e:
            yield 'data: ' + json.dumps({"error": str(e)}, ensure_ascii=False) + '\n\n'
            yield 'data: {"done": true}\n\n'

    return StreamingResponse(gen(), media_type="text/event-stream")
