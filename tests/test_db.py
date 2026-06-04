"""多租户隔离断言 — app/core/db.py。

覆盖范围：
  - source 用户隔离（list / 重复 url 约束 / delete 不越权）
  - feed 用户隔离（list / get / gc / gc_read / stats）
  - init_db 幂等 + 旧版库迁移（无 user_id 列 / meta 单主键）
"""
import os
import sqlite3
import time

import pytest

from app.core import db


# ── 辅助：快速建一条 source ─────────────────────────────────────
def _add_src(conn, user_id, url="http://example.com/feed"):
    return db.source_add(
        conn, user_id=user_id, url=url, title="测试源",
        kind="rss", freq="daily",
    )


def _add_feed(conn, user_id, sid, url="http://example.com/1"):
    fid = db.feed_add(conn, user_id=user_id, sid=sid, title="测试文章", url=url)
    conn.commit()
    return fid


# ── source 多租户隔离 ────────────────────────────────────────────

class TestSourceIsolation:
    def test_list_隔离(self, conn):
        """A 加的源在 A 的列表里，B 的列表为空。"""
        _add_src(conn, "u_a")
        assert len(db.source_list(conn, "u_a")) == 1
        assert db.source_list(conn, "u_b") == []

    def test_同url_不同用户_可共存(self, conn):
        """UNIQUE(user_id,url)：不同用户监控同一 URL 不报错。"""
        _add_src(conn, "u_a", url="http://same.com/rss")
        _add_src(conn, "u_b", url="http://same.com/rss")  # 不应抛异常
        assert len(db.source_list(conn, "u_a")) == 1
        assert len(db.source_list(conn, "u_b")) == 1

    def test_同用户_重复url_报_IntegrityError(self, conn):
        """同一用户不能重复添加同一 URL。"""
        _add_src(conn, "u_a", url="http://dup.com/rss")
        with pytest.raises(sqlite3.IntegrityError):
            _add_src(conn, "u_a", url="http://dup.com/rss")

    def test_source_get_归属校验(self, conn):
        """source_get(sid, user_id=B) 对 A 的源返 None（防越权）。"""
        src = _add_src(conn, "u_a")
        assert db.source_get(conn, src["id"], user_id="u_a") is not None
        assert db.source_get(conn, src["id"], user_id="u_b") is None

    def test_delete_不越权(self, conn):
        """B 删 A 的源：A 的数据不受影响。"""
        src = _add_src(conn, "u_a")
        db.source_delete(conn, src["id"], user_id="u_b")  # 无权限，应静默无效
        # A 的源仍在
        assert db.source_get(conn, src["id"]) is not None
        assert len(db.source_list(conn, "u_a")) == 1


# ── feed 多租户隔离 ──────────────────────────────────────────────

class TestFeedIsolation:
    def test_list_各归各(self, conn):
        """A 的 feed 不出现在 B 的列表里。"""
        src_a = _add_src(conn, "u_a", url="http://a.com/rss")
        src_b = _add_src(conn, "u_b", url="http://b.com/rss")
        _add_feed(conn, "u_a", src_a["id"], url="http://a.com/1")
        _add_feed(conn, "u_b", src_b["id"], url="http://b.com/1")

        list_a = db.feed_list(conn, "u_a")
        list_b = db.feed_list(conn, "u_b")
        assert len(list_a) == 1
        assert len(list_b) == 1
        assert list_a[0]["url"] == "http://a.com/1"
        assert list_b[0]["url"] == "http://b.com/1"

    def test_feed_get_归属校验(self, conn):
        """feed_get(fid, user_id=B) 对 A 的 feed 返 None。"""
        src_a = _add_src(conn, "u_a")
        fid = _add_feed(conn, "u_a", src_a["id"])
        assert db.feed_get(conn, fid, user_id="u_a") is not None
        assert db.feed_get(conn, fid, user_id="u_b") is None

    def test_feed_set_star_不越权(self, conn):
        """feed_set_star 带错的 user_id（B 改 A 的 feed）不生效（AND user_id=? 防线）。"""
        src_a = _add_src(conn, "u_a")
        fid = _add_feed(conn, "u_a", src_a["id"])
        db.feed_set_star(conn, fid, 3, "u_b")  # B 越权改 A 的 → 无效
        assert db.feed_get(conn, fid)["star"] == 0
        db.feed_set_star(conn, fid, 3, "u_a")  # 本人改 → 生效
        assert db.feed_get(conn, fid)["star"] == 3

    def test_feed_set_fulltext_不越权(self, conn):
        """feed_set_fulltext 带错的 user_id 不生效。"""
        src_a = _add_src(conn, "u_a")
        fid = _add_feed(conn, "u_a", src_a["id"])
        db.feed_set_fulltext(conn, fid, True, "u_b")  # 越权
        assert db.feed_get(conn, fid)["fulltext"] == 0
        db.feed_set_fulltext(conn, fid, True, "u_a")  # 本人
        assert db.feed_get(conn, fid)["fulltext"] == 1

    def test_feed_pin_不越权(self, conn):
        """feed_pin 带错的 user_id 不生效。"""
        src_a = _add_src(conn, "u_a")
        fid = _add_feed(conn, "u_a", src_a["id"])
        db.feed_pin(conn, fid, "wf1", "u_b")  # 越权
        assert db.feed_get(conn, fid)["pinned_to"] == ""
        db.feed_pin(conn, fid, "wf1", "u_a")  # 本人
        assert db.feed_get(conn, fid)["pinned_to"] == "wf1"

    def test_stats_按用户统计(self, conn):
        """stats 只统计目标用户的数据，不混入他人。"""
        src_a = _add_src(conn, "u_a", url="http://a.com/rss")
        src_b = _add_src(conn, "u_b", url="http://b.com/rss")
        _add_feed(conn, "u_a", src_a["id"], url="http://a.com/1")
        _add_feed(conn, "u_a", src_a["id"], url="http://a.com/2")
        _add_feed(conn, "u_b", src_b["id"], url="http://b.com/1")

        st_a = db.stats(conn, "u_a")
        st_b = db.stats(conn, "u_b")
        assert st_a["feed_total"] == 2
        assert st_b["feed_total"] == 1
        assert st_a["sources"] == 1
        assert st_b["sources"] == 1


# ── gc 多租户隔离 ────────────────────────────────────────────────

class TestGC:
    def test_gc_只清目标用户(self, conn):
        """gc(user_id=A) 清 A 的无星 feed，B 的不受影响。"""
        src_a = _add_src(conn, "u_a", url="http://a.com/rss")
        src_b = _add_src(conn, "u_b", url="http://b.com/rss")
        _add_feed(conn, "u_a", src_a["id"], url="http://a.com/1")  # 无星
        _add_feed(conn, "u_b", src_b["id"], url="http://b.com/1")  # 无星

        deleted = db.gc(conn, user_id="u_a", keep_unstarred_hours=0)
        assert deleted == 1
        assert db.feed_list(conn, "u_a") == []
        assert len(db.feed_list(conn, "u_b")) == 1  # B 的不受影响

    def test_gc_打星的不清(self, conn):
        """gc 不应删除有星的 feed。"""
        src_a = _add_src(conn, "u_a")
        fid = _add_feed(conn, "u_a", src_a["id"], url="http://a.com/1")
        db.feed_set_star(conn, fid, 2, "u_a")  # 打星

        deleted = db.gc(conn, user_id="u_a", keep_unstarred_hours=0)
        assert deleted == 0
        assert db.feed_get(conn, fid) is not None

    def test_gc_read_已读无星清_未读保留(self, conn):
        """gc_read：已读且无星的清掉；未读的保留；打星的保留。"""
        src = _add_src(conn, "u_a")
        fid_read   = _add_feed(conn, "u_a", src["id"], url="http://a.com/1")
        fid_unread = _add_feed(conn, "u_a", src["id"], url="http://a.com/2")
        fid_starred = _add_feed(conn, "u_a", src["id"], url="http://a.com/3")

        # 标记已读 fid_read
        db.feed_mark_read(conn, [fid_read], "u_a")
        # fid_starred 打星（未读）
        db.feed_set_star(conn, fid_starred, 1, "u_a")

        deleted = db.gc_read(conn, user_id="u_a")
        assert deleted == 1  # 只清了 fid_read

        assert db.feed_get(conn, fid_read) is None      # 已读无星 → 已清
        assert db.feed_get(conn, fid_unread) is not None  # 未读 → 保留
        assert db.feed_get(conn, fid_starred) is not None  # 打星 → 保留

    def test_gc_read_只清目标用户(self, conn):
        """gc_read(user_id=A) 清 A 的已读，B 的不受影响。"""
        src_a = _add_src(conn, "u_a", url="http://a.com/rss")
        src_b = _add_src(conn, "u_b", url="http://b.com/rss")
        fid_a = _add_feed(conn, "u_a", src_a["id"], url="http://a.com/1")
        fid_b = _add_feed(conn, "u_b", src_b["id"], url="http://b.com/1")

        db.feed_mark_read(conn, [fid_a, fid_b], "u_a")   # 只给 u_a 权限标已读
        # fid_b 不属于 u_a，mark_read 不会改它
        # 手动直接更新 fid_b 的 read_at
        conn.execute("UPDATE feed SET read_at=? WHERE id=?", (db._now(), fid_b))
        conn.commit()

        deleted = db.gc_read(conn, user_id="u_a")
        assert deleted == 1  # 只清 A 的
        assert db.feed_get(conn, fid_a) is None
        assert db.feed_get(conn, fid_b) is not None  # B 的保留


# ── init_db 幂等 + 旧版迁移 ──────────────────────────────────────

class TestInitDb:
    def test_幂等(self, conn):
        """连续两次 init_db 不报错，表结构不变。"""
        db.init_db(conn)  # 第二遍，不应报错
        # 基本函数仍可用
        src = _add_src(conn, "u_x")
        assert src is not None

    def test_旧版库迁移_补user_id列(self, tmp_path, monkeypatch):
        """旧版库（source/feed 无 user_id 列，meta 单主键）经 init_db 后：
        - user_id 列存在
        - 老行 user_id 填充为 'u_default'
        - meta 改为复合主键
        """
        test_db = str(tmp_path / "old.db")
        test_ft = str(tmp_path / "fulltext_old")
        os.makedirs(test_ft, exist_ok=True)
        monkeypatch.setattr(db, "DB_FILE", test_db)
        monkeypatch.setattr(db, "FULLTEXT_DIR", test_ft)

        # 手工建旧 schema（无 user_id 列，url 表级唯一，meta 单主键）
        old_conn = sqlite3.connect(test_db)
        old_conn.executescript("""
            CREATE TABLE source (
                id    TEXT PRIMARY KEY,
                url   TEXT UNIQUE,
                title TEXT,
                kind  TEXT,
                freq  TEXT DEFAULT 'daily',
                grp   TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                last_run TEXT DEFAULT ''
            );
            CREATE TABLE seen (
                source_id TEXT,
                url       TEXT,
                PRIMARY KEY(source_id, url)
            );
            CREATE TABLE feed (
                id         TEXT PRIMARY KEY,
                source_id  TEXT,
                title      TEXT,
                url        TEXT,
                fetched_at TEXT,
                star       INTEGER DEFAULT 0,
                fulltext   INTEGER DEFAULT 0,
                pinned_to  TEXT DEFAULT ''
            );
            CREATE TABLE meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            -- 插入一条旧数据
            INSERT INTO source(id,url,title,kind) VALUES('old_src','http://old.com','旧源','rss');
            INSERT INTO feed(id,source_id,title,url,fetched_at) VALUES('old_f','old_src','旧文章','http://old.com/1','2024-01-01 00:00:00');
            INSERT INTO meta(key,value) VALUES('last_clean','2024-01-01');
        """)
        old_conn.commit()
        old_conn.close()

        # 运行迁移
        migrated = db.connect()
        db.init_db(migrated)

        # 验证 source 有 user_id 列，旧行已填默认值
        cols_src = [r[1] for r in migrated.execute("PRAGMA table_info(source)").fetchall()]
        assert "user_id" in cols_src
        row = migrated.execute("SELECT user_id FROM source WHERE id='old_src'").fetchone()
        assert row is not None
        assert row[0] == "u_default"

        # 验证 feed 有 user_id 列，旧行已填默认值
        cols_feed = [r[1] for r in migrated.execute("PRAGMA table_info(feed)").fetchall()]
        assert "user_id" in cols_feed
        row = migrated.execute("SELECT user_id FROM feed WHERE id='old_f'").fetchone()
        assert row is not None
        assert row[0] == "u_default"

        # 验证 meta 已改为复合主键（user_id + key），旧数据迁移
        cols_meta = [r[1] for r in migrated.execute("PRAGMA table_info(meta)").fetchall()]
        assert "user_id" in cols_meta
        val = migrated.execute(
            "SELECT value FROM meta WHERE key='last_clean'"
        ).fetchone()
        assert val is not None and val[0] == "2024-01-01"

        migrated.close()
