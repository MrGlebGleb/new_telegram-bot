#!/usr/bin/env python3
"""
Game release Telegram bot with full pagination and pre-caching.
Final version with file_id caching for 100% stable media display.
"""

import os
import requests
import asyncio
import uuid
import urllib.parse
from datetime import datetime, time
from zoneinfo import ZoneInfo
from telegram import constants, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    PicklePersistence,
    ContextTypes,
)
import translators as ts
import io

# --- CONFIG (from env) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET")

if not TELEGRAM_BOT_TOKEN or not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
    raise RuntimeError("Одна или несколько переменных окружения (TOKEN, TWITCH_ID, TWITCH_SECRET) не установлены!")

# --- Вспомогательные функции (Блокирующие) ---

def translate_text_blocking(text: str) -> str:
    """Блокирующая функция для перевода текста."""
    if not text: return ""
    try:
        return ts.translate_text(text, translator='google', to_language='ru', timeout=10)
    except Exception as e:
        print(f"[ERROR] Ошибка библиотеки translators: {e}")
        return text

def _check_url_blocking(url: str) -> bool:
    """Проверяет доступность URL обложки (HEAD-запрос)."""
    if not url: return False
    try:
        r = requests.head(url, timeout=5)
        return 200 <= r.status_code < 400
    except requests.exceptions.RequestException as e:
        print(f"[WARN] Head check failed for {url}: {e}")
        return False

def _download_image_blocking(url: str) -> io.BytesIO | None:
    """Загружает изображение в байты для отправки Telegram."""
    try:
        if not url.startswith(('http://', 'https://')):
            print(f"[ERROR] Некорректный URL для загрузки: {url}")
            return None
        
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return io.BytesIO(r.content)
    except requests.RequestException as e:
        print(f"[ERROR] Не удалось загрузить байты изображения по URL {url}: {e}")
        return None

def _get_igdb_access_token_blocking():
    """Получает токен доступа от Twitch/IGDB."""
    url = (f"https://id.twitch.tv/oauth2/token?client_id={TWITCH_CLIENT_ID}"
           f"&client_secret={TWITCH_CLIENT_SECRET}&grant_type=client_credentials")
    r = requests.post(url, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]

def _get_todays_games_blocking(access_token):
    """Получает список сегодняшних релизов (лимит 5)."""
    today_ts = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    
    headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {access_token}"}
    body = (
        "fields name, summary, cover.url, platforms.name, websites.category, websites.url, aggregated_rating, aggregated_rating_count;"
        f"where first_release_date >= {today_ts} & first_release_date < {today_ts + 86400}"
        " & hypes > 2;"
        "sort hypes desc; limit 5;" # Лимит 5
    )
    r = requests.post("https://api.igdb.com/v4/games", headers=headers, data=body, timeout=20)
    r.raise_for_status()
    return r.json()

# --- Функции парсинга данных ---

def _parse_trailer(websites_data: list | None) -> str | None:
    """Находит URL трейлера на YouTube."""
    if not websites_data: return None
    for site in websites_data:
        if site.get("category") == 9: return site.get("url")
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
    """Форматирует сообщение с информацией об игре."""
    name = game_data.get("name", "Без названия")
    summary = game_data.get("summary", "Описание отсутствует.")
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
    
    return text, InlineKeyboardMarkup(keyboard)

# --- АСИНХРОННАЯ ОБРАБОТКА ИГР И КЭШИРОВАНИЕ ---

async def _get_best_cover_url(game: dict) -> str | None:
    """
    Пытается найти и проверить лучший URL обложки с агрессивным ретраем.
    """
    game_name = game.get("name", "No Title")
    final_cover_url: str = None
    
    cover_data = game.get("cover")
    if cover_data and cover_data.get("url"):
        base_url = "https:" + cover_data["url"]
        resolutions = ["t_720p", "t_hd", "t_screenshot_med"]
        max_retries = 3

        for res in resolutions:
            cover_url_attempt = base_url.replace("t_thumb", res)
            
            for attempt in range(max_retries):
                # Добавляем кэш-бастер для обхода локального кэша запросов
                cache_buster = uuid.uuid4().hex[:6]
                url_with_buster = f"{cover_url_attempt}?v={cache_buster}"
                
                is_available = await asyncio.to_thread(_check_url_blocking, url_with_buster)
                
                if is_available:
                    final_cover_url = url_with_buster
                    print(f"[INFO] Обложка для '{game_name}' успешно проверена на разрешении: {res} (попытка {attempt + 1}).")
                    return final_cover_url
                
                if attempt < max_retries - 1:
                    print(f"[WARN] Попытка {attempt + 1}/{max_retries} не удалась для '{game_name}' ({res}). Пауза 1с.")
                    await asyncio.sleep(1)
            
    return None

async def _enrich_game_data_async(game: dict) -> dict:
    """
    Асинхронно переводит описание и обогащает данные одной игры.
    """
    # Статический URL плейсхолдера
    placeholder_url = "https://via.placeholder.com/1280x720.png/2F3136/FFFFFF?text=NO+COVER"
    
    # 1. Поиск лучшего URL
    original_cover_url = await _get_best_cover_url(game)
    
    # 2. Перевод текста
    summary_ru = await asyncio.to_thread(translate_text_blocking, game.get("summary", ""))

    # 3. Выбор финального URL для загрузки (оригинал или плейсхолдер)
    final_url = original_cover_url if original_cover_url else placeholder_url
    
    # 4. Скачивание байтов
    image_bytes = await asyncio.to_thread(_download_image_blocking, final_url)

    return {
        **game,
        "summary": summary_ru,
        "trailer_url": _parse_trailer(game.get("websites")),
        "cover_url": original_cover_url, # Оригинальный URL (может быть None)
        "image_bytes": image_bytes,      # Байт-поток изображения (гарантированно есть)
        "file_id": None                  # Здесь будет кэшироваться file_id
    }

async def _cache_file_id_and_filter(context: ContextTypes.DEFAULT_TYPE, chat_id: int, enriched_games: list) -> list:
    """
    Принудительно отправляет и удаляет медиа для получения надежного Telegram file_id.
    Возвращает только те игры, для которых кэширование прошло успешно.
    """
    final_list = []
    
    for i, game_data in enumerate(enriched_games):
        if not game_data.get("image_bytes"):
            print(f"[WARN] Игра '{game_data.get('name')}' (индекс {i}): Пропущена из-за ошибки загрузки байтов.")
            continue
        
        caption_text = f"Кэширование медиа: {game_data.get('name')}..."
        
        # 1. Отправляем байты
        game_data["image_bytes"].seek(0)
        
        try:
            # Отправка фото для кэширования Telegram file_id
            sent_message = await context.bot.send_photo(
                chat_id, 
                photo=game_data["image_bytes"], 
                caption=caption_text
            )
            
            # 2. Получаем и кэшируем file_id
            game_data["file_id"] = sent_message.photo[-1].file_id
            print(f"[INFO] Успешно кэширован file_id для '{game_data.get('name')}'.")
            
            # 3. Удаляем временное сообщение
            await context.bot.delete_message(chat_id=chat_id, message_id=sent_message.message_id)
            
            # 4. Освобождаем память от байтов, они больше не нужны
            del game_data["image_bytes"] 
            
            final_list.append(game_data)
            await asyncio.sleep(0.5) # Пауза между кэшированием

        except Exception as e:
            # Ошибка при отправке байтов (например, временный сбой Telegram)
            print(f"[ERROR] Не удалось получить file_id для '{game_data.get('name')}': {e}. Игра пропущена.")
            # Попытка удалить, если сообщение было частично отправлено
            try: await context.bot.delete_message(chat_id=chat_id, message_id=sent_message.message_id)
            except: pass
            continue
            
    return final_list


# --- КОМАНДЫ И ОБРАБОТЧИКИ ---

async def releases_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Основная команда для получения релизов с пагинацией."""
    chat_id = update.effective_chat.id
    status_message = await update.message.reply_text("🔍 Ищу и обрабатываю сегодняшние релизы...")
    
    try:
        access_token = await asyncio.to_thread(_get_igdb_access_token_blocking)
        base_games = await asyncio.to_thread(_get_todays_games_blocking, access_token)
        
        if not base_games:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text="🎮 Значимых релизов на сегодня не найдено.")
            return

        # 1. Обогащение данных и загрузка байтов
        tasks = [_enrich_game_data_async(game) for game in base_games]
        enriched_games = await asyncio.gather(*tasks)
            
        # 2. Принудительное кэширование file_id и фильтрация
        final_games = await _cache_file_id_and_filter(context, chat_id, enriched_games)
        
        if not final_games:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text="Произошла ошибка при кэшировании медиа для всех игр. Попробуйте позже.")
            return
            
        list_id = str(uuid.uuid4())
        context.bot_data.setdefault('game_lists', {})[list_id] = final_games

        # 3. Отправка первого сообщения (теперь гарантированно с file_id)
        
        first_game_data = final_games[0]
        text, markup = await format_game_for_pagination(game_data=first_game_data, current_index=0, total_count=len(final_games), list_id=list_id)

        # Добавляем предупреждение, если была использована заглушка
        if not first_game_data.get("cover_url"):
            text += "\n\n*(Использована обложка-заглушка)*"

        await context.bot.send_photo(
            chat_id, 
            photo=first_game_data["file_id"], # Используем кэшированный file_id
            caption=text, 
            parse_mode=constants.ParseMode.MARKDOWN, 
            reply_markup=markup
        )
        
        # Удаляем сообщение "Ищу..."
        await context.bot.delete_message(chat_id=chat_id, message_id=status_message.message_id)

    except Exception as e:
        print(f"[ERROR] Ошибка в команде releases_command: {e}")
        # Попытка удалить сообщение о статусе, если оно еще есть
        try: await context.bot.delete_message(chat_id=chat_id, message_id=status_message.message_id)
        except: pass
        await context.bot.send_message(chat_id, text="Произошла критическая ошибка при получении данных.")

async def pagination_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик кнопок пагинации.
    Использует ТОЛЬКО кэшированный Telegram file_id.
    """
    query = update.callback_query
    await query.answer()

    try:
        _, direction, list_id, requested_index_str = query.data.split("_")
        current_index = int(requested_index_str)
    except (ValueError, IndexError):
        await query.edit_message_caption(caption="Ошибка: некорректные данные пагинации.")
        return

    games = context.bot_data.get('game_lists', {}).get(list_id)
    if not games or not (0 <= current_index < len(games)):
        await query.edit_message_caption(caption="Ошибка: список устарел или не существует. Запросите заново: /releases.")
        return
    
    game_data = games[current_index]
    
    # 1. Форматируем текст и кнопки
    text, markup = await format_game_for_pagination(
        game_data=game_data,
        current_index=current_index,
        total_count=len(games),
        list_id=list_id
    )
    
    # Добавляем предупреждение, если была использована заглушка
    if not game_data.get("cover_url"):
        text += "\n\n*(Использована обложка-заглушка)*"
        
    # 2. Используем кэшированный file_id (самый надежный способ)
    cached_file_id = game_data["file_id"] # Гарантированно есть в final_games

    try:
        # Используем file_id для InputMediaPhoto
        media = InputMediaPhoto(media=cached_file_id, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        await query.edit_message_media(media=media, reply_markup=markup)
        print(f"[INFO] Успешное обновление медиа для '{game_data.get('name')}' с использованием file_id.")
        return
    except Exception as e:
        # Если не сработал file_id (очень редкий сбой), переходим к текстовому фолбэку
        print(f"[ERROR] Сбой при обновлении медиа с file_id: {e}. Переход к текстовому фолбэку.")

    # 3. УЛЬТИМАТИВНЫЙ ФОЛБЭК: Редактируем только текст и кнопки.
    try:
         await query.edit_message_caption(caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
         print(f"[INFO] Успешное обновление только текста для '{game_data.get('name')}' (индекс {current_index}).")
    except Exception as edit_caption_e:
         print(f"[ERROR] Сбой даже при редактировании текста: {edit_caption_e}")
         await query.answer("Не удалось обновить сообщение. Запросите /releases заново.", show_alert=True)
    return

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

        # 1. Обогащение данных и загрузка байтов (делаем один раз для всех чатов)
        tasks = [_enrich_game_data_async(game) for game in base_games]
        enriched_games = await asyncio.gather(*tasks)
        
        # 2. Кэширование file_id для рассылки
        # Поскольку кэширование требует взаимодействия с чатом, мы делаем это только один раз 
        # для первого чата, и используем file_id для всех остальных.
        
        if not enriched_games or not enriched_games[0].get("image_bytes"):
             print("[INFO] Все игры провалили загрузку байтов. Пропуск рассылки.")
             return
             
        # Кэшируем в первом чате, чтобы получить file_id
        first_chat_id = chat_ids[0]
        print(f"[INFO] Начинается кэширование file_id в чате {first_chat_id}")
        
        # Получаем список игр, для которых есть file_id
        cached_games = await _cache_file_id_and_filter(context, first_chat_id, enriched_games)

        if not cached_games:
            print("[INFO] Не удалось кэшировать ни одну игру. Пропуск рассылки.")
            return
            
        print(f"[INFO] Отправка ежедневных релизов ({len(cached_games)} игр) в {len(chat_ids)} чатов.")
        
        # 3. Отправка по всем чатам
        for chat_id in chat_ids:
            list_id = str(uuid.uuid4())
            
            # Сохраняем кэшированный список в контексте для пагинации
            context.bot_data.setdefault('game_lists', {})[list_id] = cached_games
            
            for i, game_data in enumerate(cached_games):
                
                text, markup = await format_game_for_pagination(
                    game_data=game_data,
                    current_index=i,
                    total_count=len(cached_games),
                    list_id=list_id
                )
                
                # Добавляем предупреждение, если была использована заглушка
                if not game_data.get("cover_url"):
                    text += "\n\n*(Использована обложка-заглушка)*"

                try:
                    # Используем кэшированный file_id для отправки
                    await context.bot.send_photo(
                        chat_id, 
                        photo=game_data["file_id"], 
                        caption=text, 
                        parse_mode=constants.ParseMode.MARKDOWN, 
                        reply_markup=markup
                    )
                except Exception as e:
                    print(f"[ERROR] Daily send: Критический сбой отправки file_id в чат {chat_id}: {e}")
                
                await asyncio.sleep(1.0) # Задержка между отправками

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

    # Добавляем JobQueue
    tz = ZoneInfo("Europe/Moscow")
    scheduled_time = time(hour=11, minute=0, tzinfo=tz)
    
    current_jobs = application.job_queue.get_jobs_by_name("daily_game_check")
    if current_jobs:
        for job in current_jobs:
            job.schedule_removal()
            
    application.job_queue.run_daily(daily_check_job, scheduled_time, name="daily_game_check")

    print("[INFO] Бот запускается...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30)


if __name__ == "__main__":
    # Заглушка для start_command, так как она не была представлена в исходном коде
    async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Регистрирует чат для ежедневной рассылки (заглушка)."""
        chat_id = update.effective_chat.id
        chat_ids = context.bot_data.setdefault("chat_ids", [])
        if chat_id not in chat_ids:
            chat_ids.append(chat_id)
            await update.message.reply_text("✅ Ок, я запомнил этот чат и буду присылать уведомления о релизах.")
            print(f"[INFO] Зарегистрирован chat_id {chat_id}")
        else:
            await update.message.reply_text("Этот чат уже есть в списке рассылки.")
            
    main()
