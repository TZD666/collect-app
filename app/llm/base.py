#!/usr/bin/env python3
"""AI 适配层基类与工厂函数。

公共工具：
  _http_post_json   — urllib 封装，HTTPError/URLError 转中文 RuntimeError
  _http_post_stream — 同上，返回原始响应供 SSE 读取
  _sse_lines        — SSE 响应逐行生成器

协议类：
  LLMProvider       — 所有通道的基类；子类实现 complete / stream

工厂：
  get_provider(cfg) — 按 cfg["channel"] 返回对应 LLMProvider 实例
"""

import json
import urllib.request
import urllib.error
from typing import Generator


# ──────────────────────────────────────────────
# 公共工具
# ──────────────────────────────────────────────

def _raise_http_error(e: urllib.error.HTTPError, url: str):
    """将 HTTPError 转为可读中文 RuntimeError。"""
    code = e.code
    try:
        body_text = e.read().decode("utf-8", errors="replace")
    except Exception:
        body_text = ""
    # 尝试从响应体提取 message 字段（Anthropic / OpenAI 风格）
    detail = ""
    try:
        obj = json.loads(body_text)
        err_obj = obj.get("error") or {}
        if isinstance(err_obj, dict):
            detail = err_obj.get("message", "")
        if not detail:
            detail = obj.get("message", "")
    except Exception:
        detail = body_text[:200] if body_text else ""

    suffix = ("：" + detail) if detail else ""

    if code == 401:
        raise RuntimeError(f"接口返回 401：API Key 无效或已过期（{url}）{suffix}") from e
    elif code == 403:
        raise RuntimeError(f"接口返回 403：无权访问（{url}）{suffix}") from e
    elif code == 404:
        raise RuntimeError(
            f"接口返回 404：地址或模型不存在，请确认 Base URL 和模型名是否正确（{url}）"
        ) from e
    elif code == 429:
        raise RuntimeError(f"接口返回 429：请求过于频繁，已触发限流（{url}）") from e
    elif 500 <= code < 600:
        raise RuntimeError(f"接口返回 {code}：服务端错误（{url}）{suffix}") from e
    else:
        raise RuntimeError(f"接口返回 {code}（{url}）{suffix}") from e


def _http_post_json(url: str, headers: dict, body: dict, timeout: int = 120) -> dict:
    """发送 JSON POST，返回已解析的 dict。

    HTTPError 按状态码转中文 RuntimeError；
    URLError（网络不通 / 超时）也转中文。
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _raise_http_error(e, url)
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if "timed out" in reason.lower() or "timeout" in reason.lower():
            raise RuntimeError(f"无法连接 {url}：连接超时（{reason}）") from e
        raise RuntimeError(f"无法连接 {url}：{reason}") from e


def _http_post_stream(url: str, headers: dict, body: dict, timeout: int = 120):
    """发送 JSON POST，返回原始响应对象供 SSE 逐行读取（调用方负责关闭）。

    错误处理同 _http_post_json。
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        _raise_http_error(e, url)
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if "timed out" in reason.lower() or "timeout" in reason.lower():
            raise RuntimeError(f"无法连接 {url}：连接超时（{reason}）") from e
        raise RuntimeError(f"无法连接 {url}：{reason}") from e


def _sse_lines(resp) -> Generator[str, None, None]:
    """从 HTTP 响应对象逐行读取 SSE，yield 每行去掉 'data: ' 前缀后的字符串。

    遇到 'data: [DONE]' 停止生成，自动关闭响应。
    """
    try:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").rstrip("\n\r")
            if not line.startswith("data:"):
                continue
            payload = line[5:].lstrip(" ")
            if payload == "[DONE]":
                break
            yield payload
    finally:
        resp.close()


# ──────────────────────────────────────────────
# 基类
# ──────────────────────────────────────────────

class LLMProvider:
    """所有 AI 通道的协议基类。

    属性：
      available — 当前通道是否可用（CLI 找不到二进制、API Key 未填 = False）

    方法：
      complete(system, user, *, max_tokens) -> str
      stream(system, user, *, max_tokens)  -> Generator[str, None, None]，yield 文本增量
    """

    available: bool = False

    def complete(self, system: str, user: str, *, max_tokens: int = 8192) -> str:
        raise NotImplementedError

    def stream(self, system: str, user: str, *, max_tokens: int = 8192) -> Generator[str, None, None]:
        raise NotImplementedError
        yield  # 让 Python 识别为生成器


# ──────────────────────────────────────────────
# 工厂
# ──────────────────────────────────────────────

def get_provider(cfg: dict) -> LLMProvider:
    """根据 cfg["channel"] 返回对应 LLMProvider 实例。

    cfg 键（均可缺省，用 .get 容忍）：
      channel   : "cli" | "anthropic" | "openai"（默认 "cli"）
      base_url  : str — OpenAI 兼容通道必填；Anthropic 可覆盖默认地址
      api_key   : str
      model     : str
      cli_path  : str — claude CLI 绝对路径（空则 PATH 探测）

    未知通道抛中文 RuntimeError。
    """
    channel = (cfg.get("channel") or "cli").lower().strip()

    if channel == "cli":
        from app.llm.claude_cli import ClaudeCLIProvider
        return ClaudeCLIProvider(cfg)
    elif channel == "anthropic":
        from app.llm.anthropic_native import AnthropicProvider
        return AnthropicProvider(cfg)
    elif channel == "openai":
        from app.llm.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(cfg)
    else:
        raise RuntimeError(f"未知 AI 通道：{channel}（支持：cli / anthropic / openai）")
