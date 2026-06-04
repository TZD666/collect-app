# 收集App

帮社媒运营从业者**快速收集和筛选其关注领域的新闻**：添加监控源 → 定时抓取出 feed 信息流 → 人工打星筛选 → 打星才抓全文留存 → 高分文章传给下一模块。

---

## 功能特性

- **监控源管理**：支持 RSS/Atom/sitemap、政府部委站（jpaas CMS）、普通列表页启发式解析，一键添加即试抓校验
- **定时增量抓取**：内置后台调度器（每 60 秒），无需 cron / launchd，服务跑着就自动更新
- **打星筛选**：★1 存档候选 / ★2 今天用 / ★3 爆点主选题；打星才触发全文抓取落盘，不打星不浪费流量
- **已读智能判定**：卡片入屏停留 ≥10 秒（须近期有交互且标签页可见）或点链接视为已读，每晚 2:00 自动清理「已读且无星」条目
- **内置全文抓取**：纯 stdlib 实现，无需外部依赖；本机有「编辑部」项目时可选增强（支持 playwright 动态页）
- **🤖 接入助手**：常规试抓失败时唤起 AI 分析页面结构，自动生成抓取配方，支持 11 种反爬公开通道探测
- **AI 多模型可切换**：本机 claude CLI / Anthropic 官方 API / OpenAI 兼容接口（DeepSeek、Kimi、Ollama）三通道，设置页图形化配置
- **多用户支持**：`AUTH_MODE=multi` 开启注册登录，数据按用户隔离；`single` 模式本机免登录，一行命令即用
- **跨平台部署**：本地 `python run.py` 单机使用；服务器 `docker compose up -d` 多用户部署，手机/电脑均可访问

---

## 快速开始

### A. 本地运行（单机模式，免登录）

```bash
# 1. 克隆仓库
git clone <仓库地址>
cd collect-app

# 2. 创建虚拟环境并安装依赖
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt

# 3. 配置（可跳过，默认即单机免登录模式）
cp .env.example .env
# 按需编辑 .env，最常用：改端口 PORT=8771（若与其他服务冲突）

# 4. 启动
python run.py

# 5. 打开浏览器
# 访问 http://127.0.0.1:8770
```

首次启动会自动在 `data/` 创建 SQLite 数据库，在 `fulltext/` 存放打星文章全文，无需手动初始化。

---

### B. 服务器 Docker 部署（多用户模式）

适合部署到 VPS / 云服务器，手机和电脑随时登录使用。

```bash
# 1. 克隆仓库
git clone <仓库地址>
cd collect-app

# 2. 复制配置文件并修改关键项
cp .env.example .env
```

编辑 `.env`，至少修改以下两项：

```ini
AUTH_MODE=multi          # 开启多用户登录
SECRET_KEY=换一个随机长串  # 例：openssl rand -hex 32
```

```bash
# 3. 启动容器（后台运行）
docker compose up -d

# 4. 查看启动日志
docker compose logs -f

# 5. 访问
# 浏览器打开 http://<服务器IP>:8770
# 首次访问会跳转到注册/登录页
```

**停止服务**：`docker compose down`
**更新代码后重新构建**：`docker compose up -d --build`

> 数据持久化说明：`data/` 和 `fulltext/` 通过 volumes 挂载到宿主机，容器销毁后数据不丢失。

---

## AI 配置

收集App 的「🤖 接入助手」（分析难以抓取的页面，自动生成配方）支持三种 AI 通道，在**设置页**图形化配置，无需改代码。

### 通道说明

| 通道 | 适用场景 | 配置要点 |
|---|---|---|
| `cli`（本机 claude CLI） | 本地运行，已安装 Claude Code | 无需 API key，自动复用 Claude Code 登录态；`CLAUDE_BIN` 留空自动探测 |
| `anthropic`（官方 API） | 服务器部署，有 Anthropic API key | 填入 `DEFAULT_AI_API_KEY`，模型如 `claude-sonnet-4-6` |
| `openai`（OpenAI 兼容接口） | 用 DeepSeek / Kimi / Ollama 等 | 填 `DEFAULT_AI_BASE_URL` + `DEFAULT_AI_API_KEY` + 模型名 |

### OpenAI 兼容接口示例

```ini
DEFAULT_AI_CHANNEL=openai

# DeepSeek
DEFAULT_AI_BASE_URL=https://api.deepseek.com/v1
DEFAULT_AI_API_KEY=sk-xxxxxxxx
DEFAULT_AI_MODEL=deepseek-chat

# Kimi（月之暗面）
DEFAULT_AI_BASE_URL=https://api.moonshot.cn/v1
DEFAULT_AI_API_KEY=sk-xxxxxxxx
DEFAULT_AI_MODEL=moonshot-v1-8k

# Ollama（本地部署，无需 key）
DEFAULT_AI_BASE_URL=http://localhost:11434/v1
DEFAULT_AI_API_KEY=ollama
DEFAULT_AI_MODEL=llama3
```

用户可在设置页（右上角齿轮图标）为自己单独配置 AI 通道，优先级高于 `.env` 实例级默认值。

---

## 配置项说明

所有配置项均在 `.env` 文件中设置（从 `.env.example` 复制）。留空即使用代码内默认值。

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `AUTH_MODE` | `single` | `single`：本机免登录单用户；`multi`：多用户注册登录 |
| `HOST` | `127.0.0.1` | 服务监听地址；Docker 容器内默认 `0.0.0.0` |
| `PORT` | `8770` | 服务端口；与其他服务冲突时可改（如 `8771`） |
| `DB_PATH` | `data/collect.db` | SQLite 数据库路径（留空用仓库内 `data/` 目录） |
| `FULLTEXT_DIR` | `fulltext/` | 打星文章全文存放目录 |
| `CLAUDE_BIN` | 自动探测 | claude CLI 可执行文件路径；留空则 PATH 中自动找 |
| `DEFAULT_AI_CHANNEL` | `cli` | 默认 AI 通道：`cli` / `anthropic` / `openai` |
| `DEFAULT_AI_BASE_URL` | 空 | OpenAI 兼容接口地址（`openai` 通道时填） |
| `DEFAULT_AI_API_KEY` | 空 | API key（`cli` 通道不需要） |
| `DEFAULT_AI_MODEL` | `claude-sonnet-4-6` | 默认使用的模型名 |
| `EDITORIAL_ROOT` | 空 | 本机「编辑部」项目根路径（可选增强，支持 playwright 动态页全文抓取） |
| `ALLOW_REGISTER` | `true` | `multi` 模式是否允许注册新用户；设为 `false` 可关闭开放注册 |
| `SECRET_KEY` | 空 | 会话 token 签名密钥；**`multi` 模式公网部署务必设置随机长串** |

---

## 安全提示

**`multi` 模式公网部署时请注意：**

1. **`SECRET_KEY` 必须修改**：留空会使用不安全的默认行为。生成随机密钥：
   ```bash
   openssl rand -hex 32
   ```

2. **建议在反向代理后加 HTTPS**：可用 nginx + Let's Encrypt（Certbot），避免 cookie 明文传输。示例 nginx 配置：
   ```nginx
   location / {
       proxy_pass http://127.0.0.1:8770;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
   }
   ```

3. **数据目录权限**：`data/` 和 `fulltext/` 含用户数据（SQLite 库含密码 hash），注意宿主机目录权限（建议 `chmod 700`）。

4. **`ALLOW_REGISTER=false`**：部署完成、完成初始注册后可关闭开放注册防止陌生人注册。

---

## 架构简图

```
collect-app/
├── run.py              ← 本地入口：读 .env 起 uvicorn（单 worker）
├── app/
│   ├── main.py         ← FastAPI 实例 + lifespan（调度器）+ 静态挂载
│   ├── config.py       ← 配置中心（读 .env，统一暴露 settings 单例）
│   ├── deps.py         ← get_db / current_user（single 直返默认用户）
│   ├── core/
│   │   ├── db.py       ← SQLite 数据层（source/feed/seen/users/session 表）
│   │   ├── crawler.py  ← 爬虫（RSS/sitemap/启发式/agent 配方）
│   │   └── fulltext.py ← 内置全文提取（stdlib），编辑部可选增强
│   ├── auth/
│   │   └── service.py  ← 注册/登录/会话（stdlib scrypt + HttpOnly cookie）
│   ├── llm/
│   │   ├── base.py     ← 统一接口 complete() / stream()
│   │   ├── claude_cli.py     ← 本机 claude CLI 通道
│   │   ├── anthropic_native.py ← Anthropic 官方 API 通道
│   │   └── openai_compat.py  ← OpenAI 兼容通道（DeepSeek/Kimi/Ollama）
│   ├── agent_tasks.py  ← 接入助手任务管理（内存态，单 worker 限制）
│   └── routers/
│       ├── auth.py     ← /api/auth/register|login|logout|me
│       ├── sources.py  ← /api/mon-source
│       ├── feed.py     ← /api/feed + /api/mon-run
│       ├── agent.py    ← /api/agent-crack
│       └── assistant.py ← /api/assistant/chat（管家，SSE 流式）
├── web/                ← 静态页面（FastAPI 同源托管，无构建步骤）
│   ├── index.html      ← 主页：feed 信息流 + 打星 + 监控源管理
│   ├── starred.html    ← 全部打星档案，可改星
│   ├── summary.html    ← ★2/★3 当天工作集，可读全文
│   ├── login.html      ← 登录/注册页（multi 模式）
│   ├── settings.html   ← AI 通道配置页
│   └── shared.js       ← 公共 api() wrapper（相对路径 + 401 跳登录）
├── data/               ← SQLite 数据库（.gitignore 排除，docker 卷挂载）
└── fulltext/           ← 打星文章全文 JSON（.gitignore 排除，docker 卷挂载）
```

> **单 worker 限制**：接入助手任务状态（`AGENT_TASKS`）存在内存中，多 worker 实例间不共享。`run.py` 和 `Dockerfile` 均默认单 worker，不要在 `docker compose` 中添加 `--workers` 参数。后续版本将改为落库持久化。

---

## 数据目录

| 目录 | 内容 | 备注 |
|---|---|---|
| `data/collect.db` | SQLite 数据库：监控源、feed 条目、用户、会话 | 个人数据，已被 `.gitignore` 排除 |
| `fulltext/*.json` | 打星文章全文（每条 feed 一个 JSON 文件） | 个人数据，已被 `.gitignore` 排除 |

首次运行 `python run.py` 时，`data/` 和 `fulltext/` 目录及数据库会自动创建，无需手动操作。

Docker 部署时通过 `volumes` 挂载到宿主机，容器销毁后数据不丢失。

---

## 技术说明

- **无构建步骤**：前端为原生 HTML + JS，无 npm/webpack，改完刷新浏览器即可
- **最小依赖**：`fastapi` / `uvicorn[standard]` / `python-multipart`，无 ORM / Redis / Celery
- **全中文**：界面、注释、报错信息统一使用中文
- **stdlib 优先**：配置读取、密码 hash（scrypt）、HTTP 请求（urllib）均用标准库，减少依赖链风险
