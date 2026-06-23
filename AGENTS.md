# AGENTS.md — 收集App

This file provides guidance to Codex 等其他 AI 编码工具 when working with code in this repository.

## 这是什么

「收集App」：帮社媒运营从业者快速收集和筛选关注领域的新闻。监控源 → 调度抓取出 feed 信息流 → 人工打星筛选（★1 存档 / ★2 今天用 / ★3 爆点）→ 打星才抓全文留存 → 高分文章供下一模块（功能二「总结」）使用。

2026-06-04 完成工程化改造（从笔记台单机 demo → 标准 Web 服务）：FastAPI + 多用户身份层 + AI 多模型适配，同一份代码本地直跑或 Docker 服务器部署。产品蓝图与历史决策在 `docs/`（功能梳理.md 总纲、功能一-实现目标.md D1-D5 决策）。改造前的 demo 形态保留在本机「笔记台」目录（本仓库 git 基线提交也是它）。

## 运行 / 开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # 首次
.venv/bin/python run.py                  # 起服务 → http://127.0.0.1:8770（默认 single 模式免登录）
cp .env.example .env                     # 需要改配置时（端口/AUTH_MODE/AI 默认通道等）

.venv/bin/python -m pytest -q            # 38 个测试（db 多租户隔离 + auth），需先装 requirements-dev.txt
.venv/bin/python -m app.core.crawler --test <url>   # 试抓某 URL（加源校验同款判别链）
.venv/bin/python scripts/migrate_single_to_multi.py # 旧库迁移（幂等，自动备份）

docker compose up -d                     # 服务器部署（AUTH_MODE=multi，多用户注册登录）
```

改 web/ 刷新浏览器即可；改 app/ 需重启 run.py。**Docker/uvicorn 必须单 worker**（AGENT_TASKS 接入助手任务表在内存，多 worker 不可见）。

## 架构

```
run.py                 # 入口：读 .env 起 uvicorn（app.main:app）
app/
  config.py            # stdlib .env 解析 + settings 单例 + default_ai_cfg()
  main.py              # FastAPI + lifespan（init_db、调度线程）+ 异常→{"error":中文} + 静态挂载 web/
  deps.py              # get_db / current_user（AUTH_MODE=single 直返 u_default；multi 走 cookie 会话）
  core/
    db.py              # SQLite(WAL) 数据层：source/seen/feed/meta/users/session；全部业务函数带 user_id 关键字参数
    crawler.py         # 爬虫判别链：RSS/Atom/sitemap → jpaas CMS → HTML 启发式(主路) → agent 配方；PID锁+冷却+增量
    fulltext.py        # 打星抓全文：内置 stdlib 正文提取（文本密度法，<200字判失败）；EDITORIAL_ROOT 配置时优先编辑部增强
  auth/service.py      # scrypt 密码、register/login、会话、AI key 轻量加密(enc1$前缀)、resolve_ai_cfg(user)
  llm/                 # AI 适配层：get_provider(cfg) → complete/stream/available；三通道 anthropic|openai兼容|claude CLI
  agent_tasks.py       # 🤖接入助手：试抓失败→LLM 产抓取配方→验证≥2条自动加源；任务按 user 隔离，在内存（重启丢）
  routers/             # auth / sources(/mon-source) / feed(/feed,/mon-run) / agent(/agent-crack) / assistant(/ping,/chat SSE)
web/                   # 无框架单文件页：index(信息流+打星+源管理+管家抽屉) starred(打星档案) summary(★2/3工作集) login settings
data/  fulltext/       # 用户数据（gitignore；docker 卷挂载点）
scripts/  tests/
```

### 关键业务流
- **加源契约**：/mon-source add 必须先 crawler.discover() 试抓 ≥1 条才允许添加，失败返回 `{ok:false, error:中文, agent_available}`——前端据此唤起接入助手。
- **打星即一切**：star>0 触发 fulltext.fetch_fulltext 落 `fulltext/{feed_id}.json`；取消星删全文，超 7 天窗口旧文直接移出 feed；无星不持久（每晚 2:00 调度清「已读且无星」，meta 全局 last_read_clean 保证幂等）。
- **手动抓取 vs 调度**：/mon-run 默认 force=强制首抓（无视冷却、按源 backfill_days 回溯）；lifespan 调度线程每 60s 增量。
- **多租户**：source/feed 带 user_id（UNIQUE(user_id,url) 允许不同用户监控同一站）；调度器跑所有用户的源（feed_add 用 src 自带的 user_id）；feed 操作前 feed_get(fid, user_id) 校验归属。
- **AI 配置解析链**：用户级 users.ai_config（settings 页保存，api_key 加密存）→ 回落 .env 实例级默认（DEFAULT_AI_*）。管家聊天 /assistant/chat 由服务端注入近 7 天 feed 快照（≤60 条）做上下文，只给建议不改库。

### HTTP 契约要点
全业务端点 POST + JSON 体，action 字段分发（沿用 demo 契约，见 docs/功能一-实现目标.md）；前缀统一 `/api`。错误一律 `{"error":"可读中文"}`（main.py 的 exception_handler 兜底），不暴露栈。401 时前端 shared.js 自动跳 /login.html。

## 约定（必须遵守）

- **全中文**：UI、注释、报错、文档、commit。
- **router 用同步 def**（内部有阻塞 subprocess/urllib，FastAPI 自动丢线程池）——不要改成 async def 后直接阻塞调用，会卡死事件循环。
- 改 db schema：往 `_MIGRATIONS` 元组**追加** ALTER 语句（缺列就补、幂等），别只改 SCHEMA；改不了的（主键/UNIQUE）参考 `_migrate_meta` 建新表搬数据模式。
- db 业务函数全部**关键字调用**（user_id 防位置错乱）。
- `data/`、`fulltext/`、`.env` 是用户个人数据/密钥，已 gitignore，永不入库。
- 开发日志：完成决策/改动/修复后往 `devlog.md` **末尾追加**（只追加不改写，账本式）。

## 已知限制

- AGENT_TASKS 在内存：server 重启丢任务（候选改进：落库）。
- claude CLI 通道无真流式（整段一次返回），管家打字机效果在 anthropic/openai 通道才逐字。
- 内置全文提取对强反爬/JS 动态页可能失败——卡片显示 ⚠，点链接浏览器看原文不受影响；本机配 EDITORIAL_ROOT 可用编辑部（playwright 兜底）增强。
- AI key 加密是轻量级（SECRET_KEY 派生 HMAC keystream XOR，防库文件泄露明文）；公网部署务必改 SECRET_KEY、上 HTTPS 反代。
