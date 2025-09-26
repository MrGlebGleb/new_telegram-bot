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
        # Увеличиваем таймаут, так как перевод может быть медленным
        return ts.translate_text(text, translator='google', to_language='ru', timeout=10)
    except Exception as e:
        print(f"[ERROR] Ошибка библиотеки translators: {e}")
        return text

def _check_url_blocking(url: str) -> bool:
    """
    Блокирующая функция. Проверяет доступность URL обложки 
    с помощью HEAD-запроса перед отправкой в Telegram.
    """
    if not url: return False
    try:
        # Используем HEAD, чтобы не скачивать все тело изображения
        r = requests.head(url, timeout=5)
        # Успешный статус (200-399)
        return 200 <= r.status_code < 400
    except requests.exceptions.RequestException as e:
        # Ошибка таймаута, подключения или DNS
        print(f"[WARN] Head check failed for {url}: {e}")
        return False

def _get_igdb_access_token_blocking():
    """Получает токен доступа от Twitch/IGDB."""
    url = (f"https://id.twitch.tv/oauth2/token?client_id={TWITCH_CLIENT_ID}"
           f"&client_secret={TWITCH_CLIENT_SECRET}&grant_type=client_credentials")
    r = requests.post(url, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]

def _get_todays_games_blocking(access_token):
    """Получает список сегодняшних релизов (блокирующая функция)."""
    # Учитываем, что IGDB хранит даты в UTC. Если нужно точно по Москве, можно скорректировать.
    today_ts = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    
    headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {access_token}"}
    body = (
        # Добавляем больше полей, чтобы не делать лишних запросов.
        "fields name, summary, cover.url, platforms.name, websites.category, websites.url, aggregated_rating, aggregated_rating_count;"
        f"where first_release_date >= {today_ts} & first_release_date < {today_ts + 86400}"
        " & hypes > 2;"
        "sort hypes desc; limit 5;" # Лимит уменьшен до 5
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
    # Используем 'cover_url' для фото, 'placeholder_url' для заглушки (если обложка не загрузится)
    cover_url = game_data.get("cover_url")
    placeholder_url = game_data.get("placeholder_url")

    platforms_data = game_data.get("platforms", [])
    platforms = ", ".join([p["name"] for p in platforms_data if "name" in p])
    trailer_url = game_data.get("trailer_url")
    rating = game_data.get("aggregated_rating")

    text = f"🎮 *Сегодня выходит: {name}*\n\n"
    
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
    
    return text, cover_url, placeholder_url, InlineKeyboardMarkup(keyboard)

# --- АСИНХРОННАЯ ОБРАБОТКА ИГР ---

async def _enrich_game_data_async(game: dict) -> dict:
    """
    Асинхронно переводит описание и обогащает данные одной игры.
    Включает агрессивный ретрай и подбор разрешений для обложки.
    """
    game_name = game.get("name", "No Title")
    final_cover_url: str = None
    
    # ИЗМЕНЕНИЕ: Статический URL плейсхолдера для повышения надежности кэширования Telegram
    placeholder_url = "https://via.placeholder.com/1280x720.png/2F3136/FFFFFF?text=NO+COVER"


    cover_data = game.get("cover")
    if cover_data and cover_data.get("url"):
        base_url = "https:" + cover_data["url"]
        # Список разрешений для подбора. Начинаем с самого высокого.
        resolutions = ["t_720p", "t_hd", "t_screenshot_med"]
        max_retries = 3

        for res in resolutions:
            cover_url_attempt = base_url.replace("t_thumb", res)
            
            # Внутренний цикл для повторных попыток
            for attempt in range(max_retries):
                cache_buster = uuid.uuid4().hex[:6]
                url_with_buster = f"{cover_url_attempt}?v={cache_buster}"
                
                # Асинхронно проверяем доступность обложки
                is_available = await asyncio.to_thread(_check_url_blocking, url_with_buster)
                
                if is_available:
                    final_cover_url = url_with_buster
                    print(f"[INFO] Обложка для '{game_name}' успешно проверена на разрешении: {res} (попытка {attempt + 1}).")
                    break # Успех, выходим из внутреннего цикла ретраев
                
                if attempt < max_retries - 1:
                    print(f"[WARN] Попытка {attempt + 1}/{max_retries} не удалась для '{game_name}' ({res}). Пауза 1с.")
                    await asyncio.sleep(1) # Ждем перед повторной попыткой
            
            if final_cover_url:
                break # Успех, выходим из внешнего цикла разрешений

    if not final_cover_url:
         print(f"[WARN] Обложка для '{game_name}' недоступна ни в одном разрешении после всех попыток. Используется плейсхолдер.")


    # Асинхронно переводим текст
    summary_ru = await asyncio.to_thread(translate_text_blocking, game.get("summary", ""))

    return {
        **game,
        "summary": summary_ru,
        "trailer_url": _parse_trailer(game.get("websites")),
        "cover_url": final_cover_url, # Может быть None, если нет обложки
        "placeholder_url": placeholder_url # Всегда есть URL плейсхолдера
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
    # Удаляем предыдущие сообщения "Ищу..."
    await update.message.reply_text("🔍 Ищу и обрабатываю сегодняшние релизы... Это может занять несколько секунд.")
    
    try:
        # Увеличиваем лимит, чтобы получить больше игр, если некоторые не пройдут
        access_token = await asyncio.to_thread(_get_igdb_access_token_blocking)
        base_games = await asyncio.to_thread(_get_todays_games_blocking, access_token)
        
        if not base_games:
            await context.bot.send_message(chat_id, text="🎮 Значимых релизов на сегодня не найдено.")
            return

        tasks = [_enrich_game_data_async(game) for game in base_games]
        enriched_games = await asyncio.gather(*tasks)
            
        list_id = str(uuid.uuid4())
        # Сохраняем ТОЛЬКО ОБОГАЩЕННЫЕ игры. Важный момент: не пропускаем игры на этом этапе.
        context.bot_data.setdefault('game_lists', {})[list_id] = enriched_games
        
        # Находим первую игру, которую можно отправить
        first_game_index = 0
        current_game_data = enriched_games[first_game_index]

        text, cover, placeholder, markup = await format_game_for_pagination(
            game_data=current_game_data,
            current_index=first_game_index,
            total_count=len(enriched_games),
            list_id=list_id
        )
        
        # ИЗМЕНЕНИЕ: Отправляем первую игру, пытаясь использовать фото, но в случае ошибки - отправляем текстовое сообщение
        try:
            # 1. Попытка отправить с обложкой
            if cover:
                await context.bot.send_photo(
                    chat_id, 
                    photo=cover, 
                    caption=text, 
                    parse_mode=constants.ParseMode.MARKDOWN, 
                    reply_markup=markup
                )
            else:
                # 2. Если обложки нет, отправляем с плейсхолдером
                await context.bot.send_photo(
                    chat_id, 
                    photo=placeholder, 
                    caption=text + "\n\n*(Не удалось загрузить обложку)*", 
                    parse_mode=constants.ParseMode.MARKDOWN, 
                    reply_markup=markup
                )
        except Exception as e:
            # 3. Если даже с плейсхолдером не удалось (например, проблема с Telegram или chat_id), 
            # отправляем чисто текстовое сообщение БЕЗ ФОТО. Игра не пропускается!
            print(f"[WARN] Не удалось отправить фото/плейсхолдер для '{current_game_data.get('name')}'. Отправка текста. Ошибка: {e}")
            await context.bot.send_message(
                chat_id, 
                text=text + "\n\n*(Обложка недоступна)*", 
                parse_mode=constants.ParseMode.MARKDOWN, 
                reply_markup=markup
            )


    except Exception as e:
        print(f"[ERROR] Ошибка в команде releases_command: {e}")
        await context.bot.send_message(chat_id, text="Произошла критическая ошибка при получении данных.")

async def pagination_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик кнопок пагинации.
    ИЗМЕНЕНИЕ: Включает ультимативный текстовый фолбэк при сбое обновления медиа.
    """
    query = update.callback_query
    await query.answer()

    try:
        _, direction, list_id, requested_index_str = query.data.split("_")
        current_index = int(requested_index_str)
    except (ValueError, IndexError):
        # Используем edit_message_caption, так как это сообщение с фото
        await query.edit_message_caption(caption="Ошибка: некорректные данные пагинации.")
        return

    games = context.bot_data.get('game_lists', {}).get(list_id)
    if not games:
        await query.edit_message_caption(caption="Ошибка: список устарел. Запросите заново: /releases.")
        return
    
    if not (0 <= current_index < len(games)):
        # Защита от выхода за пределы списка
        await query.answer("Это конец списка!", show_alert=False)
        return

    game_data = games[current_index]
    
    text, cover, placeholder, markup = await format_game_for_pagination(
        game_data=game_data,
        current_index=current_index,
        total_count=len(games),
        list_id=list_id
    )

    # 1. Попытка отредактировать сообщение с ОРИГИНАЛЬНЫМ фото (если оно есть)
    if cover:
        try:
            media = InputMediaPhoto(media=cover, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
            await query.edit_message_media(media=media, reply_markup=markup)
            return
        except Exception as e:
            error_text = str(e).lower()
            if "wrong type of the web page content" in error_text or "failed to get http url content" in error_text:
                print(f"[WARN] Не удалось обновить фото для '{game_data.get('name')}' (индекс {current_index}). Попытка использовать плейсхолдер.")
                # Ошибка при загрузке обложки, переходим к шагу 2
            else:
                print(f"[ERROR] Непредвиденная ошибка при пагинации (фото): {e}")
                # Если ошибка не связана с загрузкой обложки, переходим к шагу 3
    
    # 2. Использование СТАТИЧЕСКОГО ПЛЕЙСХОЛДЕРА
    try:
        placeholder_caption = text + "\n\n*(Обложка недоступна)*"
        media = InputMediaPhoto(media=placeholder, caption=placeholder_caption, parse_mode=constants.ParseMode.MARKDOWN)
        await query.edit_message_media(media=media, reply_markup=markup)
        return
    except Exception as e:
        print(f"[ERROR] Непредвиденная ошибка при пагинации (плейсхолдер): {e}")
        
        # 3. УЛЬТИМАТИВНЫЙ ФОЛБЭК: Редактируем только текст и кнопки. Медиа остается как есть.
        final_caption = text + "\n\n*(Обложка недоступна. Ошибка обновления сообщения.)*"
        try:
             await query.edit_message_caption(caption=final_caption, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
             print(f"[INFO] Успешное обновление только текста для '{game_data.get('name')}' (индекс {current_index}).")
        except Exception as edit_caption_e:
             # Если и редактирование текста не сработало (например, сообщение слишком старое)
             print(f"[ERROR] Сбой даже при редактировании текста: {edit_caption_e}")
             await query.answer("Не удалось обновить сообщение. Запросите /releases заново.", show_alert=True)
        return
    
# --- ЕЖЕДНЕВНАЯ ЗАДАЧА (аналогично releases_command) ---

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
            
            # ИЗМЕНЕНИЕ: Теперь отправляем ВСЕ игры по очереди, используя заглушку, если фото не грузится
            for i, game_data in enumerate(enriched_games):
                text, cover, placeholder, markup = await format_game_for_pagination(
                    game_data=game_data,
                    current_index=i,
                    total_count=len(enriched_games),
                    list_id=list_id
                )
                
                # ИЗМЕНЕНИЕ: Отправляем игру, пытаясь использовать фото, но в случае ошибки - отправляем текстовое сообщение
                message_sent = False
                
                # 1. Попытка отправить с обложкой
                if cover:
                    try:
                        await context.bot.send_photo(chat_id, photo=cover, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
                        message_sent = True
                    except Exception as e:
                        # Если не удалось с обложкой, печатаем предупреждение и переходим к плейсхолдеру
                        print(f"[WARN] Daily send: Не удалось отправить фото для '{game_data.get('name')}' в чат {chat_id}. Попытка с плейсхолдером. Ошибка: {e}")
                
                # 2. Если не удалось с обложкой или обложки не было, пробуем отправить с плейсхолдером
                if not message_sent:
                    try:
                        await context.bot.send_photo(chat_id, photo=placeholder, caption=text + "\n\n*(Не удалось загрузить обложку)*", parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
                        message_sent = True
                    except Exception as e:
                        # 3. Если даже с плейсхолдером не удалось, отправляем чисто текстовое сообщение
                        print(f"[ERROR] Daily send: Не удалось отправить фото/плейсхолдер для '{game_data.get('name')}' в чат {chat_id}. Отправка текста. Ошибка: {e}")
                        await context.bot.send_message(chat_id, text=text + "\n\n*(Обложка недоступна)*", parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
                        message_sent = True

                if message_sent:
                    # Задержка между отправками в чат для предотвращения флуда
                    await asyncio.sleep(1.0) 

    except Exception as e:
        print(f"[ERROR] Сбой в ежедневной задаче: {e}")


# --- СБОРКА И ЗАПУСК ---
def main():
    """Основная функция для запуска бота."""
    # Убедитесь, что Telegram Bot API использует более высокие лимиты
    persistence = PicklePersistence(filepath="bot_data.pkl")
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("releases", releases_command))
    # Обновляем pattern, чтобы избежать ошибки Attribute Error при пагинации
    application.add_handler(CallbackQueryHandler(pagination_handler, pattern="^page_(fwd|back)_"))
    application.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern="^noop$"))

    # Добавляем JobQueue
    tz = ZoneInfo("Europe/Moscow")
    scheduled_time = time(hour=11, minute=0, tzinfo=tz)
    
    # Удаляем job, если он уже есть, перед добавлением, чтобы избежать дублирования после перезапуска
    current_jobs = application.job_queue.get_jobs_by_name("daily_game_check")
    if current_jobs:
        for job in current_jobs:
            job.schedule_removal()
            
    application.job_queue.run_daily(daily_check_job, scheduled_time, name="daily_game_check")

    print("[INFO] Бот запускается...")
    # Увеличиваем таймаут для polling
    application.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30)


if __name__ == "__main__":
    main()
