import asyncio
import os
import random
import threading          # ← ДОБАВЛЕНО
import time
from datetime import datetime, timedelta

import motor.motor_asyncio
from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================
# НАСТРОЙКИ
# =========================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ВАШ_ТОКЕН_СЮДА")

MONGO_URL = os.getenv(
    "MONGO_URL",
    "mongodb+srv://sheep_bot:meewoolbotsheep199@cluster0.jjp6pia.mongodb.net/?appName=Cluster0"
)

mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = mongo_client["miraculous_battle"]
players_collection = db["players"]

# =========================================================
# КВАМИ (7 БАЗОВЫХ)
# =========================================================

KWAMI_LIST = {
    "Божья коровка": "Отменяет способность любого игрока, при этом он теряет её.",
    "Чёрный кот": "Замораживает игрока на 12 часов.",
    "Рыжая лисица": "Меняет способность двух игроков местами.",
    "Королева пчёл": "Использует способность игрока на выбранную цель.",
    "Черепаха": "Защищает игрока от нападений на 1 раз.",
    "Павлин": "Привязывается к игроку.",
    "Мотылёк": "Удваивает чужую способность.",
}

KWAMI_NAMES = list(KWAMI_LIST.keys())

# =========================================================
# КНОПКИ (ТОЛЬКО 2)
# =========================================================

main_kb = ReplyKeyboardMarkup(
    [
        [KeyboardButton("👤 Просмотреть профиль")],
        [KeyboardButton("🎲 Получить способность")],
    ],
    resize_keyboard=True
)

# =========================================================
# РАБОТА С БАЗОЙ
# =========================================================

async def get_user(user_id, username):
    user = await players_collection.find_one({"user_id": user_id})
    if not user:
        user = {
            "user_id": user_id,
            "username": username,
            "kwami": None,
            "last_claim": None,
            "frozen_until": None,
            "ability_used": False,
        }
        await players_collection.insert_one(user)
    elif user.get("username") != username:
        await players_collection.update_one({"user_id": user_id}, {"$set": {"username": username}})
        user["username"] = username
    return user

async def save_user(user):
    await players_collection.update_one({"user_id": user["user_id"]}, {"$set": user})

# =========================================================
# КОМАНДА /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await get_user(update.effective_user.id, update.effective_user.username)
    await update.message.reply_text(
        "🐞 Добро пожаловать в Miraculous!\n"
        "Нажми 🎲 Получить способность, чтобы получить Квами.\n"
        "Атакуй игроков через /use @username",
        reply_markup=main_kb
    )

# =========================================================
# КНОПКА: ПОЛУЧИТЬ СПОСОБНОСТЬ
# =========================================================

async def claim_ability(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)
    now = datetime.now()

    # Проверка кулдауна 12 часов
    if user.get("last_claim"):
        time_diff = (now - user["last_claim"]).total_seconds()
        if time_diff < 43200:  # 12 часов = 43200 секунд
            await update.message.reply_text("⏳ Способность уже получена!")
            return

    # Выбор случайного Квами
    kwami = random.choice(KWAMI_NAMES)
    user["kwami"] = kwami
    user["last_claim"] = now
    user["ability_used"] = False
    await save_user(user)

    # Форматированное сообщение
    emoji_map = {
        "Божья коровка": "🐞",
        "Чёрный кот": "🐈⬛",
        "Рыжая лисица": "🦊",
        "Королева пчёл": "🐝",
        "Черепаха": "🐢",
        "Павлин": "🦚",
        "Мотылёк": "🦋",
    }
    emoji = emoji_map.get(kwami, "✨")
    await update.message.reply_text(
        f"🎲 Способность получена! 🔥 Получена: {emoji} {kwami}"
    )

# =========================================================
# КНОПКА: ПРОСМОТРЕТЬ ПРОФИЛЬ
# =========================================================

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)
    kwami = user.get("kwami") or "Нет"
    desc = KWAMI_LIST.get(kwami, "")
    await update.message.reply_text(
        f"👤 Профиль\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🆔 @{user['username']}\n"
        f"🔮 Квами: {kwami}\n"
        f"📖 Описание: {desc}\n"
        f"⏳ Следующее получение: через 12 часов"
    )

# =========================================================
# КОМАНДА: /USE (АТАКА)
# =========================================================

async def use_ability(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)

    # Проверка: есть ли Квами
    if not user.get("kwami"):
        await update.message.reply_text("❌ Сначала получи способность через 🎲 Получить способность")
        return

    # Проверка: не заморожен ли игрок
    if user.get("frozen_until") and user["frozen_until"] > datetime.now():
        await update.message.reply_text("❌ Ты заморожен и не можешь атаковать!")
        return

    # Проверка: не использована ли способность
    if user.get("ability_used"):
        await update.message.reply_text("❌ Ты уже использовал способность сегодня. Жди 12 часов!")
        return

    # Проверка: указана ли цель
    if not context.args:
        await update.message.reply_text("❌ Укажи цель: /use @username")
        return

    target_username = context.args[0].replace("@", "").strip()
    if not target_username:
        await update.message.reply_text("❌ Укажи цель: /use @username")
        return

    # Поиск цели в БД
    target = await players_collection.find_one({"username": target_username})
    if not target:
        await update.message.reply_text("❌ Игрок не найден.")
        return

    if target["user_id"] == update.effective_user.id:
        await update.message.reply_text("❌ Нельзя атаковать самого себя!")
        return

    # Применяем способность
    kwami = user["kwami"]
    await update.message.reply_text(
        f"⚔️ {update.effective_user.first_name} использовал {kwami} на @{target_username}!\n"
        f"✨ Эффект: {KWAMI_LIST[kwami]}"
    )

    # Отмечаем способность как использованную
    user["ability_used"] = True
    await save_user(user)

# =========================================================
# FLASK ДЛЯ RENDER
# =========================================================

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Miraculous Battle Bot is running!"

def run_flask():
    port = int(os.getenv("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

# =========================================================
# ЗАПУСК
# =========================================================

async def main():
    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("use", use_ability))

    # Кнопки
    app.add_handler(MessageHandler(filters.Regex("^👤 Просмотреть профиль$"), profile))
    app.add_handler(MessageHandler(filters.Regex("^🎲 Получить способность$"), claim_ability))

    # Запуск Flask в потоке
    thread = threading.Thread(target=run_flask)
    thread.daemon = True
    thread.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
