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

## 2026-06-04（续）
- 【改动】波次3：收集管家聊天上线（/api/assistant/chat SSE 流式 + 服务端注入近7天 feed 快照 + index 右下角抽屉打字机 UI）
- 【测试】最小 pytest 套件 38 绿（db 多租户隔离 27 + auth/加密 11），conftest 拦截 DB_FILE 保证不碰真实库
- 【文档】CLAUDE.md/AGENTS.md 重写为新架构交接文档；旧版描述的 8 颗钉子全部解除
- 【里程碑】进入最终自检验收：独立 verifier 跑验收矩阵 + code-reviewer 独立审查

## 2026-06-04（终审修复）
- 【修复·C1】index.html sendBeacon 关页上报已读改用 `Blob(...,{type:'application/json'})`——原 text/plain 被 FastAPI 拒收 422（已复现），最后一批已读不再静默丢失。
- 【修复·H1】db.py `feed_set_star/feed_set_fulltext/feed_pin` 增 user_id 必选参 + UPDATE `AND user_id=?` 越权防线；同步 feed.py 调用点与取消星 DELETE；补 3 条越权不生效测试。
- 【修复·H2】API Key 加密升级 enc2 格式：每条 `os.urandom(16)` 随机 salt，keystream=HMAC(SECRET_KEY, salt+counter)，存 `enc2$<salt_hex>$<b64>`；dec 兼容 enc2/enc1（旧固定流）/无前缀明文。杜绝同密钥同流复用。
- 【修复·H3】claude_cli.py 删除硬编码 `~/.local/bin/claude` 兜底，只留 cfg["cli_path"]→which("claude") 两级。
- 【修复·H4/L5】feed.py/sources.py 改 `conn=Depends(get_db)`，去掉每请求 connect+init_db（迁移检查浪费），连接关闭由 get_db 的 finally 保证。
- 【修复·M1/M7/L2】shared.js esc() 补单引号转义；index.html kind 标签 + 时间 chip 补 esc()。
- 【修复·M3】agent_tasks 加 `_TASKS_LOCK`；agent.py 任务创建加锁、latest 改 `list(...)` 加锁快照迭代。
- 【修复·M4】main.py lifespan：multi 模式未配 SECRET_KEY 时打印安全告警（flush）。
- 【修复·M5】db.py fulltext_path 校验 fid 仅含 `[A-Za-z0-9_]`，挡路径穿越。
- 【修复·M6】COOKIE_SECURE 可配（config + .env.example），set_cookie 加 secure。
- 【收尾】run.py setdefault PYTHONUNBUFFERED；调度循环 print 加 flush；db._migrate_source_unique f-string 处注释「collist 来自 PRAGMA，无注入面」。
- 【测试】pytest 43 绿（新增 enc2 随机性/enc1 兼容/feed 三函数越权防线）；`from app import main` 通过；8777 实测 list 正常、text/plain→422、json→200、enc2 往返成功；app/ web/ 无硬编码本机绝对路径。
- 【修复】用户实测发现缺口：后端 /auth/logout 早已就绪但前端无登出入口（多 agent 拼接缝漏点）。index 顶栏补「⎋ 登出」按钮 + 👤 当前用户名（仅 multi 模式显示），curl 全链路验证注册→me→登出→401 通过
- 【改动】用户端入口收官（用户确认：网址即用户端，不做下载安装包）：App 图标（渐变+五角星，scripts/make_icons.py 生成）、PWA 化（manifest+五页 head 注入+主题色）、登录页门面升级（图标+slogan）、品牌名统一为「收集App」、HOST=0.0.0.0 时启动横幅打印局域网手机访问地址、macOS 双击启动器 启动收集App.command、README 增「📱用户怎么用」「🌐公网部署(Caddy HTTPS)」两节
- 【清洗】敏感数据收尾：删本地 .bak 迁移备份、清测试账号 tommy2；git 追踪文件审计确认无密钥/个人路径（sk- 均为占位示例）
- 【改动】公网服务上线（复刻舒尔特方格的 ngrok 方法）：scripts/ngrok_wrapper.py（按端口匹配隧道+多应用 start --all）+ 开启公网隧道.command；ngrok 免费固定域名经用户拍板划给收集App（https://sphinx-throwaway-campsite.ngrok-free.dev → 8780），舒尔特公网下线（本地 8888 照常，其项目内 cloudflared 方案可随时复活）；公网 curl 实测 /api/info 通过。顺带修复舒尔特 ngrok_wrapper 同款「抓第一个隧道」bug
- 【改动】2:00 清理规则变更（用户要求）：原「清已读且无星」→ 改为「全清资讯流」。feed 加 archived 列（迁移幂等补列）；db.feed_daily_clear() 无星条目（含未读）直接删、打星条目置 archived=1 移出资讯流但保留在「重点新闻」（连日期+全文）；feed_list 资讯流视图加 archived=0 过滤、重点新闻/总结(only_starred)含归档；stats 的 feed_total/unread 只算未归档、starred/star3 仍算全量档案；main.py 调度切到 feed_daily_clear。新增 4 个 db 测试（共 46 绿）+ 临时库沙箱模拟 2:00 全链路（含旧库迁移+幂等守卫）通过。

## 2026-06-15（反爬抓取修复）
- 【修复】强反爬站「半截掐断」导致加源/抓取失败：路透 news-sitemap 等会先传一段再断开连接，`fetch()` 的 `resp.read()` 抛 `http.client.IncompleteRead` 整个失败（实测加源报 `IncompleteRead(28760 bytes read)`、`crawler --test` FAIL）。改为捕获 `IncompleteRead` 取 `e.partial` 抢救已收字节；并加 `_salvage_truncated_xml()`：解析 ParseError 时，截到最后一个完整条目（`</url>`/`</item>`/`</entry>`）补齐根闭合标签再解析一次，救出前面的完整条目。pytest 46 绿（无回归）+ 真实网络端到端验证：tommy 加 sitemap 源→/mon-run 抓得 47 条 feed。
- 【说明】路透「板块页」(/business/healthcare-pharmaceuticals/) 仍 401 硬封、discover() 不自动探公开通道，故只能直接加 news-sitemap 地址；且该 sitemap 是全站非医药板块，盯医药建议换 FiercePharma/Endpoints/STAT/FDA 等有 RSS 的站。

## 2026-06-23（2:00 清理补偿式触发 + CLI 路径修复）
- 【修复】用户反馈 6-20/21/22 的无星条目仍在资讯流没被清。根因：清理是 main.py 调度线程的**进程内定时器**，仅当进程恰在 02:00–02:59 这个钟点执行到判断行才触发；个人 Mac 半夜睡眠会挂起进程、错过该窗口（meta.last_read_clean 停在 2026-06-19，6-20/21/22 三晚均未跑）。**非 SQL/逻辑错，是定时机制脆弱。**
- 【改动】调度切「补偿式 + 按日期范围」：`tm_hour==2` 改 `tm_hour>=2`（当天未清过且已过 2 点就清，电脑醒来当天首跑即补清）；`db.feed_daily_clear` 增 `before_date` 形参，调度按 `before_date=today` 只清「今天之前」的条目，避免补偿时误删当天还没筛选的新条目。`before_date=None` 保持原「全清」语义、向后兼容（不影响既有测试）。
- 【处理】外科式清掉积压的 6-22 及更早无星 15 条（6-20:7 / 6-21:1 / 6-22:7），保留当天 21 条待筛选。
- 【配置】`.env` 加 `CLAUDE_BIN=~/.local/bin/claude`：AI「本机 claude CLI」测试报「未找到」实为 PATH 问题——GUI/双击/launchd 启动的进程 PATH 不含 `~/.local/bin`，`shutil.which("claude")` 扑空；显式配绝对路径绕开探测（config.py:99「非空直接用」）。
