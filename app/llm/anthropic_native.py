#!/usr/bin/env python3
"""Anthropic 原生通道。

直调 https://api.anthropic.com/v1/messages（或 cfg["base_url"] 覆盖）。
纯 stdlib：urllib.request + json，无工具循环，无 SDK 依赖。

使用示例：
    from app.llm.base import get_provider
    p = get_provider({"channel": "anthropic", "api_key": "sk-ant-...", "model": "claude-sonnet-4-6"})
    print(p.complete("你是助手", "你好"))
"""

import json
from typing import Generator

from app.llm.base import LLMProvider, _http_post_json, _http_post_stream, _sse_lines

# Anthropic API 版本头
_ANTHROPIC_VERSION = "2023-06-01"
# 默认接口地址
_DEFAULT_BASE_URL = "https://api.anthropic.com"


class AnthropicProvider(LLMProvider):
    """Anthropic 原生 API 通道（urllib 直调，无工具循环）。

    available = bool(api_key)——未填 key 则不可用。
    """

    def __init__(self, cfg: dict):
        self._api_key = (cfg.get("api_key") or "").strip()
        # base_url 允许覆盖（如代理、私有部署）；结尾斜杠统一去掉
        raw_base = (cfg.get("base_url") or "").strip().rstrip("/")
        self._base_url = raw_base if raw_base else _DEFAULT_BASE_URL
        self._model = (cfg.get("model") or "claude-sonnet-4-6").strip()
        self.available = bool(self._api_key)

    def _build_headers(self) -> dict:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _messages_url(self) -> str:
        return f"{self._base_url}/v1/messages"

    def complete(self, system: str, user: str, *, max_tokens: int = 8192) -> str:
        """同步调用，返回完整回复字符串。timeout 120s。"""
        if not self._api_key:
            raise RuntimeError("Anthropic 通道未配置 API Key，请在设置中填写")

        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        resp = _http_post_json(self._messages_url(), self._build_headers(), body, timeout=120)
        # 拼接所有 text 块
        content = resp.get("content") or []
        return "\n".join(b["text"] for b in content if b.get("type") == "text")

    def stream(self, system: str, user: str, *, max_tokens: int = 8192) -> Generator[str, None, None]:
        """流式调用，yield 文本增量。

        SSE 事件格式（Anthropic）：
          event: content_block_delta
          data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"..."},...}
        遇到 message_stop 事件停止。
        """
        if not self._api_key:
            raise RuntimeError("Anthropic 通道未配置 API Key，请在设置中填写")

        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "stream": True,
        }
        resp = _http_post_stream(self._messages_url(), self._build_headers(), body, timeout=120)

        for payload in _sse_lines(resp):
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            obj_type = obj.get("type", "")
            # 文本增量事件
            if obj_type == "content_block_delta":
                delta = obj.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        yield text
            # 消息结束事件
            elif obj_type == "message_stop":
                break
