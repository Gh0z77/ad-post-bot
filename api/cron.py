"""Vercel Cron: GET /api/cron — har daqiqada chaqiriladi, vaqti kelgan broadcastlarni yuboradi."""
import os
import sys
from http.server import BaseHTTPRequestHandler
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(__file__))
import _store as S
import _tg as T


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Vercel Hobby: ichki cron faqat kuniga 1 marta. Har daqiqalik
        # yuborish uchun cron-job.org dan tashqi ping keladi:
        #   https://<domeyn>/api/cron?secret=CRON_SECRET (har 1 daqiqada)
        # CRON_SECRET Vercel Env da bo'sh bo'lsa — himoya yo'q (har kim chaqira oladi,
        # lekin cron faqat vaqti kelganlarni yuboradi, xavfi kam).
        try:
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            need = os.getenv("CRON_SECRET", "").strip()
            if need and q.get("secret", [""])[0] != need:
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":false,"error":"bad secret"}')
                return
        except Exception:
            pass
        try:
            S.init_db()
            if S.IS_EPHEMERAL:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":false,"sent":0,"error":"ephemeral db: set DATABASE_URL (Neon/Supabase) in Vercel Env"}')
                return
            con = S.db(); cur = con.cursor()
            cur.execute("SELECT * FROM broadcasts WHERE is_active=1")
            rows = cur.fetchall()
            con.close()
            sent = 0
            now = datetime.now()
            for bc in rows:
                iv = bc["interval_min"] or 10
                last = bc["last_sent"]
                due = True
                if last:
                    try:
                        due = now - datetime.fromisoformat(last) >= timedelta(minutes=iv)
                    except Exception:
                        due = True
                if not due:
                    continue
                if not (bc["source_msg_id"] or str(S.bc_val(bc, "web_text", "")).strip()):
                    continue
                uid = bc["user_id"]
                con2 = S.db(); cur2 = con2.cursor()
                S._ex(cur2, """SELECT g.chat_id FROM user_groups ug JOIN groups g ON g.chat_id=ug.group_id
                                WHERE ug.user_id=?""", (uid,))
                groups = cur2.fetchall()
                con2.close()
                web_text = str(S.bc_val(bc, "web_text", "") or "").strip()
                for g in groups:
                    if web_text:
                        r = T.send_message(g["chat_id"], web_text)
                    else:
                        r = T.copy_message(g["chat_id"], bc["source_chat_id"], bc["source_msg_id"])
                    if not r.get("ok"):
                        desc = str(r).lower()
                        if "not found" in desc or "deleted" in desc or "kicked" in desc:
                            try:
                                conx = S.db(); curx = conx.cursor()
                                S._ex(curx, "DELETE FROM groups WHERE chat_id=?", (g["chat_id"],))
                                S._ex(curx, "DELETE FROM user_groups WHERE group_id=?", (g["chat_id"],))
                                S._ex(curx, "DELETE FROM group_owners WHERE group_id=?", (g["chat_id"],))
                                conx.commit(); conx.close()
                            except Exception:
                                pass
                con3 = S.db(); cur3 = con3.cursor()
                S._ex(cur3, "UPDATE broadcasts SET last_sent=? WHERE user_id=?",
                      (now.isoformat(timespec="seconds"), uid))
                con3.commit(); con3.close()
                sent += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(f'{{"ok":true,"sent":{sent}}}'.encode())
        except Exception as e:
            print("cron xato:", e)
            self.send_response(500)
            self.end_headers()
