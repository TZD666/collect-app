#!/usr/bin/env python3
"""Claude CLI 通道。

复用本机 claude CLI 的 OAuth 认证，无需 API Key。
迁自 server.py 的 call_via_cli，加入中文错误处理和 PATH 探测。

cli_path 解析优先级：
  1. cfg["cli_path"]（非空）
  2. shutil.which("claude")（PATH 探测）
  找不到 → available = False，调用时抛中文 RuntimeError

stream() 说明：
  CLI 不支持真流式输出。stream() 的实现是一次性调用 complete()，
  然后将整段结果作为单条增量 yield 出去。调用方如需逐字动画效果，
  可在前端对单次 yield 的长字符串自行做打字机展示。

使用示例：
    from app.llm.base import get_provider
    p = get_provider({"channel": "cli"})
    if p.available:
        print(p.complete("你是回声机", "回复两个字：成功"))
"""

import shutil
import subprocess
from typing import Generator

from app.llm.base import LLMProvider


class ClaudeCLIProvider(LLMProvider):
    """本机 claude CLI 通道。

    available = CLI 可执行文件存在。
    """

    def __init__(self, cfg: dict):
        # 优先 cfg 里的路径，其次 PATH 探测
        cli_path = (cfg.get("cli_path") or "").strip()
        if not cli_path:
            cli_path = shutil.which("claude") or ""
        self._cli_path = cli_path
        self._model = (cfg.get("model") or "").strip()
        self.available = bool(cli_path)

    def complete(self, system: str, user: str, *, max_tokens: int = 8192) -> str:
        """调用 CLI，返回完整回复字符串。timeout 600s（兼容长任务）。

        returncode != 0 时抛出中文 RuntimeError，附 stderr 摘要。
        """
        if not self._cli_path:
            raise RuntimeError(
                "本机未找到 claude CLI（可在设置中改用 API 通道）"
            )

        # 将 system 和 user 拼为单一 prompt（CLI -p 模式）
        full_prompt = f"{system}\n\n---\n\n{user}"
        cmd = [self._cli_path, "-p", full_prompt, "--output-format", "text"]
        if self._model:
            cmd += ["--model", self._model]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("claude CLI 调用超时（超过 600 秒）") from None
        except FileNotFoundError:
            # 文件在 available 检查后被删除的极端情况
            raise RuntimeError(
                f"找不到 claude CLI 可执行文件：{self._cli_path}"
                "（可在设置中改用 API 通道）"
            ) from None

        if result.returncode != 0:
            # 提取 stderr 末尾最有用的部分（最多 300 字符）
            err = (result.stderr or "").strip()
            if len(err) > 300:
                err = "…" + err[-300:]
            if not err:
                err = f"退出码 {result.returncode}"
            raise RuntimeError(f"claude CLI 返回错误：{err}")

        return result.stdout.strip()

    def stream(self, system: str, user: str, *, max_tokens: int = 8192) -> Generator[str, None, None]:
        """CLI 通道不支持真流式输出。

        退化行为：一次性调用 complete()，将整段结果作为单条增量 yield。
        前端如需逐字动画，可对该单次 yield 的字符串自行做打字机展示。
        """
        result = self.complete(system, user, max_tokens=max_tokens)
        yield result
