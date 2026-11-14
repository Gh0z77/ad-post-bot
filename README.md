# Ad Post Bot

Telegram bot that posts a user's ad (text + photo) to the groups they choose, on a schedule they set.

- Pick a message, an interval and target groups; the bot posts automatically
- Web dashboard to start/stop, change interval and message, test a post
- Founder panel: statistics, users, ban, broadcast announcements, stop all
- Runs as a long-polling bot or serverless on Vercel (webhook + cron)

## Stack

Python · python-telegram-bot · Flask · PostgreSQL / SQLite · Vercel / Render / Docker

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in BOT_TOKEN and the rest
python bot.py
```
