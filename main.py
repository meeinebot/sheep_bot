import asyncio
import os
import random
import time
import threading
from datetime import datetime, timedelta

import motor.motor_asyncio
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================
# НАСТРОЙКИ
# =========================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

MONGO_URL = os.getenv(
    "MONGO_URL",
    "mongodb+srv://sheep_bot:meewoolbotsheep199@cluster0.jjp6pia.mongodb.net/?appName=Cluster0"
)

mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = mongo_client["miraculous_battle"]
players_collection = db["players"]

# =========================================================
# КВАМИ (ВСЕ 19)
# =========================================================

FREE_KWAMI = ["Тикки", "Плагг", "Трикс", "Вэйзз", "Нууру", "Дуусу", "Поллен"]

KWAMI_ABILITIES = {
    "Тикки": "Отменяет способность любого игрока",
    "Плагг": "Замораживает игрока на 12 часов",
    "Трикс": "Меняет способности двух игроков местами",
    "Вэйзз": "Щит для всех (кроме себя) на 6ч",
    "Нууру": "Удваивает чужую способность",
    "Дуусу": "Привязывается к игроку",
    "Поллен": "Крадёт ход и применяет на цель",
    "Барк": "Узнаёт и блокирует Квами цели",
    "Шуппу": "Ломает способность цели на 6ч",
    "Орикко": "Выбирает любого Квами на день",
    "Зигги": "Рандомная способность союзнику",
    "Каалки": "Передаёт ход",
    "Сасс": "Ловушка: способность сгорает",
    "Лонг": "Всем случайные способности на 1ч",
    "Флафф": "Откат игрока на 12ч",
    "Роаарр": "Снимает все заморозки",
    "Стомпп": "Ломает щит и замораживает на 6ч",
    "Мулло": "2 способности за день",
    "Даиззи": "Перемирие между игроками",
}

PAID_KWAMI = [k for k in KWAMI_ABILITIES.keys() if k not in FREE_KWAMI]

# =========================================================
# КНОПКИ
# =========================================================

main_kb = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🎲 Взять Квами"), KeyboardButton("👤 Профиль")],
        [KeyboardButton("⚔️ Применить"), KeyboardButton("📦 Магазин")],
        [KeyboardButton("🏆 Топ"), KeyboardButton("🎁 Шкатулка")],
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
            "kwami": "",
            "eq_kwami": "",
            "coins": 0,
            "wins": 0,
            "daily_streak": 0,
            "last_take": None,
            "frozen_until": None,
            "shield_until": None,
            "banned": False,
        }
        await players_collection.insert_one(user)
    elif user.get("username") != username:
        await players_collection.update_one({"user_id": user_id}, {"$set": {"username": username}})
        user["username"] = username
    return user

async def save_user(user):
    await players_collection.update_one({"user_id": user["user_id"]}, {"$set": user})

# =========================================================
# КОМАНДЫ
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)
    await update.message.reply_text(
        f"🐞 Привет, {update.effective_user.first_name}!\n"
        "Это **Miraculous: Битва Квами**.\n"
        "🎲 Бери Квами, сражайся и копи круассаны!\n"
        "Используй кнопки ниже.",
        reply_markup=main_kb
    )

async def take_kwami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)

    if user.get("banned"):
        await update.message.reply_text("❌ Ты забанен.")
        return

    if user.get("frozen_until") and user["frozen_until"] > datetime.now():
        remain = (user["frozen_until"] - datetime.now()).seconds // 3600
        await update.message.reply_text(f"❌ Ты заморожен ещё {remain} часов.")
        return

    last_take = user.get("last_take")
    if last_take:
        if (datetime.now() - last_take).days < 1:
            await update.message.reply_text("⏳ Ты уже брал Квами сегодня. Жди завтра!")
            return

    kwami = random.choice(FREE_KWAMI)
    user["kwami"] = kwami
    user["last_take"] = datetime.now()
    user["coins"] = user.get("coins", 0) + 5
    await save_user(user)

    await update.message.reply_text(
        f"✨ Ты получил: **{kwami}**!\n"
        f"{KWAMI_ABILITIES[kwami]}\n"
        f"🥐 +5 круассанов (всего: {user['coins']})"
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)
    kwami = user.get("kwami") or "Нет"
    coins = user.get("coins", 0)
    wins = user.get("wins", 0)
    days = 0
    if user.get("last_take"):
        days = (datetime.now() - user["last_take"]).days

    await update.message.reply_text(
        f"👤 **ПРОФИЛЬ**\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🆔 @{user['username']}\n"
        f"🐞 Квами: {kwami}\n"
        f"🥐 Круассаны: {coins}\n"
        f"🏆 Побед: {wins}\n"
        f"📅 В игре: {days} дн."
    )

async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)
    text = (
        f"🏪 **МАГАЗИН**\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🥐 Баланс: {user.get('coins', 0)}\n\n"
        f"🔹 Удвоение силы — 50 🥐\n"
        f"🔹 Защита от заморозки — 30 🥐\n"
        f"🔹 Смена Квами — 25 🥐\n"
        f"🔹 Платный Квами — 250 🥐 (любой)\n"
    )
    await update.message.reply_text(text)

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor = players_collection.find().sort("coins", -1).limit(5)
    users = await cursor.to_list(length=5)
    if not users:
        await update.message.reply_text("Пока нет игроков.")
        return
    text = "🏆 **ТОП ИГРОКОВ**\n━━━━━━━━━━━━━━━\n"
    for i, u in enumerate(users, 1):
        text += f"{i}. @{u.get('username', 'unknown')} — 🥐 {u.get('coins', 0)}\n"
    await update.message.reply_text(text)

async def box(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)
    if user.get("coins", 0) < 100:
        await update.message.reply_text("❌ Нужно 100 круассанов для шкатулки.")
        return
    user["coins"] -= 100
    kwami = random.choice(PAID_KWAMI)
    user["eq_kwami"] = kwami
    await save_user(user)
    await update.message.reply_text(
        f"🎁 Ты открыл шкатулку и получил: **{kwami}**!\n"
        f"{KWAMI_ABILITIES[kwami]}\n"
        f"🥐 Осталось: {user['coins']}"
    )

async def use_kwami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚔️ Выбери цель (@username). Пока просто демо.")

# =========================================================
# FLASK
# =========================================================

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Miraculous: Битва Квами — бот работает!"

def run_flask():
    port = int(os.getenv("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

# =========================================================
# ЗАПУСК
# =========================================================

async def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^🎲 Взять Квами$"), take_kwami))
    app.add_handler(MessageHandler(filters.Regex("^👤 Профиль$"), profile))
    app.add_handler(MessageHandler(filters.Regex("^📦 Магазин$"), shop))
    app.add_handler(MessageHandler(filters.Regex("^🏆 Топ$"), top))
    app.add_handler(MessageHandler(filters.Regex("^🎁 Шкатулка$"), box))
    app.add_handler(MessageHandler(filters.Regex("^⚔️ Применить$"), use_kwami))

    thread = threading.Thread(target=run_flask)
    thread.daemon = True
    thread.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
