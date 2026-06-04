#!/usr/bin/env python3
"""OpenAI 兼容通道。

覆盖所有遵循 /chat/completions 接口的服务：
  DeepSeek / Kimi / Qwen / Ollama / 本地 LLM 等。

cfg 必填：base_url（如 https://api.deepseek.com/v1 或 http://localhost:11434/v1）。
api_key 对于 Ollama 等本地服务可随便填（如 "ollama"），但字段不能空——
  若确实不需要 key，填任意非空字符串即可，服务端一般不校验。

使用示例：
    from app.llm.base import get_provider
    p = get_provider({
        "channel": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-...",
        "model": "deepseek-chat",
    })
    print(p.complete("你是助手", "你好"))
"""

import json
from typing import Generator

from app.llm.base import LLMProvider, _http_post_json, _http_post_stream, _sse_lines


class OpenAICompatProvider(LLMProvider):
    """OpenAI 兼容 /chat/completions 通道。

    available = bool(api_key and base_url)。
    Ollama 等本地服务 key 可填占位符（如 "ollama"），不影响可用性判断。
    """

    def __init__(self, cfg: dict):
        self._api_key = (cfg.get("api_key") or "").strip()
        raw_base = (cfg.get("base_url") or "").strip().rstrip("/")
        self._base_url = raw_base
        self._model = (cfg.get("model") or "gpt-4o").strip()
        self.available = bool(self._api_key and self._base_url)

    def _completions_url(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }

    def _validate(self):
        """调用前校验必填项，缺失抛中文错误。"""
        if not self._base_url:
            raise RuntimeError(
                "OpenAI 兼容通道需要填写 Base URL"
                "（如 https://api.deepseek.com/v1 或 http://localhost:11434/v1）"
            )
        if not self._api_key:
            raise RuntimeError(
                'OpenAI 兼容通道需要填写 API Key'
                '（Ollama 等本地服务可填任意非空字符串，如 "ollama"）'
            )

    def complete(self, system: str, user: str, *, max_tokens: int = 8192) -> str:
        """同步调用，返回完整回复字符串。timeout 120s。

        若连接失败且 base_url 不含 /v1，报错提示尝试加 /v1 后缀。
        """
        self._validate()
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            resp = _http_post_json(self._completions_url(), self._build_headers(), body, timeout=120)
        except RuntimeError as e:
            msg = str(e)
            # 连接失败且地址不含 /v1 时给出提示
            if "无法连接" in msg and "/v1" not in self._base_url:
                raise RuntimeError(
                    msg + "（提示：请确认地址是否需要 /v1 后缀，如 https://api.example.com/v1）"
                ) from None
            raise

        choices = resp.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenAI 兼容接口返回空 choices（模型：{self._model}）")
        return choices[0].get("message", {}).get("content", "")

    def stream(self, system: str, user: str, *, max_tokens: int = 8192) -> Generator[str, None, None]:
        """流式调用，yield 文本增量。

        SSE 格式（OpenAI 兼容）：
          data: {"choices":[{"delta":{"content":"..."},...}],...}
          data: [DONE]
        """
        self._validate()
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            resp = _http_post_stream(self._completions_url(), self._build_headers(), body, timeout=120)
        except RuntimeError as e:
            msg = str(e)
            if "无法连接" in msg and "/v1" not in self._base_url:
                raise RuntimeError(
                    msg + "（提示：请确认地址是否需要 /v1 后缀，如 https://api.example.com/v1）"
                ) from None
            raise

        for payload in _sse_lines(resp):
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            text = delta.get("content", "")
            if text:
                yield text
