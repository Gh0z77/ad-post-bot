"""Vercel webhook: POST /api/index <- Telegram update."""
import os
import sys
import json
import time
from http.server import BaseHTTPRequestHandler
from datetime import datetime

sys.path.append(os.path.dirname(__file__))
import _store as S
import _tg as T


def is_founder(tg_user):
    if not tg_user:
        return False
    if tg_user.get("id") in S.ADMIN_IDS:
        return True
    if (tg_user.get("username") or "").lower() == S.FOUNDER_USERNAME.lower():
        return True
    con = S.db(); cur = con.cursor()
    S._ex(cur, "SELECT is_founder FROM users WHERE user_id=?", (tg_user["id"],))
    r = cur.fetchone(); con.close()
    if r and r["is_founder"]:
        return True
    return False


def is_banned(uid):
    con = S.db(); cur = con.cursor()
    S._ex(cur, "SELECT is_banned FROM users WHERE user_id=?", (uid,))
    r = cur.fetchone(); con.close()
    return bool(r and r["is_banned"])


def ensure_user(tu, founder=False):
    con = S.db(); cur = con.cursor()
    now = datetime.now().isoformat(timespec="seconds")
    S._ex(cur, "SELECT * FROM users WHERE user_id=?", (tu["id"],))
    ex = cur.fetchone()
    if ex:
        S._ex(cur, "UPDATE users SET username=?, first_name=?, is_founder=? WHERE user_id=?",
              (tu.get("username") or "", tu.get("first_name") or "",
               1 if (founder or ex["is_founder"]) else 0, tu["id"]))
    else:
        S._ex(cur, "INSERT INTO users(user_id,username,first_name,is_founder,created) VALUES(?,?,?,?,?)",
              (tu["id"], tu.get("username") or "", tu.get("first_name") or "",
               1 if founder else 0, now))
        # XAVFSIZLIK: begona guruhlarni auto-biriktirmaymiz
    con.commit(); con.close()


def do_broadcast(uid):
    con = S.db(); cur = con.cursor()
    S._ex(cur, "SELECT * FROM broadcasts WHERE user_id=?", (uid,))
    bc = cur.fetchone()
    S._ex(cur, """SELECT g.chat_id FROM user_groups ug JOIN groups g ON g.chat_id=ug.group_id
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
            desc = str(r).lower()
            if "not found" in desc or "deleted" in desc or "kicked" in desc:
                try:
                    con2 = S.db(); cur2 = con2.cursor()
                    S._ex(cur2, "DELETE FROM groups WHERE chat_id=?", (g["chat_id"],))
                    S._ex(cur2, "DELETE FROM user_groups WHERE group_id=?", (g["chat_id"],))
                    con2.commit(); con2.close()
                except Exception:
                    pass
        time.sleep(0.1)
    con = S.db(); cur = con.cursor()
    S._ex(cur, "UPDATE broadcasts SET last_sent=? WHERE user_id=?",
          (datetime.now().isoformat(timespec="seconds"), uid))
    con.commit(); con.close()
    return ok, fail


def founder_stats_text():
    con = S.db(); cur = con.cursor()
    cur.execute("SELECT COUNT(*) c FROM users"); uc = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM groups"); gc = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM broadcasts WHERE is_active=1"); ac = cur.fetchone()["c"]
    con.close()
    return f"📊 <b>Statistika</b>\n👥 Userlar: {uc}\n📋 Guruhlar: {gc}\n🟢 Faol broadcast: {ac}"


def handle(update):
    S.init_db()
    # 1) bot guruhga qo'shilganda
    if "my_chat_member" in update:
        m = update["my_chat_member"]
        chat = m["chat"]
        st = m["new_chat_member"]["status"]
        if chat["type"] in ("group", "supergroup", "channel"):
            con = S.db(); cur = con.cursor()
            now = datetime.now().isoformat(timespec="seconds")
            if st in ("member", "administrator"):
                S.upsert_group(cur, chat["id"], chat.get("title") or str(chat["id"]), chat["type"], now)
                owner = (m.get("from") or {})
                owner_id = owner.get("id")
                if owner_id and not owner.get("is_bot"):
                    try:
                        S.link_user_group(cur, owner_id, chat["id"])
                    except Exception:
                        pass
            else:
                S._ex(cur, "DELETE FROM groups WHERE chat_id=?", (chat["id"],))
                S._ex(cur, "DELETE FROM user_groups WHERE group_id=?", (chat["id"],))
            con.commit(); con.close()
        return

    # 2) callback (inline tugmalar)
    if "callback_query" in update:
        q = update["callback_query"]
        uid = q["from"]["id"]
        data = q.get("data", "")
        msg_id = q["message"]["message_id"]
        T.api_call("answerCallbackQuery", {"callback_query_id": q["id"]})
        founder = is_founder(q["from"])

        if data.startswith("t:"):
            try:
                gid = int(data[2:])
            except ValueError:
                return
            con = S.db(); cur = con.cursor()
            S._ex(cur, "SELECT * FROM user_groups WHERE user_id=? AND group_id=?", (uid, gid))
            if cur.fetchone():
                S._ex(cur, "DELETE FROM user_groups WHERE user_id=? AND group_id=?", (uid, gid))
                txt = f"o'chirildi ❌ ({gid})"
            else:
                S.link_user_group(cur, uid, gid)
                txt = f"qo'shildi ✅ ({gid})"
            con.commit(); con.close()
            T.api_call("editMessageText", {"chat_id": uid, "message_id": msg_id, "text": txt})
            return
        if data == "a:all":
            con = S.db(); cur = con.cursor()
            cur.execute("SELECT chat_id FROM groups")
            n = 0
            for g in cur.fetchall():
                S.link_user_group(cur, uid, g["chat_id"])
                n += 1
            con.commit(); con.close()
            T.api_call("editMessageText", {"chat_id": uid, "message_id": msg_id,
                                           "text": f"✅ Hamma guruhlar tanlandi ({n} ta)."})
            return
        if data == "a:none":
            con = S.db(); cur = con.cursor()
            S._ex(cur, "DELETE FROM user_groups WHERE user_id=?", (uid,))
            con.commit(); con.close()
            T.api_call("editMessageText", {"chat_id": uid, "message_id": msg_id, "text": "🧹 Tanlov tozalandi."})
            return
        if data == "m:main":
            T.api_call("editMessageText", {"chat_id": uid, "message_id": msg_id,
                                           "text": "👑 Founder panel yopildi. Menyudan foydalaning."})
            return
        if data == "f:back":
            T.api_call("editMessageText", {"chat_id": uid, "message_id": msg_id,
                                           "text": "👑 <b>Founder panel</b>:", "parse_mode": "HTML",
                                           "reply_markup": T.founder_inline()})
            return

        if not founder:
            T.api_call("editMessageText", {"chat_id": uid, "message_id": msg_id, "text": "⛔ Faqat founder uchun."})
            return
        if data == "f:stats":
            T.api_call("editMessageText", {"chat_id": uid, "message_id": msg_id,
                "text": founder_stats_text(), "parse_mode": "HTML", "reply_markup": T.founder_inline()})
        elif data == "f:groups":
            con = S.db(); cur = con.cursor()
            cur.execute("SELECT * FROM groups ORDER BY added_at DESC LIMIT 30")
            groups = cur.fetchall(); con.close()
            if not groups:
                txt = "📭 Guruhlar yo'q."
            else:
                txt = "📋 <b>Barcha guruhlar:</b>\n" + "\n".join(
                    f"• {g['title']} (<code>{g['chat_id']}</code>)" for g in groups)
            T.api_call("editMessageText", {"chat_id": uid, "message_id": msg_id,
                "text": txt, "parse_mode": "HTML", "reply_markup": T.founder_inline()})
        elif data == "f:users":
            con = S.db(); cur = con.cursor()
            cur.execute("SELECT * FROM users ORDER BY created DESC LIMIT 20")
            rows = cur.fetchall(); con.close()
            if not rows:
                T.api_call("editMessageText", {"chat_id": uid, "message_id": msg_id, "text": "👥 Userlar yo'q.",
                                               "reply_markup": T.founder_inline()})
            else:
                kb = []
                for r in rows:
                    nm = f"@{r['username']}" if r["username"] else (r["first_name"] or str(r["user_id"]))
                    mark = "🚫" if r["is_banned"] else "✅"
                    kb.append([{"text": f"{mark} {nm}", "callback_data": f"b:{r['user_id']}"}])
                kb.append([{"text": "◀️ Orqaga", "callback_data": "f:back"}])
                T.api_call("editMessageText", {"chat_id": uid, "message_id": msg_id,
                    "text": "👥 <b>So'nggi 20 user</b> (banni bosish uchun):", "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": kb}})
        elif data.startswith("b:"):
            try:
                target = int(data[2:])
            except ValueError:
                return
            con = S.db(); cur = con.cursor()
            S._ex(cur, "SELECT * FROM users WHERE user_id=?", (target,))
            r = cur.fetchone()
            if r:
                new_ban = 0 if r["is_banned"] else 1
                S._ex(cur, "UPDATE users SET is_banned=? WHERE user_id=?", (new_ban, target))
                con.commit()
                T.api_call("editMessageText", {"chat_id": uid, "message_id": msg_id,
                    "text": f"{'🚫 Ban qilindi' if new_ban else '✅ Ban olindi'}: {target}",
                    "reply_markup": T.founder_inline()})
            con.close()
        elif data == "f:announce":
            S.set_state(uid, "wait_announce")
            T.api_call("editMessageText", {"chat_id": uid, "message_id": msg_id,
                "text": "📢 Announce matnini yuboring (hamma userga boradi)."})
        elif data == "f:stopall":
            con = S.db(); cur = con.cursor()
            cur.execute("UPDATE broadcasts SET is_active=0"); con.commit(); con.close()
            T.api_call("editMessageText", {"chat_id": uid, "message_id": msg_id,
                "text": "⛔ Hamma broadcast to'xtatildi.", "reply_markup": T.founder_inline()})
        return

    if "message" not in update:
        return
    msg = update["message"]
    chat = msg["chat"]
    tu = msg.get("from", {})

    # guruhdagi xabar — faqat nomni yangilash
    if chat["type"] in ("group", "supergroup", "channel"):
        con = S.db(); cur = con.cursor()
        S.upsert_group(cur, chat["id"], chat.get("title") or str(chat["id"]), chat["type"],
                       datetime.now().isoformat(timespec="seconds"))
        con.commit(); con.close()
        if msg.get("text", "").startswith("/start"):
            T.send_message(chat["id"], "👋 Salom! Sozlash uchun lichkamga yozing.")
        return

    # lichka
    uid = tu["id"]
    founder = is_founder(tu)
    ensure_user(tu, founder)
    if is_banned(uid) and not founder:
        T.send_message(uid, "⛔ Siz bloklangansiz.")
        return
    text = (msg.get("text") or "").strip()
    state = S.get_state(uid)

    if state == "wait_announce" and founder and text:
        S.set_state(uid, None)
        con = S.db(); cur = con.cursor()
        cur.execute("SELECT user_id FROM users WHERE is_banned=0")
        users = [r["user_id"] for r in cur.fetchall()]; con.close()
        ok = 0
        for u in users:
            r = T.send_message(u, f"📢 <b>Founder xabari:</b>\n\n{text}")
            if r.get("ok"):
                ok += 1
            time.sleep(0.05)
        T.send_message(uid, f"✅ Announce {ok} userga yuborildi.", reply_markup=T.main_menu(founder))
        return

    if state == "wait_interval" and text:
        try:
            mins = int(text.split()[0])
            assert 1 <= mins <= 10080
            con = S.db(); cur = con.cursor()
            S._ex(cur, "SELECT * FROM broadcasts WHERE user_id=?", (uid,))
            if cur.fetchone():
                S._ex(cur, "UPDATE broadcasts SET interval_min=? WHERE user_id=?", (mins, uid))
            else:
                S._ex(cur, "INSERT INTO broadcasts(user_id,interval_min,is_active) VALUES(?,?,0)", (uid, mins))
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
        S._ex(cur, "SELECT * FROM broadcasts WHERE user_id=?", (uid,))
        if cur.fetchone():
            S._ex(cur, """UPDATE broadcasts SET source_chat_id=?, source_msg_id=?, preview=?
                           WHERE user_id=?""", (chat["id"], msg["message_id"], preview, uid))
        else:
            S._ex(cur, """INSERT INTO broadcasts(user_id,source_chat_id,source_msg_id,preview,interval_min,is_active)
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
            "1️⃣ Meni guruhga qo'shing (admin).\n2️⃣ 📝 Xabar yaratish\n3️⃣ ⏱ Interval\n4️⃣ 📋 Guruhlarim → tanlash\n5️⃣ ▶️ Start",
            reply_markup=T.main_menu(founder))
    elif text == "📝 Xabar yaratish":
        S.set_state(uid, "wait_message")
        T.send_message(uid, "✍️ Yuboriladigan xabarni shu yerga tashlang.\nMatn, foto, video, dokument — hammasini qabul qilaman.")
    elif text == "⏱ Interval":
        S.set_state(uid, "wait_interval")
        con = S.db(); cur = con.cursor()
        S._ex(cur, "SELECT interval_min FROM broadcasts WHERE user_id=?", (uid,))
        r = cur.fetchone(); con.close()
        cur_iv = r["interval_min"] if r else 10
        T.send_message(uid, f"Hozirgi: <b>{cur_iv} daqiqa</b>.\nYangi intervalni daqiqada yuboring (masalan: 5, 10, 30):")
    elif text == "▶️ Start":
        con = S.db(); cur = con.cursor()
        S._ex(cur, "SELECT * FROM broadcasts WHERE user_id=?", (uid,))
        bc = cur.fetchone()
        if not bc or not bc["source_msg_id"]:
            T.send_message(uid, "❌ Avval 📝 Xabar yaratish.")
        else:
            S._ex(cur, "UPDATE broadcasts SET is_active=1, last_sent=? WHERE user_id=?",
                  (datetime.now().isoformat(timespec="seconds"), uid))
            T.send_message(uid, f"▶️ Boshladim! Har {bc['interval_min']} daqiqada yuboraman (Vercel cron orqali).",
                           reply_markup=T.main_menu(founder))
        con.commit(); con.close()
    elif text == "⏸ Stop":
        con = S.db(); cur = con.cursor()
        S._ex(cur, "UPDATE broadcasts SET is_active=0 WHERE user_id=?", (uid,))
        con.commit(); con.close()
        T.send_message(uid, "⏸ To'xtatildi.", reply_markup=T.main_menu(founder))
    elif text == "🚀 Test yuborish":
        ok, fail = do_broadcast(uid)
        T.send_message(uid, f"🚀 Test: ✅ {ok} | ❌ {fail}")
    elif text == "📊 Status":
        con = S.db(); cur = con.cursor()
        S._ex(cur, "SELECT * FROM broadcasts WHERE user_id=?", (uid,))
        bc = cur.fetchone()
        S._ex(cur, "SELECT COUNT(*) c FROM user_groups WHERE user_id=?", (uid,))
        gc = cur.fetchone()["c"]
        con.close()
        if not bc or not bc["source_msg_id"]:
            T.send_message(uid, "📊 Xabar hali yaratilmagan.", reply_markup=T.main_menu(founder))
        else:
            T.send_message(uid, f"📊 {'🟢 Faol' if bc['is_active'] else '🔴 Stop'} | Har {bc['interval_min']} min | Guruh: {gc}",
                           reply_markup=T.main_menu(founder))
    elif text == "📋 Guruhlarim":
        con = S.db(); cur = con.cursor()
        cur.execute("SELECT * FROM groups ORDER BY added_at DESC LIMIT 30"); groups = cur.fetchall()
        S._ex(cur, "SELECT group_id FROM user_groups WHERE user_id=?", (uid,))
        my = {r["group_id"] for r in cur.fetchall()}
        con.close()
        if not groups:
            T.send_message(uid, "📭 Guruh yo'q. Botni guruhga qo'shing (o'zingiz qo'shganingiz sizga biriktiriladi).")
        else:
            kb = [[{"text": f"{'✅' if g['chat_id'] in my else '❌'} {(g['title'] or '')[:25]}",
                    "callback_data": f"t:{g['chat_id']}"}] for g in groups]
            kb.append([{"text": "✅ Hammasini tanlash", "callback_data": "a:all"},
                       {"text": "🧹 Tozalash", "callback_data": "a:none"}])
            T.send_message(uid, f"📋 Guruhlar ({len(groups)}):", reply_markup={"inline_keyboard": kb})
    elif text == "👑 Founder panel":
        if founder:
            T.send_message(uid, "👑 <b>Founder panel</b>:", reply_markup=T.founder_inline())
        else:
            T.send_message(uid, "⛔ Faqat founder.")
    elif text == "❓ Yordam":
        T.send_message(uid, "Botni guruhga admin qiling → 📝 Xabar → ⏱ Interval → 📋 Guruhlarim → ▶️ Start.\nVercel'da yuborish har daqiqalik cron orqali ishlaydi.")
    elif text and not text.startswith("/"):
        if msg.get("photo") or msg.get("video") or msg.get("document") or len(text) > 1:
            preview = (text or msg.get("caption") or "[media]")[:200]
            con = S.db(); cur = con.cursor()
            S._ex(cur, "SELECT * FROM broadcasts WHERE user_id=?", (uid,))
            if cur.fetchone():
                S._ex(cur, "UPDATE broadcasts SET source_chat_id=?, source_msg_id=?, preview=? WHERE user_id=?",
                      (chat["id"], msg["message_id"], preview, uid))
            else:
                S._ex(cur, """INSERT INTO broadcasts(user_id,source_chat_id,source_msg_id,preview,interval_min,is_active)
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
