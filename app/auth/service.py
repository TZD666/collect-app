#!/usr/bin/env python3
"""身份服务 — 密码 hash、注册/登录、AI 配置解析、API Key 轻量加密。纯 stdlib。

设计取舍：
  · 密码 hash 用 hashlib.scrypt（不引 bcrypt/passlib），格式自描述（含参数）。
  · API Key 用 SECRET_KEY 派生的 keystream XOR 轻量加密——**仅防库文件泄露后明文外溢**，
    不是强加密。强安全请用实例级 .env（DEFAULT_AI_API_KEY）配置，勿把生产 Key 存库。
  · 报错统一中文；登录失败一律「用户名或密码错误」（不区分用户不存在/密码错，防枚举）。
"""
import base64
import hashlib
import hmac
import json
import os

from app.config import settings
from app.core import db


# ── 密码 hash（scrypt）─────────────────────────────────────────
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1


def hash_password(pw: str) -> str:
    """返回 "scrypt$N$r$p$salt_hex$hash_hex"。salt=os.urandom(16)。"""
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                        p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    """常量时间比对。空 stored（如 u_default 的 pw_hash=''）永不通过。"""
    if not stored:
        return False
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=int(n), r=int(r),
                            p=int(p), dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


# ── 注册 / 登录 ────────────────────────────────────────────────
def register(conn, username: str, password: str) -> str:
    """注册成功返回 session token；失败抛中文 ValueError。"""
    if not settings.ALLOW_REGISTER:
        raise ValueError("本实例已关闭注册")
    username = (username or "").strip()
    if not (1 <= len(username) <= 32):
        raise ValueError("用户名需 1-32 个字符")
    if len(password or "") < 6:
        raise ValueError("密码至少 6 位")
    if db.user_by_name(conn, username):
        raise ValueError("该用户名已被占用")
    user = db.user_create(conn, username, hash_password(password))
    return db.session_create(conn, user["id"])


def login(conn, username: str, password: str) -> str:
    """登录成功返回 session token；失败抛中文 ValueError（统一文案防枚举）。"""
    username = (username or "").strip()
    user = db.user_by_name(conn, username)
    if not user or not verify_password(password or "", user["pw_hash"]):
        raise ValueError("用户名或密码错误")
    return db.session_create(conn, user["id"])


# ── API Key 轻量加密 ───────────────────────────────────────────
# 用 SECRET_KEY + 每条随机 salt 派生 HMAC-SHA256 keystream，与明文 XOR 后 base64。
# 格式 "enc2$<salt_hex>$<b64>"——每条 salt 不同，keystream 不再固定，
# 杜绝「知道自己明文即可解他人密文」的同流复用风险。
# 兼容旧 "enc1$<b64>"（固定 keystream，无 salt）与无前缀明文。
# SECRET_KEY 为空时退化为明文存储（README 层面提示用户配置 SECRET_KEY）。
# 诚实标注：这是轻量混淆，只防库文件被翻到时一眼看到明文；非抗攻击级加密。
_ENC2_PREFIX = "enc2$"
_ENC1_PREFIX = "enc1$"


def _keystream(length: int, salt: bytes = b"") -> bytes:
    """HMAC-SHA256(SECRET_KEY, salt + counter) 拼接成所需长度的 keystream。
    salt=b'' 时即旧 enc1 的固定 keystream（仅 dec 旧格式时用）。"""
    key = (settings.SECRET_KEY or "").encode("utf-8")
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(key, salt + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:length]


def enc_secret(plain: str) -> str:
    """加密明文 → "enc2$<salt_hex>$<b64>"。SECRET_KEY 为空则原样返回明文（不加前缀）。"""
    if not plain:
        return ""
    if not settings.SECRET_KEY:
        return plain  # 无密钥：明文存储（README 提示配置 SECRET_KEY）
    salt = os.urandom(16)
    data = plain.encode("utf-8")
    ks = _keystream(len(data), salt)
    xored = bytes(a ^ b for a, b in zip(data, ks))
    return f"{_ENC2_PREFIX}{salt.hex()}${base64.b64encode(xored).decode('ascii')}"


def dec_secret(cipher: str) -> str:
    """解密。兼容三种格式：
      · "enc2$<salt_hex>$<b64>"（新，每条随机 salt）
      · "enc1$<b64>"（旧，固定 keystream 无 salt）
      · 无前缀（明文，兼容旧值/无密钥存储）。"""
    if not cipher:
        return ""
    if cipher.startswith(_ENC2_PREFIX):
        try:
            salt_hex, b64 = cipher[len(_ENC2_PREFIX):].split("$", 1)
            salt = bytes.fromhex(salt_hex)
            xored = base64.b64decode(b64)
            ks = _keystream(len(xored), salt)
            return bytes(a ^ b for a, b in zip(xored, ks)).decode("utf-8")
        except Exception:
            return ""  # 密钥变更/数据损坏 → 视为未配置
    if cipher.startswith(_ENC1_PREFIX):
        try:
            xored = base64.b64decode(cipher[len(_ENC1_PREFIX):])
            ks = _keystream(len(xored))  # salt=b''：旧固定 keystream
            return bytes(a ^ b for a, b in zip(xored, ks)).decode("utf-8")
        except Exception:
            return ""
    return cipher  # 明文兼容


# ── AI 配置解析 ────────────────────────────────────────────────
def resolve_ai_cfg(user: dict) -> dict:
    """合并用户级 ai_config（优先，空值不覆盖）与实例级默认。
    解密 api_key。返回喂给 app.llm.get_provider 的 cfg dict。"""
    base = settings.default_ai_cfg()
    if not user:
        return base
    try:
        ucfg = json.loads(user.get("ai_config") or "{}")
    except (ValueError, TypeError):
        ucfg = {}
    merged = dict(base)
    for k in ("channel", "base_url", "model"):
        v = (ucfg.get(k) or "").strip() if isinstance(ucfg.get(k), str) else ucfg.get(k)
        if v:
            merged[k] = v
    api_key = dec_secret(ucfg.get("api_key") or "")
    if api_key:
        merged["api_key"] = api_key
    return merged
