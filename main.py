import asyncio
import os
import random
import threading
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
# КВАМИ (ТОЛЬКО 5)
# =========================================================

KWAMI_LIST = {
    "Божья коровка": "Отменяет способность любого игрока, при этом он теряет её.",
    "Чёрный кот": "Замораживает игрока на 12 часов.",
    "Мотылёк": "Удваивает чужую способность.",
    "Павлин": "Привязывается к игроку.",
    "Рыжая лисица": "Меняет способность двух игроков местами.",
}

KWAMI_NAMES = list(KWAMI_LIST.keys())

# =========================================================
# КНОПКИ
# =========================================================

def get_keyboard(has_ability=False):
    if has_ability:
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton("👤 Профиль")],
                [KeyboardButton("🎲 Применить способность")],
            ],
            resize_keyboard=True
        )
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("👤 Профиль")],
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
            "coins": 0,
            "wins": 0,
            "streak": 0,
        }
        await players_collection.insert_one(user)
    elif user.get("username") != username:
        await players_collection.update_one({"user_id": user_id}, {"$set": {"username": username}})
        user["username"] = username
    return user

async def save_user(user):
    await players_collection.update_one({"user_id": user["user_id"]}, {"$set": user})

# =========================================================
# /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)
    has_ability = user.get("kwami") is not None
    await update.message.reply_text(
        "🐞 Добро пожаловать в Париж!\n"
        "Получи способность и сразись с друзьями.\n"
        "Ответь на сообщение игрока командой /use",
        reply_markup=get_keyboard(has_ability)
    )

# =========================================================
# ПОЛУЧИТЬ СПОСОБНОСТЬ
# =========================================================

async def claim_ability(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)
    now = datetime.now()

    if user.get("last_claim"):
        time_diff = (now - user["last_claim"]).total_seconds()
        if time_diff < 43200:
            await update.message.reply_text("⏳ Способность уже получена!")
            return

    kwami = random.choice(KWAMI_NAMES)
    user["kwami"] = kwami
    user["last_claim"] = now
    user["ability_used"] = False
    user["coins"] += 5
    await save_user(user)

    await update.message.reply_text(
        f"🎲 Способность получена! 🔥 Получена: {kwami}\n\n"
        f"📖 {KWAMI_LIST[kwami]}\n\n"
        f"🥐 +5 круассанов (всего: {user['coins']})",
        reply_markup=get_keyboard(True)
    )

# =========================================================
# ПРИМЕНИТЬ СПОСОБНОСТЬ
# =========================================================

async def use_ability(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)

    if not user.get("kwami"):
        await update.message.reply_text("❌ Сначала получи способность!")
        return

    if user.get("ability_used"):
        await update.message.reply_text("❌ Способность уже использована!")
        return

    if user.get("frozen_until") and user["frozen_until"] > datetime.now():
        await update.message.reply_text("❌ Ты заморожен! Нельзя использовать способность.")
        return

    # Если есть цель (ответ на сообщение)
    target_username = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        if target_user and target_user.id != update.effective_user.id:
            target_username = target_user.username or target_user.first_name

    # Если ответили на сообщение — применяем на цель
    if target_username:
        target = await players_collection.find_one({"username": target_username})
        if target:
            user["ability_used"] = True
            user["coins"] += 5
            await save_user(user)

            kwami = user["kwami"]
            await update.message.reply_text(
                f"🎲 Способность применена! 🔥 Получено: 🥐 +5 круассанов\n\n"
                f"⚔️ {kwami} использован на @{target_username}\n"
                f"📖 {KWAMI_LIST[kwami]}",
                reply_markup=get_keyboard(False)
            )
            return

    # Если нет цели — применяем впустую (или на себя)
    user["ability_used"] = True
    user["coins"] += 5
    await save_user(user)

    kwami = user["kwami"]
    await update.message.reply_text(
        f"🎲 Способность применена! 🔥 Получено: 🥐 +5 круассанов\n\n"
        f"⚔️ {kwami} использован впустую (цель не найдена)\n"
        f"📖 {KWAMI_LIST[kwami]}",
        reply_markup=get_keyboard(False)
    )

# =========================================================
# ПРОФИЛЬ
# =========================================================

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)

    kwami = user.get("kwami") or "Нет"
    desc = KWAMI_LIST.get(kwami, "")
    coins = user.get("coins", 0)
    wins = user.get("wins", 0)
    streak = user.get("streak", 0)

    await update.message.reply_text(
        f"👤 ПРОФИЛЬ\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"🆔 @{user['username']}\n"
        f"🔮 Квами: {kwami}\n"
        f"📖 {desc}\n"
        f"🥐 Круассаны: {coins}\n"
        f"🏆 Побед: {wins}\n"
        f"🔥 Серия: {streak} дней"
    )

# =========================================================
# /USE (ответ на сообщение)
# =========================================================

async def use_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 Чтобы применить способность, нажми кнопку '🎲 Применить способность'."
    )

# =========================================================
# FLASK
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("use", use_command))

    app.add_handler(MessageHandler(filters.Regex("^👤 Профиль$"), profile))
    app.add_handler(MessageHandler(filters.Regex("^🎲 Получить способность$"), claim_ability))
    app.add_handler(MessageHandler(filters.Regex("^🎲 Применить способность$"), use_ability))

    thread = threading.Thread(target=run_flask)
    thread.daemon = True
    thread.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
