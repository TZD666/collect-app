#!/usr/bin/env python3
"""监控源接入助手（agent）— 从 server.py 迁入，逻辑保持。

试抓失败的源 → 经 AI 分析页面产出「抓取配方」JSON → 验证 ≥2 条才收并自动加源。
反爬站自动探测 11 种「给搜索引擎留的公开通道」（news-sitemap / RSS 等）+ 栏目过滤。
任务态在内存（AGENT_TASKS）——server 重启即丢（已知限制）。

【工程化迁移】改动：
  `import crawler` → `from app.core import crawler`
  `_cdb()` → `from app.core import db as cdb`
  原 `call_via_cli(RECIPE_SPEC, user_msg)` → `app.llm.get_provider(...).complete(...)`
  新增 agent_available()：替代原 CLI_AVAILABLE，统一走 llm 层的 provider.available。
"""
import json
import re
import time

from app.config import settings
from app.core import crawler
from app.core import db as cdb

# 接入助手任务表（内存态，单 worker 单进程；server 重启即丢）
AGENT_TASKS = {}

RECIPE_SPEC = """你是「监控源接入工程师」，唯一任务：分析给定网页，产出一份 JSON 抓取配方(recipe)，
让程序能从该页提取文章列表（标题+链接+日期）。只处理本任务，拒绝任何无关请求。
配方格式（输出严格的单个 JSON 对象，不要 markdown 代码块，不要解释）：
{
 "fetch_url": "可选。默认抓源地址；若列表数据来自页面 JS 调用的接口(XHR/JSON)，写完整接口地址（分页参数写死第1页）",
 "headers": {"可选": "附加请求头"},
 "extract": "regex 或 json",
 "pattern": "extract=regex 时：对响应文本运行的正则，必须含命名组 (?P<title>...) 和 (?P<url>...)，可选 (?P<date>...)；将以 re.DOTALL 执行",
 "list_path": "extract=json 时：点路径定位数据，如 data.list 或 data.html",
 "then_pattern": "若 list_path 取到的是 HTML 字符串，再用此命名组正则提取",
 "title_key": "...", "url_key": "...", "date_key": "..."
}
要求：宁可保守只命中真实文章行，也不要匹配到导航/页脚/相关推荐；url 可以是相对路径。"""


def agent_available():
    """接入助手是否可用 = 默认 AI 通道的 provider 是否就绪（替代原 CLI_AVAILABLE）。"""
    try:
        from app.llm.base import get_provider
        return get_provider(settings.default_ai_cfg()).available
    except Exception:
        return False


def _alog(tid, who, text):
    t = AGENT_TASKS.get(tid)
    if t is not None:
        t["log"].append({"ts": time.strftime("%H:%M:%S"), "who": who, "text": text})


_PROBE_PATHS = (
    "/arc/outboundfeeds/news-sitemap/?outputType=xml",  # Arc CMS（路透/华盛顿邮报等）
    "/arc/outboundfeeds/sitemap/?outputType=xml",
    "/arc/outboundfeeds/rss/?outputType=xml",
    "/news-sitemap.xml", "/sitemap-news.xml", "/sitemap.xml",
    "/rss.xml", "/rss", "/feed", "/atom.xml", "/index.xml",
)


def _probe_channels(tid):
    """直连被反爬时：探测站点常见公开通道（搜索引擎专用 sitemap/RSS），命中即自动加源。
    原 URL 带栏目路径时优先按栏目过滤（feed 配方），过滤后太少则退全站。成功返回 True。"""
    from urllib.parse import urlsplit
    t = AGENT_TASKS[tid]
    url = t["url"]
    pu = urlsplit(url)
    base = f"{pu.scheme}://{pu.netloc}"
    section = pu.path.rstrip("/") if len(pu.path.rstrip("/")) > 1 else ""
    _alog(tid, "agent", "直连被拒，改试该站「给搜索引擎留的公开通道」（news-sitemap / RSS …）")
    for path in _PROBE_PATHS:
        cand = base + path
        try:
            kind, items = crawler.discover(cand)
        except Exception:
            continue
        if len(items) < 2:
            continue
        _alog(tid, "agent", f"通道命中：{cand}（{kind.upper()}，{len(items)} 条）")
        recipe, use_items, src_url, src_kind = "", items, cand, kind
        if section:
            filtered = [i for i in items if section + "/" in i["url"]]
            if len(filtered) >= 2:
                recipe = json.dumps({"fetch_url": cand, "extract": "feed",
                                     "url_contains": section + "/"}, ensure_ascii=False)
                use_items, src_url, src_kind = filtered, url, "agent"
                _alog(tid, "agent", f"按栏目 {section}/ 过滤 → {len(filtered)} 条")
            else:
                _alog(tid, "agent", f"该通道为全站源，栏目 {section}/ 仅 {len(filtered)} 条 → 改接全站（可打星自筛）")
        conn = cdb.connect()
        cdb.init_db(conn)
        try:
            src = cdb.source_add(conn, src_url, t["params"].get("title") or (pu.netloc + section), src_kind,
                                 t["params"].get("freq", "daily"), t["params"].get("grp", ""),
                                 t["params"].get("run_times", "08:00"),
                                 t["params"].get("backfill_days", 7), recipe)
        except Exception as e:
            _alog(tid, "agent", f"该通道源已存在或入库失败（{e}），继续探下一个…")
            conn.close()
            continue
        conn.close()
        t["state"] = "success"
        t["source"] = src
        t["preview"] = use_items[:5]
        _alog(tid, "agent", f"✅ 成功！经公开通道接入（{len(use_items)} 条，如「{use_items[0]['title'][:30]}」）。"
                            "注意：此类站文章页可能仍反爬，打星抓全文或失败（⚠），点链接浏览器看原文不受影响。")
        return True
    return False


def _agent_attempts(tid, rounds=2):
    """跑至多 rounds 次「产配方→验证」。成功自动加源；不成功转 waiting 等用户线索。"""
    t = AGENT_TASKS[tid]
    url = t["url"]
    try:
        if not t.get("html"):
            _alog(tid, "sys", "抓取页面原始 HTML…")
            t["html"] = crawler.fetch(url)
            _alog(tid, "sys", f"已取回 {len(t['html'])} 字符")
    except Exception as e:
        _alog(tid, "agent", f"页面直连失败（{e}）——多半是反爬。")
        if _probe_channels(tid):
            return
        t["state"] = "waiting"
        _alog(tid, "agent", "公开通道也没探到。给我线索：该站的 RSS/sitemap 地址、手机版入口、或数据接口，我接着试。")
        return

    for _ in range(rounds):
        hints = "\n".join(t["hints"]) if t["hints"] else ""
        t["hints"] = []
        user_msg = (f"源地址：{url}\n\n页面 HTML（截断 30000 字符）：\n{t['html'][:30000]}\n\n"
                    + (f"此前尝试的失败记录：\n{chr(10).join(t['history'][-4:])}\n\n" if t["history"] else "")
                    + (f"用户补充线索：{hints}\n\n" if hints else "")
                    + "请输出配方 JSON。")
        n = len(t["history"]) + 1
        _alog(tid, "agent", f"第 {n} 次：请 AI 分析产出配方…")
        try:
            # 惰性 import：避免启动时 llm 包未就绪报错
            from app.llm.base import get_provider
            provider = get_provider(settings.default_ai_cfg())
            text = provider.complete(RECIPE_SPEC, user_msg)
        except Exception as e:
            t["history"].append(f"第{n}次：调用 AI 失败 {e}")
            _alog(tid, "agent", f"调用 AI 失败：{e}")
            continue
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            t["history"].append(f"第{n}次：输出里没有 JSON")
            _alog(tid, "agent", "这次输出不含合法 JSON，重试…")
            continue
        try:
            recipe = json.loads(m.group(0))
        except ValueError as e:
            t["history"].append(f"第{n}次：JSON 解析失败 {e}")
            _alog(tid, "agent", "配方 JSON 解析失败，重试…")
            continue
        _alog(tid, "agent", f"拿到配方（{recipe.get('extract','?')} 模式），验证执行…")
        try:
            items = crawler.run_recipe(url, recipe)
        except Exception as e:
            t["history"].append(f"第{n}次：配方执行报错 {e}（配方：{json.dumps(recipe, ensure_ascii=False)[:300]}）")
            _alog(tid, "agent", f"配方执行报错：{str(e)[:120]}")
            continue
        if len(items) >= 2:
            conn = cdb.connect()
            cdb.init_db(conn)
            try:
                src = cdb.source_add(conn, url, t["params"].get("title") or url, "agent",
                                     t["params"].get("freq", "daily"), t["params"].get("grp", ""),
                                     t["params"].get("run_times", "08:00"),
                                     t["params"].get("backfill_days", 7),
                                     json.dumps(recipe, ensure_ascii=False))
            finally:
                conn.close()
            t["state"] = "success"
            t["source"] = src
            t["preview"] = items[:5]
            _alog(tid, "agent", f"✅ 成功！提取到 {len(items)} 条，例如「{items[0]['title'][:30]}」。已自动添加为监控源（AGENT 配方）。")
            return
        t["history"].append(f"第{n}次：配方只提取到 {len(items)} 条（需≥2）（配方：{json.dumps(recipe, ensure_ascii=False)[:300]}）")
        _alog(tid, "agent", f"配方只提取到 {len(items)} 条，不达标，换思路重试…")

    t["state"] = "waiting"
    _alog(tid, "agent", "暂时没成功。你可以告诉我线索——比如列表数据在哪个接口、页面哪一块是文章列表、或换一个入口地址——我接着试。")
