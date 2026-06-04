#!/usr/bin/env python3
"""收集模块 SQLite 数据层 — stdlib only（sqlite3）。

【工程化迁移】逻辑零改动，仅路径常量改读 app.config.settings：
  DB_FILE      → settings.DB_PATH
  FULLTEXT_DIR → settings.FULLTEXT_DIR

范式取自 claude进化史/db.py：集中管理 schema + 连接 + 查询构造器，
供路由在请求时调用、crawler.py 写入。

三张表：
  source —— 监控源（全局/主体级，跨笔记本共享，仅结构化源 RSS/sitemap）
  seen   —— 每源已见 URL 快照（增量去重）
  feed   —— feed 条目（无星=临时；有星=持久，触发全文落盘）

决策见 功能一-实现目标.md「决策锁定」：
  打星三档 1/2/3 = 使用意图；无星不持久（24h GC 清）；打星即抓全文，取消即删。
"""
import os
import re
import sqlite3
import time

from app.config import settings

DB_FILE = settings.DB_PATH
FULLTEXT_DIR = settings.FULLTEXT_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
    id        TEXT PRIMARY KEY,
    user_id   TEXT DEFAULT 'u_default',  -- 归属用户（多租户隔离；存量行默认归 u_default）
    url       TEXT,
    title     TEXT,
    kind      TEXT,              -- rss | sitemap | registry | html
    freq      TEXT DEFAULT 'daily',
    run_times TEXT DEFAULT '08:00',  -- freq=daily 时：每天几点抓，逗号分隔（如 08:00,14:00）
    backfill_days INTEGER DEFAULT 7, -- 首抓回溯天数（1-14，加源时一次性设定）
    recipe    TEXT DEFAULT '',     -- AI 产出的抓取配方（JSON）；非空时按配方抓取
    grp       TEXT DEFAULT '',
    enabled   INTEGER DEFAULT 1,
    last_run  TEXT DEFAULT '',
    UNIQUE(user_id, url)           -- URL 唯一性按用户隔离：不同用户可监控同一站点
);
CREATE TABLE IF NOT EXISTS seen (
    source_id TEXT,
    url       TEXT,
    PRIMARY KEY(source_id, url)
);
CREATE TABLE IF NOT EXISTS feed (
    id         TEXT PRIMARY KEY,
    user_id    TEXT DEFAULT 'u_default',  -- 归属用户（多租户隔离）
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
    user_id TEXT DEFAULT '',
    key     TEXT,
    value   TEXT,
    PRIMARY KEY(user_id, key)
);
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    username   TEXT UNIQUE,
    pw_hash    TEXT,
    created_at TEXT,
    ai_config  TEXT DEFAULT '{}'    -- 用户级 AI 配置（JSON；api_key 加密存）
);
CREATE TABLE IF NOT EXISTS session (
    token      TEXT PRIMARY KEY,
    user_id    TEXT,
    created_at TEXT,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_feed_star    ON feed(star);
CREATE INDEX IF NOT EXISTS idx_feed_fetched ON feed(fetched_at);
CREATE INDEX IF NOT EXISTS idx_feed_source  ON feed(source_id);
CREATE INDEX IF NOT EXISTS idx_session_user ON session(user_id);
"""

# user_id 索引建在 _MIGRATIONS 补列之后（旧表此刻才有 user_id 列，否则 CREATE INDEX 报无此列）
_USER_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_source_user ON source(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_feed_user   ON feed(user_id)",
)

# 旧库平滑迁移：缺列就补（幂等）。只追加、不改旧条目（账本式）。
_MIGRATIONS = (
    "ALTER TABLE source ADD COLUMN run_times TEXT DEFAULT '08:00'",
    "ALTER TABLE source ADD COLUMN backfill_days INTEGER DEFAULT 7",
    "ALTER TABLE source ADD COLUMN recipe TEXT DEFAULT ''",
    "ALTER TABLE feed ADD COLUMN pub_date TEXT DEFAULT ''",
    "ALTER TABLE feed ADD COLUMN read_at TEXT DEFAULT ''",
    # 多租户：存量 source/feed 补 user_id，缺省归默认用户
    "ALTER TABLE source ADD COLUMN user_id TEXT DEFAULT 'u_default'",
    "ALTER TABLE feed ADD COLUMN user_id TEXT DEFAULT 'u_default'",
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
    _migrate_meta(conn)          # meta 改复合主键须在建表前（旧表无 user_id 列时重建）
    _migrate_source_unique(conn) # source 的 url 唯一性从表级改成 (user_id,url)
    conn.executescript(SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # 列已存在
    for stmt in _USER_INDEXES:  # 补列后再建 user_id 索引
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    if own:
        conn.close()


def _migrate_meta(conn):
    """meta 表改 (user_id,key) 复合主键。
    SQLite 不能 ALTER 主键——检测旧表（PRAGMA 无 user_id 列）则建新表搬数据：
    旧行的 user_id 一律置 ''（全局态，如调度器的 last_read_clean）。幂等 + 容错。"""
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(meta)").fetchall()]
    except sqlite3.OperationalError:
        return  # meta 表尚不存在，交给 SCHEMA 建新表（已含复合主键）
    if not cols or "user_id" in cols:
        return  # 表不存在或已是新结构
    try:
        conn.execute(
            "CREATE TABLE meta_new ("
            "user_id TEXT DEFAULT '', key TEXT, value TEXT, PRIMARY KEY(user_id,key))"
        )
        conn.execute("INSERT INTO meta_new(user_id,key,value) SELECT '', key, value FROM meta")
        conn.execute("DROP TABLE meta")
        conn.execute("ALTER TABLE meta_new RENAME TO meta")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 已迁移过或并发竞态，忽略


def _migrate_source_unique(conn):
    """source 的 URL 唯一性从「表级 UNIQUE(url)」改成「UNIQUE(user_id,url)」。
    旧表（demo 时期）是表级唯一——多租户下会阻止两个用户监控同一站点。
    检测旧建表 SQL 含 `url TEXT UNIQUE` 则原地重建（保留全部数据）。幂等 + 容错。
    须先确保 user_id 列已存在（旧库可能尚无），故先补列再重建。"""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='source'"
        ).fetchone()
    except sqlite3.OperationalError:
        return
    if not row or not row[0]:
        return  # 表不存在，交给 SCHEMA 建新表（已是 UNIQUE(user_id,url)）
    sql_compact = " ".join(row[0].split()).replace(" ", "")  # 归一化空白后比对
    if "UNIQUE(user_id,url)" in sql_compact or "urlTEXTUNIQUE" not in sql_compact:
        return  # 已是新结构，或本就无表级 url 唯一约束
    cols = [r[1] for r in conn.execute("PRAGMA table_info(source)").fetchall()]
    if "user_id" not in cols:
        try:
            conn.execute("ALTER TABLE source ADD COLUMN user_id TEXT DEFAULT 'u_default'")
        except sqlite3.OperationalError:
            pass
        cols.append("user_id")
    # collist 来自 PRAGMA table_info（库内已有列名），无用户输入，无注入面
    collist = ",".join(cols)
    try:
        conn.execute(
            "CREATE TABLE source_new ("
            "id TEXT PRIMARY KEY, user_id TEXT DEFAULT 'u_default', url TEXT, title TEXT, "
            "kind TEXT, freq TEXT DEFAULT 'daily', run_times TEXT DEFAULT '08:00', "
            "backfill_days INTEGER DEFAULT 7, recipe TEXT DEFAULT '', grp TEXT DEFAULT '', "
            "enabled INTEGER DEFAULT 1, last_run TEXT DEFAULT '', UNIQUE(user_id, url))"
        )
        conn.execute(f"INSERT INTO source_new({collist}) SELECT {collist} FROM source")
        conn.execute("DROP TABLE source")
        conn.execute("ALTER TABLE source_new RENAME TO source")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 已迁移过或并发竞态，忽略


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _new_id(prefix):
    return f"{prefix}_{int(time.time() * 1000)}{os.urandom(2).hex()}"


# ── meta（kv，记录如「上次已读清理日期」等状态）──────────────────
# user_id='' = 全局态（调度器的 last_read_clean 用）；可按用户存私有状态。
def meta_set(conn, key, value, user_id=""):
    conn.execute(
        "INSERT INTO meta(user_id,key,value) VALUES(?,?,?) "
        "ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",
        (user_id, key, str(value)),
    )
    conn.commit()


def meta_get(conn, key, default="", user_id=""):
    r = conn.execute(
        "SELECT value FROM meta WHERE user_id=? AND key=?", (user_id, key)
    ).fetchone()
    return r["value"] if r else default


# ── 监控源 ──────────────────────────────────────────────────────
# 业务函数全部走关键字传参（防多租户改造后位置错乱）。
def source_add(conn, *, user_id, url, title, kind, freq="daily", grp="",
               run_times="08:00", backfill_days=7, recipe=""):
    sid = _new_id("mon")
    days = max(1, min(14, int(backfill_days or 7)))  # 上限 14 天
    conn.execute(
        "INSERT INTO source(id,user_id,url,title,kind,freq,run_times,backfill_days,recipe,grp,enabled,last_run) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,1,'')",
        (sid, user_id, url, title, kind, freq, run_times or "08:00", days, recipe or "", grp),
    )
    conn.commit()
    return source_get(conn, sid)


def source_get(conn, sid, user_id=None):
    """user_id 给了则校验归属：不匹配返 None（防越权访问他人源）。"""
    r = conn.execute("SELECT * FROM source WHERE id=?", (sid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    if user_id is not None and d.get("user_id") != user_id:
        return None
    return d


def source_list(conn, user_id):
    rows = conn.execute(
        "SELECT * FROM source WHERE user_id=? ORDER BY grp, title", (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def source_list_all(conn):
    """跨用户取全部源（调度器用，不过滤 user）。"""
    rows = conn.execute("SELECT * FROM source ORDER BY grp, title").fetchall()
    return [dict(r) for r in rows]


def source_delete(conn, sid, user_id):
    conn.execute("DELETE FROM seen WHERE source_id=?", (sid,))
    conn.execute("DELETE FROM source WHERE id=? AND user_id=?", (sid, user_id))
    # 留下该源已打星的 feed（用户资产），只删未打星的
    conn.execute("DELETE FROM feed WHERE source_id=? AND user_id=? AND star=0", (sid, user_id))
    conn.commit()


def source_toggle(conn, sid, user_id, enabled):
    conn.execute(
        "UPDATE source SET enabled=? WHERE id=? AND user_id=?",
        (1 if enabled else 0, sid, user_id),
    )
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
def feed_add(conn, *, user_id, sid, title, url, pub_date=""):
    """新增一条 feed（增量：调用前应先 seen_has 判重）。返回 feed id。"""
    fid = _new_id("f")
    conn.execute(
        "INSERT INTO feed(id,user_id,source_id,title,url,fetched_at,pub_date,read_at,star,fulltext,pinned_to) "
        "VALUES(?,?,?,?,?,?,?,'',0,0,'')",
        (fid, user_id, sid, title, url, _now(), pub_date or ""),
    )
    return fid


def feed_list(conn, user_id, only_starred=False, source_id=None):
    """按报道日期（无则抓取日）倒序；已读不影响排序——卡片位置稳定，已读仅贴标签。"""
    q = "SELECT f.*, s.title AS source_title, s.grp AS source_grp " \
        "FROM feed f LEFT JOIN source s ON s.id=f.source_id WHERE f.user_id=?"
    args = [user_id]
    if only_starred:
        q += " AND f.star>0"
    if source_id:
        q += " AND f.source_id=?"
        args.append(source_id)
    q += (" ORDER BY "
          "CASE WHEN f.pub_date<>'' THEN f.pub_date ELSE substr(f.fetched_at,1,10) END DESC, "
          "f.rowid DESC")
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def feed_mark_read(conn, ids, user_id):
    """批量标已读（入屏≥3s / 点开链接）。已读过的不覆盖时间；限本人 feed。"""
    now = _now()
    conn.executemany(
        "UPDATE feed SET read_at=? WHERE id=? AND user_id=? AND read_at=''",
        [(now, i, user_id) for i in ids],
    )
    conn.commit()


def feed_get(conn, fid, user_id=None):
    """user_id 给了则校验归属：不匹配返 None。"""
    r = conn.execute("SELECT * FROM feed WHERE id=?", (fid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    if user_id is not None and d.get("user_id") != user_id:
        return None
    return d


def feed_set_star(conn, fid, star, user_id):
    conn.execute("UPDATE feed SET star=? WHERE id=? AND user_id=?", (int(star), fid, user_id))
    conn.commit()


def feed_set_fulltext(conn, fid, has, user_id):
    conn.execute("UPDATE feed SET fulltext=? WHERE id=? AND user_id=?", (1 if has else 0, fid, user_id))
    conn.commit()


def feed_pin(conn, fid, workflow, user_id):
    conn.execute("UPDATE feed SET pinned_to=? WHERE id=? AND user_id=?", (workflow or "", fid, user_id))
    conn.commit()


# ── 全文落盘 ─────────────────────────────────────────────────────
def fulltext_path(fid):
    if not re.fullmatch(r'[A-Za-z0-9_]+', fid):
        raise ValueError("非法 feed id")
    return os.path.join(FULLTEXT_DIR, f"{fid}.json")


def fulltext_delete(fid):
    p = fulltext_path(fid)
    if os.path.isfile(p):
        os.remove(p)


# ── GC ──────────────────────────────────────────────────────────
def gc(conn, user_id=None, keep_unstarred_hours=0):
    """删除 star=0 且 pinned_to='' 且超过保留时长的 feed。
    手动「清理无星」传 0 = 立即全清；自动 GC 可传时长。
    user_id=None = 全局清（所有用户）；给了则只清该用户。返回删除条数。"""
    cutoff = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(time.time() - keep_unstarred_hours * 3600),
    )
    q = "SELECT id FROM feed WHERE star=0 AND pinned_to='' AND fetched_at <= ?"
    args = [cutoff]
    if user_id is not None:
        q += " AND user_id=?"
        args.append(user_id)
    rows = conn.execute(q, args).fetchall()
    ids = [r["id"] for r in rows]
    for fid in ids:
        fulltext_delete(fid)  # 理论上无星不会有全文，稳妥起见也清
    if ids:
        conn.executemany("DELETE FROM feed WHERE id=?", [(i,) for i in ids])
        conn.commit()
    return len(ids)


def gc_read(conn, user_id=None):
    """每晚 2:00 自动清理：已读 且 无星 且 未挂工作流。未读保留。
    user_id=None = 所有用户一起清（调度器用）；给了则只清该用户。"""
    q = "SELECT id FROM feed WHERE star=0 AND pinned_to='' AND read_at<>''"
    args = []
    if user_id is not None:
        q += " AND user_id=?"
        args.append(user_id)
    rows = conn.execute(q, args).fetchall()
    ids = [r["id"] for r in rows]
    for fid in ids:
        fulltext_delete(fid)
    if ids:
        conn.executemany("DELETE FROM feed WHERE id=?", [(i,) for i in ids])
        conn.commit()
    return len(ids)


def stats(conn, user_id):
    one = lambda q: conn.execute(q, (user_id,)).fetchone()[0]
    return {
        "sources": one("SELECT COUNT(*) FROM source WHERE user_id=?"),
        "feed_total": one("SELECT COUNT(*) FROM feed WHERE user_id=?"),
        "unread": one("SELECT COUNT(*) FROM feed WHERE user_id=? AND read_at=''"),
        "starred": one("SELECT COUNT(*) FROM feed WHERE user_id=? AND star>0"),
        "star3": one("SELECT COUNT(*) FROM feed WHERE user_id=? AND star=3"),
        "fulltext": one("SELECT COUNT(*) FROM feed WHERE user_id=? AND fulltext=1"),
    }


# ── 用户 ────────────────────────────────────────────────────────
def user_create(conn, username, pw_hash):
    uid = _new_id("u")
    conn.execute(
        "INSERT INTO users(id,username,pw_hash,created_at,ai_config) VALUES(?,?,?,?,'{}')",
        (uid, username, pw_hash, _now()),
    )
    conn.commit()
    return user_get(conn, uid)


def user_get(conn, uid):
    r = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(r) if r else None


def user_by_name(conn, username):
    r = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(r) if r else None


def user_set_ai_config(conn, uid, ai_config_json):
    conn.execute("UPDATE users SET ai_config=? WHERE id=?", (ai_config_json, uid))
    conn.commit()


def ensure_default_user(conn):
    """幂等建默认用户 u_default（single 模式与存量数据归属）。
    pw_hash='' → 永不可登录（见 auth.service.verify_password）。"""
    if not user_get(conn, "u_default"):
        conn.execute(
            "INSERT INTO users(id,username,pw_hash,created_at,ai_config) "
            "VALUES('u_default','default','',?,'{}')",
            (_now(),),
        )
        conn.commit()
    return user_get(conn, "u_default")


# ── 会话 ────────────────────────────────────────────────────────
def session_create(conn, uid, ttl_days=30):
    import secrets
    token = secrets.token_hex(32)
    now = time.time()
    created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    expires = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + ttl_days * 86400))
    conn.execute(
        "INSERT INTO session(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
        (token, uid, created, expires),
    )
    conn.commit()
    return token


def session_user(conn, token):
    """凭 token 取用户 dict；token 无效或已过期返 None。"""
    r = conn.execute("SELECT * FROM session WHERE token=?", (token,)).fetchone()
    if not r:
        return None
    if r["expires_at"] and r["expires_at"] <= _now():
        return None
    return user_get(conn, r["user_id"])


def session_delete(conn, token):
    conn.execute("DELETE FROM session WHERE token=?", (token,))
    conn.commit()


def session_gc(conn):
    """清理过期会话。返回删除条数。"""
    cur = conn.execute("DELETE FROM session WHERE expires_at<>'' AND expires_at <= ?", (_now(),))
    conn.commit()
    return cur.rowcount


if __name__ == "__main__":
    init_db()
    print(f"✓ 收集模块 DB 已就绪 → {DB_FILE}")
