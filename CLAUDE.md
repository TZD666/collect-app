# CLAUDE.md — 收集App（工程化改造交接文档）

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这个文件夹是什么

「收集App」是从 `/Users/edy/Desktop/笔记台` **复制打包**出来的功能一「收集资料」模块（2026-06-04 打包，原 demo 在笔记台里继续保留）。它已在 HTML demo 形态下完整跑通并通过用户验收，现在的任务是**工程化改造**。

**本期改造目标（用户拍板，按此验收）：**
1. 能上传到 GitHub（干净的仓库结构、不带个人数据）；
2. 在**任意一台其他电脑**上 clone / 安装下来就能使用。

远期方向（本期不做，但改造时别堵死路）：部署到服务器、多用户登录、身份层 + 模块间权限——见 `docs/功能梳理.md` 0.7「模块化架构观」。

**改造约束（用户原话）**：已沉淀的基础代码**不推翻**，在其上做更规范、更工程化的改造。

## 产品定位

帮社媒运营从业者**快速收集和筛选其关注领域的新闻**：添加监控源 → 定时抓取出 feed 信息流 → 人工打星筛选（★1 存档 / ★2 今天用 / ★3 爆点）→ 打星才抓全文留存 → 高分文章传给下一模块（功能二「总结」）。后续扩展：收集公众号某个号、各社媒/发文媒体的某个频道。

产品蓝图与决策记录都在 `docs/`：`功能梳理.md`（总纲）、`功能一-实现目标.md`（构建契约 D1-D5 + 验收记录 + 各端点请求契约）。

## 运行 / 调试命令

无测试、无 lint、无构建、无 pip 依赖（纯 Python stdlib）。

```bash
python3 server.py                      # 起服务（前台，127.0.0.1:8770），自带后台调度线程
open 收集.html                          # 主入口页面（直接文件协议打开，无需静态服务器）

# 爬虫可独立调试（不经 server）：
python3 收集/crawler.py                 # 跑所有「到期 + 启用」的源（增量）
python3 收集/crawler.py <source_id>    # 只跑指定源，忽略冷却
python3 收集/crawler.py --test <url>   # 试抓某 URL，输出可解析条目数（加源校验同款逻辑）

python3 收集/db.py                      # 初始化/迁移数据库（幂等）
sqlite3 收集/data/collect.db "SELECT * FROM source"   # 直接查库
```

- 改 HTML 后刷新浏览器即可；改 `server.py` / `收集/*.py` 后需重启 server（先 `lsof -ti:8770 | xargs kill`）。
- ⚠️ 端口 8770 与原笔记台 server 冲突，**同一时间只能跑一个**（待改造项，见下）。

## 架构与数据流

```
server.py        # HTTP 桥接服务（stdlib，ThreadingMixIn 多线程，127.0.0.1:8770）
收集.html        # 页面1：feed 信息流 + 打星 + 监控源管理（主入口）
重点新闻.html    # 页面2：全部打星档案，可改星
总结.html        # 页面3：仅 ★2/★3 当天工作集，可读全文
收集/db.py       # SQLite 数据层（data/collect.db，WAL）：source/seen/feed/meta 四表 + GC + stats
收集/crawler.py  # 爬虫：只负责「发现新条目」（标题+链接+日期），不抓全文
收集/data/ 收集/fulltext/   # 运行数据（个人数据，已 .gitignore，勿入 git）
收集/com.bijitai.collect.plist + install-crawl.sh   # launchd 定时（遗留，路径已失效）
```

收集模块真正用到的端点：`/info` `/mon-source` `/mon-run` `/feed` `/agent-crack`。请求契约细节见 `docs/功能一-实现目标.md`。server.py 里其余端点（`/notebook` `/collect` `/publish` `/crawl-config` `/claude` 的前端调用方）是笔记台遗留（见钉子清单 1）。

关键业务流（多文件协作，读码前先理解这个）：

- **发现新条目**：`_collect_scheduler()` daemon 线程每 60s 调 `crawler.run()`（按 freq/run_times 冷却判断到期源）→ `crawler.discover(url)` 判别链依序尝试：RSS/Atom/sitemap（含 Google News 扩展）→ 大汉 jpaas CMS（政府站）→ HTML 列表页启发式（**主路**：href 含日期路径/链接旁日期 + 锚文本≥8字）→ agent 配方（`run_recipe`，extract=regex|json|feed）。带 PID 锁 + seen 表增量去重 + 7 天报道日期窗口 + 单源单次 40 条上限。
- **手动抓取 vs 定时**：`/mon-run` 默认 `force=True` = 强制首抓（无视冷却、按源 `backfill_days` 回溯重拉，可找回已清理条目）；调度器走增量。
- **打星即一切**（`/feed action=star`）：star>0 且无全文 → `fulltext_fetch()` 惰性抓全文落 `收集/fulltext/{feed_id}.json`；star=0 → 删全文，且报道日期超 7 天窗口的条目直接从 feed 删除。无星不持久化。
- **已读与 GC**：前端入屏停留判定后批量上报 `/feed action=read`（只记首次时间，不影响排序）；调度器每晚 2:00 清「已读且无星且未挂工作流」（meta 表记 `last_read_clean` 保证一天一次幂等）；`/feed action=gc, hours=0` 手动立即清全部无星。
- **🤖 接入助手**（`/agent-crack`，加源试抓失败时前端小窗唤起）：`start` 起线程 → `crawler.fetch` 拿 HTML → 经 claude CLI 按 `RECIPE_SPEC` 产抓取配方 JSON → `run_recipe` 验证 ≥2 条才收并自动加源（kind=agent）。页面直连被拒时先探 11 种「给搜索引擎留的公开通道」（`_PROBE_PATHS`：Arc CMS news-sitemap / rss 等）+ 按原 URL 栏目路径过滤。任务态 `running/waiting/success`；`say` 递线索续跑、`status` 轮询日志、`latest` 页面启动接上活跃任务、`log` 供外部工作者报工（`start` 传 `external:true` 则不起内置线程，由外部 Claude 接管）。
- **加源契约**（`/mon-source action=add`）：必须先 `crawler.discover()` 试抓出 ≥1 条才允许添加，否则返回 `{ok:false, error:中文原因, agent_available}`。

DB 注意：`db.init_db()` 的 `_MIGRATIONS` 是「缺列就补」的幂等迁移——改 schema 时同步往该元组追加 ALTER 语句，别只改 `SCHEMA`。

## ⚠️ 改造时要解开的钉子（硬编码/遗留清单）

1. **server.py 含大量「笔记台」遗留**：`/notebook` `/collect` `/publish` `/crawl-config`、notebook_* / collect_source / publish_notebook / PROMPTS 流水线相关代码都属于原笔记本应用，**与收集模块无关，可剥离**（剥离前对照上面「真正用到的端点」清单；注意 `/claude` 的 `call_via_cli` 被接入助手复用，不能整段删）。
2. **claude CLI 路径硬编码**：`CLAUDE_BIN = /Users/edy/.local/bin/claude`（接入助手 `_agent_attempts` 用它产抓取配方）。换机器需可配置 / PATH 探测；CLI 不存在时接入助手已会优雅降级（`agent_available`/`CLI_AVAILABLE` 标志）。
3. **打星抓全文依赖外部「编辑部」项目**：`fulltext_fetch()` 调 `/Users/edy/Desktop/自动化文案发布全流程/编辑部/采集.py`（readability + playwright 兜底）。**其他电脑上没有这个项目**——需要内置一个简易全文抓取（stdlib urllib + 正文提取即可起步），编辑部仅作本机可选增强。
4. **前端硬编码** `const API='http://127.0.0.1:8770'`（三个 HTML 都有）。
5. **launchd plist / install-crawl.sh 写死** `/Users/edy/Desktop/笔记台/收集`（打包后路径已失效；且 launchd 是 macOS 专属——server 内置调度器已能替代它，跨平台可考虑直接砍掉）。
6. **端口 8770 与原笔记台冲突**：本文件夹的 server.py 和笔记台的 server.py 同端口，**同一时间只能跑一个**。改造时建议换端口或做成可配置。
7. **🤖接入助手任务表 `AGENT_TASKS` 在内存**：server 重启即丢（已知限制，可后续落库）。
8. **注释/文档有过时陈述**：如 `crawler.py` 头注释仍写「HTML 列表页发现 = 后续档，本期不做」（实际已是主路）、server 启动横幅写「每 5 分钟检查到期源」（实际 60s）。改造顺手修正，别照抄。

## 约定

- **全中文**：界面、注释、报错、文档统一中文。
- 报错回传前端用可读中文（沿用 `_clean_traceback` 风格，从 traceback 末尾抽异常信息），不暴露长栈。
- 数据文件（`collect.db`、`fulltext/`）是用户个人数据：`.gitignore` 已排除，仓库里应放空目录占位或首次运行自动建（`db.connect()` 已会自动建 `data/` 目录）。
- 原笔记台有「开发笔记 devlog」账本约定；本仓库独立后可自带一份开发日志（形式不限，但保持「只追加」习惯）。
