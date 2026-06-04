#!/usr/bin/env python3
"""收集模块爬虫 — 只负责「发现新条目」，纯 stdlib（urllib + ElementTree）。

【工程化迁移】逻辑零改动，仅：
  `import db`     → `from app.core import db`
  LOCK_FILE       → settings.DATA_DIR/.crawl.lock
  去掉 sys.path 注入

范式取自 claude进化史/scrape_official_news.py：锁(PID) + 冷却(按 freq) + 快照(增量去重)。
发现判别链：RSS/Atom/sitemap → HTML 列表页启发式（主路） → 大汉 jpaas CMS 兜底 → AI 配方。

职责边界：
  本脚本只把「新条目（标题+链接）」写进 feed 表，不抓全文。
  全文抓取由打星时惰性触发（app.core.fulltext）。

用法：
  python -m app.core.crawler            # 跑所有「到期 + 启用」的源
  python -m app.core.crawler <source_id> # 只跑指定源（忽略冷却）
  python -m app.core.crawler --test <url># 试抓某 URL，输出可解析条目数（加源校验用）
"""
import html as html_lib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

from app.config import settings
from app.core import db

LOCK_FILE = os.path.join(settings.DATA_DIR, ".crawl.lock")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121 Safari/537.36"
FRESH_LIMIT = 40   # 单源单次最多入库的新条目数，防列表页过长
WINDOW_DAYS = 7    # 报道日期回溯窗口：老于此窗口的条目不入 feed（首抓只回溯 7 天）


# ── 报道日期解析 ─────────────────────────────────────────────────
_URL_DATE = re.compile(r"/(20\d\d)(\d\d)(\d\d)/")          # /20260602/
_URL_DATE2 = re.compile(r"/(20\d\d)-(\d\d)-(\d\d)")        # /2026-06-02
_URL_DATE3 = re.compile(r"/(20\d\d)(\d\d)/(\d\d)\b")       # /202606/03/（ce.cn 等）
_URL_DATE4 = re.compile(r"\bt(20\d\d)(\d\d)(\d\d)_")       # t20260603_（经济网文件名）
_URL_DATE5 = re.compile(r"/(20\d\d)/(\d\d)(\d\d)/")        # /2026/0603/（人民网等）
_ISO_DATE = re.compile(r"(20\d\d)-(\d\d)-(\d\d)")
_RFC_DATE = re.compile(r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(20\d\d)")
_MONTH = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
          "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"}


def extract_pub_date(url, raw_date=""):
    """报道日期：优先解析 RSS/sitemap 的日期字段，回落 URL 日期路径。返回 YYYY-MM-DD 或 ''。"""
    if raw_date:
        m = _ISO_DATE.search(raw_date)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = _RFC_DATE.search(raw_date)
        if m:
            return f"{m.group(3)}-{_MONTH[m.group(2)]}-{m.group(1).zfill(2)}"
    for pat in (_URL_DATE, _URL_DATE2, _URL_DATE3, _URL_DATE4, _URL_DATE5):
        m = pat.search(url)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


# ── 网络 ─────────────────────────────────────────────────────────
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace")


def _localname(tag):
    """去掉命名空间：'{http://...}item' -> 'item'。"""
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


# ── 解析：RSS / Atom / sitemap 三合一 ───────────────────────────
def parse_feed(xml_text):
    """返回 (kind, items)；items=[{title,url,date}]。无法解析则 kind=None。"""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return None, []

    rootname = _localname(root.tag)

    # ── sitemap：<urlset><url><loc>+<lastmod>；Google News 扩展再带 <news:title>/<news:publication_date>
    if rootname == "urlset":
        items = []
        for url_el in root:
            if _localname(url_el.tag) != "url":
                continue
            loc, lastmod, news_title = "", "", ""
            for ch in url_el:
                ln = _localname(ch.tag)
                if ln == "loc":
                    loc = (ch.text or "").strip()
                elif ln == "lastmod":
                    lastmod = (ch.text or "").strip()[:10]
                elif ln == "news":  # news-sitemap 扩展块
                    for nch in ch.iter():
                        nln = _localname(nch.tag)
                        if nln == "title":
                            news_title = (nch.text or "").strip()
                        elif nln == "publication_date" and not lastmod:
                            lastmod = (nch.text or "").strip()[:10]
            if loc:
                # 无标题时把 URL slug 美化成可读文字（连字符→空格，去尾部日期）
                slug = loc.rstrip("/").rsplit("/", 1)[-1]
                slug = re.sub(r"-?20\d\d-\d\d-\d\d$", "", slug).replace("-", " ").strip()
                items.append({"title": news_title or slug or loc,
                              "url": loc, "date": lastmod})
        return "sitemap", items

    # ── RSS：<rss><channel><item><title>+<link>
    if rootname == "rss":
        items = []
        for ch in root.iter():
            if _localname(ch.tag) != "item":
                continue
            items.append(_rss_item(ch))
        return "rss", [i for i in items if i["url"]]

    # ── Atom：<feed><entry><title>+<link href>
    if rootname == "feed":
        items = []
        for entry in root:
            if _localname(entry.tag) != "entry":
                continue
            items.append(_atom_entry(entry))
        return "rss", [i for i in items if i["url"]]  # Atom 统一标 kind=rss 对用户无感

    return None, []


def _rss_item(item):
    title, link, date = "", "", ""
    for ch in item:
        ln = _localname(ch.tag)
        if ln == "title":
            title = (ch.text or "").strip()
        elif ln == "link":
            link = (ch.text or "").strip()
        elif ln in ("pubdate", "date", "updated", "published"):
            date = (ch.text or "").strip()
    return {"title": title or link, "url": link, "date": date}


def _atom_entry(entry):
    title, link, date = "", "", ""
    for ch in entry:
        ln = _localname(ch.tag)
        if ln == "title":
            title = (ch.text or "").strip()
        elif ln == "link":
            # 优先 rel=alternate 或无 rel 的 href
            href = ch.get("href", "")
            rel = ch.get("rel", "alternate")
            if href and rel in ("alternate", ""):
                link = href.strip()
            elif href and not link:
                link = href.strip()
        elif ln in ("updated", "published"):
            date = (ch.text or "").strip()
    return {"title": title or link, "url": link, "date": date}


# ── 解析：HTML 列表页（主路）──────────────────────────────────
# 文章详情页特征：href 路径里带日期（YYYYMMDD / YYYY-MM-DD / YYYY/MM/DD）
_DATE_PATH = re.compile(r"/20\d{6}/|/20\d\d-\d\d-\d\d|/20\d\d/\d\d/\d\d|/20\d\d/\d\d/")
_A_TAG = re.compile(r'<a[^>]+href="([^"#?][^"]*)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)


def _clean_text(t):
    return html_lib.unescape(re.sub(r"<[^>]+>", "", t)).strip()


def parse_html_list(base_url, html_text):
    """从 HTML 栏目页启发式提取文章链接。
    规则：锚文本≥8 字 + 日期证据（href 路径含日期，或链接后 160 字符内有 ISO 日期，
    如政府站列表的 <span>2026-06-01</span>）；相对链接补全、限同域、去重。"""
    host = urlparse(base_url).netloc
    out, seen = [], set()
    for m in _A_TAG.finditer(html_text):
        href, inner = m.group(1), m.group(2)
        title = _clean_text(inner)
        if len(title) < 8:
            continue
        # 链接后邻近的日期（列表行尾常见）
        trail = html_text[m.end():m.end() + 160]
        tm = _ISO_DATE.search(trail)
        adj_date = f"{tm.group(1)}-{tm.group(2)}-{tm.group(3)}" if tm else ""
        if not _DATE_PATH.search(href) and not adj_date:
            continue
        full = urljoin(base_url, href.strip())
        pu = urlparse(full)
        if pu.scheme not in ("http", "https"):
            continue
        if host and pu.netloc and pu.netloc != host:  # 限同域，过滤外链
            continue
        if full in seen:
            continue
        seen.add(full)
        out.append({"title": title[:300], "url": full, "date": adj_date})
    return out


# ── 大汉 jpaas CMS 兜底（工信部等政府部委站标配）─────────────────
# 列表由 AuthorizedRead/unitbuild.js 动态拉取；script 标签上带全套接口参数，
# 直接调 /front/page/build/unit 拿渲染后的列表 HTML，再走 parse_html_list。
_JPAAS_TAG = re.compile(r'<script[^>]+AuthorizedRead/unitbuild\.js[^>]*>', re.IGNORECASE)


def fetch_jpaas_unit(base_url, html_text):
    """识别 jpaas 动态列表并调接口取渲染 HTML；非 jpaas 页返回 ''。"""
    m = _JPAAS_TAG.search(html_text)
    if not m:
        return ""
    tag = m.group(0)
    um = re.search(r'\burl="([^"]+)"', tag)
    qm = re.search(r'queryData="([^"]+)"', tag)
    if not um or not qm:
        return ""
    try:
        qd = json.loads(html_lib.unescape(qm.group(1)).replace("'", '"'))
    except (ValueError, TypeError):
        return ""
    api = urljoin(base_url, um.group(1)) + "?" + urllib.parse.urlencode(qd)
    req = urllib.request.Request(api, headers={"User-Agent": UA, "Referer": base_url})
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("success"):
        return ""
    return (data.get("data") or {}).get("html", "")


# ── AI 配方执行器（接入助手产出的 recipe 在此执行，零硬编码扩展点）──────
def _dig(obj, path):
    """点路径取值：data.list / data.0.html"""
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)] if int(part) < len(obj) else None
        else:
            return None
    return obj


def _regex_items(text, pattern):
    """带命名组的正则提取：(?P<title>…) (?P<url>…) 可选 (?P<date>…)"""
    if not pattern:
        return []
    out = []
    for m in re.finditer(pattern, text, re.DOTALL):
        g = m.groupdict()
        out.append({"title": g.get("title", "") or "", "url": g.get("url", "") or "",
                    "date": g.get("date", "") or ""})
    return out


def run_recipe(src_url, recipe):
    """执行 AI 抓取配方。recipe（dict 或 JSON 串）：
      fetch_url   可选；默认源地址。可指向页面 JS 里发现的数据接口
      headers     可选附加请求头
      extract     "regex"（对响应文本跑 pattern）或 "json"
      pattern     extract=regex 的命名组正则
      list_path   extract=json：点路径定位；取到字符串(HTML)→ 跑 then_pattern；
                  取到对象数组 → 按 title_key/url_key/date_key 映射
    返回标准 items（同 parse_html_list 出参）。"""
    r = json.loads(recipe) if isinstance(recipe, str) else (recipe or {})
    url = r.get("fetch_url") or src_url
    headers = {"User-Agent": UA, "Referer": src_url}
    headers.update(r.get("headers") or {})
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gb18030", errors="replace")

    if r.get("extract") == "feed":
        # 结构化源（RSS/sitemap）+ 可选栏目过滤：url_contains 命中才保留
        _, items = parse_feed(text)
        contains = r.get("url_contains") or ""
        if contains:
            items = [i for i in items if contains in i["url"]]
    elif r.get("extract") == "json":
        val = _dig(json.loads(text), r.get("list_path", ""))
        if isinstance(val, str):
            items = _regex_items(val, r.get("then_pattern") or r.get("pattern", ""))
        elif isinstance(val, list):
            items = [{"title": str(it.get(r.get("title_key", "title"), "")).strip(),
                      "url": str(it.get(r.get("url_key", "url"), "")).strip(),
                      "date": str(it.get(r.get("date_key", "date"), "")).strip()}
                     for it in val if isinstance(it, dict)]
        else:
            items = []
    else:
        items = _regex_items(text, r.get("pattern", ""))

    out, seen = [], set()
    for it in items:
        t = _clean_text(it.get("title", ""))
        u = (it.get("url") or "").strip()
        if len(t) < 6 or not u:
            continue
        full = urljoin(src_url, html_lib.unescape(u))
        if urlparse(full).scheme not in ("http", "https") or full in seen:
            continue
        seen.add(full)
        out.append({"title": t[:300], "url": full, "date": it.get("date", "")})
    return out


def discover_for(src):
    """按源抓取：有 AI 配方走配方，否则走通用 discover。"""
    rec = src.get("recipe") or ""
    if rec:
        items = run_recipe(src["url"], rec)
        return ("agent" if items else None), items
    return discover(src["url"])


def discover(url):
    """试抓：拉取并解析，返回 (kind, items)。供加源校验 + 正式抓取共用。
    顺序：结构化源（RSS/Atom/sitemap）→ HTML 列表解析（主路）→ jpaas 动态列表（政府站兜底）。"""
    text = fetch(url)
    kind, items = parse_feed(text)
    if items:
        return kind, items
    items = parse_html_list(url, text)
    if items:
        return "html", items
    try:
        unit_html = fetch_jpaas_unit(url, text)
    except Exception:
        unit_html = ""
    if unit_html:
        items = parse_html_list(url, unit_html)
        if items:
            return "jpaas", items
    return None, []


# ── 锁 ───────────────────────────────────────────────────────────
def acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            old = int(open(LOCK_FILE).read().strip())
            os.kill(old, 0)
            return False
        except (OSError, ValueError):
            try:
                os.remove(LOCK_FILE)
            except OSError:
                pass
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def release_lock():
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


# ── 冷却 / 调度 ──────────────────────────────────────────────────
def _due(src):
    """到期判断。last_run 空=从未跑=到期。
    hourly：距上次 ≥1h。
    daily：按 run_times（如 "08:00,14:00"）——最近一个已过的时刻晚于 last_run 即到期。"""
    last = src.get("last_run") or ""
    if not last:
        return True
    t = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):  # 新带秒 / 旧到分
        try:
            t = time.mktime(time.strptime(last, fmt))
            break
        except ValueError:
            continue
    if t is None:
        return True
    now = time.time()
    if src.get("freq", "daily") == "hourly":
        return (now - t) >= 3600

    times = [s.strip() for s in (src.get("run_times") or "08:00").split(",") if s.strip()]
    sched = []
    for day_off in (0, -1):  # 今天 + 昨天的时刻表（覆盖跨天）
        day_str = time.strftime("%Y-%m-%d", time.localtime(now + day_off * 86400))
        for hm in times:
            try:
                ts = time.mktime(time.strptime(f"{day_str} {hm}", "%Y-%m-%d %H:%M"))
            except ValueError:
                continue
            if ts <= now:
                sched.append(ts)
    if not sched:
        return (now - t) >= 24 * 3600  # 时刻表无效则退回 24h 间隔
    return t < max(sched)


# ── 抓单源 ───────────────────────────────────────────────────────
def crawl_source(conn, src, force=False):
    """对一个源做发现，新条目写 feed。返回新增条数。
    增量模式（默认）：seen 快照去重——看过/清理过的不再回来。
    force 模式（手动抓取）：等同「首抓」——按 backfill_days 重拉窗口内全部条目，
    只要不在当前 feed 里就重新入库（清理后可找回）。"""
    try:
        kind, items = discover_for(src)
    except Exception as e:
        print(f"  · 源「{src['title']}」抓取失败：{e}")
        return 0
    if not items:
        print(f"  · 源「{src['title']}」无可解析条目")
        return 0

    # 报道日期窗口：首抓/强制 用源自带 backfill_days（1-14）；常规增量固定 WINDOW_DAYS
    first_run = not (src.get("last_run") or "")
    try:
        bf = max(1, min(14, int(src.get("backfill_days") or WINDOW_DAYS)))
    except (TypeError, ValueError):
        bf = WINDOW_DAYS
    window = bf if (first_run or force) else WINDOW_DAYS
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - window * 86400))

    added = skipped_old = 0
    for it in items:
        if added >= FRESH_LIMIT:
            break
        u = it["url"]
        if not u:
            continue
        if force:
            if db.feed_has_url(conn, src["id"], u):  # 已在流里的不重复
                continue
        elif db.seen_has(conn, src["id"], u):
            continue
        db.seen_add(conn, src["id"], u)  # 窗口外的也记快照，避免每轮重复评估
        pub = extract_pub_date(u, it.get("date", ""))
        if pub and pub < cutoff:
            skipped_old += 1
            continue
        db.feed_add(conn, user_id=src["user_id"], sid=src["id"],
                    title=it["title"][:300], url=u, pub_date=pub)
        added += 1
    conn.commit()
    db.source_touch(conn, src["id"])
    print(f"  · 源「{src['title']}」新增 {added} 条" + (f"（跳过 {skipped_old} 条 {window} 天前旧文）" if skipped_old else ""))
    return added


def run(only_source_id=None, force=False, user_id=None):
    """force=True（手动抓取）：所有启用源无视冷却，按首抓语义重拉。
    user_id 给了则只跑该用户的源（手动抓取）；不给则跨全用户（定时调度）。"""
    if not acquire_lock():
        print("已有抓取进程在跑，跳过")
        return
    try:
        conn = db.connect()
        db.init_db(conn)
        srcs = db.source_list_all(conn)
        if user_id is not None:
            srcs = [s for s in srcs if s["user_id"] == user_id]
        if only_source_id:
            srcs = [s for s in srcs if s["id"] == only_source_id]
            due = srcs  # 指定源忽略冷却
        elif force:
            due = [s for s in srcs if s["enabled"]]  # 手动：全部启用源
        else:
            due = [s for s in srcs if s["enabled"] and _due(s)]
        print(f"待抓源 {len(due)}/{len(srcs)}" + ("（强制首抓模式）" if force else ""))
        total = sum(crawl_source(conn, s, force=force) for s in due)
        print(f"完成。本轮新增 {total} 条。")
        conn.close()
    finally:
        release_lock()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--test":
        if len(args) < 2:
            print("用法：python -m app.core.crawler --test <url>")
            sys.exit(1)
        try:
            kind, items = discover(args[1])
        except Exception as e:
            print(f"FAIL 抓取异常：{e}")
            sys.exit(2)
        if not items:
            print("FAIL 非结构化源或无条目（请提供 RSS/Atom/sitemap 地址）")
            sys.exit(3)
        print(f"OK kind={kind} 条目={len(items)}")
        for it in items[:5]:
            print(f"   - {it['title'][:60]}  {it['url']}")
    elif args:
        run(only_source_id=args[0])
    else:
        run()
