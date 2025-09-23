import os
import requests
import time
import asyncio
import aioschedule as schedule
from datetime import datetime
from telegram.ext import Application, CommandHandler
from flask import Flask # <-- ДОБАВЛЕНО: импорт для веб-сервера
import threading # <-- ДОБАВЛЕНО: для запуска веб-сервера в фоне

# --- НАСТРОЙКИ (будут браться с сервера) ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TWITCH_CLIENT_ID = os.environ.get('TWITCH_CLIENT_ID')
TWITCH_CLIENT_SECRET = os.environ.get('TWITCH_CLIENT_SECRET')
CHAT_ID_FILE = 'chat_id.txt'

# --- IGDB API (без изменений) ---
def get_igdb_access_token():
    url = f'https://id.twitch.tv/oauth2/token?client_id={TWITCH_CLIENT_ID}&client_secret={TWITCH_CLIENT_SECRET}&grant_type=client_credentials'
    response = requests.post(url)
    response.raise_for_status()
    return response.json()['access_token']

def get_upcoming_significant_games(access_token):
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

# --- Логика бота (без изменений) ---
async def start(update, context):
    chat_id = update.message.chat_id
    with open(CHAT_ID_FILE, 'w') as f:
        f.write(str(chat_id))
    await update.message.reply_text(
        'Отлично! Я запомнил этот чат и теперь буду присылать сюда уведомления о выходе игр. 🎮'
    )
    print(f"Бот был активирован в чате с ID: {chat_id}")

def format_game_message(game):
    name = game.get('name', 'Без названия')
    summary = game.get('summary', 'Описание отсутствует.')
    cover_url = game.get('cover', {}).get('url')
    if cover_url:
        cover_url = 'https:' + cover_url.replace('t_thumb', 't_cover_big')
    message = f"🎮 **ВЫШЛА ИГРА: {name}** 🎮\n\n{summary}\n"
    return message, cover_url

async def send_telegram_message(bot, chat_id, message, photo_url):
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
    print(f"[{datetime.now()}] Проверка выхода новых игр...")
    if not os.path.exists(CHAT_ID_FILE):
        print("Бот еще не был активирован. Пропускаю.")
        return
        
    with open(CHAT_ID_FILE, 'r') as f:
        chat_id = f.read().strip()
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

# --- Планировщик (без изменений) ---
async def scheduler(bot):
    schedule.every().day.at("10:00").do(check_for_game_releases, bot=bot)
    print("Расписание настроено: проверка каждый день в 10:00.")
    while True:
        await schedule.run_pending()
        await asyncio.sleep(1)

# --- НОВЫЙ БЛОК: Запуск веб-сервера ---
app = Flask(__name__)
@app.route('/')
def index():
    return "Бот жив!"

def run_flask():
    app.run(host='0.0.0.0', port=10000)

# --- Основная функция запуска бота ---
async def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    
    # Запускаем планировщик как фоновую задачу
    asyncio.create_task(scheduler(application.bot))
    
    print("Бот запущен и ждет команды /start...")
    
    # Запускаем бота
    await application.run_polling()

if __name__ == "__main__":
    # Запускаем веб-сервер в отдельном потоке, чтобы он не мешал боту
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Запускаем основную асинхронную функцию бота
    asyncio.run(main())