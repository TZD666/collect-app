#!/usr/bin/env python3
"""收集模块 SQLite 数据层 — stdlib only（sqlite3），不破坏笔记台「无 pip」铁律。

范式取自 claude进化史/db.py：集中管理 schema + 连接 + 查询构造器，
供 server.py 在请求时调用、crawler.py 写入。

三张表：
  source —— 监控源（全局/主体级，跨笔记本共享，仅结构化源 RSS/sitemap）
  seen   —— 每源已见 URL 快照（增量去重）
  feed   —— feed 条目（无星=临时；有星=持久，触发全文落盘）

决策见 功能一-实现目标.md「决策锁定」：
  打星三档 1/2/3 = 使用意图；无星不持久（24h GC 清）；打星即抓全文，取消即删。
"""
import os
import sqlite3
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "data", "collect.db")
FULLTEXT_DIR = os.path.join(BASE_DIR, "fulltext")

SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
    id        TEXT PRIMARY KEY,
    url       TEXT UNIQUE,
    title     TEXT,
    kind      TEXT,              -- rss | sitemap | registry | html
    freq      TEXT DEFAULT 'daily',
    run_times TEXT DEFAULT '08:00',  -- freq=daily 时：每天几点抓，逗号分隔（如 08:00,14:00）
    backfill_days INTEGER DEFAULT 7, -- 首抓回溯天数（1-14，加源时一次性设定）
    recipe    TEXT DEFAULT '',     -- AI 产出的抓取配方（JSON）；非空时按配方抓取
    grp       TEXT DEFAULT '',
    enabled   INTEGER DEFAULT 1,
    last_run  TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS seen (
    source_id TEXT,
    url       TEXT,
    PRIMARY KEY(source_id, url)
);
CREATE TABLE IF NOT EXISTS feed (
    id         TEXT PRIMARY KEY,
    source_id  TEXT,
    title      TEXT,
    url        TEXT,
    fetched_at TEXT,
    pub_date   TEXT DEFAULT '',     -- 报道/发布日期（展示用，URL 或 RSS 解析）
    read_at    TEXT DEFAULT '',     -- 已读时间（入屏≥3s 或点开链接）
    star       INTEGER DEFAULT 0,   -- 0 无星 / 1 / 2 / 3
    fulltext   INTEGER DEFAULT 0,   -- 是否已抓全文落盘
    pinned_to  TEXT DEFAULT ''      -- 挂到某工作流则豁免 GC
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_feed_star    ON feed(star);
CREATE INDEX IF NOT EXISTS idx_feed_fetched ON feed(fetched_at);
CREATE INDEX IF NOT EXISTS idx_feed_source  ON feed(source_id);
"""

# 旧库平滑迁移：缺列就补（幂等）
_MIGRATIONS = (
    "ALTER TABLE source ADD COLUMN run_times TEXT DEFAULT '08:00'",
    "ALTER TABLE source ADD COLUMN backfill_days INTEGER DEFAULT 7",
    "ALTER TABLE source ADD COLUMN recipe TEXT DEFAULT ''",
    "ALTER TABLE feed ADD COLUMN pub_date TEXT DEFAULT ''",
    "ALTER TABLE feed ADD COLUMN read_at TEXT DEFAULT ''",
)


def connect():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL：crawler 写入与 server 读取并发不互相阻塞
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn=None):
    own = conn is None
    conn = conn or connect()
    conn.executescript(SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.commit()
    if own:
        conn.close()


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _new_id(prefix):
    return f"{prefix}_{int(time.time() * 1000)}{os.urandom(2).hex()}"


# ── meta（kv，记录如「上次已读清理日期」等状态）──────────────────
def meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def meta_get(conn, key, default=""):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


# ── 监控源 ──────────────────────────────────────────────────────
def source_add(conn, url, title, kind, freq="daily", grp="", run_times="08:00", backfill_days=7, recipe=""):
    sid = _new_id("mon")
    days = max(1, min(14, int(backfill_days or 7)))  # 上限 14 天
    conn.execute(
        "INSERT INTO source(id,url,title,kind,freq,run_times,backfill_days,recipe,grp,enabled,last_run) "
        "VALUES(?,?,?,?,?,?,?,?,?,1,'')",
        (sid, url, title, kind, freq, run_times or "08:00", days, recipe or "", grp),
    )
    conn.commit()
    return source_get(conn, sid)


def source_get(conn, sid):
    r = conn.execute("SELECT * FROM source WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None


def source_list(conn):
    rows = conn.execute("SELECT * FROM source ORDER BY grp, title").fetchall()
    return [dict(r) for r in rows]


def source_delete(conn, sid):
    conn.execute("DELETE FROM seen WHERE source_id=?", (sid,))
    conn.execute("DELETE FROM source WHERE id=?", (sid,))
    # 留下该源已打星的 feed（用户资产），只删未打星的
    conn.execute("DELETE FROM feed WHERE source_id=? AND star=0", (sid,))
    conn.commit()


def source_toggle(conn, sid, enabled):
    conn.execute("UPDATE source SET enabled=? WHERE id=?", (1 if enabled else 0, sid))
    conn.commit()


def source_touch(conn, sid):
    conn.execute("UPDATE source SET last_run=? WHERE id=?", (_now(), sid))
    conn.commit()


# ── 快照 / 去重 ─────────────────────────────────────────────────
def seen_has(conn, sid, url):
    return conn.execute(
        "SELECT 1 FROM seen WHERE source_id=? AND url=?", (sid, url)
    ).fetchone() is not None


def seen_add(conn, sid, url):
    conn.execute(
        "INSERT OR IGNORE INTO seen(source_id,url) VALUES(?,?)", (sid, url)
    )


def feed_has_url(conn, sid, url):
    """该 URL 是否已在当前 feed 中（强制重抓时的去重依据）。"""
    return conn.execute(
        "SELECT 1 FROM feed WHERE source_id=? AND url=?", (sid, url)
    ).fetchone() is not None


# ── feed ────────────────────────────────────────────────────────
def feed_add(conn, sid, title, url, pub_date=""):
    """新增一条 feed（增量：调用前应先 seen_has 判重）。返回 feed id。"""
    fid = _new_id("f")
    conn.execute(
        "INSERT INTO feed(id,source_id,title,url,fetched_at,pub_date,read_at,star,fulltext,pinned_to) "
        "VALUES(?,?,?,?,?,?,'',0,0,'')",
        (fid, sid, title, url, _now(), pub_date or ""),
    )
    return fid


def feed_list(conn, only_starred=False, source_id=None):
    """按报道日期（无则抓取日）倒序；已读不影响排序——卡片位置稳定，已读仅贴标签。"""
    q = "SELECT f.*, s.title AS source_title, s.grp AS source_grp " \
        "FROM feed f LEFT JOIN source s ON s.id=f.source_id WHERE 1=1"
    args = []
    if only_starred:
        q += " AND f.star>0"
    if source_id:
        q += " AND f.source_id=?"
        args.append(source_id)
    q += (" ORDER BY "
          "CASE WHEN f.pub_date<>'' THEN f.pub_date ELSE substr(f.fetched_at,1,10) END DESC, "
          "f.rowid DESC")
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def feed_mark_read(conn, ids):
    """批量标已读（入屏≥3s / 点开链接）。已读过的不覆盖时间。"""
    now = _now()
    conn.executemany(
        "UPDATE feed SET read_at=? WHERE id=? AND read_at=''",
        [(now, i) for i in ids],
    )
    conn.commit()


def feed_get(conn, fid):
    r = conn.execute("SELECT * FROM feed WHERE id=?", (fid,)).fetchone()
    return dict(r) if r else None


def feed_set_star(conn, fid, star):
    conn.execute("UPDATE feed SET star=? WHERE id=?", (int(star), fid))
    conn.commit()


def feed_set_fulltext(conn, fid, has):
    conn.execute("UPDATE feed SET fulltext=? WHERE id=?", (1 if has else 0, fid))
    conn.commit()


def feed_pin(conn, fid, workflow):
    conn.execute("UPDATE feed SET pinned_to=? WHERE id=?", (workflow or "", fid))
    conn.commit()


# ── 全文落盘 ─────────────────────────────────────────────────────
def fulltext_path(fid):
    return os.path.join(FULLTEXT_DIR, f"{fid}.json")


def fulltext_delete(fid):
    p = fulltext_path(fid)
    if os.path.isfile(p):
        os.remove(p)


# ── GC ──────────────────────────────────────────────────────────
def gc(conn, keep_unstarred_hours=0):
    """删除 star=0 且 pinned_to='' 且超过保留时长的 feed。
    手动「清理无星」传 0 = 立即全清；自动 GC 可传时长。返回删除条数。"""
    cutoff = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(time.time() - keep_unstarred_hours * 3600),
    )
    rows = conn.execute(
        "SELECT id FROM feed WHERE star=0 AND pinned_to='' AND fetched_at <= ?",
        (cutoff,),
    ).fetchall()
    ids = [r["id"] for r in rows]
    for fid in ids:
        fulltext_delete(fid)  # 理论上无星不会有全文，稳妥起见也清
    if ids:
        conn.executemany("DELETE FROM feed WHERE id=?", [(i,) for i in ids])
        conn.commit()
    return len(ids)


def gc_read(conn):
    """每晚 2:00 自动清理：已读 且 无星 且 未挂工作流。未读保留。"""
    rows = conn.execute(
        "SELECT id FROM feed WHERE star=0 AND pinned_to='' AND read_at<>''"
    ).fetchall()
    ids = [r["id"] for r in rows]
    for fid in ids:
        fulltext_delete(fid)
    if ids:
        conn.executemany("DELETE FROM feed WHERE id=?", [(i,) for i in ids])
        conn.commit()
    return len(ids)


def stats(conn):
    one = lambda q: conn.execute(q).fetchone()[0]
    return {
        "sources": one("SELECT COUNT(*) FROM source"),
        "feed_total": one("SELECT COUNT(*) FROM feed"),
        "unread": one("SELECT COUNT(*) FROM feed WHERE read_at=''"),
        "starred": one("SELECT COUNT(*) FROM feed WHERE star>0"),
        "star3": one("SELECT COUNT(*) FROM feed WHERE star=3"),
        "fulltext": one("SELECT COUNT(*) FROM feed WHERE fulltext=1"),
    }


if __name__ == "__main__":
    init_db()
    print(f"✓ 收集模块 DB 已就绪 → {DB_FILE}")
