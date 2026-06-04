#!/usr/bin/env python3
"""通用工具 — 从 server.py 迁入。"""
import re


def _clean_traceback(stderr):
    """从 Python traceback 抽出末尾异常信息（含多行 RuntimeError 详情），去掉栈帧。

    子进程（编辑部采集/排版）失败时，把冗长 Python 栈裁成一句可读中文回传前端。
    """
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
