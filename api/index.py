"""Vercel webhook: POST /api/index <- Telegram update."""
import os
import sys
import json
from http.server import BaseHTTPRequestHandler
from datetime import datetime

sys.path.append(os.path.dirname(__file__))
import _store as S
import _tg as T


def is_founder(tg_user):
    if tg_user and (tg_user.get("username") or "").lower() == S.FOUNDER_USERNAME.lower():
        return True
    if tg_user:
        con = S.db(); cur = con.cursor()
        cur.execute("SELECT is_founder FROM users WHERE user_id=?", (tg_user["id"],))
        r = cur.fetchone(); con.close()
        if r and r["is_founder"]:
            return True
    return False


def ensure_user(tu, founder=False):
    con = S.db(); cur = con.cursor()
    now = datetime.now().isoformat(timespec="seconds")
    cur.execute("SELECT * FROM users WHERE user_id=?", (tu["id"],))
    ex = cur.fetchone()
    if ex:
        cur.execute("UPDATE users SET username=?, first_name=?, is_founder=? WHERE user_id=?",
                    (tu.get("username") or "", tu.get("first_name") or "",
                     1 if (founder or ex["is_founder"]) else 0, tu["id"]))
    else:
        cur.execute("INSERT INTO users(user_id,username,first_name,is_founder,created) VALUES(?,?,?,?,?)",
                    (tu["id"], tu.get("username") or "", tu.get("first_name") or "",
                     1 if founder else 0, now))
        cur.execute("SELECT chat_id FROM groups")
        for g in cur.fetchall():
            cur.execute("INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES(?,?)",
                        (tu["id"], g["chat_id"]))
    con.commit(); con.close()


def do_broadcast(uid):
    con = S.db(); cur = con.cursor()
    cur.execute("SELECT * FROM broadcasts WHERE user_id=?", (uid,))
    bc = cur.fetchone()
    cur.execute("""SELECT g.chat_id FROM user_groups ug JOIN groups g ON g.chat_id=ug.group_id
                   WHERE ug.user_id=?""", (uid,))
    groups = cur.fetchall()
    con.close()
    if not bc or not bc["source_msg_id"] or not groups:
        return 0, 0
    ok = fail = 0
    for g in groups:
        r = T.copy_message(g["chat_id"], bc["source_chat_id"], bc["source_msg_id"])
        if r.get("ok"):
            ok += 1
        else:
            fail += 1
    con = S.db(); cur = con.cursor()
    cur.execute("UPDATE broadcasts SET last_sent=? WHERE user_id=?",
                (datetime.now().isoformat(timespec="seconds"), uid))
    con.commit(); con.close()
    return ok, fail


def handle(update):
    S.init_db()
    # 1) bot guruhga qo'shilganda
    if "my_chat_member" in update:
        m = update["my_chat_member"]
        chat = m["chat"]
        st = m["new_chat_member"]["status"]
        if chat["type"] in ("group", "supergroup", "channel"):
            con = S.db(); cur = con.cursor()
            if st in ("member", "administrator"):
                cur.execute("INSERT OR REPLACE INTO groups(chat_id,title,gtype,added_at) VALUES(?,?,?,?)",
                            (chat["id"], chat.get("title") or str(chat["id"]), chat["type"],
                             datetime.now().isoformat(timespec="seconds")))
                cur.execute("SELECT user_id FROM users WHERE is_banned=0")
                for u in cur.fetchall():
                    cur.execute("INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES(?,?)",
                                (u["user_id"], chat["id"]))
            else:
                cur.execute("DELETE FROM groups WHERE chat_id=?", (chat["id"],))
                cur.execute("DELETE FROM user_groups WHERE group_id=?", (chat["id"],))
            con.commit(); con.close()
        return

    # 2) callback (inline tugmalar)
    if "callback_query" in update:
        q = update["callback_query"]
        uid = q["from"]["id"]
        data = q.get("data", "")
        T.api_call("answerCallbackQuery", {"callback_query_id": q["id"]})
        if data == "f:stats":
            con = S.db(); cur = con.cursor()
            cur.execute("SELECT COUNT(*) c FROM users"); uc = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) c FROM groups"); gc = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) c FROM broadcasts WHERE is_active=1"); ac = cur.fetchone()["c"]
            con.close()
            T.api_call("editMessageText", {"chat_id": uid, "message_id": q["message"]["message_id"],
                "text": f"📊 Userlar: {uc} | Guruhlar: {gc} | Faol: {ac}", "reply_markup": T.founder_inline()})
        elif data == "f:stopall":
            con = S.db(); cur = con.cursor()
            cur.execute("UPDATE broadcasts SET is_active=0"); con.commit(); con.close()
            T.api_call("editMessageText", {"chat_id": uid, "message_id": q["message"]["message_id"],
                "text": "⛔ Hamma broadcast to'xtatildi.", "reply_markup": T.founder_inline()})
        elif data.startswith("t:"):
            gid = int(data[2:])
            con = S.db(); cur = con.cursor()
            cur.execute("SELECT * FROM user_groups WHERE user_id=? AND group_id=?", (uid, gid))
            if cur.fetchone():
                cur.execute("DELETE FROM user_groups WHERE user_id=? AND group_id=?", (uid, gid))
                txt = f"o'chirildi ❌ ({gid})"
            else:
                cur.execute("INSERT INTO user_groups(user_id,group_id) VALUES(?,?)", (uid, gid))
                txt = f"qo'shildi ✅ ({gid})"
            con.commit(); con.close()
            T.api_call("editMessageText", {"chat_id": uid, "message_id": q["message"]["message_id"], "text": txt})
        return

    if "message" not in update:
        return
    msg = update["message"]
    chat = msg["chat"]
    tu = msg.get("from", {})

    # guruhdagi xabar — nomni yangilash
    if chat["type"] in ("group", "supergroup", "channel"):
        con = S.db(); cur = con.cursor()
        cur.execute("INSERT OR REPLACE INTO groups(chat_id,title,gtype,added_at) VALUES(?,?,?,?)",
                    (chat["id"], chat.get("title") or str(chat["id"]), chat["type"],
                     datetime.now().isoformat(timespec="seconds")))
        con.commit(); con.close()
        if msg.get("text", "").startswith("/start"):
            T.send_message(chat["id"], "👋 Salom! Sozlash uchun lichkamga yozing.")
        return

    # lichka
    uid = tu["id"]
    founder = is_founder(tu)
    ensure_user(tu, founder)
    text = (msg.get("text") or "").strip()
    state = S.get_state(uid)

    if state == "wait_interval" and text:
        try:
            mins = int(text.split()[0])
            assert 1 <= mins <= 10080
            con = S.db(); cur = con.cursor()
            cur.execute("SELECT * FROM broadcasts WHERE user_id=?", (uid,))
            if cur.fetchone():
                cur.execute("UPDATE broadcasts SET interval_min=? WHERE user_id=?", (mins, uid))
            else:
                cur.execute("INSERT INTO broadcasts(user_id,interval_min,is_active) VALUES(?,?,0)", (uid, mins))
            con.commit(); con.close()
            S.set_state(uid, None)
            T.send_message(uid, f"✅ Interval: har <b>{mins} daqiqada</b>. ▶️ Start ni bosing.",
                           reply_markup=T.main_menu(founder))
        except Exception:
            T.send_message(uid, "❌ 1-10080 oralig'ida son yuboring. Masalan: 10")
        return

    if state == "wait_message" and (text or msg.get("photo") or msg.get("video") or msg.get("document")):
        preview = (text or msg.get("caption") or "[media]")[:200]
        con = S.db(); cur = con.cursor()
        cur.execute("SELECT * FROM broadcasts WHERE user_id=?", (uid,))
        if cur.fetchone():
            cur.execute("""UPDATE broadcasts SET source_chat_id=?, source_msg_id=?, preview=?
                           WHERE user_id=?""", (chat["id"], msg["message_id"], preview, uid))
        else:
            cur.execute("""INSERT INTO broadcasts(user_id,source_chat_id,source_msg_id,preview,interval_min,is_active)
                           VALUES(?,?,?,?,10,0)""", (uid, chat["id"], msg["message_id"], preview))
        con.commit(); con.close()
        S.set_state(uid, None)
        T.send_message(uid, "✅ Xabar saqlandi! ⏱ Interval belgilang, keyin ▶️ Start.",
                       reply_markup=T.main_menu(founder))
        return

    if text == "/start":
        T.send_message(uid,
            f"Assalomu alaykum, {tu.get('first_name','')}!\n"
            f"{'👑 <b>Founder</b>' if founder else '👤 Foydalanuvchi'} sifatida kirdingiz.\n\n"
            "1️⃣ Meni guruhga qo'shing (admin).\n2️⃣ 📝 Xabar yaratish\n3️⃣ ⏱ Interval\n4️⃣ ▶️ Start",
            reply_markup=T.main_menu(founder))
    elif text == "📝 Xabar yaratish":
        S.set_state(uid, "wait_message")
        T.send_message(uid, "✍️ Yuboriladigan xabarni shu yerga tashlang.")
    elif text == "⏱ Interval":
        S.set_state(uid, "wait_interval")
        T.send_message(uid, "Daqiqada yuboring (masalan: 10). Min 1.")
    elif text == "▶️ Start":
        con = S.db(); cur = con.cursor()
        cur.execute("SELECT * FROM broadcasts WHERE user_id=?", (uid,))
        bc = cur.fetchone()
        if not bc or not bc["source_msg_id"]:
            T.send_message(uid, "❌ Avval 📝 Xabar yaratish.")
        else:
            cur.execute("UPDATE broadcasts SET is_active=1, last_sent=? WHERE user_id=?",
                        (datetime.now().isoformat(timespec="seconds"), uid))
            T.send_message(uid, f"▶️ Boshladim! Har {bc['interval_min']} daqiqada yuboraman (Vercel cron orqali).",
                           reply_markup=T.main_menu(founder))
        con.commit(); con.close()
    elif text == "⏸ Stop":
        con = S.db(); cur = con.cursor()
        cur.execute("UPDATE broadcasts SET is_active=0 WHERE user_id=?", (uid,))
        con.commit(); con.close()
        T.send_message(uid, "⏸ To'xtatildi.", reply_markup=T.main_menu(founder))
    elif text == "🚀 Test yuborish":
        ok, fail = do_broadcast(uid)
        T.send_message(uid, f"🚀 Test: ✅ {ok} | ❌ {fail}")
    elif text == "📊 Status":
        con = S.db(); cur = con.cursor()
        cur.execute("SELECT * FROM broadcasts WHERE user_id=?", (uid,))
        bc = cur.fetchone()
        cur.execute("SELECT COUNT(*) c FROM user_groups WHERE user_id=?", (uid,))
        gc = cur.fetchone()["c"]
        con.close()
        if not bc or not bc["source_msg_id"]:
            T.send_message(uid, "📊 Xabar hali yaratilmagan.", reply_markup=T.main_menu(founder))
        else:
            T.send_message(uid, f"📊 {'🟢 Faol' if bc['is_active'] else '🔴 Stop'} | Har {bc['interval_min']} min | Guruh: {gc}",
                           reply_markup=T.main_menu(founder))
    elif text == "📋 Guruhlarim":
        con = S.db(); cur = con.cursor()
        cur.execute("SELECT * FROM groups LIMIT 30"); groups = cur.fetchall()
        cur.execute("SELECT group_id FROM user_groups WHERE user_id=?", (uid,))
        my = {r["group_id"] for r in cur.fetchall()}
        con.close()
        if not groups:
            T.send_message(uid, "📭 Guruh yo'q. Botni guruhga qo'shing.")
        else:
            kb = [[{"text": f"{'✅' if g['chat_id'] in my else '❌'} {(g['title'] or '')[:25]}",
                    "callback_data": f"t:{g['chat_id']}"}] for g in groups]
            T.send_message(uid, f"📋 Guruhlar ({len(groups)}):", reply_markup={"inline_keyboard": kb})
    elif text == "👑 Founder panel":
        if founder:
            T.send_message(uid, "👑 <b>Founder panel</b>:", reply_markup=T.founder_inline())
        else:
            T.send_message(uid, "⛔ Faqat founder.")
    elif text == "❓ Yordam":
        T.send_message(uid, "Botni guruhga admin qiling → 📝 Xabar → ⏱ Interval → ▶️ Start.\nVercel'da yuborish har daqiqalik cron orqali ishlaydi.")
    elif text and not text.startswith("/"):
        # to'g'ridan media/matn tashlansa xabar sifatida saqlash
        if msg.get("photo") or msg.get("video") or msg.get("document") or len(text) > 1:
            preview = (text or msg.get("caption") or "[media]")[:200]
            con = S.db(); cur = con.cursor()
            cur.execute("SELECT * FROM broadcasts WHERE user_id=?", (uid,))
            if cur.fetchone():
                cur.execute("UPDATE broadcasts SET source_chat_id=?, source_msg_id=?, preview=? WHERE user_id=?",
                            (chat["id"], msg["message_id"], preview, uid))
            else:
                cur.execute("""INSERT INTO broadcasts(user_id,source_chat_id,source_msg_id,preview,interval_min,is_active)
                               VALUES(?,?,?,?,10,0)""", (uid, chat["id"], msg["message_id"], preview))
            con.commit(); con.close()
            T.send_message(uid, "✅ Xabar saqlandi!", reply_markup=T.main_menu(founder))


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true,"service":"ad-post-bot webhook"}')

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            update = json.loads(body.decode() or "{}")
            handle(update)
        except Exception as e:
            print("handler xato:", e)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')
