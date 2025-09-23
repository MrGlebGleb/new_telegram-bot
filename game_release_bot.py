import os
import requests
import asyncio
import aioschedule as schedule
from datetime import datetime
from telegram.ext import Application, CommandHandler, PicklePersistence

# --- НАСТРОЙКИ (будут браться с сервера) ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TWITCH_CLIENT_ID = os.environ.get('TWITCH_CLIENT_ID')
TWITCH_CLIENT_SECRET = os.environ.get('TWITCH_CLIENT_SECRET')
PORT = int(os.environ.get('PORT', 10000))

# --- IGDB API ---
def get_igdb_access_token():
    """Получает токен доступа для API IGDB от Twitch."""
    url = f'https://id.twitch.tv/oauth2/token?client_id={TWITCH_CLIENT_ID}&client_secret={TWITCH_CLIENT_SECRET}&grant_type=client_credentials'
    response = requests.post(url)
    response.raise_for_status()
    return response.json()['access_token']

def get_upcoming_significant_games(access_token):
    """Получает список значимых релизов игр на сегодня."""
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_timestamp = int(today_start.timestamp())
    headers = {'Client-ID': TWITCH_CLIENT_ID, 'Authorization': f'Bearer {access_token}'}
    body = (
        f'fields name, summary, cover.url, first_release_date;'
        f'where first_release_date >= {today_timestamp} & first_release_date < {today_timestamp + 86400} & cover != null & hypes > 5;'
        f'sort hypes desc; limit 5;'
    )
    response = requests.post('https://api.igdb.com/v4/games', headers=headers, data=body)
    response.raise_for_status()
    return response.json()

# --- Логика бота ---
async def start(update, context):
    """Обрабатывает команду /start и сохраняет chat_id."""
    chat_id = update.message.chat_id
    context.chat_data['chat_id'] = chat_id
    await update.message.reply_text(
        'Отлично! Я запомнил этот чат и теперь буду присылать сюда уведомления о выходе игр. 🎮'
    )
    print(f"Бот был активирован в чате с ID: {chat_id}")

def format_game_message(game):
    """Форматирует сообщение об игре."""
    name = game.get('name', 'Без названия')
    summary = game.get('summary', 'Описание отсутствует.')
    cover_url = game.get('cover', {}).get('url')
    if cover_url:
        cover_url = 'https:' + cover_url.replace('t_thumb', 't_cover_big')
    message = f"🎮 **ВЫШЛА ИГРА: {name}** 🎮\n\n{summary}\n"
    return message, cover_url

async def send_telegram_message(bot, chat_id, message, photo_url):
    """Отправляет сообщение в Telegram."""
    try:
        if photo_url:
            await bot.send_photo(chat_id=chat_id, photo=photo_url, caption=message, parse_mode='Markdown')
        else:
            await bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
        return True
    except Exception as e:
        print(f"Не удалось отправить сообщение в чат {chat_id}: {e}")
        return False

async def check_for_game_releases(bot):
    """Планируемая задача для проверки релизов игр."""
    print(f"[{datetime.now()}] Проверка выхода новых игр...")
    persistence_manager = bot.application.persistence
    chat_id = persistence_manager.chat_data.get('chat_id')
    
    if not chat_id:
        print("Бот еще не был активирован. Пропускаю.")
        return
        
    try:
        access_token = get_igdb_access_token()
        games = get_upcoming_significant_games(access_token)
        if not games:
            print("Сегодня нет значимых релизов.")
            return
        for game in games:
            message, cover_url = format_game_message(game)
            if await send_telegram_message(bot, chat_id, message, cover_url):
                print(f"Отправлено уведомление об игре: {game.get('name')}")
            await asyncio.sleep(1)
    except Exception as e:
        print(f"Произошла ошибка при проверке игр: {e}")

# --- Планировщик ---
async def scheduler_task(bot):
    """Функция-планировщик для запуска задач по расписанию."""
    schedule.every().day.at("10:00").do(check_for_game_releases, bot=bot)
    print("Расписание настроено: проверка каждый день в 10:00.")
    while True:
        await schedule.run_pending()
        await asyncio.sleep(1)

# --- Основная функция запуска бота ---
async def main():
    """Основная точка входа для запуска бота с веб-хуками."""
    # Используем PicklePersistence для сохранения данных между перезапусками
    persistence = PicklePersistence(filepath='bot_data.pkl')
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).persistence(persistence).build()
    
    # Добавляем обработчик команды /start
    application.add_handler(CommandHandler("start", start))
    
    print("Бот запущен и ждет команды /start...")
    
    # Запускаем планировщик как фоновую задачу
    asyncio.create_task(scheduler_task(application.bot))
    
    # Запускаем веб-сервер и слушаем входящие обновления
    await application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=f"/{TELEGRAM_BOT_TOKEN}",
        webhook_url=f"https://{os.environ.get('RAILWAY_STATIC_URL')}/{TELEGRAM_BOT_TOKEN}"
    )

if __name__ == "__main__":
    asyncio.run(main())
