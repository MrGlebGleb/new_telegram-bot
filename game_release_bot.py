#!/usr/bin/env python3
"""
Game release Telegram bot with full pagination and pre-caching.
"""

import os
import requests
import asyncio
import uuid
import urllib.parse
from datetime import datetime, time
from zoneinfo import ZoneInfo
from telegram import constants, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    PicklePersistence,
    ContextTypes,
)
import translators as ts

# --- CONFIG (from env) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET")

if not TELEGRAM_BOT_TOKEN or not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
    raise RuntimeError("Одна или несколько переменных окружения (TOKEN, TWITCH_ID, TWITCH_SECRET) не установлены!")

# --- Вспомогательные функции ---

def translate_text_blocking(text: str) -> str:
    """Блокирующая функция для перевода текста."""
    if not text: return ""
    try:
        return ts.translate_text(text, translator='google', to_language='ru')
    except Exception as e:
        print(f"[ERROR] Ошибка библиотеки translators: {e}")
        return text

def _get_igdb_access_token_blocking():
    """Получает токен доступа от Twitch/IGDB."""
    url = (f"https://id.twitch.tv/oauth2/token?client_id={TWITCH_CLIENT_ID}"
           f"&client_secret={TWITCH_CLIENT_SECRET}&grant_type=client_credentials")
    r = requests.post(url, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]

def _get_todays_games_blocking(access_token):
    """Получает список сегодняшних релизов (блокирующая функция)."""
    today_ts = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {access_token}"}
    # ИЗМЕНЕНИЕ: Добавляем поля для рейтинга
    body = (
        "fields name, summary, cover.url, platforms.name, websites.category, websites.url, aggregated_rating, aggregated_rating_count;"
        f"where first_release_date >= {today_ts} & first_release_date < {today_ts + 86400}"
        " & hypes > 2;"
        "sort hypes desc; limit 10;"
    )
    r = requests.post("https://api.igdb.com/v4/games", headers=headers, data=body, timeout=20)
    r.raise_for_status()
    return r.json()

# --- Функции парсинга данных ---

def _parse_trailer(websites_data: list | None) -> str | None:
    """Находит URL трейлера на YouTube в списке сайтов."""
    if not websites_data:
        return None
    for site in websites_data:
        if site.get("category") == 9: # Категория 9 в IGDB API - это YouTube
            return site.get("url")
    return None

def _get_rating_emoji(rating: float | None) -> str:
    """Возвращает цветной эмодзи в зависимости от оценки."""
    if rating is None: return ""
    if rating >= 75: return "🟢"
    if rating >= 50: return "🟡"
    if rating > 0: return "🔴"
    return ""

# --- ФОРМАТИРОВАНИЕ И ПАГИНАЦИЯ ---

async def format_game_for_pagination(game_data: dict, current_index: int, total_count: int, list_id: str):
    """Форматирует сообщение с информацией об игре для отправки пользователю."""
    name = game_data.get("name", "Без названия")
    summary = game_data.get("summary", "Описание отсутствует.")
    cover_url = game_data.get("cover_url")
    platforms_data = game_data.get("platforms", [])
    platforms = ", ".join([p["name"] for p in platforms_data if "name" in p])
    trailer_url = game_data.get("trailer_url")
    rating = game_data.get("aggregated_rating")

    text = f"🎮 *Сегодня выходит: {name}*\n\n"
    
    # ИЗМЕНЕНИЕ: Добавляем строку с рейтингом, если он есть
    if rating:
        emoji = _get_rating_emoji(rating)
        text += f"{emoji} *Рейтинг Metacritic:* {rating:.0f}/100\n"

    if platforms: text += f"*Платформы:* {platforms}\n\n"
    text += summary
    
    keyboard = []
    nav_buttons = []
    if current_index > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page_back_{list_id}_{current_index - 1}"))
    
    nav_buttons.append(InlineKeyboardButton(f"[{current_index + 1}/{total_count}]", callback_data="noop"))
    
    if current_index < total_count - 1:
        nav_buttons.append(InlineKeyboardButton("➡️ Вперед", callback_data=f"page_fwd_{list_id}_{current_index + 1}"))
    
    keyboard.append(nav_buttons)
    
    if trailer_url:
        keyboard.append([InlineKeyboardButton("🎬 Смотреть трейлер", url=trailer_url)])
    
    return text, cover_url, InlineKeyboardMarkup(keyboard)

# --- АСИНХРОННАЯ ОБРАБОТКА ИГР ---

async def _enrich_game_data_async(game: dict) -> dict:
    """
    Асинхронно переводит описание и обогащает данные одной игры.
    Если обложка отсутствует, генерирует URL-заглушку.
    """
    game_name = game.get("name", "No Title")
    final_cover_url: str

    cover_data = game.get("cover")
    if not cover_data or not cover_data.get("url"):
        print(f"[INFO] Для игры '{game_name}' не найдена обложка, используется плейсхолдер.")
        encoded_name = urllib.parse.quote(game_name)
        final_cover_url = f"https://via.placeholder.com/1280x720.png/2F3136/FFFFFF?text={encoded_name}"
    else:
        cover_url = "https:" + cover_data["url"].replace("t_thumb", "t_720p")
        cache_buster = uuid.uuid4().hex[:6]
        final_cover_url = f"{cover_url}?v={cache_buster}"

    summary_ru = await asyncio.to_thread(translate_text_blocking, game.get("summary", ""))

    return {
        **game,
        "summary": summary_ru,
        "trailer_url": _parse_trailer(game.get("websites")),
        "cover_url": final_cover_url
    }

# --- КОМАНДЫ И ОБРАБОТЧИКИ ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Регистрирует чат для ежедневной рассылки."""
    chat_id = update.effective_chat.id
    chat_ids = context.bot_data.setdefault("chat_ids", [])

    if chat_id not in chat_ids:
        chat_ids.append(chat_id)
        await update.message.reply_text(
            f"✅ Ок, я запомнил этот чат ({chat_id}) и буду присылать сюда уведомления о релизах."
        )
        print(f"[INFO] Зарегистрирован chat_id {chat_id}")
    else:
        await update.message.reply_text("Этот чат уже есть в списке рассылки.")

async def releases_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Основная команда для получения релизов с пагинацией."""
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔍 Ищу и обрабатываю сегодняшние релизы... Это может занять несколько секунд.")
    
    try:
        access_token = await asyncio.to_thread(_get_igdb_access_token_blocking)
        base_games = await asyncio.to_thread(_get_todays_games_blocking, access_token)
        
        if not base_games:
            await context.bot.send_message(chat_id, text="🎮 Значимых релизов на сегодня не найдено.")
            return

        tasks = [_enrich_game_data_async(game) for game in base_games]
        enriched_games = await asyncio.gather(*tasks)
            
        list_id = str(uuid.uuid4())
        context.bot_data.setdefault('game_lists', {})[list_id] = enriched_games
        
        message_sent = False
        for i, game_data in enumerate(enriched_games):
            text, cover, markup = await format_game_for_pagination(
                game_data=game_data,
                current_index=i,
                total_count=len(enriched_games),
                list_id=list_id
            )
            try:
                await context.bot.send_photo(chat_id, photo=cover, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
                message_sent = True
                break
            except Exception as e:
                print(f"[WARN] Не удалось отправить стартовое фото для '{game_data.get('name')}': {e}")
                continue
        
        if not message_sent:
            await context.bot.send_message(chat_id, text="🎮 На сегодня есть релизы, но не удалось загрузить для них обложки.")

    except Exception as e:
        print(f"[ERROR] Ошибка в команде releases_command: {e}")
        await context.bot.send_message(chat_id, text="Произошла критическая ошибка при получении данных.")

async def pagination_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок пагинации с автоматическим пропуском 'битых' обложек."""
    query = update.callback_query
    await query.answer()

    try:
        _, direction, list_id, requested_index_str = query.data.split("_")
        current_index = int(requested_index_str)
        step = 1 if direction == "fwd" else -1
    except (ValueError, IndexError):
        await query.edit_message_text("Ошибка: некорректные данные пагинации.")
        return

    games = context.bot_data.get('game_lists', {}).get(list_id)
    if not games:
        await query.edit_message_text("Ошибка: список устарел. Запросите заново: /releases.")
        return
        
    while 0 <= current_index < len(games):
        game_data = games[current_index]
        
        text, cover, markup = await format_game_for_pagination(
            game_data=game_data,
            current_index=current_index,
            total_count=len(games),
            list_id=list_id
        )

        try:
            media = InputMediaPhoto(media=cover, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
            await query.edit_message_media(media=media, reply_markup=markup)
            return
        except Exception as e:
            error_text = str(e).lower()
            if "wrong type of the web page content" in error_text or "failed to get http url content" in error_text:
                print(f"[WARN] Пропуск '{game_data.get('name')}' (индекс {current_index}) из-за битой обложки.")
                current_index += step
                continue
            else:
                print(f"[ERROR] Непредвиденная ошибка при пагинации: {e}")
                await context.bot.send_message(
                    chat_id=query.effective_chat.id,
                    text="Произошла неизвестная ошибка. Попробуйте запросить список заново: /releases"
                )
                return
    
    print("[INFO] Достигнут конец списка при пропуске 'битых' обложек.")


async def daily_check_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневная задача для рассылки релизов."""
    print(f"[{datetime.now().isoformat()}] Запуск ежедневной проверки релизов")
    chat_ids = context.bot_data.get("chat_ids", [])
    if not chat_ids:
        print("[INFO] Нет зарегистрированных чатов, пропуск.")
        return
    
    try:
        access_token = await asyncio.to_thread(_get_igdb_access_token_blocking)
        base_games = await asyncio.to_thread(_get_todays_games_blocking, access_token)
        if not base_games:
            print("[INFO] Релизов на сегодня нет.")
            return

        tasks = [_enrich_game_data_async(game) for game in base_games]
        enriched_games = await asyncio.gather(*tasks)
        
        if not enriched_games:
            print("[INFO] Релизов на сегодня нет после обработки.")
            return

        print(f"[INFO] Отправка ежедневных релизов в {len(chat_ids)} чатов.")
        for chat_id in chat_ids:
            list_id = str(uuid.uuid4())
            context.bot_data.setdefault('game_lists', {})[list_id] = enriched_games
            
            message_sent = False
            for i, game_data in enumerate(enriched_games):
                text, cover, markup = await format_game_for_pagination(
                    game_data=game_data,
                    current_index=i,
                    total_count=len(enriched_games),
                    list_id=list_id
                )
                try:
                    await context.bot.send_photo(chat_id, photo=cover, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
                    message_sent = True
                    await asyncio.sleep(0.5)
                    break 
                except Exception as e:
                    print(f"[WARN] Daily send: Не удалось отправить фото для '{game_data.get('name')}' в чат {chat_id}: {e}")
                    continue
            
            if not message_sent:
                print(f"[WARN] Daily send: Не удалось отправить ни одной игры в чат {chat_id}")

    except Exception as e:
        print(f"[ERROR] Сбой в ежедневной задаче: {e}")


# --- СБОРКА И ЗАПУСК ---
def main():
    """Основная функция для запуска бота."""
    persistence = PicklePersistence(filepath="bot_data.pkl")
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("releases", releases_command))
    application.add_handler(CallbackQueryHandler(pagination_handler, pattern="^page_(fwd|back)_"))
    application.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern="^noop$"))

    tz = ZoneInfo("Europe/Moscow")
    scheduled_time = time(hour=11, minute=0, tzinfo=tz)
    application.job_queue.run_daily(daily_check_job, scheduled_time, name="daily_game_check")

    print("[INFO] Бот запускается...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

