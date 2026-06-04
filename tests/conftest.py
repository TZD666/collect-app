"""pytest 全局 fixture。

关键约束：db.py 在 import 时就把 settings.DB_PATH 和 settings.FULLTEXT_DIR 读入
模块级常量 DB_FILE / FULLTEXT_DIR，因此必须在 import db 之前（或通过
monkeypatch.setattr 直接替换模块属性）让这两个常量指向临时路径，
确保测试绝不碰真实的 data/collect.db。
"""
import os
import pytest

from app.core import db  # 先 import，再用 monkeypatch patch 模块属性


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """返回指向临时测试库的已初始化连接。

    - DB_FILE  → tmp_path/t.db
    - FULLTEXT_DIR → tmp_path/fulltext/
    测试结束后关闭连接；tmp_path 由 pytest 自动清理。
    """
    test_db = str(tmp_path / "t.db")
    test_ft = str(tmp_path / "fulltext")
    os.makedirs(test_ft, exist_ok=True)

    # 替换模块级常量，让 connect() 和 fulltext_path() 指向临时路径
    monkeypatch.setattr(db, "DB_FILE", test_db)
    monkeypatch.setattr(db, "FULLTEXT_DIR", test_ft)

    c = db.connect()
    db.init_db(c)
    yield c
    c.close()
