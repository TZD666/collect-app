"""身份服务断言 — app/auth/service.py。

覆盖范围：
  - hash_password / verify_password 往返
  - register（重名、密码过短、注册关闭）
  - login（成功、错密码）+ session 过期 / 删除
  - enc_secret / dec_secret（往返、明文兼容、SECRET_KEY 为空退化行为）
  - resolve_ai_cfg（用户覆盖默认、空字段不覆盖、api_key 解密）
"""
import json
import time

import pytest

from app.auth import service
from app.config import settings
from app.core import db


# ── 密码 hash ────────────────────────────────────────────────────

class TestPassword:
    def test_往返_正确密码(self):
        """hash_password → verify_password 对正确密码返回 True。"""
        h = service.hash_password("hello123")
        assert service.verify_password("hello123", h) is True

    def test_错密码_返回False(self):
        """错误密码返回 False。"""
        h = service.hash_password("hello123")
        assert service.verify_password("wrongpw", h) is False

    def test_空stored_永不通过(self):
        """pw_hash=''（如 u_default）永不通过，防默认账户被登录。"""
        assert service.verify_password("anything", "") is False
        assert service.verify_password("", "") is False

    def test_两次hash不同(self):
        """同一密码两次 hash 因随机 salt 不同，结果不同。"""
        h1 = service.hash_password("pw")
        h2 = service.hash_password("pw")
        assert h1 != h2


# ── register ─────────────────────────────────────────────────────

class TestRegister:
    def test_成功注册(self, conn):
        """注册成功返回 session token（非空字符串）。"""
        token = service.register(conn, "alice", "password123")
        assert isinstance(token, str) and len(token) > 0

    def test_重名报错(self, conn):
        """重复用户名抛中文 ValueError。"""
        service.register(conn, "bob", "password123")
        with pytest.raises(ValueError, match="已被占用"):
            service.register(conn, "bob", "password456")

    def test_密码过短报错(self, conn):
        """密码不足 6 位抛中文 ValueError。"""
        with pytest.raises(ValueError, match="6"):
            service.register(conn, "carol", "12345")

    def test_注册关闭时报错(self, conn, monkeypatch):
        """ALLOW_REGISTER=False 时抛「关闭注册」错误。"""
        monkeypatch.setattr(settings, "ALLOW_REGISTER", False)
        with pytest.raises(ValueError, match="关闭注册"):
            service.register(conn, "dave", "password123")


# ── login + session ───────────────────────────────────────────────

class TestLogin:
    def test_登录成功返回token(self, conn):
        """正确账密 → 有效 token，session_user 能取回同一用户。"""
        service.register(conn, "eve", "password123")
        token = service.login(conn, "eve", "password123")
        user = db.session_user(conn, token)
        assert user is not None
        assert user["username"] == "eve"

    def test_错密码报错(self, conn):
        """错误密码抛统一中文错误（不区分用户不存在/密码错）。"""
        service.register(conn, "frank", "password123")
        with pytest.raises(ValueError, match="用户名或密码"):
            service.login(conn, "frank", "wrongpass")

    def test_不存在用户报错(self, conn):
        """不存在的用户名抛同样的统一错误。"""
        with pytest.raises(ValueError, match="用户名或密码"):
            service.login(conn, "nobody", "password123")

    def test_session_过期返回None(self, conn):
        """手工把 expires_at 改成过去 → session_user 返 None。"""
        service.register(conn, "grace", "password123")
        token = service.login(conn, "grace", "password123")
        # 把过期时间改到过去
        conn.execute(
            "UPDATE session SET expires_at='2000-01-01 00:00:00' WHERE token=?",
            (token,),
        )
        conn.commit()
        assert db.session_user(conn, token) is None

    def test_session_delete后失效(self, conn):
        """session_delete 后 session_user 返 None。"""
        service.register(conn, "henry", "password123")
        token = service.login(conn, "henry", "password123")
        db.session_delete(conn, token)
        assert db.session_user(conn, token) is None

    def test_无效token返回None(self, conn):
        """随机无效 token → session_user 返 None（不报错）。"""
        assert db.session_user(conn, "totally_invalid_token") is None


# ── enc_secret / dec_secret ──────────────────────────────────────

class TestEncDec:
    def test_往返_有密钥(self, monkeypatch):
        """SECRET_KEY 非空时 enc→dec 往返正确。"""
        monkeypatch.setattr(settings, "SECRET_KEY", "testsecret123")
        enc = service.enc_secret("my_api_key")
        assert enc.startswith("enc1$")
        assert service.dec_secret(enc) == "my_api_key"

    def test_空明文返回空(self, monkeypatch):
        """enc_secret('') 返回 ''，dec_secret('') 返回 ''。"""
        monkeypatch.setattr(settings, "SECRET_KEY", "testsecret123")
        assert service.enc_secret("") == ""
        assert service.dec_secret("") == ""

    def test_无前缀明文兼容(self):
        """dec_secret 对无 enc1$ 前缀的明文直接返回原文（向后兼容）。"""
        assert service.dec_secret("plain_key_value") == "plain_key_value"

    def test_SECRET_KEY_为空_退化为明文(self, monkeypatch):
        """SECRET_KEY 为空时 enc_secret 直接返回明文（不加前缀）；
        dec_secret 也能正确还原（无前缀兼容路径）。

        这是代码注释里明确说明的「退化行为」：
        「SECRET_KEY 为空则原样返回明文（不加前缀）」。
        """
        monkeypatch.setattr(settings, "SECRET_KEY", "")
        plain = "my_plain_key"
        enc = service.enc_secret(plain)
        # 退化：enc 就是明文本身，无 enc1$ 前缀
        assert enc == plain
        assert not enc.startswith("enc1$")
        # dec 走无前缀兼容路径，也能还原
        assert service.dec_secret(enc) == plain

    def test_密钥变更_dec返回空(self, monkeypatch):
        """用密钥 A 加密，再用密钥 B 解密：decode 结果不等于原文
        （XOR keystream 变了），dec_secret 返回乱码或空，不崩溃。"""
        monkeypatch.setattr(settings, "SECRET_KEY", "key_A")
        enc = service.enc_secret("secret_value")

        monkeypatch.setattr(settings, "SECRET_KEY", "key_B")
        result = service.dec_secret(enc)
        # 不等于原文（XOR 结果不同）且不抛异常
        assert result != "secret_value"


# ── resolve_ai_cfg ────────────────────────────────────────────────

class TestResolveAiCfg:
    def _base_cfg(self):
        """取实例级默认配置快照（用于比对）。"""
        return settings.default_ai_cfg()

    def test_无用户返回默认(self):
        """user=None 时返回实例级默认配置。"""
        cfg = service.resolve_ai_cfg(None)
        assert cfg["channel"] == settings.DEFAULT_AI_CHANNEL

    def test_用户配置覆盖默认(self, monkeypatch):
        """用户设置的 channel/model 优先于实例级默认。"""
        monkeypatch.setattr(settings, "DEFAULT_AI_CHANNEL", "cli")
        monkeypatch.setattr(settings, "DEFAULT_AI_MODEL", "claude-sonnet-4-6")
        user = {
            "ai_config": json.dumps({"channel": "openai", "model": "gpt-4o"}),
        }
        cfg = service.resolve_ai_cfg(user)
        assert cfg["channel"] == "openai"
        assert cfg["model"] == "gpt-4o"

    def test_空字段不覆盖默认(self, monkeypatch):
        """用户 ai_config 里空字符串字段不覆盖实例级默认。"""
        monkeypatch.setattr(settings, "DEFAULT_AI_CHANNEL", "cli")
        user = {
            "ai_config": json.dumps({"channel": "", "model": ""}),
        }
        cfg = service.resolve_ai_cfg(user)
        # 空值不覆盖，沿用实例默认
        assert cfg["channel"] == "cli"

    def test_api_key_解密(self, monkeypatch):
        """用户存的 api_key 经 dec_secret 解密后放入 cfg。"""
        monkeypatch.setattr(settings, "SECRET_KEY", "testsecret")
        enc_key = service.enc_secret("sk-real-key-123")
        user = {
            "ai_config": json.dumps({"api_key": enc_key}),
        }
        cfg = service.resolve_ai_cfg(user)
        assert cfg["api_key"] == "sk-real-key-123"

    def test_ai_config_损坏JSON_不崩溃(self):
        """ai_config 字段是损坏 JSON 时 resolve_ai_cfg 不崩溃，返回默认配置。"""
        user = {"ai_config": "not_valid_json{{"}
        cfg = service.resolve_ai_cfg(user)
        assert "channel" in cfg  # 沿用默认，不报错
