#!/usr/bin/env python3
"""
Game release Telegram bot (clean rewrite).

- Uses python-telegram-bot v20 Application + JobQueue.
- Schedules daily check (10:00 Europe/Amsterdam).
- Uses asyncio.to_thread for blocking requests (requests lib).
- Persists chat ids with PicklePersistence.
"""

import os
import requests
import asyncio
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from telegram import constants
from telegram.ext import (
    Application,
    CommandHandler,
    PicklePersistence,
    ContextTypes,
)

# --- CONFIG (from env) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET")

# safety-check envs
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
    raise RuntimeError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET must be set")


# --- IGDB helpers (blocking) ---
def _get_igdb_access_token_blocking():
    url = (
        "https://id.twitch.tv/oauth2/token"
        f"?client_id={TWITCH_CLIENT_ID}"
        f"&client_secret={TWITCH_CLIENT_SECRET}"
        "&grant_type=client_credentials"
    )
    r = requests.post(url, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def _get_upcoming_significant_games_blocking(access_token):
    # take UTC day (00:00 - 23:59) — you can adjust if you want local-day behaviour
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_ts = int(today_start.timestamp())
    headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {access_token}"}
    body = (
        "fields name, summary, cover.url, first_release_date;"
        f"where first_release_date >= {today_ts} & first_release_date < {today_ts + 86400}"
        " & cover != null & hypes > 5;"
        "sort hypes desc; limit 5;"
    )
    r = requests.post("https://api.igdb.com/v4/games", headers=headers, data=body, timeout=20)
    r.raise_for_status()
    return r.json()


# --- telegram handlers ---
async def start_command(update, context: ContextTypes.DEFAULT_TYPE):
    """Register chat for notifications."""
    chat_id = update.effective_chat.id
    # store as list for JSON/pickle safety (sets aren't always pickle-friendly across versions)
    chat_ids = context.bot_data.get("chat_ids")
    if chat_ids is None:
        chat_ids = []
        context.bot_data["chat_ids"] = chat_ids

    if chat_id not in chat_ids:
        chat_ids.append(chat_id)
        # persistence will save bot_data automatically when application stops; you can force save if needed
    await update.message.reply_text(
        "✅ Ок, я запомнил этот чат и буду присылать сюда уведомления о значимых релизах игр."
    )
    print(f"[INFO] Registered chat_id {chat_id}")


def _format_game_message(game: dict):
    name = game.get("name", "Без названия")
    summary = game.get("summary", "Описание отсутствует.")
    cover = game.get("cover", {}).get("url")
    if cover:
        cover = "https:" + cover.replace("t_thumb", "t_cover_big")
    # Keep message simple Markdown-friendly (avoid MarkdownV2 escaping complexity)
    text = f"🎮 *ВЫШЛА ИГРА: {name}*\n\n{summary}"
    return text, cover


async def _send_to_chat(app: Application, chat_id: int, text: str, photo_url: str | None):
    try:
        if photo_url:
            await app.bot.send_photo(chat_id=chat_id, photo=photo_url, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        else:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=constants.ParseMode.MARKDOWN)
        return True
    except Exception as e:
        print(f"[WARN] Failed to send to {chat_id}: {e}")
        return False


# --- job that will be scheduled by JobQueue (runs inside asyncio loop) ---
async def check_for_game_releases(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue callback. Uses asyncio.to_thread to call blocking requests."""
    app: Application = context.application
    print(f"[{datetime.now().isoformat()}] Running scheduled check_for_game_releases")

    chat_ids = app.bot_data.get("chat_ids") or []
    if not chat_ids:
        print("[INFO] No registered chats; skipping.")
        return

    try:
        access_token = await asyncio.to_thread(_get_igdb_access_token_blocking)
        games = await asyncio.to_thread(_get_upcoming_significant_games_blocking, access_token)
    except Exception as e:
        print(f"[ERROR] IGDB request failed: {e}")
        return

    if not games:
        print("[INFO] No significant releases found today.")
        return

    for game in games:
        text, cover = _format_game_message(game)
        for cid in chat_ids:
            await _send_to_chat(app, cid, text, cover)
        # small pause to avoid hitting limits
        await asyncio.sleep(0.8)


# --- main builder (synchronous) ---
def build_and_run():
    persistence = PicklePersistence(filepath="bot_data.pkl")
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .build()
    )

    # handlers
    application.add_handler(CommandHandler("start", start_command))

    # Schedule: every day at 10:00 Europe/Amsterdam
    tz = ZoneInfo("Europe/Amsterdam")
    scheduled_time = time(hour=10, minute=0, tzinfo=tz)

    # JobQueue: run_daily expects (callback, time)
    # We pass a context (not required here)
    application.job_queue.run_daily(check_for_game_releases, scheduled_time, name="daily_game_check")

    # Also optionally run a check at startup once (short delay) to confirm bot works
    async def startup_run(context):
        # small delay to allow bot initialization
        await asyncio.sleep(2)
        await check_for_game_releases(context)

    application.job_queue.run_once(startup_run, when=5)  # seconds after start

    print("[INFO] Starting bot (run_polling). Registered handlers and jobs.")
    # run_polling() is the synchronous entrypoint that manages loop for us
    application.run_polling(stop_signals=None)


if __name__ == "__main__":
    build_and_run()
