"""AI 适配层。

公开接口：
  get_provider(cfg) -> LLMProvider   — 工厂，按 cfg["channel"] 返回通道实例
  LLMProvider                        — 基类（类型标注用）

三个内置通道：
  claude_cli        — 本机 claude CLI（无需 API Key，复用 OAuth）
  anthropic_native  — Anthropic 原生 API（urllib 直调，无 SDK）
  openai_compat     — OpenAI 兼容接口（DeepSeek / Kimi / Ollama 等）
"""

from app.llm.base import get_provider, LLMProvider

__all__ = ["get_provider", "LLMProvider"]
