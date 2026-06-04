# 收集App 开发日志

> 账本式追加，只增不改历史。格式：日期 + 条目。

---

## 2026-06-04

**里程碑：工程化改造启动**

- **基线打包（原始 demo）**：从 `/笔记台` 原样复制功能一「收集资料」模块，包含 server.py（笔记台全量 HTTP 服务）、三个 HTML 页面（收集 / 重点新闻 / 总结）、收集/目录（db.py + crawler.py + launchd plist + 数据）。改造前已通过用户完整验收（73 条 feed，打星/已读/GC/接入助手全流程跑通）。

- **波次 1 — FastAPI 骨架 + 前端同源 + AI 三通道**：
  - 建 `app/` 包（main.py / config.py / deps.py / utils.py），db.py / crawler.py 迁入 `app/core/`，内置全文抓取 `app/core/fulltext.py`（stdlib readability，去编辑部外部依赖）。
  - 五个 collect 路由迁至 `app/routers/`（sources / feed / agent），调度器进 lifespan。
  - AI 适配层 `app/llm/`（base / claude_cli / anthropic_native / openai_compat），统一 `complete()` 接口，三通道可配切换。
  - 前端三页迁入 `web/`（收集→index.html / 重点新闻→starred.html / 总结→summary.html），抽 `web/shared.js`（相对路径 + /api 前缀 + 401 跳登录），新增 `web/login.html` + `web/settings.html`（AI 通道配置 + 测试连接）。
  - 剥离笔记台遗留（/notebook / /collect / /publish / /claude / /crawl-config 及相关函数）。
  - `.env.example` 覆盖全部配置项，stdlib 手写 dotenv 不引 python-dotenv，`requirements.txt` 仅三个核心依赖。

- **波次 2 — 身份层 + 多租户（进行中）**：
  - `app/auth/service.py`：stdlib `hashlib.scrypt` 密码 hash，服务端 session token（HttpOnly cookie），`AUTH_MODE=single` 直返默认用户无需登录。
  - `app/core/db.py`：`source/feed` 加 `user_id` 列，新增 `users/session` 表，约 12 个业务函数加 user 形参。
  - 迁移脚本 `scripts/migrate_single_to_multi.py`：存量数据幂等归 `u_default`。

- **波次 2 收尾 — 打包发布（本次）**：
  - 新建 `Dockerfile`（python:3.12-slim，单 worker，ENV AUTH_MODE=multi HOST=0.0.0.0）。
  - 新建 `.dockerignore`（排除 data/fulltext/env/pycache 等，镜像只含应用代码）。
  - 新建 `docker-compose.yml`（volumes 挂载 data/fulltext 持久化，env_file 注入配置）。
  - 新建 `README.md`（全中文，快速开始 A 本地/B Docker，AI 配置三通道说明，配置项表格，安全提示，架构简图）。
  - 新建 `devlog.md`（本文件，独立账本）。
  - 删除旧遗留文件：server.py、收集.html、重点新闻.html、总结.html、收集/ 目录（含旧 data/fulltext 副本，根目录已有新副本）。
