"""Umumiy DB: DATABASE_URL (Postgres/Neon) bo'lsa Postgres, bo'lmasa sqlite.
Vercel'da /tmp ephemeral — doimiy uchun DATABASE_URL shart."""
import os
import sqlite3

IS_VERCEL = bool(os.getenv("VERCEL"))
if IS_VERCEL and not os.getenv("DATABASE_URL"):
    DB_PATH = "/tmp/bot.db"
else:
    DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.db")

FOUNDER_USERNAME = os.getenv("FOUNDER_USERNAME", "dior_coder")
_raw_admins = os.getenv("ADMIN_IDS", "") or os.getenv("FOUNDER_IDS", "")
try:
    ADMIN_IDS = {int(x.strip()) for x in _raw_admins.split(",") if x.strip().lstrip("-").isdigit()}
except Exception:
    ADMIN_IDS = set()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

_pg_mods = {}
if USE_PG:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    _pg_mods["psycopg2"] = psycopg2
    _pg_mods["RealDictCursor"] = RealDictCursor


def db():
    if USE_PG:
        return _pg_mods["psycopg2"].connect(DATABASE_URL, cursor_factory=_pg_mods["RealDictCursor"])
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _ex(cur, query, params=()):
    if USE_PG:
        cur.execute(query.replace("?", "%s"), params)
    else:
        cur.execute(query, params)


def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
        is_founder INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0, created TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS groups(
        chat_id BIGINT PRIMARY KEY, title TEXT, gtype TEXT, added_at TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS user_groups(
        user_id BIGINT, group_id BIGINT, PRIMARY KEY(user_id, group_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS broadcasts(
        user_id BIGINT PRIMARY KEY, source_chat_id BIGINT, source_msg_id BIGINT,
        preview TEXT, has_text INTEGER DEFAULT 0,
        interval_min INTEGER DEFAULT 10, is_active INTEGER DEFAULT 0,
        last_sent TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS states(
        user_id BIGINT PRIMARY KEY, state TEXT)""")
    try:
        if USE_PG:
            cur.execute("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS has_text INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS last_sent TEXT")
        else:
            cur.execute("PRAGMA table_info(broadcasts)")
            cols = {r[1] for r in cur.fetchall()}
            if "has_text" not in cols:
                cur.execute("ALTER TABLE broadcasts ADD COLUMN has_text INTEGER DEFAULT 0")
            if "last_sent" not in cols:
                cur.execute("ALTER TABLE broadcasts ADD COLUMN last_sent TEXT")
    except Exception:
        pass
    con.commit()
    con.close()


def link_user_group(cur, user_id, group_id):
    if USE_PG:
        cur.execute("INSERT INTO user_groups(user_id,group_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, group_id))
    else:
        cur.execute("INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES(?,?)", (user_id, group_id))


def upsert_group(cur, chat_id, title, gtype, now):
    if USE_PG:
        cur.execute(
            """INSERT INTO groups(chat_id,title,gtype,added_at) VALUES(%s,%s,%s,%s)
               ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title, gtype=EXCLUDED.gtype, added_at=EXCLUDED.added_at""",
            (chat_id, title, gtype, now))
    else:
        cur.execute("INSERT OR REPLACE INTO groups(chat_id,title,gtype,added_at) VALUES(?,?,?,?)",
                    (chat_id, title, gtype, now))


def get_state(uid):
    con = db(); cur = con.cursor()
    _ex(cur, "SELECT state FROM states WHERE user_id=?", (uid,))
    r = cur.fetchone(); con.close()
    return r["state"] if r else None


def set_state(uid, state):
    con = db(); cur = con.cursor()
    if state is None:
        _ex(cur, "DELETE FROM states WHERE user_id=?", (uid,))
    else:
        if USE_PG:
            cur.execute("INSERT INTO states(user_id,state) VALUES(%s,%s) "
                        "ON CONFLICT(user_id) DO UPDATE SET state=EXCLUDED.state", (uid, state))
        else:
            cur.execute("INSERT OR REPLACE INTO states(user_id,state) VALUES(?,?)", (uid, state))
    con.commit(); con.close()


def is_founder_id(uid):
    return uid in ADMIN_IDS
