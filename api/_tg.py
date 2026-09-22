"""Telegram Bot API helper (serverless uchun, requests bilan)."""
import os
import json
import urllib.request

TOKEN = os.getenv("BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""


def api_call(method, payload):
    if not TOKEN:
        print("BOT_TOKEN yo'q")
        return {}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"API xato {method}: {e}")
        return {}


def send_message(chat_id, text, parse_mode="HTML", reply_markup=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        p["reply_markup"] = reply_markup
    return api_call("sendMessage", p)


def copy_message(chat_id, from_chat_id, message_id):
    return api_call("copyMessage", {
        "chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id})


def main_menu(is_founder=False):
    kb = [
        [{"text": "📝 Xabar yaratish"}, {"text": "⏱ Interval"}],
        [{"text": "📋 Guruhlarim"}, {"text": "📊 Status"}],
        [{"text": "▶️ Start"}, {"text": "⏸ Stop"}],
        [{"text": "🚀 Test yuborish"}, {"text": "❓ Yordam"}],
    ]
    if is_founder:
        kb.append([{"text": "👑 Founder panel"}])
    return {"keyboard": kb, "resize_keyboard": True}


def founder_inline():
    return {"inline_keyboard": [
        [{"text": "📊 Statistika", "callback_data": "f:stats"},
         {"text": "📋 Guruhlar", "callback_data": "f:groups"}],
        [{"text": "⛔ Hammasini to'xtatish", "callback_data": "f:stopall"}],
    ]}
