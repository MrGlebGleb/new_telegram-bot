#!/usr/bin/env python3
"""
Game release Telegram bot (clean rewrite).
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
# --- НОВЫЙ ИМПОРТ ---
from deep_translator import MyMemoryTranslator

# --- CONFIG (from env) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET")

# safety-check envs
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
    raise RuntimeError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET must be set")


# --- НОВАЯ ФУНКЦИЯ ПЕРЕВОДА ---
def translate_text_blocking(text: str) -> str:
    """Переводит текст на русский с помощью MyMemory API."""
    if not text:
        return ""
    try:
        # Автоматически определяет исходный язык и переводит на русский ('ru')
        translated_text = MyMemoryTranslator(source="auto", target="ru").translate(text)
        # Иногда API возвращает None, если не может перевести
        return translated_text if translated_text else text
    except Exception as e:
        print(f"[ERROR] MyMemory translation failed: {e}")
        return text # В случае ошибки возвращаем оригинальный текст


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
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_ts = int(today_start.timestamp())
    headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {access_token}"}
    body = (
        "fields name, summary, cover.url, first_release_date, platforms.name, websites.url, websites.category;"
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
    chat_ids = context.bot_data.get("chat_ids")
    if chat_ids is None:
        chat_ids = []
        context.bot_data["chat_ids"] = chat_ids

    if chat_id not in chat_ids:
        chat_ids.append(chat_id)
    await update.message.reply_text(
        "✅ Ок, я запомнил этот чат и буду присылать сюда уведомления о значимых релизах игр."
    )
    print(f"[INFO] Registered chat_id {chat_id}")


def _format_game_message(game: dict):
    name = game.get("name", "Без названия")
    summary = game.get("summary", "Описание отсутствует.")
    cover = game.get("cover", {}).get("url")
    if cover:
        # Запрашиваем изображение высокого разрешения
        cover = "https:" + cover.replace("t_thumb", "t_1080p")

    platforms = ", ".join([p["name"] for p in game.get("platforms", [])])
    
    steam_url = None
    for site in game.get("websites", []):
        if site.get("category") == 13:  # 13 is the category for Steam
            steam_url = site.get("url")
            break

    text = f"🎮 *ВЫШЛА ИГРА: {name}*\n\n"
    if platforms:
        text += f"*Платформы:* {platforms}\n\n"
    
    text += f"{summary}"

    if steam_url:
        text += f"\n\n[Купить в Steam]({steam_url})"
        
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
        # Переводим описание
        if game.get("summary"):
            game["summary"] = await asyncio.to_thread(translate_text_blocking, game["summary"])
            
        text, cover = _format_game_message(game)
        for cid in chat_ids:
            await _send_to_chat(app, cid, text, cover)
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

    application.add_handler(CommandHandler("start", start_command))

    tz = ZoneInfo("Europe/Amsterdam")
    scheduled_time = time(hour=10, minute=0, tzinfo=tz)
    application.job_queue.run_daily(check_for_game_releases, scheduled_time, name="daily_game_check")

    async def startup_run(context):
        await asyncio.sleep(2)
        await check_for_game_releases(context)

    application.job_queue.run_once(startup_run, when=5)

    print("[INFO] Starting bot (run_polling). Registered handlers and jobs.")
    application.run_polling(stop_signals=None)


if __name__ == "__main__":
    build_and_run()
