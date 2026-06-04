#!/usr/bin/env python3
"""
笔记台桥接服务 — Python stdlib only, 无需 pip
Port: 8770

复用底座（原样取自 文案面板/server.py）：
  POST /claude       — 调用 Claude（优先 CLI 认证，备用 API Key）
  POST /file         — 文件系统操作 (read / write / list)
  GET  /pick-folder  — 原生 macOS 文件夹选择器
  GET  /pick-file    — 原生 macOS 文件选择器
  GET  /info         — 服务信息（是否 CLI 模式）

笔记台新增：
  POST /notebook     — 笔记本元数据 (list / create / get / save / delete)
  POST /collect      — 调编辑部 流水线.py collect 采集来源入库
  POST /publish      — 调编辑部 流水线.py upload 排版+自检+落微信草稿
  POST /crawl-config — 保存笔记本监控站点（Phase 2 launchd 调度的数据底座）
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

PORT = 8770
CLAUDE_BIN = '/Users/edy/.local/bin/claude'
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"

CLI_AVAILABLE = os.path.exists(CLAUDE_BIN)

# ── 路径常量 ──
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
NOTEBOOKS_DIR = os.path.join(APP_ROOT, "notebooks")
EDITORIAL_ROOT = "/Users/edy/Desktop/自动化文案发布全流程"
EDITORIAL_PKG = os.path.join(EDITORIAL_ROOT, "编辑部")
EDITORIAL_PY = os.path.join(EDITORIAL_ROOT, ".venv", "bin", "python")
PIPELINE = os.path.join(EDITORIAL_PKG, "流水线.py")

os.makedirs(NOTEBOOKS_DIR, exist_ok=True)

# ── 文件系统工具（供 API 模式 tool_use 使用）──
FS_TOOLS = [
    {
        "name": "read_file",
        "description": "读取文件内容，返回文本",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "写入文件（自动创建中间目录）",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "list_directory",
        "description": "列出目录中的文件和子目录",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"]
        }
    }
]


def fs_execute(name, inp):
    try:
        if name == "read_file":
            path = inp["path"]
            if path.lower().endswith(('.pdf', '.docx', '.doc', '.pptx', '.xlsx', '.xls')):
                return {"ok": True, "content": f"[跳过二进制文件：{os.path.basename(path)}，请将内容转为 .txt 或 .md 格式]"}
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return {"ok": True, "content": f.read()}
        elif name == "write_file":
            p = inp["path"]
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(inp["content"])
            return {"ok": True, "message": f"已写入: {p}"}
        elif name == "list_directory":
            entries = []
            for item in sorted(os.listdir(inp["path"])):
                fp = os.path.join(inp["path"], item)
                entries.append({"name": item, "type": "dir" if os.path.isdir(fp) else "file", "path": fp})
            return {"ok": True, "entries": entries}
        else:
            return {"ok": False, "error": f"未知工具: {name}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def call_via_cli(system_prompt, user_msg, model=DEFAULT_MODEL):
    """使用本地 claude CLI 调用，复用 Claude Code 的 OAuth 认证，无需 API Key"""
    full_prompt = f"{system_prompt}\n\n---\n\n{user_msg}"
    cmd = [CLAUDE_BIN, '-p', full_prompt, '--output-format', 'text', '--model', model]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        err = result.stderr.strip() or f'CLI exit code {result.returncode}'
        raise Exception(f"Claude CLI 错误: {err}")
    return result.stdout.strip()


def call_via_api(api_key, system_prompt, messages, model=DEFAULT_MODEL):
    """直接调用 Anthropic API，处理 tool_use 循环（API Key 模式）"""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json"
    }
    msgs = list(messages)

    while True:
        body = {
            "model": model,
            "max_tokens": 8192,
            "system": system_prompt,
            "messages": msgs,
            "tools": FS_TOOLS
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(CLAUDE_API_URL, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                response = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise Exception(f"API {e.code}: {e.read().decode('utf-8')}")

        stop_reason = response.get("stop_reason", "")
        content = response.get("content", [])

        if stop_reason == "tool_use":
            msgs.append({"role": "assistant", "content": content})
            tool_results = []
            for block in content:
                if block.get("type") == "tool_use":
                    result = fs_execute(block["name"], block.get("input", {}))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": json.dumps(result, ensure_ascii=False)
                    })
            msgs.append({"role": "user", "content": tool_results})
        else:
            return "\n".join(b["text"] for b in content if b.get("type") == "text")


def pick_folder_native():
    script = 'POSIX path of (choose folder with prompt "选择素材文件夹")'
    r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
    if r.returncode == 0:
        return r.stdout.strip().rstrip('/')
    return None


def pick_file_native():
    script = 'POSIX path of (choose file with prompt "选择参考资料文件")'
    r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
    if r.returncode == 0:
        return r.stdout.strip()
    return None


# ──────────────────────────────────────────────────────────
# 笔记台业务逻辑
# ──────────────────────────────────────────────────────────

def _clean_traceback(stderr):
    """从 Python traceback 抽出末尾异常信息（含多行 RuntimeError 详情），去掉栈帧。"""
    if not stderr:
        return ""
    lines = stderr.rstrip().splitlines()
    # 找最后一个形如 'XxxError: ...' / 'Exception: ...' 的行，取它及之后所有行
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^\w*(Error|Exception|RuntimeError):", ln):
            start = i
    if start is None:
        return lines[-1] if lines else ""
    msg = "\n".join(lines[start:])
    return re.sub(r"^\w*(Error|Exception|RuntimeError):\s*", "", msg).strip()


def _new_id(prefix):
    """单调递增 id：前缀 + 毫秒时间戳 + os.urandom 后缀，避免同毫秒碰撞。"""
    return f"{prefix}_{int(time.time() * 1000)}{os.urandom(2).hex()}"


def _nb_dir(nb_id):
    return os.path.join(NOTEBOOKS_DIR, nb_id)


def _meta_path(nb_id):
    return os.path.join(_nb_dir(nb_id), "meta.json")


def _read_meta(nb_id):
    with open(_meta_path(nb_id), "r", encoding="utf-8") as f:
        return json.load(f)


def _write_meta(nb_id, meta):
    os.makedirs(_nb_dir(nb_id), exist_ok=True)
    with open(_meta_path(nb_id), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def notebook_list():
    items = []
    if not os.path.isdir(NOTEBOOKS_DIR):
        return items
    for name in os.listdir(NOTEBOOKS_DIR):
        mp = _meta_path(name)
        if os.path.isfile(mp):
            try:
                with open(mp, "r", encoding="utf-8") as f:
                    m = json.load(f)
                items.append({
                    "id": m.get("id", name),
                    "title": m.get("title", "未命名笔记本"),
                    "created": m.get("created", ""),
                    "updated": m.get("updated", m.get("created", "")),
                    "sourceCount": len(m.get("sources", [])),
                    "hasDraft": bool(m.get("draft", {}).get("media_id")),
                })
            except Exception:
                continue
    items.sort(key=lambda x: x.get("updated", ""), reverse=True)
    return items


def notebook_create(title):
    nb_id = _new_id("nb")
    today = time.strftime("%Y-%m-%d")
    meta = {
        "id": nb_id,
        "title": title or "未命名笔记本",
        "created": today,
        "updated": today,
        "sources": [],
        "monitors": [],
        "draft": {},
    }
    d = _nb_dir(nb_id)
    for sub in ("sources", "publish/raw/images", "publish/final/images"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    _write_meta(nb_id, meta)
    return meta


def collect_source(nb_id, source):
    """调编辑部 流水线.py collect 采集一个来源，落 sources/{id}.json 并回写 meta。"""
    meta = _read_meta(nb_id)
    publish_dir = os.path.join(_nb_dir(nb_id), "publish")
    for sub in ("raw/images", "final/images"):
        os.makedirs(os.path.join(publish_dir, sub), exist_ok=True)

    cmd = [EDITORIAL_PY, PIPELINE, "collect", source, "--run-dir", publish_dir]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=EDITORIAL_PKG, timeout=600)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if "No module named 'playwright'" in err:
            raise Exception("该页面是动态渲染（静态正文过少），需要 playwright 兜底，但编辑部 venv 未安装。"
                            "请改用「复制文本」粘贴正文，或在编辑部 venv 安装 playwright。")
        raise Exception(f"采集失败: {err.splitlines()[-1] if err else '未知错误'}")

    raw_path = os.path.join(publish_dir, "raw", "raw.json")
    with open(raw_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    src_id = _new_id("src")
    src = {
        "id": src_id,
        "type": "url" if source.startswith(("http://", "https://")) else "file",
        "title": raw.get("title", source),
        "text": raw.get("text", ""),
        "images": raw.get("images", []),
        "source_url": raw.get("source_url", source if source.startswith("http") else ""),
        "addedAt": time.strftime("%Y-%m-%d %H:%M"),
    }
    with open(os.path.join(_nb_dir(nb_id), "sources", f"{src_id}.json"), "w", encoding="utf-8") as f:
        json.dump(src, f, ensure_ascii=False, indent=2)

    meta.setdefault("sources", []).append({
        "id": src_id, "type": src["type"], "title": src["title"],
        "source_url": src["source_url"], "addedAt": src["addedAt"],
        "chars": len(src["text"]), "imageCount": len(src["images"]),
    })
    meta["updated"] = time.strftime("%Y-%m-%d")
    _write_meta(nb_id, meta)
    return {"id": src_id, "title": src["title"], "chars": len(src["text"]),
            "imageCount": len(src["images"]), "source_url": src["source_url"]}


def save_paste_source(nb_id, title, text):
    """粘贴文本直接存为来源（不走采集）。"""
    meta = _read_meta(nb_id)
    src_id = _new_id("src")
    src = {
        "id": src_id, "type": "paste", "title": title or "粘贴文本",
        "text": text, "images": [], "source_url": "",
        "addedAt": time.strftime("%Y-%m-%d %H:%M"),
    }
    os.makedirs(os.path.join(_nb_dir(nb_id), "sources"), exist_ok=True)
    with open(os.path.join(_nb_dir(nb_id), "sources", f"{src_id}.json"), "w", encoding="utf-8") as f:
        json.dump(src, f, ensure_ascii=False, indent=2)
    meta.setdefault("sources", []).append({
        "id": src_id, "type": "paste", "title": src["title"],
        "source_url": "", "addedAt": src["addedAt"],
        "chars": len(text), "imageCount": 0,
    })
    meta["updated"] = time.strftime("%Y-%m-%d")
    _write_meta(nb_id, meta)
    return {"id": src_id, "title": src["title"], "chars": len(text)}


def load_sources_text(nb_id):
    """合并所有来源正文（带来源标注），供前端写作链/聊天注入。"""
    meta = _read_meta(nb_id)
    parts = []
    for s in meta.get("sources", []):
        sp = os.path.join(_nb_dir(nb_id), "sources", f"{s['id']}.json")
        if not os.path.isfile(sp):
            continue
        with open(sp, "r", encoding="utf-8") as f:
            full = json.load(f)
        parts.append(f"【来源：{full.get('title','-')}】\n{full.get('text','')}")
    return "\n\n".join(parts)


def first_source_image(nb_id):
    """取第一张来源图片本地路径，作封面候选；无则 None。"""
    meta = _read_meta(nb_id)
    for s in meta.get("sources", []):
        sp = os.path.join(_nb_dir(nb_id), "sources", f"{s['id']}.json")
        if not os.path.isfile(sp):
            continue
        with open(sp, "r", encoding="utf-8") as f:
            full = json.load(f)
        for img in full.get("images", []):
            p = img.get("path")
            if p and os.path.isfile(p):
                return p
    return None


def publish_notebook(nb_id, md_rel, title, cover=None, source_url=""):
    """调编辑部 流水线.py upload：排版+强制自检+上传微信草稿。
    md_rel 相对 publish/ 目录（前端写作链已把 article.md 落 publish/final/）。"""
    meta = _read_meta(nb_id)
    publish_dir = os.path.join(_nb_dir(nb_id), "publish")
    md_path = os.path.join(publish_dir, md_rel)
    if not os.path.isfile(md_path):
        raise Exception(f"找不到正文文件: {md_path}")

    if cover is None:
        cover = first_source_image(nb_id)
    if not source_url:
        srcs = meta.get("sources", [])
        source_url = next((s.get("source_url", "") for s in srcs if s.get("source_url")), "")

    cmd = [EDITORIAL_PY, PIPELINE, "upload", md_path, title, "--run-dir", publish_dir]
    if cover:
        cmd += ["--cover", cover]
    if source_url:
        cmd += ["--source-url", source_url]

    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=EDITORIAL_PKG, timeout=600)
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        # 从 traceback 末尾抽出有意义的异常信息（自检红线 / 微信 API 报错），去掉冗长栈
        raise Exception(_clean_traceback(proc.stderr) or out.strip())

    meta_draft_path = os.path.join(publish_dir, "final", "draft_meta.json")
    media_id = ""
    if os.path.isfile(meta_draft_path):
        with open(meta_draft_path, "r", encoding="utf-8") as f:
            media_id = json.load(f).get("media_id", "")
    meta["draft"] = {"media_id": media_id, "title": title}
    meta["updated"] = time.strftime("%Y-%m-%d")
    _write_meta(nb_id, meta)
    return {"media_id": media_id, "log": out.strip()}


# ──────────────────────────────────────────────────────────
# 收集模块桥接（功能一）—— DB/爬虫在 收集/ 子包，惰性导入
# ──────────────────────────────────────────────────────────
COLLECT_DIR = os.path.join(APP_ROOT, "收集")
if COLLECT_DIR not in sys.path:
    sys.path.insert(0, COLLECT_DIR)


def _cdb():
    """惰性导入收集模块 db。"""
    import db as cdb  # 收集/db.py
    return cdb


def fulltext_fetch(url, dest_path):
    """惰性全文抓取：打星时触发，复用编辑部 采集.py（readability + playwright 兜底）。"""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="ft_")
    try:
        cmd = [EDITORIAL_PY, os.path.join(EDITORIAL_PKG, "采集.py"), url, tmp]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=EDITORIAL_PKG, timeout=180)
        raw_path = os.path.join(tmp, "raw.json")
        if proc.returncode != 0 or not os.path.isfile(raw_path):
            err = _clean_traceback(proc.stderr) or (proc.stdout or "").strip()
            raise Exception(err or "全文抓取失败")
        with open(raw_path, encoding="utf-8") as f:
            raw = json.load(f)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "w", encoding="utf-8") as f:
            json.dump({"title": raw.get("title", ""), "text": raw.get("text", ""),
                       "images": raw.get("images", []), "source_url": url},
                      f, ensure_ascii=False, indent=2)
        return {"title": raw.get("title", ""), "chars": len(raw.get("text", ""))}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ──────────────────────────────────────────────────────────
# 监控源接入助手（agent）：试抓失败的源 → Claude 分析页面产出抓取配方
# 任务态在内存（单机单用户），前端小窗轮询日志 + 可递线索续跑
# ──────────────────────────────────────────────────────────
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
    import crawler
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
        cdb = _cdb()
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
    import crawler
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
        _alog(tid, "agent", f"第 {n} 次：请 Claude 分析产出配方…")
        try:
            text = call_via_cli(RECIPE_SPEC, user_msg)
        except Exception as e:
            t["history"].append(f"第{n}次：调用 Claude 失败 {e}")
            _alog(tid, "agent", f"调用 Claude 失败：{e}")
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
            cdb = _cdb()
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


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self.cors()
        self.end_headers()

    def do_GET(self):
        if self.path == '/info':
            self.reply(200, {"ok": True, "cli_mode": CLI_AVAILABLE, "cli_path": CLAUDE_BIN, "app": "笔记台"})
        elif self.path == '/pick-folder':
            path = pick_folder_native()
            self.reply(200, {"ok": True, "path": path} if path else {"ok": False, "error": "未选择文件夹"})
        elif self.path == '/pick-file':
            path = pick_file_native()
            self.reply(200, {"ok": True, "path": path} if path else {"ok": False, "error": "未选择文件"})
        else:
            self.reply(404, {"error": "未知路由"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self.reply(400, {"error": "无效 JSON"})

        route = {
            "/claude": self.handle_claude,
            "/file": self.handle_file,
            "/notebook": self.handle_notebook,
            "/collect": self.handle_collect,
            "/publish": self.handle_publish,
            "/crawl-config": self.handle_crawl_config,
            "/mon-source": self.handle_mon_source,
            "/mon-run": self.handle_mon_run,
            "/feed": self.handle_feed,
            "/agent-crack": self.handle_agent_crack,
        }.get(self.path)
        if route:
            route(payload)
        else:
            self.reply(404, {"error": "未知路由"})

    def handle_claude(self, p):
        system  = p.get("system", "你是一个文案助手。")
        msgs    = p.get("messages", [])
        api_key = p.get("api_key", "").strip()
        model   = p.get("model", DEFAULT_MODEL)

        if not msgs:
            return self.reply(400, {"error": "缺少 messages"})

        user_msg = ""
        for m in msgs:
            if m.get("role") == "user":
                c = m.get("content", "")
                user_msg = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                break

        try:
            if CLI_AVAILABLE and not api_key:
                text = call_via_cli(system, user_msg, model)
            elif api_key:
                text = call_via_api(api_key, system, msgs, model)
            else:
                return self.reply(400, {"error": "未找到 claude CLI，且未提供 API Key"})
            self.reply(200, {"ok": True, "text": text})
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def handle_file(self, p):
        action = p.get("action")
        path   = p.get("path", "")
        if action == "read":
            self.reply(200, fs_execute("read_file", {"path": path}))
        elif action == "write":
            self.reply(200, fs_execute("write_file", {"path": path, "content": p.get("content", "")}))
        elif action == "list":
            self.reply(200, fs_execute("list_directory", {"path": path}))
        else:
            self.reply(400, {"error": f"未知 action: {action}"})

    def handle_notebook(self, p):
        action = p.get("action")
        try:
            if action == "list":
                self.reply(200, {"ok": True, "notebooks": notebook_list()})
            elif action == "create":
                self.reply(200, {"ok": True, "notebook": notebook_create(p.get("title", ""))})
            elif action == "get":
                self.reply(200, {"ok": True, "notebook": _read_meta(p["id"])})
            elif action == "save":
                _write_meta(p["id"], p["meta"])
                self.reply(200, {"ok": True})
            elif action == "delete":
                import shutil
                d = _nb_dir(p["id"])
                if os.path.isdir(d):
                    shutil.rmtree(d)
                self.reply(200, {"ok": True})
            else:
                self.reply(400, {"error": f"未知 action: {action}"})
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def handle_collect(self, p):
        try:
            nb_id  = p["nbId"]
            if p.get("text") is not None:
                res = save_paste_source(nb_id, p.get("title", ""), p["text"])
            else:
                res = collect_source(nb_id, p["source"])
            self.reply(200, {"ok": True, "source": res})
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def handle_publish(self, p):
        try:
            res = publish_notebook(
                p["nbId"], p.get("md", "final/article.md"), p["title"],
                cover=p.get("cover"), source_url=p.get("source_url", ""))
            self.reply(200, {"ok": True, **res})
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def handle_crawl_config(self, p):
        """Phase 2 数据底座：保存监控站点到 meta.monitors[]。调度器后续接入。"""
        try:
            nb_id = p["nbId"]
            meta = _read_meta(nb_id)
            meta["monitors"] = p.get("monitors", [])
            meta["updated"] = time.strftime("%Y-%m-%d")
            _write_meta(nb_id, meta)
            self.reply(200, {"ok": True, "monitors": meta["monitors"],
                             "note": "已保存监控站点；定时调度将在 Phase 2 开启"})
        except Exception as e:
            self.reply(500, {"error": str(e)})

    # ── 收集模块（功能一）端点 ──────────────────────────────────
    def handle_mon_source(self, p):
        """监控源 CRUD；add 先试抓校验（结构化源优先，HTML 列表主路）。"""
        try:
            cdb = _cdb()
            import crawler  # 收集/crawler.py
            action = p.get("action")
            conn = cdb.connect()
            cdb.init_db(conn)
            try:
                if action == "list":
                    self.reply(200, {"ok": True, "sources": cdb.source_list(conn),
                                     "stats": cdb.stats(conn)})
                elif action == "add":
                    url = (p.get("url") or "").strip()
                    if not url:
                        return self.reply(400, {"error": "缺少 url"})
                    try:
                        kind, items = crawler.discover(url)
                    except Exception as e:
                        return self.reply(200, {"ok": False, "agent_available": CLI_AVAILABLE,
                                                "error": f"试抓失败：{e}"})
                    if not items:
                        return self.reply(200, {"ok": False, "agent_available": CLI_AVAILABLE,
                            "error": "该页未解析出文章链接（换个栏目列表页，或详情页日期规律更清晰的源）"})
                    src = cdb.source_add(conn, url, p.get("title") or url, kind,
                                         p.get("freq", "daily"), p.get("grp", ""),
                                         p.get("run_times", "08:00"),
                                         p.get("backfill_days", 7))
                    self.reply(200, {"ok": True, "source": src,
                                     "preview": items[:5], "count": len(items)})
                elif action == "delete":
                    cdb.source_delete(conn, p["id"])
                    self.reply(200, {"ok": True})
                elif action == "toggle":
                    cdb.source_toggle(conn, p["id"], p.get("enabled", True))
                    self.reply(200, {"ok": True})
                else:
                    self.reply(400, {"error": f"未知 action: {action}"})
            finally:
                conn.close()
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def handle_mon_run(self, p):
        """手动触发抓取 = 对所有启用源强制「首抓」：无视冷却、按各源回溯天数重拉，
        清理过的窗口内条目也会找回（定时调度仍走增量，不受影响）。"""
        try:
            import crawler
            crawler.run(only_source_id=p.get("sourceId"), force=p.get("force", True))
            cdb = _cdb()
            conn = cdb.connect()
            st = cdb.stats(conn)
            conn.close()
            self.reply(200, {"ok": True, "stats": st})
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def handle_agent_crack(self, p):
        """接入助手任务：start 起线程分析 / status 轮询日志 / say 递线索续跑。"""
        try:
            action = p.get("action")
            if action == "start":
                if not CLI_AVAILABLE:
                    return self.reply(200, {"ok": False, "error": "本机无 claude CLI，接入助手不可用"})
                url = (p.get("url") or "").strip()
                if not url:
                    return self.reply(400, {"error": "缺少 url"})
                tid = f"ac_{int(time.time()*1000)}"
                AGENT_TASKS[tid] = {
                    "id": tid, "url": url, "state": "running",
                    "log": [], "hints": [], "history": [], "html": "",
                    "params": {k: p.get(k) for k in ("title", "grp", "freq", "run_times", "backfill_days")},
                    "source": None, "preview": [],
                }
                _alog(tid, "sys", f"任务开始：{url}")
                if not p.get("external"):  # external=外部工作者（如终端 Claude）接管，不起内置线程
                    threading.Thread(target=_agent_attempts, args=(tid,), daemon=True).start()
                self.reply(200, {"ok": True, "id": tid})
            elif action == "log":
                # 外部工作者实时回报：往任务日志追加一行，可顺带改状态/挂结果
                t = AGENT_TASKS.get(p.get("id"))
                if not t:
                    return self.reply(404, {"error": "任务不存在"})
                _alog(t["id"], p.get("who", "agent"), p.get("text", ""))
                if p.get("state") in ("running", "waiting", "success"):
                    t["state"] = p["state"]
                if p.get("source"):
                    t["source"] = p["source"]
                self.reply(200, {"ok": True})
            elif action == "latest":
                # 页面启动时自动接上最近的活跃任务（服务重启/刷新后续命）
                live = [t for t in AGENT_TASKS.values() if t["state"] in ("running", "waiting")]
                live.sort(key=lambda t: t["id"], reverse=True)
                if live:
                    self.reply(200, {"ok": True, "id": live[0]["id"], "state": live[0]["state"]})
                else:
                    self.reply(200, {"ok": True, "id": None})
            elif action == "status":
                t = AGENT_TASKS.get(p.get("id"))
                if not t:
                    return self.reply(404, {"error": "任务不存在（服务可能重启过）"})
                self.reply(200, {"ok": True, "state": t["state"], "log": t["log"],
                                 "source": t.get("source"), "preview": t.get("preview", [])})
            elif action == "say":
                t = AGENT_TASKS.get(p.get("id"))
                if not t:
                    return self.reply(404, {"error": "任务不存在（服务可能重启过）"})
                text = (p.get("text") or "").strip()
                if not text:
                    return self.reply(400, {"error": "空消息"})
                _alog(t["id"], "user", text)
                t["hints"].append(text)
                if t["state"] in ("waiting",):
                    t["state"] = "running"
                    threading.Thread(target=_agent_attempts, args=(t["id"],), daemon=True).start()
                self.reply(200, {"ok": True})
            else:
                self.reply(400, {"error": f"未知 action: {action}"})
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def handle_feed(self, p):
        """feed 读取 / 打星(触发或删全文) / GC。"""
        try:
            cdb = _cdb()
            action = p.get("action")
            conn = cdb.connect()
            cdb.init_db(conn)
            try:
                if action == "list":
                    self.reply(200, {"ok": True,
                        "items": cdb.feed_list(conn, only_starred=p.get("starred", False),
                                               source_id=p.get("sourceId")),
                        "stats": cdb.stats(conn)})
                elif action == "star":
                    fid = p["id"]
                    star = int(p.get("star", 0))
                    f = cdb.feed_get(conn, fid)
                    if not f:
                        return self.reply(404, {"error": "feed 不存在"})
                    cdb.feed_set_star(conn, fid, star)
                    if star > 0 and not f["fulltext"]:
                        # 打星 → 惰性抓全文
                        try:
                            info = fulltext_fetch(f["url"], cdb.fulltext_path(fid))
                            cdb.feed_set_fulltext(conn, fid, True)
                            self.reply(200, {"ok": True, "fulltext": True, **info})
                        except Exception as e:
                            self.reply(200, {"ok": True, "fulltext": False,
                                             "warn": f"全文抓取失败：{e}"})
                    elif star == 0:
                        # 取消星 → 删全文；窗口外旧文直接从资讯流移除（不再赖在 feed 里）
                        if f["fulltext"]:
                            cdb.fulltext_delete(fid)
                            cdb.feed_set_fulltext(conn, fid, False)
                        cutoff = time.strftime(
                            "%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
                        removed = False
                        if f.get("pub_date") and f["pub_date"] < cutoff:
                            conn.execute("DELETE FROM feed WHERE id=?", (fid,))
                            conn.commit()
                            removed = True
                        self.reply(200, {"ok": True, "fulltext": False, "removed": removed})
                    else:
                        self.reply(200, {"ok": True})
                elif action == "read":
                    # 批量标已读（前端入屏≥3s / 点开链接时上报）
                    cdb.feed_mark_read(conn, p.get("ids", []))
                    self.reply(200, {"ok": True})
                elif action == "fulltext":
                    # 读取已落盘的全文（总结页阅读用）
                    fp = cdb.fulltext_path(p["id"])
                    if not os.path.isfile(fp):
                        return self.reply(404, {"error": "全文未保存或已删除"})
                    with open(fp, encoding="utf-8") as f:
                        self.reply(200, {"ok": True, "doc": json.load(f)})
                elif action == "gc":
                    # 手动清理：默认 0 = 立即清所有无星
                    n = cdb.gc(conn, int(p.get("hours", 0)))
                    self.reply(200, {"ok": True, "removed": n})
                else:
                    self.reply(400, {"error": f"未知 action: {action}"})
            finally:
                conn.close()
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def reply(self, status, data):
        try:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass


class ThreadedServer(ThreadingMixIn, HTTPServer):
    """多线程服务器：Claude 长请求期间仍可处理文件写入等其他请求"""
    daemon_threads = True
    allow_reuse_address = True


def _collect_scheduler():
    """收集模块后台调度（server 在跑时资讯自动流入，无需手点）：
    - 每 1 分钟检查到期监控源并增量抓取（crawler 自带锁，与 launchd/手动触发互斥）
    - 每晚 2:00 清理「已读且无星」（一天一次，未读保留）"""
    import threading  # noqa: F401  （文档提示：本函数运行于 daemon 线程）
    while True:
        try:
            import crawler
            crawler.run()
        except Exception as e:
            print(f"[收集调度] 抓取异常：{e}")
        try:
            cdb = _cdb()
            conn = cdb.connect()
            cdb.init_db(conn)
            today = time.strftime("%Y-%m-%d")
            if time.localtime().tm_hour == 2 and cdb.meta_get(conn, "last_read_clean") != today:
                n = cdb.gc_read(conn)
                cdb.meta_set(conn, "last_read_clean", today)
                print(f"[收集调度] 2:00 已读清理：{n} 条")
            conn.close()
        except Exception as e:
            print(f"[收集调度] 清理异常：{e}")
        time.sleep(60)


if __name__ == "__main__":
    import threading
    mode = f"CLI 模式（{CLAUDE_BIN}）" if CLI_AVAILABLE else "API Key 模式"
    print(f"✓ 笔记台服务已启动 → http://127.0.0.1:{PORT}")
    print(f"  认证方式：{mode}")
    print(f"  笔记本目录：{NOTEBOOKS_DIR}")
    print("  收集调度：每 5 分钟检查到期源 · 每晚 2:00 清理已读无星")
    print("  按 Ctrl+C 停止")
    threading.Thread(target=_collect_scheduler, daemon=True).start()
    ThreadedServer(("127.0.0.1", PORT), Handler).serve_forever()
