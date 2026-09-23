#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Avto-Xabar Web Panel — bot.py bilan bir xil DB ni ishlatadi.
- User bot'dagi link orqali kiradi: https://saytingiz.com/?uid=TELEGRAM_ID
- Xabar (matn+rasm), interval, guruh tanlash, Start/Stop/Test — hammasi web'da
- Fon scheduler har 30 sek daqti kelganlarni Telegram guruhlarga yuboradi
- Founder panel: statistika, userlar/ban, announce, stopall
Deploy: Render Web Service -> Start: gunicorn webapp:app  (yoki python webapp.py)
"""
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, request, redirect, url_for, send_from_directory, jsonify, g
from werkzeug.utils import secure_filename

# ============ CONFIG ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
WEB_URL = os.getenv("WEB_URL", "").rstrip("/")

_pg = {}
if USE_PG:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    _pg["psycopg2"] = psycopg2
    _pg["RealDictCursor"] = RealDictCursor

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "mp4", "pdf", "doc", "docx", "mp3", "ogg", "wav"}

# ============ DB ============
def db():
    if USE_PG:
        return _pg["psycopg2"].connect(DATABASE_URL, cursor_factory=_pg["RealDictCursor"])
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def _ex(cur, q, p=()):
    cur.execute(q.replace("?", "%s"), p) if USE_PG else cur.execute(q, p)

def init_db():
    con = db(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
        is_founder INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0, created TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS groups(
        chat_id BIGINT PRIMARY KEY, title TEXT, gtype TEXT, added_at TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS user_groups(
        user_id BIGINT, group_id BIGINT, PRIMARY KEY(user_id, group_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS group_owners(
        user_id BIGINT, group_id BIGINT, PRIMARY KEY(user_id, group_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS broadcasts(
        user_id BIGINT PRIMARY KEY, source_chat_id BIGINT, source_msg_id BIGINT,
        preview TEXT, has_text INTEGER DEFAULT 0,
        interval_min INTEGER DEFAULT 10, is_active INTEGER DEFAULT 0, last_sent TEXT)""")
    # web uchun qo'shimcha ustunlar (migratsiya — xavfsiz)
    try:
        if USE_PG:
            cur.execute("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS web_text TEXT DEFAULT ''")
            cur.execute("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS web_media TEXT DEFAULT ''")
            cur.execute("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS has_text INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS last_sent TEXT")
        else:
            cur.execute("PRAGMA table_info(broadcasts)")
            cols = {r[1] for r in cur.fetchall()}
            for col, ddl in (("web_text", "ALTER TABLE broadcasts ADD COLUMN web_text TEXT DEFAULT ''"),
                             ("web_media", "ALTER TABLE broadcasts ADD COLUMN web_media TEXT DEFAULT ''"),
                             ("has_text", "ALTER TABLE broadcasts ADD COLUMN has_text INTEGER DEFAULT 0"),
                             ("last_sent", "ALTER TABLE broadcasts ADD COLUMN last_sent TEXT")):
                if col not in cols:
                    cur.execute(ddl)
    except Exception:
        pass
    try:
        if USE_PG:
            cur.execute("INSERT INTO group_owners(user_id,group_id) SELECT user_id,group_id FROM user_groups ON CONFLICT DO NOTHING")
        else:
            cur.execute("INSERT OR IGNORE INTO group_owners(user_id,group_id) SELECT user_id,group_id FROM user_groups")
    except Exception:
        pass
    con.commit(); con.close()

def get_user(uid):
    con = db(); cur = con.cursor()
    _ex(cur, "SELECT * FROM users WHERE user_id=?", (uid,))
    r = cur.fetchone(); con.close()
    return r

def is_founder_uid(uid):
    if uid in ADMIN_IDS:
        return True
    u = get_user(uid)
    if u and u["is_founder"]:
        return True
    if u and u["username"] and u["username"].lower() == FOUNDER_USERNAME.lower():
        return True
    return False

def owned_groups(uid, founder=False):
    con = db(); cur = con.cursor()
    if founder:
        cur.execute("SELECT * FROM groups ORDER BY title LIMIT 100")
    else:
        _ex(cur, """SELECT g.chat_id,g.title,g.gtype FROM group_owners o
                    JOIN groups g ON g.chat_id=o.group_id WHERE o.user_id=? ORDER BY g.title""", (uid,))
    rows = cur.fetchall()
    _ex(cur, "SELECT group_id FROM user_groups WHERE user_id=?", (uid,))
    sel = {r["group_id"] for r in cur.fetchall()}
    con.close()
    return rows, sel

# ============ TELEGRAM API ============
import urllib.request, urllib.parse, json, mimetypes

def tg_call(method, payload=None, files=None):
    if not BOT_TOKEN:
        return {"ok": False, "desc": "BOT_TOKEN yo'q"}
    try:
        import requests
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
        if files:
            r = requests.post(url, data=payload, files=files, timeout=25)
        else:
            r = requests.post(url, json=payload, timeout=25)
        return r.json()
    except Exception as e:
        return {"ok": False, "desc": str(e)}

def send_broadcast_to_groups(uid):
    """Bitta user broadcastini yuboradi. (web_text/media ustun, aks holda copy)"""
    con = db(); cur = con.cursor()
    _ex(cur, "SELECT * FROM broadcasts WHERE user_id=?", (uid,))
    bc = cur.fetchone()
    _ex(cur, """SELECT g.chat_id FROM user_groups ug JOIN groups g ON g.chat_id=ug.group_id
                WHERE ug.user_id=?""", (uid,))
    groups = cur.fetchall()
    con.close()
    if not bc or not groups:
        return 0, 0, "Xabar yoki guruh yo'q"
    has_copy = bool(bc["source_msg_id"])
    has_web = bool((bc["web_text"] or "").strip() or (bc["web_media"] or "").strip())
    if not has_copy and not has_web:
        return 0, 0, "Xabar topilmadi"
    ok = fail = 0
    for g in groups:
        try:
            if has_web:
                media = (bc["web_media"] or "").strip()
                txt = (bc["web_text"] or "").strip()
                if media:
                    fpath = os.path.join(UPLOAD_DIR, os.path.basename(media))
                    ext = media.rsplit(".", 1)[-1].lower() if "." in media else ""
                    if os.path.exists(fpath):
                        import requests
                        url = f"https://api.telegram.org/bot{BOT_TOKEN}/"
                        with open(fpath, "rb") as f:
                            if ext in ("png", "jpg", "jpeg", "gif"):
                                r = requests.post(url + "sendPhoto", data={"chat_id": g["chat_id"], "caption": txt[:1024]}, files={"photo": f}, timeout=25).json()
                            elif ext == "mp4":
                                r = requests.post(url + "sendVideo", data={"chat_id": g["chat_id"], "caption": txt[:1024]}, files={"video": f}, timeout=25).json()
                            else:
                                r = requests.post(url + "sendDocument", data={"chat_id": g["chat_id"], "caption": txt[:1024]}, files={"document": f}, timeout=25).json()
                    else:
                        r = tg_call("sendMessage", {"chat_id": g["chat_id"], "text": txt or "(media topilmadi)"})
                else:
                    r = tg_call("sendMessage", {"chat_id": g["chat_id"], "text": txt})
            else:
                r = tg_call("copyMessage", {"chat_id": g["chat_id"], "from_chat_id": bc["source_chat_id"], "message_id": bc["source_msg_id"]})
            if r.get("ok"):
                ok += 1
            else:
                fail += 1
                if any(k in str(r).lower() for k in ("not found", "deleted", "kicked", "bot was kicked", "chat not found")):
                    try:
                        c2 = db(); cu = c2.cursor()
                        _ex(cu, "DELETE FROM groups WHERE chat_id=?", (g["chat_id"],))
                        _ex(cu, "DELETE FROM user_groups WHERE group_id=?", (g["chat_id"],))
                        _ex(cu, "DELETE FROM group_owners WHERE group_id=?", (g["chat_id"],))
                        c2.commit(); c2.close()
                    except Exception:
                        pass
        except Exception:
            fail += 1
        time.sleep(0.35)
    try:
        c = db(); cu = c.cursor()
        _ex(cu, "UPDATE broadcasts SET last_sent=? WHERE user_id=?", (datetime.now().isoformat(timespec="seconds"), uid))
        c.commit(); c.close()
    except Exception:
        pass
    return ok, fail, ""

def scheduler_loop():
    while True:
        try:
            con = db(); cur = con.cursor()
            cur.execute("SELECT * FROM broadcasts WHERE is_active=1")
            rows = cur.fetchall(); con.close()
            now = datetime.now()
            for bc in rows:
                try:
                    uid = bc["user_id"]
                    u = get_user(uid)
                    if u and u["is_banned"]:
                        continue
                    iv = bc["interval_min"] or 10
                    due = True
                    if bc["last_sent"]:
                        try:
                            due = now - datetime.fromisoformat(bc["last_sent"]) >= timedelta(minutes=iv)
                        except Exception:
                            due = True
                    if due:
                        has = bool(bc["source_msg_id"] or (bc["web_text"] or "").strip() or (bc["web_media"] or "").strip())
                        if has:
                            send_broadcast_to_groups(uid)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(30)

# ============ HTML SHELL ============
CSS = """
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:0}
.wrap{max-width:860px;margin:0 auto;padding:20px}
.card{background:#1e293b;border:1px solid #334155;border-radius:14px;padding:18px;margin:14px 0}
.btn{display:inline-block;background:#2563eb;color:#fff;border:0;border-radius:10px;padding:10px 16px;margin:4px;cursor:pointer;text-decoration:none;font-weight:600}
.btn.green{background:#16a34a}.btn.red{background:#dc2626}.btn.gray{background:#475569}
input,textarea,select{width:100%;background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:10px;padding:10px;margin:6px 0}
label{font-size:14px;color:#94a3b8}
.grp{display:flex;align-items:center;gap:10px;background:#0f172a;border:1px solid #334155;border-radius:10px;padding:8px 12px;margin:6px 0}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px}.on{background:#16a34a}.off{background:#475569}
a{color:#60a5fa}.top{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap}
small{color:#94a3b8}h1{font-size:24px}h2{font-size:18px;margin:6px 0}
"""

def shell(title, body, uid=None):
    nav = ""
    if uid:
        nav = f'<div class="top"><small>UID: <b>{uid}</b></small><span><a class="btn gray" href="/dash?uid={uid}">🏠 Panel</a> <a class="btn gray" href="/founder?uid={uid}">👑 Founder</a> <a class="btn gray" href="/">↩️ Chiqish</a></span></div>'
    tok_warn = "" if BOT_TOKEN else '<div class="card" style="border-color:#dc2626">⚠️ <b>BOT_TOKEN topilmadi!</b> Render Environment ga BOT_TOKEN qo‘shing, aks holda yuborish ishlamaydi (sayt ochiladi, yuborish ishlamaydi).</div>'
    return f"""<!doctype html><html lang="uz"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>📢 Avto-Xabar Web Panel</h1>{nav}{tok_warn}{body}<small style="display:block;margin-top:20px">Bot bilan bir xil baza (bot.db / Postgres). Guruhga botni admin qilib qo‘shgan user avtomatik ko‘rinadi.</small></div></body></html>"""

def need_uid():
    uid = request.args.get("uid") or request.form.get("uid") or ""
    uid = str(uid).strip()
    if not uid.isdigit():
        return None
    return int(uid)

# ============ ROUTES ============
@app.route("/")
def index():
    uid = request.args.get("uid", "")
    body = f"""
<div class="card"><h2>👋 Kirish</h2>
<p>Telegram ID raqamingizni yozing (bot sizga bergan linkda avtomatik keladi).</p>
<form action="/dash" method="get">
<label>Telegram user ID</label>
<input name="uid" placeholder="masalan: 123456789" value="{uid}">
<button class="btn" type="submit">➡️ Kabinetga kirish</button>
</form>
<p><small>ID ni bilmasangiz: botga <b>/start</b> yozing — bot sizga Web Panel tugmasi bilan shaxsiy linkingizni beradi.</small></p></div>
<div class="card"><h2>❓ Qanday ishlaydi?</h2>
<p>1️⃣ Botni guruhingizga <b>admin</b> qilib qo‘shing.<br>2️⃣ Web panelda <b>xabar + rasm</b> yozing.<br>
3️⃣ <b>Interval</b> belgilang (daq).<br>4️⃣ Guruhlarni ✅ belgilang.<br>5️⃣ <b>▶️ Start</b> — har N daqiqada avtomatik yuboriladi.</p></div>"""
    return shell("Kirish", body)

@app.route("/dash", methods=["GET"])
def dash():
    uid = need_uid()
    if not uid:
        return redirect(url_for("index"))
    init_db()
    u = get_user(uid)
    if not u:
        # yangi user — ro'yxatga olamiz (bot /start dagi kabi)
        con = db(); cur = con.cursor()
        _ex(cur, "INSERT INTO users(user_id,username,first_name,is_founder,created) VALUES(?,?,?,?,?)",
            (uid, "", "", 0, datetime.now().isoformat(timespec="seconds")))
        con.commit(); con.close()
        u = get_user(uid)
    if u and u["is_banned"] and not is_founder_uid(uid):
        return shell("Blok", '<div class="card">⛔ Siz bloklangansiz.</div>', uid)
    founder = is_founder_uid(uid)
    con = db(); cur = con.cursor()
    _ex(cur, "SELECT * FROM broadcasts WHERE user_id=?", (uid,))
    bc = cur.fetchone(); con.close()
    groups, sel = owned_groups(uid, founder)
    iv = bc["interval_min"] if bc else 10
    active = bool(bc and bc["is_active"])
    preview = (bc["preview"] if bc and bc["preview"] else "") or ""
    web_text = (bc["web_text"] if bc and "web_text" in bc.keys() else "") or ""
    web_media = (bc["web_media"] if bc and "web_media" in bc.keys() else "") or ""
    if not web_text and preview and not (bc and bc["source_msg_id"]):
        web_text = preview
    ghtml = ""
    if not groups:
        ghtml = "<p>📭 Guruh topilmadi. Botni guruhga <b>o‘zingiz</b> qo‘shing va admin qiling (1-2 daqiqada shu yerda chiqadi).</p>"
    else:
        for gg in groups:
            cid = gg["chat_id"]; t = (gg["title"] or str(cid))[:40]
            chk = "checked" if cid in sel else ""
            ghtml += f'<label class="grp"><input type="checkbox" data-gid="{cid}" {chk} onchange="toggleG({cid},this.checked)" style="width:auto"> <span>{"✅" if cid in sel else "❌"} {t}</span> <small>{cid}</small></label>'
    media_html = f'<p>📎 Media: <b>{web_media}</b></p>' if web_media else '<p><small>📎 Media yuklanmagan</small></p>'
    body = f"""
<div class="card"><div class="top"><h2>📊 Status: {'<span class="badge on">🟢 Faol</span>' if active else '<span class="badge off">🔴 Stop</span>'}</h2>
<span><button class="btn green" onclick="act('start')">▶️ Start</button>
<button class="btn red" onclick="act('stop')">⏸ Stop</button>
<button class="btn" onclick="act('test')">🚀 Test yuborish</button></span></div>
<p><small>⏱ Har <b>{iv} daqiqada</b> • 📋 Tanlangan: <b>{len(sel)} ta</b> • Xabar: {preview[:80] or web_text[:80] or '—'}</small></p>
<p id="msg"></p></div>
<div class="card"><h2>📝 Xabar yaratish</h2>
<form action="/api/message?uid={uid}" method="post" enctype="multipart/form-data">
<label>Matn</label><textarea name="text" rows="4" placeholder="Reklama matni...">{web_text}</textarea>
<label>Rasm/Video/File (ixtiyoriy, 20MB gacha)</label><input type="file" name="media">
{media_html}
<button class="btn" type="submit">💾 Saqlash</button></form>
<p><small>Izoh: bot lichkasida yaratilgan xabar bo‘lsa — web uni ham yuboradi (copy orqali). Web da yozsangiz — web matni ustun turadi.</small></p></div>
<div class="card"><h2>⏱ Interval (daqiqa)</h2>
<form action="/api/interval?uid={uid}" method="post">
<input name="minutes" type="number" min="1" max="10080" value="{iv}"><button class="btn" type="submit">Saqlash</button></form></div>
<div class="card"><h2>📋 Guruhlarim ({len(groups)} ta)</h2>{ghtml}
<div><button class="btn gray" onclick="act('select_all')">✅ Hammasini tanlash</button>
<button class="btn gray" onclick="act('clear')">🧹 Tozalash</button></div></div>
<script>
const UID={uid};
function act(a){{fetch('/api/'+a+'?uid='+UID,{{method:'POST'}}).then(r=>r.json()).then(j=>{{document.getElementById('msg').innerText=j.msg||JSON.stringify(j);setTimeout(()=>location.reload(),800);}});}}
function toggleG(gid,on){{fetch('/api/toggle?uid='+UID+'&gid='+gid+'&on='+(on?1:0),{{method:'POST'}}).then(r=>r.json()).then(j=>{{document.getElementById('msg').innerText=j.msg;}});}}
</script>"""
    return shell("Panel", body, uid)

@app.route("/api/message", methods=["POST"])
def api_message():
    uid = need_uid()
    if not uid:
        return jsonify({"ok": False}), 400
    init_db()
    text = (request.form.get("text") or "").strip()[:4000]
    media = ""
    f = request.files.get("media")
    if f and f.filename:
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else "bin"
        if ext in ALLOWED_EXT:
            fname = f"{uid}_{int(time.time())}_{secure_filename(f.filename)}"
            f.save(os.path.join(UPLOAD_DIR, fname))
            media = fname
    con = db(); cur = con.cursor()
    _ex(cur, "SELECT * FROM broadcasts WHERE user_id=?", (uid,))
    ex = cur.fetchone()
    preview = (text[:200] or (media or "media"))
    if ex:
        if text:
            _ex(cur, "UPDATE broadcasts SET web_text=?, preview=? WHERE user_id=?", (text, preview, uid))
        if media:
            _ex(cur, "UPDATE broadcasts SET web_media=?, preview=? WHERE user_id=?", (media, preview, uid))
        if not text and not media:
            con.close(); return redirect(f"/dash?uid={uid}")
    else:
        _ex(cur, "INSERT INTO broadcasts(user_id,preview,web_text,web_media,interval_min,is_active) VALUES(?,?,?,?,10,0)",
            (uid, preview, text, media))
    con.commit(); con.close()
    return redirect(f"/dash?uid={uid}")

@app.route("/api/interval", methods=["POST"])
def api_interval():
    uid = need_uid()
    try:
        mins = int((request.form.get("minutes") or "10").split()[0])
        assert 1 <= mins <= 10080
    except Exception:
        return "1-10080 oralig'ida son kiriting. <a href='/dash?uid=%s'>Orqaga</a>" % uid, 400
    con = db(); cur = con.cursor()
    _ex(cur, "SELECT * FROM broadcasts WHERE user_id=?", (uid,))
    if cur.fetchone():
        _ex(cur, "UPDATE broadcasts SET interval_min=? WHERE user_id=?", (mins, uid))
    else:
        _ex(cur, "INSERT INTO broadcasts(user_id,interval_min,is_active) VALUES(?,?,0)", (uid, mins))
    con.commit(); con.close()
    return redirect(f"/dash?uid={uid}")

def _is_owner_check(uid, gid, founder):
    if founder:
        return True
    con = db(); cur = con.cursor()
    _ex(cur, "SELECT 1 FROM group_owners WHERE user_id=? AND group_id=?", (uid, gid))
    r = cur.fetchone(); con.close()
    return bool(r)

@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    uid = need_uid()
    try:
        gid = int(request.args.get("gid")); on = request.args.get("on") == "1"
    except Exception:
        return jsonify({"ok": False})
    if not _is_owner_check(uid, gid, is_founder_uid(uid)):
        return jsonify({"ok": False, "msg": "⛔ Bu guruh sizniki emas"})
    con = db(); cur = con.cursor()
    if on:
        if USE_PG:
            cur.execute("INSERT INTO user_groups(user_id,group_id) VALUES(%s,%s) ON CONFLICT DO NOTHING", (uid, gid))
        else:
            cur.execute("INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES(?,?)", (uid, gid))
        msg = "✅ qo'shildi"
    else:
        _ex(cur, "DELETE FROM user_groups WHERE user_id=? AND group_id=?", (uid, gid))
        msg = "❌ o'chirildi"
    con.commit(); con.close()
    return jsonify({"ok": True, "msg": msg})

@app.route("/api/select_all", methods=["POST"])
def api_select_all():
    uid = need_uid()
    founder = is_founder_uid(uid)
    rows, _ = owned_groups(uid, founder)
    con = db(); cur = con.cursor()
    for gg in rows:
        if USE_PG:
            cur.execute("INSERT INTO user_groups(user_id,group_id) VALUES(%s,%s) ON CONFLICT DO NOTHING", (uid, gg["chat_id"]))
        else:
            cur.execute("INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES(?,?)", (uid, gg["chat_id"]))
    con.commit(); con.close()
    return jsonify({"ok": True, "msg": f"✅ {len(rows)} ta tanlandi"})

@app.route("/api/clear", methods=["POST"])
def api_clear():
    uid = need_uid()
    con = db(); cur = con.cursor()
    _ex(cur, "DELETE FROM user_groups WHERE user_id=?", (uid,))
    con.commit(); con.close()
    return jsonify({"ok": True, "msg": "🧹 Tozalandi"})

@app.route("/api/start", methods=["POST"])
def api_start():
    uid = need_uid()
    con = db(); cur = con.cursor()
    _ex(cur, "SELECT * FROM broadcasts WHERE user_id=?", (uid,))
    bc = cur.fetchone()
    if not bc or not (bc["source_msg_id"] or (bc["web_text"] or "").strip() or (bc["web_media"] or "").strip()):
        con.close(); return jsonify({"ok": False, "msg": "❌ Avval xabar yarating"})
    _ex(cur, "SELECT COUNT(*) c FROM user_groups WHERE user_id=?", (uid,))
    if cur.fetchone()["c"] == 0:
        con.close(); return jsonify({"ok": False, "msg": "❌ Guruh tanlanmagan"})
    _ex(cur, "UPDATE broadcasts SET is_active=1, last_sent=? WHERE user_id=?",
        (datetime.now().isoformat(timespec="seconds"), uid))
    con.commit(); con.close()
    return jsonify({"ok": True, "msg": "▶️ Boshladim!"})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    uid = need_uid()
    con = db(); cur = con.cursor()
    _ex(cur, "UPDATE broadcasts SET is_active=0 WHERE user_id=?", (uid,))
    con.commit(); con.close()
    return jsonify({"ok": True, "msg": "⏸ To'xtatildi"})

@app.route("/api/test", methods=["POST"])
def api_test():
    uid = need_uid()
    ok, fail, msg = send_broadcast_to_groups(uid)
    if msg:
        return jsonify({"ok": False, "msg": "❌ " + msg})
    return jsonify({"ok": True, "msg": f"🚀 Test: ✅ {ok} | ❌ {fail}"})

# ============ FOUNDER ============
def founder_required(fn):
    @wraps(fn)
    def w(*a, **kw):
        uid = need_uid()
        if not uid or not is_founder_uid(uid):
            return shell("Taqiq", '<div class="card">⛔ Faqat founder uchun. <a href="/">Bosh sahifa</a></div>'), 403
        return fn(uid, *a, **kw)
    return w

@app.route("/founder")
@founder_required
def founder(uid):
    con = db(); cur = con.cursor()
    cur.execute("SELECT COUNT(*) c FROM users"); uc = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM groups"); gc = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM broadcasts WHERE is_active=1"); ac = cur.fetchone()["c"]
    cur.execute("SELECT * FROM users ORDER BY created DESC LIMIT 20"); users = cur.fetchall()
    cur.execute("SELECT * FROM groups ORDER BY added_at DESC LIMIT 30"); groups = cur.fetchall()
    con.close()
    uhtml = "".join(
        f'<div class="grp"><span>{"🚫" if r["is_banned"] else "✅"} @{r["username"] or "-"} ({r["first_name"] or r["user_id"]})</span>'
        f'<a class="btn gray" href="/founder/ban?uid={uid}&target={r["user_id"]}">Ban/Alish</a></div>' for r in users)
    ghtml = "".join(f'<div class="grp"><span>{(r["title"] or "")[:40]}</span><small>{r["chat_id"]}</small></div>' for r in groups)
    body = f"""
<div class="card"><h2>📊 Statistika</h2><p>👥 Userlar: <b>{uc}</b> • 📋 Guruhlar: <b>{gc}</b> • 🟢 Faol: <b>{ac}</b></p>
<p><a class="btn red" href="/founder/stopall?uid={uid}">⛔ Hammasini to'xtatish</a></p></div>
<div class="card"><h2>📢 Announce (hamma userga)</h2>
<form action="/founder/announce?uid={uid}" method="post"><textarea name="text" rows="3" placeholder="Xabar..."></textarea>
<button class="btn" type="submit">Yuborish</button></form></div>
<div class="card"><h2>👥 So'nggi 20 user</h2>{uhtml or 'yo‘q'}</div>
<div class="card"><h2>📋 Barcha guruhlar</h2>{ghtml or 'yo‘q'}</div>"""
    return shell("Founder", body, uid)

@app.route("/founder/ban")
@founder_required
def fban(uid):
    try:
        t = int(request.args.get("target"))
    except Exception:
        return redirect(f"/founder?uid={uid}")
    con = db(); cur = con.cursor()
    _ex(cur, "SELECT * FROM users WHERE user_id=?", (t,))
    r = cur.fetchone()
    if r:
        _ex(cur, "UPDATE users SET is_banned=? WHERE user_id=?", (0 if r["is_banned"] else 1, t))
        if r["is_banned"] == 0:  # endi ban qilindi -> broadcastini o'chirish
            _ex(cur, "UPDATE broadcasts SET is_active=0 WHERE user_id=?", (t,))
        con.commit()
    con.close()
    return redirect(f"/founder?uid={uid}")

@app.route("/founder/stopall")
@founder_required
def fstopall(uid):
    con = db(); cur = con.cursor()
    cur.execute("UPDATE broadcasts SET is_active=0"); con.commit(); con.close()
    return redirect(f"/founder?uid={uid}")

@app.route("/founder/announce", methods=["POST"])
@founder_required
def fannounce(uid):
    text = (request.form.get("text") or "").strip()
    if not text:
        return redirect(f"/founder?uid={uid}")
    con = db(); cur = con.cursor()
    cur.execute("SELECT user_id FROM users WHERE is_banned=0")
    users = [r["user_id"] for r in cur.fetchall()]; con.close()
    for u in users:
        tg_call("sendMessage", {"chat_id": u, "text": f"📢 Founder xabari:\n\n{text}"})
        time.sleep(0.05)
    return redirect(f"/founder?uid={uid}")

@app.route("/uploads/<path:f>")
def uploads(f):
    return send_from_directory(UPLOAD_DIR, f)

@app.route("/health")
def health():
    return jsonify({"ok": True, "db": "pg" if USE_PG else "sqlite", "token": bool(BOT_TOKEN)})

init_db()
threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"🌐 Web panel: http://localhost:{port} | DB: {'PG' if USE_PG else 'sqlite'} | Token: {'bor' if BOT_TOKEN else 'YOQ'}")
    app.run(host="0.0.0.0", port=port)
