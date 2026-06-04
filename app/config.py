#!/usr/bin/env python3
"""配置中心 — stdlib only（不引 python-dotenv）。

启动时读仓库根的 .env（存在才读，简单 KEY=VALUE 解析，os.environ.setdefault），
然后用 settings 单例统一暴露各项配置。所有路径、端口、AI 默认通道集中在此，
换机器只需改 .env，不必动代码。
"""
import os
import shutil


# ── 仓库根 = app/ 的父目录 ──
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv():
    """读 BASE_DIR/.env（存在才读）。每行 KEY=VALUE，# 开头为注释，
    用 os.environ.setdefault——已在真实环境变量里的不被覆盖。"""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # 去掉可选的成对引号
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key:
                    os.environ.setdefault(key, val)
    except Exception as e:
        print(f"[配置] 读取 .env 失败（忽略，用默认值）：{e}")


_load_dotenv()


def _env(key, default=""):
    return os.environ.get(key, default)


def _env_int(key, default):
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(key, default):
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "是")


class Settings:
    """配置单例。属性惰性无副作用，路径全部基于 BASE_DIR 推导。"""

    BASE_DIR = BASE_DIR

    # 身份模式：single=本机免登录单用户 / multi=多用户登录
    AUTH_MODE = _env("AUTH_MODE", "single")

    # 服务监听
    HOST = _env("HOST", "127.0.0.1")
    PORT = _env_int("PORT", 8770)

    # 数据路径
    DB_PATH = _env("DB_PATH", os.path.join(BASE_DIR, "data", "collect.db"))
    DATA_DIR = os.path.dirname(_env("DB_PATH", os.path.join(BASE_DIR, "data", "collect.db")))
    FULLTEXT_DIR = _env("FULLTEXT_DIR", os.path.join(BASE_DIR, "fulltext"))
    WEB_DIR = os.path.join(BASE_DIR, "web")

    # claude CLI 路径（空则 shutil.which 探测）
    CLAUDE_BIN = _env("CLAUDE_BIN", "")

    # 默认 AI 通道（实例级缺省，用户级配置优先于此）
    DEFAULT_AI_CHANNEL = _env("DEFAULT_AI_CHANNEL", "cli")   # cli | anthropic | openai
    DEFAULT_AI_BASE_URL = _env("DEFAULT_AI_BASE_URL", "")
    DEFAULT_AI_API_KEY = _env("DEFAULT_AI_API_KEY", "")
    DEFAULT_AI_MODEL = _env("DEFAULT_AI_MODEL", "claude-sonnet-4-6")

    # 编辑部项目根（本机可选增强；空=只用内置全文抓取）
    EDITORIAL_ROOT = _env("EDITORIAL_ROOT", "")

    # 注册开关 / 会话密钥
    ALLOW_REGISTER = _env_bool("ALLOW_REGISTER", True)
    SECRET_KEY = _env("SECRET_KEY", "")
    # session cookie 是否加 Secure 标志（HTTPS 部署时设 true）
    COOKIE_SECURE = _env_bool("COOKIE_SECURE", False)

    def resolved_claude_bin(self):
        """CLAUDE_BIN 非空直接用；空则 PATH 探测 'claude'，找不到返回 ''。"""
        if self.CLAUDE_BIN:
            return self.CLAUDE_BIN
        return shutil.which("claude") or ""

    def default_ai_cfg(self):
        """实例级默认 AI 配置，喂给 app.llm.get_provider。"""
        return {
            "channel": self.DEFAULT_AI_CHANNEL,
            "base_url": self.DEFAULT_AI_BASE_URL,
            "api_key": self.DEFAULT_AI_API_KEY,
            "model": self.DEFAULT_AI_MODEL,
            "cli_path": self.resolved_claude_bin(),
        }


settings = Settings()
