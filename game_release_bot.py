import os
import requests
import asyncio
import aioschedule as schedule
from datetime import datetime
from telegram.ext import Application, CommandHandler, PicklePersistence

# --- НАСТРОЙКИ ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET")


# --- IGDB API ---
def get_igdb_access_token():
    """Получает токен доступа для API IGDB от Twitch."""
    url = (
        f"https://id.twitch.tv/oauth2/token"
        f"?client_id={TWITCH_CLIENT_ID}"
        f"&client_secret={TWITCH_CLIENT_SECRET}"
        f"&grant_type=client_credentials"
    )
    response = requests.post(url)
    response.raise_for_status()
    return response.json()["access_token"]


def get_upcoming_significant_games(access_token):
    """Получает список значимых релизов игр на сегодня."""
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_timestamp = int(today_start.timestamp())
    headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {access_token}"}
    body = (
        f"fields name, summary, cover.url, first_release_date;"
        f"where first_release_date >= {today_timestamp} "
        f"& first_release_date < {today_timestamp + 86400} "
        f"& cover != null & hypes > 5;"
        f"sort hypes desc; limit 5;"
    )
    response = requests.post("https://api.igdb.com/v4/games", headers=headers, data=body)
    response.raise_for_status()
    return response.json()


# --- Логика бота ---
async def start(update, context):
    """Обрабатывает команду /start и сохраняет chat_id."""
    chat_id = update.effective_chat.id
    context.bot_data.setdefault("chat_ids", set()).add(chat_id)
    await update.message.reply_text(
        "✅ Я запомнил этот чат и буду присылать уведомления о релизах игр 🎮"
    )
    print(f"[INFO] Бот активирован в чате {chat_id}")


def format_game_message(game):
    """Форматирует сообщение об игре."""
    name = game.get("name", "Без названия")
    summary = game.get("summary", "Описание отсутствует.")
    cover_url = game.get("cover", {}).get("url")
    if cover_url:
        cover_url = "https:" + cover_url.replace("t_thumb", "t_cover_big")
    message = f"🎮 *ВЫШЛА ИГРА: {name}* 🎮\n\n{summary}"
    return message, cover_url


async def send_telegram_message(app, chat_id, message, photo_url):
    """Отправляет сообщение в Telegram."""
    try:
        if photo_url:
            await app.bot.send_photo(
                chat_id, photo=photo_url, caption=message, parse_mode="Markdown"
            )
        else:
            await app.bot.send_message(
                chat_id, text=message, parse_mode="Markdown"
            )
        return True
    except Exception as e:
        print(f"[ERROR] Не удалось отправить сообщение в {chat_id}: {e}")
        return False


async def check_for_game_releases(app):
    """Проверяет новые релизы игр и отправляет уведомления."""
    print(f"[{datetime.now()}] Проверка выхода новых игр...")
    chat_ids = app.bot_data.get("chat_ids", set())

    if not chat_ids:
        print("[INFO] Нет активированных чатов.")
        return

    try:
        access_token = get_igdb_access_token()
        games = get_upcoming_significant_games(access_token)
        if not games:
            print("[INFO] Сегодня релизов нет.")
            return

        for game in games:
            message, cover_url = format_game_message(game)
            for chat_id in chat_ids:
                await send_telegram_message(app, chat_id, message, cover_url)
            await asyncio.sleep(1)
    except Exception as e:
        print(f"[ERROR] Ошибка при проверке игр: {e}")


# --- Планировщик ---
async def scheduler_task(app):
    """Функция-планировщик для запуска задач по расписанию."""
    schedule.every().day.at("10:00").do(check_for_game_releases, app)
    print("[INFO] Расписание настроено: проверка каждый день в 10:00.")
    while True:
        await schedule.run_pending()
        await asyncio.sleep(1)


# --- Основная функция ---
async def main():
    persistence = PicklePersistence(filepath="bot_data.pkl")
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .build()
    )

    application.add_handler(CommandHandler("start", start))

    print("[INFO] Бот запущен...")

    # Запускаем планировщик
    asyncio.create_task(scheduler_task(application))

    # Запускаем бота в режиме polling
    await application.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
