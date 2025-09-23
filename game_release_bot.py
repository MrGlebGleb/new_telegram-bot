#!/usr/bin/env python3
"""
Game release Telegram bot (clean rewrite).
"""

import os
import requests
import asyncio
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from telegram import constants, Update
from telegram.ext import (
    Application,
    CommandHandler,
    PicklePersistence,
    ContextTypes,
    filters,
)
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


# --- Translation helper ---
def translate_text_blocking(text: str) -> str:
    if not text:
        return ""
    try:
        translated_text = MyMemoryTranslator(source="auto", target="ru").translate(text)
        return translated_text if translated_text else text
    except Exception as e:
        print(f"[ERROR] MyMemory translation failed: {e}")
        return text


# --- IGDB helpers (blocking) ---
def _get_igdb_access_token_blocking():
    # ... (код этой функции не меняется)
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
    # ... (код этой функции не меняется)
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


# --- Shared logic for sending releases ---
async def send_releases_to_chat(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Fetches and sends game releases to a specific chat."""
    app: Application = context.application
    try:
        access_token = await asyncio.to_thread(_get_igdb_access_token_blocking)
        games = await asyncio.to_thread(_get_upcoming_significant_games_blocking, access_token)
    except Exception as e:
        print(f"[ERROR] IGDB request failed for chat {chat_id}: {e}")
        await app.bot.send_message(chat_id=chat_id, text="Не удалось получить данные о релизах. Попробуйте позже.")
        return

    if not games:
        await app.bot.send_message(chat_id=chat_id, text="🎮 Значимых релизов сегодня не найдено.")
        return

    for game in games:
        if game.get("summary"):
            game["summary"] = await asyncio.to_thread(translate_text_blocking, game["summary"])
            
        text, cover = _format_game_message(game)
        await _send_to_chat(app, chat_id, text, cover)
        await asyncio.sleep(0.8)


# --- telegram handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Register chat for notifications."""
    chat = update.effective_chat
    bot_username = context.bot.username
    
    # В групповом чате проверяем, было ли упоминание бота
    if chat.type in [chat.GROUP, chat.SUPERGROUP]:
        # Сообщение должно начинаться с упоминания бота
        if not update.message.text.startswith(f"@{bot_username}"):
            print(f"[INFO] Ignoring /start in group {chat.id} without mention.")
            return

    chat_id = chat.id
    chat_ids = context.bot_data.setdefault("chat_ids", [])

    if chat_id not in chat_ids:
        chat_ids.append(chat_id)
        await update.message.reply_text(
            "✅ Ок, я запомнил этот чат и буду присылать сюда ежедневные уведомления о релизах."
        )
        print(f"[INFO] Registered chat_id {chat_id}")
    else:
        await update.message.reply_text(
            "Этот чат уже есть в списке рассылки."
        )

# --- НОВЫЙ ОБРАБОТЧИК ---
async def releases_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """On-demand check for today's releases."""
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔍 Ищу сегодняшние релизы...")
    await send_releases_to_chat(chat_id, context)


def _format_game_message(game: dict):
    # ... (код этой функции не меняется)
    name = game.get("name", "Без названия")
    summary = game.get("summary", "Описание отсутствует.")
    cover = game.get("cover", {}).get("url")
    if cover:
        cover = "https:" + cover.replace("t_thumb", "t_1080p")

    platforms = ", ".join([p["name"] for p in game.get("platforms", [])])
    
    steam_url = None
    for site in game.get("websites", []):
        if site.get("category") == 13:
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
    # ... (код этой функции не меняется)
    try:
        if photo_url:
            await app.bot.send_photo(chat_id=chat_id, photo=photo_url, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        else:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=constants.ParseMode.MARKDOWN)
        return True
    except Exception as e:
        print(f"[WARN] Failed to send to {chat_id}: {e}")
        return False


# --- job that will be scheduled by JobQueue ---
async def daily_check_job(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue callback that sends releases to all registered chats."""
    print(f"[{datetime.now().isoformat()}] Running scheduled daily_check_job")
    chat_ids = context.bot_data.get("chat_ids", [])
    if not chat_ids:
        print("[INFO] No registered chats; skipping daily job.")
        return
    
    print(f"[INFO] Sending daily releases to {len(chat_ids)} chats.")
    for chat_id in chat_ids:
        await send_releases_to_chat(chat_id, context)


# --- main builder ---
def build_and_run():
    persistence = PicklePersistence(filepath="bot_data.pkl")
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .build()
    )

    # handlers
    # Регистрируем /start. Фильтр нужен, чтобы бот не реагировал на команду в чужом сообщении в группе.
    application.add_handler(CommandHandler("start", start_command, filters.COMMAND))
    # --- РЕГИСТРАЦИЯ НОВОЙ КОМАНДЫ ---
    application.add_handler(CommandHandler("releases", releases_command))

    # Schedule
    tz = ZoneInfo("Europe/Amsterdam")
    scheduled_time = time(hour=10, minute=0, tzinfo=tz)
    application.job_queue.run_daily(daily_check_job, scheduled_time, name="daily_game_check")

    # Optional: Run once on startup
    application.job_queue.run_once(lambda ctx: releases_command(Update(0), ctx), when=5)

    print("[INFO] Starting bot (run_polling). Registered handlers and jobs.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    build_and_run()
