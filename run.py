#!/usr/bin/env python3
"""本地入口：读 settings 起 uvicorn。

  python run.py            # 用 .env / 默认配置起服务
  PORT=8771 python run.py  # 临时换端口
"""
import os

import uvicorn

from app.config import settings

# 让调度/启动日志即时刷出，不被块缓冲卡住（后台运行时尤甚）
os.environ.setdefault("PYTHONUNBUFFERED", "1")


def main():
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT)


if __name__ == "__main__":
    main()
