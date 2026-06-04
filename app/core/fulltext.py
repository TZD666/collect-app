#!/usr/bin/env python3
"""全文抓取 — 打星时惰性触发，落盘 {title,text,images,source_url}。

去外部依赖：默认走内置 stdlib 正文提取（urllib + 极简 Readability，纯 re，不引 bs4）；
若配置了 settings.EDITORIAL_ROOT 且本机存在「编辑部/采集.py」，优先调它（playwright 兜底，
质量更好），失败再回落内置。其他电脑上没有编辑部项目也能起步可用。

对外接口：fetch_fulltext(url) -> {"title","text","images","source_url"}
正文 <200 字视为提取失败（多半是 JS 动态渲染页），raise 可读中文异常。
"""
import json
import os
import re
import subprocess
import tempfile
from urllib.parse import urljoin

from app.config import settings
from app.core import crawler
from app.utils import _clean_traceback


# ──────────────────────────────────────────────────────────
# 内置极简 Readability（纯 stdlib re）
# ──────────────────────────────────────────────────────────
_DROP_BLOCK = re.compile(
    r"<(script|style|noscript|nav|header|footer|aside)\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_TITLE_OG = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
# 把页面按块级标签切段，对每个 <p>/<div>/<article>/<section>/<li> 区块计分
_BLOCK = re.compile(
    r"<(p|div|article|section|li)\b[^>]*>(.*?)</\1>",
    re.DOTALL | re.IGNORECASE,
)
_A_INNER = re.compile(r"<a\b[^>]*>(.*?)</a>", re.DOTALL | re.IGNORECASE)
_IMG_SRC = re.compile(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


def _strip_tags(s):
    import html as html_lib
    s = _TAG.sub("", s)
    return html_lib.unescape(s)


def _text_len(block_html):
    """块的「正文密度」分：纯文字长度 − 链接锚文本长度（去掉导航/相关推荐这类满是链接的块）。"""
    text = _strip_tags(block_html)
    text_len = len(text.strip())
    anchor_len = sum(len(_strip_tags(m.group(1)).strip()) for m in _A_INNER.finditer(block_html))
    return text_len - anchor_len


def builtin_extract(url, html):
    """内置正文提取：og:title/<title> 取标题；剥噪音块；按密度选最高分正文块；抽图。"""
    # ── 标题 ──
    title = ""
    m = _TITLE_OG.search(html)
    if m:
        title = _strip_tags(m.group(1)).strip()
    if not title:
        m = _TITLE_TAG.search(html)
        if m:
            title = _strip_tags(m.group(1)).strip()
            # 常见「标题 - 站名」尾巴，取首段
            title = re.split(r"\s*[\|\-—_·]\s*", title)[0].strip() or title

    # ── 剥噪音块 ──
    cleaned = _DROP_BLOCK.sub(" ", html)

    # ── 选最高分正文块 ──
    best_html, best_score = "", 0
    for bm in _BLOCK.finditer(cleaned):
        block_html = bm.group(2)
        score = _text_len(block_html)
        if score > best_score:
            best_score, best_html = score, block_html

    if not best_html:
        best_html = cleaned  # 兜底：整页去噪后当正文

    # ── 正文文本 ──
    raw_text = _strip_tags(best_html)
    raw_text = _WS.sub(" ", raw_text)
    lines = [ln.strip() for ln in raw_text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    text = _NL.sub("\n\n", text).strip()

    # ── 抽该块内图片，补全绝对路径 ──
    images = []
    seen = set()
    for im in _IMG_SRC.finditer(best_html):
        src = im.group(1).strip()
        if not src or src.startswith("data:"):
            continue
        full = urljoin(url, src)
        if full in seen:
            continue
        seen.add(full)
        images.append({"url": full})

    if len(text) < 200:
        raise Exception("正文提取过短（可能是动态渲染页），可点链接浏览器查看原文")

    return {"title": title, "text": text, "images": images, "source_url": url}


# ──────────────────────────────────────────────────────────
# 编辑部增强（本机可选）
# ──────────────────────────────────────────────────────────
def _editorial_available():
    root = settings.EDITORIAL_ROOT
    if not root:
        return None
    cj = os.path.join(root, "编辑部", "采集.py")
    py = os.path.join(root, ".venv", "bin", "python")
    if os.path.isfile(cj) and os.path.isfile(py):
        return (py, cj, os.path.join(root, "编辑部"))
    return None


def _editorial_fetch(url):
    """复用本机编辑部 采集.py（readability + playwright 兜底）。失败 raise。"""
    av = _editorial_available()
    if not av:
        raise Exception("本机无编辑部增强")
    py, collect_py, cwd = av
    tmp = tempfile.mkdtemp(prefix="ft_")
    try:
        cmd = [py, collect_py, url, tmp]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=180)
        raw_path = os.path.join(tmp, "raw.json")
        if proc.returncode != 0 or not os.path.isfile(raw_path):
            err = _clean_traceback(proc.stderr) or (proc.stdout or "").strip()
            raise Exception(err or "编辑部全文抓取失败")
        with open(raw_path, encoding="utf-8") as f:
            raw = json.load(f)
        return {
            "title": raw.get("title", ""),
            "text": raw.get("text", ""),
            "images": raw.get("images", []),
            "source_url": url,
        }
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ──────────────────────────────────────────────────────────
# 对外入口
# ──────────────────────────────────────────────────────────
def fetch_fulltext(url):
    """抓取单 URL 全文，返回 {title,text,images,source_url}。

    优先编辑部增强（若本机配置且存在），失败回落内置；否则直接内置。
    内置正文过短会 raise，由上层（打星分支）转成「全文抓取失败」warn 降级。
    """
    # 编辑部增强（本机可选）
    if _editorial_available():
        try:
            return _editorial_fetch(url)
        except Exception as e:
            print(f"[全文] 编辑部增强失败，回落内置：{e}")

    # 内置抓取
    try:
        html = crawler.fetch(url)
    except Exception as e:
        raise Exception(f"页面抓取失败（可能反爬或网络不通）：{e}")
    return builtin_extract(url, html)
