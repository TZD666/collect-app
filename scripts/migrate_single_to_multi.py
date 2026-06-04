#!/usr/bin/env python3
"""单用户 → 多租户 迁移脚本（幂等）。

做四件事：
  ① 备份 db 文件（copy 加 .bak-时间戳）；
  ② db.init_db()（触发 source/feed 的 user_id 列迁移 + meta 表复合主键重建）；
  ③ ensure_default_user（建 u_default，存量行已默认归它）；
  ④ 打印各表计数确认。

用法：
  python -m scripts.migrate_single_to_multi            # 用 settings.DB_PATH
  python -m scripts.migrate_single_to_multi --db /path/to/collect.db

多次运行安全：列迁移/meta 重建/建用户都幂等，重复跑不会改变结果。
"""
import argparse
import os
import shutil
import sys
import time

# 允许 python scripts/migrate_single_to_multi.py 直跑（补 sys.path）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.core import db  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="收集App 单→多租户迁移")
    ap.add_argument("--db", default=settings.DB_PATH, help="数据库文件路径（默认 settings.DB_PATH）")
    args = ap.parse_args()
    db_path = args.db

    # ① 备份（库文件存在才备份）
    if os.path.isfile(db_path):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bak = f"{db_path}.bak-{stamp}"
        shutil.copy(db_path, bak)
        print(f"① 已备份：{bak}")
    else:
        print(f"① 库文件不存在（将新建）：{db_path}")

    # 让 db 模块连到目标库（settings.DB_PATH 是模块加载时定的，这里直接连指定路径）
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    db.DB_FILE = db_path  # 覆盖模块级常量，使 connect() 连到目标库

    conn = db.connect()
    try:
        # ② 迁移 schema（列 + meta 复合主键）
        db.init_db(conn)
        print("② init_db 完成（user_id 列迁移 + meta 复合主键重建）")

        # ③ 默认用户
        db.ensure_default_user(conn)
        print("③ ensure_default_user 完成（u_default）")

        # ④ 计数确认
        one = lambda q: conn.execute(q).fetchone()[0]
        users_n = one("SELECT COUNT(*) FROM users")
        src_n = one("SELECT COUNT(*) FROM source")
        src_def = one("SELECT COUNT(*) FROM source WHERE user_id='u_default'")
        feed_n = one("SELECT COUNT(*) FROM feed")
        feed_def = one("SELECT COUNT(*) FROM feed WHERE user_id='u_default'")
        seen_n = one("SELECT COUNT(*) FROM seen")
        sess_n = one("SELECT COUNT(*) FROM session")
        print("④ 各表计数：")
        print(f"    users        : {users_n}")
        print(f"    source       : {src_n}（归 u_default：{src_def}）")
        print(f"    feed         : {feed_n}（归 u_default：{feed_def}）")
        print(f"    seen         : {seen_n}")
        print(f"    session      : {sess_n}")
        print("✓ 迁移完成。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
