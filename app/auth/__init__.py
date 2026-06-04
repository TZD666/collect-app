"""身份层：密码 hash（stdlib scrypt）、注册/登录、会话、AI 配置解析与轻量加密。

公开接口见 app.auth.service：
  hash_password / verify_password   — scrypt 密码哈希
  register / login                  — 注册/登录（返回 token 或抛中文错）
  resolve_ai_cfg                    — 解析用户级 AI 配置（解密 key + 合并默认）
  enc_secret / dec_secret           — API Key 轻量对称加密
"""
