"""Vercel uchun umumiy DB (sqlite). Diqqat: Vercel'da /tmp ephemeral — redeployda tozalanadi.
Doimiy kerak bo'lsa DATABASE_URL (Postgres/Neon) ulang yoki VPS ishlating."""
import os
import sqlite3
from datetime import datetime

IS_VERCEL = bool(os.getenv("VERCEL"))
if IS_VERCEL:
    DB_PATH = "/tmp/bot.db"
else:
    DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.db")

FOUNDER_USERNAME = "dior_coder"


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        is_founder INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0, created TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS groups(
        chat_id INTEGER PRIMARY KEY, title TEXT, gtype TEXT, added_at TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS user_groups(
        user_id INTEGER, group_id INTEGER, PRIMARY KEY(user_id, group_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS broadcasts(
        user_id INTEGER PRIMARY KEY, source_chat_id INTEGER, source_msg_id INTEGER,
        preview TEXT, interval_min INTEGER DEFAULT 10, is_active INTEGER DEFAULT 0,
        last_sent TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS states(
        user_id INTEGER PRIMARY KEY, state TEXT)""")
    con.commit()
    con.close()


def get_state(uid):
    con = db(); cur = con.cursor()
    cur.execute("SELECT state FROM states WHERE user_id=?", (uid,))
    r = cur.fetchone(); con.close()
    return r["state"] if r else None


def set_state(uid, state):
    con = db(); cur = con.cursor()
    if state is None:
        cur.execute("DELETE FROM states WHERE user_id=?", (uid,))
    else:
        cur.execute("INSERT OR REPLACE INTO states(user_id,state) VALUES(?,?)", (uid, state))
    con.commit(); con.close()
