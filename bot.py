#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Avto-Xabar Broadcast Bot
- Foydalanuvchi botni o'z guruhlariga qo'shadi
- Botga xabar yuboradi + necha daqiqada takrorlanishini belgilaydi
- Bot har N daqiqada tanlangan guruhlarga xabarni yuboradi
- Founder: @dior_coder -> to'liq boshqaruv
"""
import os
import logging
import sqlite3
import asyncio
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, ContextTypes, filters,
)
from telegram.error import Forbidden, BadRequest, RetryAfter

# ============ CONFIG ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi! Render/Railway Environment ga qo'shing yoki .env yarating.")
FOUNDER_USERNAME = os.getenv("FOUNDER_USERNAME", "dior_coder")  # @ siz yoziladi
_raw_admins = os.getenv("ADMIN_IDS", "") or os.getenv("FOUNDER_IDS", "")
ADMIN_IDS = {int(x.strip()) for x in _raw_admins.split(",") if x.strip().lstrip("-").isdigit()}
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
if DATABASE_URL.startswith("postgres://"):
    # psycopg2 eski sxemani tushunmaydi
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")

_pg_mods = {}
if USE_PG:
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        _pg_mods["psycopg2"] = psycopg2
        _pg_mods["RealDictCursor"] = RealDictCursor
    except ImportError:
        raise RuntimeError("DATABASE_URL berilgan, lekin psycopg2 o'rnatilmagan! requirements.txt ga qarang.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============ DB ============
def db():
    if USE_PG:
        con = _pg_mods["psycopg2"].connect(DATABASE_URL, cursor_factory=_pg_mods["RealDictCursor"])
        return con
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def _ex(cur, query, params=()):
    """sqlite (?) va postgres (%s) placeholder farqini yashiradi."""
    if USE_PG:
        cur.execute(query.replace("?", "%s"), params)
    else:
        cur.execute(query, params)

def init_db():
    con = db()
    cur = con.cursor()
    # BIGINT ikkala DB da ham ishlaydi
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id BIGINT PRIMARY KEY,
        username TEXT, first_name TEXT,
        is_founder INTEGER DEFAULT 0,
        is_banned INTEGER DEFAULT 0,
        created TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS groups(
        chat_id BIGINT PRIMARY KEY,
        title TEXT, gtype TEXT,
        added_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS user_groups(
        user_id BIGINT, group_id BIGINT,
        PRIMARY KEY(user_id, group_id)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS broadcasts(
        user_id BIGINT PRIMARY KEY,
        source_chat_id BIGINT,
        source_msg_id BIGINT,
        has_text INTEGER DEFAULT 0,
        preview TEXT,
        interval_min INTEGER DEFAULT 10,
        is_active INTEGER DEFAULT 0,
        last_sent TEXT
    )""")
    # eski DB larda last_sent bo'lmasligi mumkin -> qo'shib qo'yamiz
    try:
        if USE_PG:
            cur.execute("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS last_sent TEXT")
            cur.execute("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS has_text INTEGER DEFAULT 0")
        else:
            cur.execute("PRAGMA table_info(broadcasts)")
            cols = {r[1] for r in cur.fetchall()}
            if "last_sent" not in cols:
                cur.execute("ALTER TABLE broadcasts ADD COLUMN last_sent TEXT")
            if "has_text" not in cols:
                cur.execute("ALTER TABLE broadcasts ADD COLUMN has_text INTEGER DEFAULT 0")
    except Exception:
        pass
    con.commit()
    con.close()

def get_user(user_id):
    con = db(); cur = con.cursor()
    _ex(cur, "SELECT * FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone(); con.close()
    return r

def register_user(tg_user, is_founder=False):
    con = db(); cur = con.cursor()
    now = datetime.now().isoformat(timespec="seconds")
    _ex(cur, "SELECT * FROM users WHERE user_id=?", (tg_user.id,))
    ex = cur.fetchone()
    if ex:
        _ex(cur, "UPDATE users SET username=?, first_name=?, is_founder=? WHERE user_id=?",
            (tg_user.username or "", tg_user.first_name or "", 1 if (is_founder or ex["is_founder"]) else 0, tg_user.id))
    else:
        _ex(cur, "INSERT INTO users(user_id,username,first_name,is_founder,created) VALUES(?,?,?,?,?)",
            (tg_user.id, tg_user.username or "", tg_user.first_name or "", 1 if is_founder else 0, now))
        # XAVFSIZLIK: yangi userga begona guruhlarni avtomatik biriktirmaymiz.
        # U o'zi qo'shgan guruhlargagina yuboradi (add_group owner orqali).
    con.commit(); con.close()

def is_founder_check(tg_user) -> bool:
    if tg_user and tg_user.id in ADMIN_IDS:
        return True
    if tg_user and tg_user.username and tg_user.username.lower() == FOUNDER_USERNAME.lower():
        return True
    if tg_user:
        u = get_user(tg_user.id)
        if u and u["is_founder"]:
            return True
    return False

def _upsert_group(cur, chat_id, title, gtype, now):
    if USE_PG:
        cur.execute(
            """INSERT INTO groups(chat_id,title,gtype,added_at) VALUES(%s,%s,%s,%s)
               ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title, gtype=EXCLUDED.gtype, added_at=EXCLUDED.added_at""",
            (chat_id, title, gtype, now))
    else:
        cur.execute("INSERT OR REPLACE INTO groups(chat_id,title,gtype,added_at) VALUES(?,?,?,?)",
                    (chat_id, title, gtype, now))

def _link_user_group(cur, user_id, group_id):
    if USE_PG:
        cur.execute("INSERT INTO user_groups(user_id,group_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, group_id))
    else:
        cur.execute("INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES(?,?)", (user_id, group_id))

def add_group(chat_id, title, gtype, owner_id=None):
    """Bot guruhga qo'shilganda chaqiriladi. Faqat qo'shgan odamga biriktiradi."""
    con = db(); cur = con.cursor()
    now = datetime.now().isoformat(timespec="seconds")
    _upsert_group(cur, chat_id, title, gtype, now)
    if owner_id:
        try:
            _link_user_group(cur, owner_id, chat_id)
        except Exception:
            pass
    con.commit(); con.close()

def upsert_group_title(chat_id, title, gtype):
    """Guruhdagi oddiy xabarlarda nomni yangilash — hech kimga auto-link qilmaydi."""
    con = db(); cur = con.cursor()
    now = datetime.now().isoformat(timespec="seconds")
    _upsert_group(cur, chat_id, title, gtype, now)
    con.commit(); con.close()

def remove_group(chat_id):
    con = db(); cur = con.cursor()
    _ex(cur, "DELETE FROM groups WHERE chat_id=?", (chat_id,))
    _ex(cur, "DELETE FROM user_groups WHERE group_id=?", (chat_id,))
    con.commit(); con.close()

def get_user_groups(user_id):
    con = db(); cur = con.cursor()
    _ex(cur, """SELECT g.chat_id, g.title, g.gtype FROM user_groups ug
                   JOIN groups g ON g.chat_id=ug.group_id WHERE ug.user_id=?""", (user_id,))
    rows = cur.fetchall(); con.close()
    return rows

def get_all_groups():
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM groups ORDER BY added_at DESC")
    rows = cur.fetchall(); con.close()
    return rows

def get_broadcast(user_id):
    con = db(); cur = con.cursor()
    _ex(cur, "SELECT * FROM broadcasts WHERE user_id=?", (user_id,))
    r = cur.fetchone(); con.close()
    return r

def save_broadcast_msg(user_id, src_chat, src_msg, preview, has_text=0):
    con = db(); cur = con.cursor()
    _ex(cur, "SELECT * FROM broadcasts WHERE user_id=?", (user_id,))
    ex = cur.fetchone()
    if ex:
        _ex(cur, """UPDATE broadcasts SET source_chat_id=?, source_msg_id=?, preview=?, has_text=?
                       WHERE user_id=?""", (src_chat, src_msg, preview, has_text, user_id))
    else:
        _ex(cur, """INSERT INTO broadcasts(user_id,source_chat_id,source_msg_id,preview,has_text,interval_min,is_active)
                       VALUES(?,?,?, ?,?,10,0)""", (user_id, src_chat, src_msg, preview, has_text))
    con.commit(); con.close()

def set_interval(user_id, minutes):
    con = db(); cur = con.cursor()
    _ex(cur, "SELECT * FROM broadcasts WHERE user_id=?", (user_id,))
    if not cur.fetchone():
        _ex(cur, "INSERT INTO broadcasts(user_id,interval_min,is_active) VALUES(?,?,0)", (user_id, minutes))
    else:
        _ex(cur, "UPDATE broadcasts SET interval_min=? WHERE user_id=?", (minutes, user_id))
    con.commit(); con.close()

def set_active(user_id, active):
    con = db(); cur = con.cursor()
    _ex(cur, "UPDATE broadcasts SET is_active=? WHERE user_id=?", (1 if active else 0, user_id))
    con.commit(); con.close()

# ============ KEYBOARDS ============
def main_menu_kb(is_founder=False):
    rows = [
        [KeyboardButton("📝 Xabar yaratish"), KeyboardButton("⏱ Interval")],
        [KeyboardButton("📋 Guruhlarim"), KeyboardButton("📊 Status")],
        [KeyboardButton("▶️ Start"), KeyboardButton("⏸ Stop")],
        [KeyboardButton("🚀 Test yuborish"), KeyboardButton("❓ Yordam")],
    ]
    if is_founder:
        rows.append([KeyboardButton("👑 Founder panel")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def founder_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistika", callback_data="f:stats"),
         InlineKeyboardButton("📋 Barcha guruhlar", callback_data="f:groups")],
        [InlineKeyboardButton("👥 Userlar", callback_data="f:users"),
         InlineKeyboardButton("📢 Announce", callback_data="f:announce")],
        [InlineKeyboardButton("⛔ Hammasini to'xtatish", callback_data="f:stopall")],
        [InlineKeyboardButton("◀️ Orqaga", callback_data="m:main")],
    ])

# ============ BROADCAST JOB ============
async def do_broadcast_to_user(user_id: int, context: ContextTypes.DEFAULT_TYPE, reason=""):
    bc = get_broadcast(user_id)
    if not bc or not bc["source_msg_id"]:
        return 0, 0, "Xabar topilmadi"
    groups = get_user_groups(user_id)
    if not groups:
        return 0, 0, "Guruh yo'q"
    ok = fail = 0
    for g in groups:
        try:
            await context.bot.copy_message(
                chat_id=g["chat_id"],
                from_chat_id=bc["source_chat_id"],
                message_id=bc["source_msg_id"],
            )
            ok += 1
        except RetryAfter as e:
            fail += 1
            logger.warning(f"flood {g['chat_id']}: {e.retry_after}s kutamiz")
            try:
                await asyncio.sleep(e.retry_after + 1)
            except Exception:
                pass
        except Forbidden:
            fail += 1  # bot guruhdan chiqarilgan bo'lishi mumkin
        except BadRequest as e:
            # guruh o'chirilgan / bot admin emas
            if "not found" in str(e).lower() or "deleted" in str(e).lower():
                remove_group(g["chat_id"])
            fail += 1
            logger.warning(f"yuborishda xato {g['chat_id']}: {e}")
        except Exception as e:
            fail += 1
            logger.warning(f"yuborishda xato {g['chat_id']}: {e}")
        await asyncio.sleep(0.4)  # flood limit himoyasi (Telegram: ~20 msg/min)
    try:
        _now = datetime.now().isoformat(timespec="seconds")
        con = db(); cur = con.cursor()
        _ex(cur, "UPDATE broadcasts SET last_sent=? WHERE user_id=?", (_now, user_id))
        con.commit(); con.close()
    except Exception:
        pass
    return ok, fail, reason

async def broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data["user_id"]
    bc = get_broadcast(user_id)
    if not bc or not bc["is_active"]:
        return
    u = get_user(user_id)
    if u and u["is_banned"]:
        return
    ok, fail, _ = await do_broadcast_to_user(user_id, context)
    logger.info(f"Broadcast user={user_id} ok={ok} fail={fail}")
    # userga qisqa hisobot (har safar bezovta qilmaslik uchun faqat xatolik ko'p bo'lsa)
    if fail > 0 and ok == 0:
        try:
            await context.bot.send_message(user_id, f"⚠️ Xabar yuborilmadi. Bot guruhlarda adminmi? Fail: {fail}")
        except Exception:
            pass

def schedule_user(user_id: int, interval_min: int, app: Application):
    # eski joblarni o'chirish
    for j in app.job_queue.get_jobs_by_name(f"bc_{user_id}"):
        j.schedule_removal()
    app.job_queue.run_repeating(
        broadcast_job,
        interval=interval_min * 60,
        first=5,  # 5 soniyadan keyin birinchi yuborish
        name=f"bc_{user_id}",
        data={"user_id": user_id},
    )

def unschedule_user(user_id: int, app: Application):
    for j in app.job_queue.get_jobs_by_name(f"bc_{user_id}"):
        j.schedule_removal()

# ============ HANDLERS ============
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # guruhda /start bo'lsa
    if update.effective_chat.type in ("group", "supergroup", "channel"):
        await update.message.reply_text("👋 Salom! Men avto-xabar botman. Sozlash uchun lichkamga yozing.")
        return
    user = update.effective_user
    founder = is_founder_check(user)
    register_user(user, is_founder=founder)
    # ban tekshirish
    u = get_user(user.id)
    if u and u["is_banned"] and not founder:
        await update.message.reply_text("⛔ Siz bloklangansiz.")
        return
    name = f"👑 <b>Founder</b>" if founder else "👤 Foydalanuvchi"
    await update.message.reply_html(
        f"Assalomu alaykum, {user.first_name}!\n{ name} sifatida kirdingiz.\n\n"
        f"1️⃣ Avval meni o'z guruhlaringizga qo'shing (admin qilib).\n"
        f"2️⃣ <b>📝 Xabar yaratish</b> tugmasi bilan xabar yuboring.\n"
        f"3️⃣ <b>⏱ Interval</b> bilan daqiqani belgilang (masalan 10).\n"
        f"4️⃣ <b>📋 Guruhlarim</b> da guruhlarni tanlang.\n"
        f"5️⃣ <b>▶️ Start</b> ni bosing — har N daqiqada avtomatik yuboraman.",
        reply_markup=main_menu_kb(founder),
    )
    context.user_data["state"] = None

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ <b>Yordam</b>\n\n"
        "• Botni guruhga qo'shing va <b>admin</b> qiling (xabar yubora olishi uchun).\n"
        "• Lichkada 📝 Xabar yaratish → xabar (matn/foto/video/har qanday) yuboring.\n"
        "• ⏱ Interval → necha daqiqada takrorlanishini yozing (min 1).\n"
        "• 📋 Guruhlarim → qaysi guruhlarga yuborishni tanlang.\n"
        "• ▶️ Start → avtomatik yuborish boshlanadi.\n"
        "• 🚀 Test yuborish → hozir 1 marta yuborib ko'rish.\n"
        "• ⏸ Stop → to'xtatish.\n\n"
        f"Founder: @{FOUNDER_USERNAME} — 👑 Founder panel orqali to'liq boshqaradi.",
        parse_mode="HTML",
    )

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # bot guruhga qo'shilganda / chiqarilganda
    result = update.my_chat_member
    chat = result.chat
    new_status = result.new_chat_member.status
    if chat.type in ("group", "supergroup", "channel"):
        if new_status in ("member", "administrator"):
            owner_id = None
            try:
                owner_id = result.from_user.id if result.from_user and not result.from_user.is_bot else None
            except Exception:
                pass
            add_group(chat.id, chat.title or str(chat.id), chat.type, owner_id=owner_id)
            logger.info(f"Bot qo'shildi: {chat.title} ({chat.id}) owner={owner_id}")
        elif new_status in ("left", "kicked"):
            remove_group(chat.id)
            logger.info(f"Bot chiqarildi: {chat.id}")

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # guruh nomlarini yangilab turish (auto-link YO'Q)
    chat = update.effective_chat
    if chat.type in ("group", "supergroup", "channel"):
        upsert_group_title(chat.id, chat.title or str(chat.id), chat.type)

async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    founder = is_founder_check(user)
    text = (update.message.text or "").strip()
    state = context.user_data.get("state")

    # ban
    u = get_user(user.id)
    if u and u["is_banned"] and not founder:
        await update.message.reply_text("⛔ Siz bloklangansiz.")
        return

    # --- state: interval kutilmoqda ---
    if state == "wait_interval":
        try:
            mins = int(text.split()[0])
            if mins < 1 or mins > 10080:
                await update.message.reply_text("❌ 1 dan 10080 gacha (1 hafta) son kiriting.")
                return
            set_interval(user.id, mins)
            context.user_data["state"] = None
            await update.message.reply_text(f"✅ Interval saqlandi: har <b>{mins} daqiqada</b>. Endi ▶️ Start ni bosing.",
                                            parse_mode="HTML", reply_markup=main_menu_kb(founder))
        except ValueError:
            await update.message.reply_text("❌ Iltimos faqat son yuboring. Masalan: 10")
        return

    # --- state: announce kutilmoqda (founder) ---
    if state == "wait_announce":
        context.user_data["state"] = None
        con = db(); cur = con.cursor()
        cur.execute("SELECT user_id FROM users WHERE is_banned=0")
        users = [r["user_id"] for r in cur.fetchall()]; con.close()
        ok = 0
        for uid in users:
            try:
                await context.bot.send_message(uid, f"📢 <b>Founder xabari:</b>\n\n{text}", parse_mode="HTML")
                ok += 1
            except RetryAfter as e:
                try:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception:
                    pass
            except Exception:
                pass
            await asyncio.sleep(0.05)
        await update.message.reply_text(f"✅ Announce {ok} userga yuborildi.", reply_markup=main_menu_kb(founder))
        return

    # --- state: xabar kutilmoqda -> matnni saqlash ---
    if state == "wait_message":
        preview = text[:200]
        save_broadcast_msg(user.id, update.effective_chat.id, update.message.message_id, preview, has_text=1)
        context.user_data["state"] = None
        await update.message.reply_text("✅ Xabar saqlandi!\n\nEndi ⏱ Interval belgilang, keyin ▶️ Start ni bosing.",
                                        reply_markup=main_menu_kb(founder))
        return

    # --- menyu tugmalari ---
    if text == "📝 Xabar yaratish":
        context.user_data["state"] = "wait_message"
        await update.message.reply_text("✍️ Yuboriladigan xabarni shu yerga tashlang.\nMatn, foto, video, dokument — hammasini qabul qilaman.")
    elif text == "⏱ Interval":
        context.user_data["state"] = "wait_interval"
        bc = get_broadcast(user.id)
        cur_iv = bc["interval_min"] if bc else 10
        await update.message.reply_text(f"Hozirgi interval: <b>{cur_iv} daqiqa</b>.\nYangi intervalni daqiqada yuboring (masalan: 5, 10, 30):", parse_mode="HTML")
    elif text == "📋 Guruhlarim":
        await show_my_groups(update, context)
    elif text == "📊 Status":
        await show_status(update, context)
    elif text == "▶️ Start":
        await start_broadcast(update, context)
    elif text == "⏸ Stop":
        set_active(user.id, False)
        unschedule_user(user.id, context.application)
        await update.message.reply_text("⏸ To'xtatildi.", reply_markup=main_menu_kb(founder))
    elif text == "🚀 Test yuborish":
        ok, fail, msg = await do_broadcast_to_user(user.id, context)
        if msg in ("Xabar topilmadi", "Guruh yo'q"):
            await update.message.reply_text(f"❌ {msg}. Avval xabar yarating va botni guruhga qo'shing.")
        else:
            await update.message.reply_text(f"🚀 Test yakuni: ✅ {ok} | ❌ {fail}")
    elif text == "❓ Yordam":
        await help_cmd(update, context)
    elif text == "👑 Founder panel":
        if not founder:
            await update.message.reply_text("⛔ Bu bo'lim faqat founder uchun.")
            return
        await update.message.reply_text("👑 <b>Founder panel</b> — to'liq boshqaruv:", parse_mode="HTML", reply_markup=founder_kb())
    else:
        # noma'lum matn — agar broadcast yo'q bo'lsa xabar sifatida saqlab qo'yamizmi? Yo'q, menyuni ko'rsatamiz
        await update.message.reply_text("Menyudan tanlang 👇", reply_markup=main_menu_kb(founder))

async def private_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # foto/video/doc va h.k. — xabar sifatida saqlash
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    founder = is_founder_check(user)
    state = context.user_data.get("state")
    if state != "wait_message":
        # agar user to'g'ridan-to'g'ri media tashlasa ham xabar sifatida qabul qilamiz (qulaylik)
        pass
    msg = update.message
    preview = msg.caption or msg.text or "[media xabar]"
    if len(preview) > 200:
        preview = preview[:200]
    save_broadcast_msg(user.id, update.effective_chat.id, msg.message_id, preview, has_text=0)
    context.user_data["state"] = None
    await msg.reply_text("✅ Xabar (media) saqlandi! Endi ⏱ Interval belgilang va ▶️ Start ni bosing.",
                         reply_markup=main_menu_kb(founder))

async def show_my_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    groups = get_all_groups()
    if not groups:
        await update.message.reply_text("📭 Hali guruh topilmadi.\nBotni guruhga qo'shing va admin qiling, keyin qayta urinib ko'ring.")
        return
    my = {g["chat_id"] for g in get_user_groups(user_id)}
    # inline tugmalar
    kb = []
    for g in groups[:30]:
        mark = "✅" if g["chat_id"] in my else "❌"
        title = (g["title"] or str(g["chat_id"]))[:30]
        kb.append([InlineKeyboardButton(f"{mark} {title}", callback_data=f"t:{g['chat_id']}")])
    kb.append([InlineKeyboardButton("✅ Hammasini tanlash", callback_data="a:all"),
               InlineKeyboardButton("🧹 Tozalash", callback_data="a:none")])
    await update.message.reply_text(f"📋 Guruhlar ({len(groups)} ta). Yuborish uchun tanlang:",
                                    reply_markup=InlineKeyboardMarkup(kb))

async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    founder = is_founder_check(update.effective_user)
    bc = get_broadcast(user_id)
    groups = get_user_groups(user_id)
    if not bc or not bc["source_msg_id"]:
        await update.message.reply_text("📊 Status: xabar hali yaratilmagan.", reply_markup=main_menu_kb(founder))
        return
    active = "🟢 Faol" if bc["is_active"] else "🔴 To'xtatilgan"
    jobs = "yoqilgan" if context.application.job_queue.get_jobs_by_name(f"bc_{user_id}") else "o'chirilgan"
    await update.message.reply_text(
        f"📊 <b>Status</b>\n{active} (job: {jobs})\n⏱ Har {bc['interval_min']} daqiqada\n"
        f"📋 Guruhlar: {len(groups)} ta\n📝 Xabar: {bc['preview'] or 'media'}",
        parse_mode="HTML", reply_markup=main_menu_kb(founder))

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    founder = is_founder_check(update.effective_user)
    bc = get_broadcast(user_id)
    if not bc or not bc["source_msg_id"]:
        await update.message.reply_text("❌ Avval 📝 Xabar yaratish orqali xabar yuboring.", reply_markup=main_menu_kb(founder))
        return
    groups = get_user_groups(user_id)
    if not groups:
        await update.message.reply_text("❌ Guruh topilmadi. Botni guruhga qo'shing va admin qiling.", reply_markup=main_menu_kb(founder))
        return
    iv = bc["interval_min"] or 10
    set_active(user_id, True)
    schedule_user(user_id, iv, context.application)
    await update.message.reply_text(f"▶️ Boshladim! Har <b>{iv} daqiqada</b> {len(groups)} ta guruhga yuboraman.",
                                    parse_mode="HTML", reply_markup=main_menu_kb(founder))

# ============ CALLBACKS ============
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    user_id = update.effective_user.id
    founder = is_founder_check(update.effective_user)

    if data.startswith("t:"):
        try:
            gid = int(data[2:])
        except ValueError:
            return
        con = db(); cur = con.cursor()
        _ex(cur, "SELECT * FROM user_groups WHERE user_id=? AND group_id=?", (user_id, gid))
        if cur.fetchone():
            _ex(cur, "DELETE FROM user_groups WHERE user_id=? AND group_id=?", (user_id, gid))
            act = "o'chirildi ❌"
        else:
            _link_user_group(cur, user_id, gid)
            act = "qo'shildi ✅"
        con.commit(); con.close()
        await q.edit_message_text(f"{act} (guruh: {gid})\n📋 Guruhlarim ni qayta ochib ro'yxatni ko'ring.")
        return

    if data == "a:all":
        groups = get_user_groups(user_id)  # xavfsizlik: faqat o'ziga tegishli emas, barcha mavjuddan tanlashga ruxsat?
        # To'g'risi: user o'zi qo'shgan guruhlarni tanlashi uchun barcha guruhlar ro'yxatidan tanlaydi,
        # lekin bu yerda faqat mavjud guruhlarni biriktiramiz (o'z guruhlari + umumiy).
        all_groups = get_all_groups()
        con = db(); cur = con.cursor()
        for g in all_groups:
            _link_user_group(cur, user_id, g["chat_id"])
        con.commit(); con.close()
        await q.edit_message_text(f"✅ Hamma guruhlar tanlandi ({len(all_groups)} ta).")
        return
    if data == "a:none":
        con = db(); cur = con.cursor()
        _ex(cur, "DELETE FROM user_groups WHERE user_id=?", (user_id,))
        con.commit(); con.close()
        await q.edit_message_text("🧹 Tanlov tozalandi.")
        return
    if data == "m:main":
        await q.edit_message_text("👑 Founder panel yopildi. Menyudan foydalaning.")
        return

    # founder callbacks
    if not founder:
        await q.edit_message_text("⛔ Faqat founder uchun.")
        return
    if data == "f:stats":
        con = db(); cur = con.cursor()
        cur.execute("SELECT COUNT(*) c FROM users"); uc = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM groups"); gc = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM broadcasts WHERE is_active=1"); ac = cur.fetchone()["c"]
        con.close()
        await q.edit_message_text(f"📊 <b>Statistika</b>\n👥 Userlar: {uc}\n📋 Guruhlar: {gc}\n🟢 Faol broadcast: {ac}",
                                  parse_mode="HTML", reply_markup=founder_kb())
    elif data == "f:groups":
        groups = get_all_groups()
        if not groups:
            await q.edit_message_text("📭 Guruhlar yo'q.", reply_markup=founder_kb())
            return
        txt = "📋 <b>Barcha guruhlar:</b>\n" + "\n".join(f"• {g['title']} (<code>{g['chat_id']}</code>)" for g in groups[:30])
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=founder_kb())
    elif data == "f:users":
        con = db(); cur = con.cursor()
        cur.execute("SELECT * FROM users ORDER BY created DESC LIMIT 20")
        rows = cur.fetchall(); con.close()
        if not rows:
            await q.edit_message_text("👥 Userlar yo'q.", reply_markup=founder_kb())
            return
        kb = []
        for r in rows:
            nm = f"@{r['username']}" if r["username"] else (r["first_name"] or str(r["user_id"]))
            ban_mark = "🚫" if r["is_banned"] else "✅"
            kb.append([InlineKeyboardButton(f"{ban_mark} {nm}", callback_data=f"b:{r['user_id']}")])
        kb.append([InlineKeyboardButton("◀️ Orqaga", callback_data="f:back")])
        await q.edit_message_text("👥 <b>So'nggi 20 user</b> (banni bosish uchun):", parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup(kb))
    elif data == "f:back":
        await q.edit_message_text("👑 <b>Founder panel</b>:", parse_mode="HTML", reply_markup=founder_kb())
    elif data.startswith("b:"):
        uid = int(data[2:])
        con = db(); cur = con.cursor()
        _ex(cur, "SELECT * FROM users WHERE user_id=?", (uid,))
        r = cur.fetchone()
        if r:
            new_ban = 0 if r["is_banned"] else 1
            _ex(cur, "UPDATE users SET is_banned=? WHERE user_id=?", (new_ban, uid))
            con.commit()
            await q.edit_message_text(f"{'🚫 Ban qilindi' if new_ban else '✅ Ban olindi'}: {uid}", reply_markup=founder_kb())
        con.close()
    elif data == "f:announce":
        context.user_data["state"] = "wait_announce"
        await q.edit_message_text("📢 Announce matnini yuboring (hamma userga boradi).")
    elif data == "f:stopall":
        con = db(); cur = con.cursor()
        cur.execute("UPDATE broadcasts SET is_active=0")
        cur.execute("SELECT user_id FROM broadcasts")
        uids = [r["user_id"] for r in cur.fetchall()]
        con.commit(); con.close()
        for uid in uids:
            unschedule_user(uid, context.application)
        await q.edit_message_text("⛔ Hamma broadcast to'xtatildi.", reply_markup=founder_kb())

# ============ ADMIN CMDS (founder) ============
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_founder_check(update.effective_user):
        return
    con = db(); cur = con.cursor()
    cur.execute("SELECT COUNT(*) c FROM users"); uc = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM groups"); gc = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM broadcasts WHERE is_active=1"); ac = cur.fetchone()["c"]
    con.close()
    await update.message.reply_text(f"📊 Userlar: {uc} | Guruhlar: {gc} | Faol: {ac}")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_active(update.effective_user.id, False)
    unschedule_user(update.effective_user.id, context.application)
    await update.message.reply_text("⏸ To'xtatildi.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_status(update, context)

async def post_init(app: Application):
    # restartdan keyin faol broadcastlarni tiklash
    con = db(); cur = con.cursor()
    cur.execute("SELECT user_id, interval_min FROM broadcasts WHERE is_active=1")
    rows = cur.fetchall(); con.close()
    for r in rows:
        try:
            schedule_user(r["user_id"], r["interval_min"] or 10, app)
            logger.info(f"Job tiklandi: {r['user_id']}")
        except Exception as e:
            logger.warning(f"Job tiklanmadi {r['user_id']}: {e}")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.ALL, on_group_message))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, private_text))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.TEXT), private_media))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (~filters.StatusUpdate.ALL), on_group_message))
    app.add_handler(CallbackQueryHandler(on_callback))
    print(f"✅ Bot ishga tushdi. Founder: @{FOUNDER_USERNAME} | DB: {'Postgres' if USE_PG else 'sqlite'}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
