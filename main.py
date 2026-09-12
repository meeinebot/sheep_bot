import asyncio
import os
import random
import threading
from datetime import datetime, timezone

import motor.motor_asyncio
from flask import Flask
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
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
# УТИЛИТЫ ДЛЯ ВРЕМЕНИ (исправление таймера)
# =========================================================

def now_utc():
    """Naive UTC — единый формат для всей БД."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

def to_naive_utc(dt):
    """Приводит любой datetime (aware/naive) к naive UTC."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

# =========================================================
# КВАМИ (7 БАЗОВЫХ)
# =========================================================

KWAMI_LIST = {
    "Божья коровка": {
        "emoji": "🐞",
        "desc": "Отменяет способность любого игрока."
    },
    "Чёрный кот": {
        "emoji": "🐈‍⬛",
        "desc": "Замораживает любого игрока на 12 часов."
    },
    "Рыжая лисица": {
        "emoji": "🦊",
        "desc": "Меняет способности между двумя игроками."
    },
    "Черепаха": {
        "emoji": "🐢",
        "desc": "Иммунитет от нападений игроков."
    },
    "Павлин": {
        "emoji": "🦚",
        "desc": "Привязывается к любому игроку."
    },
    "Мотылёк": {
        "emoji": "🦋",
        "desc": "Удваивает способность любому игроку."
    },
    "Королева пчёл": {
        "emoji": "🐝",
        "desc": "Использует чужую способность."
    },
}

KWAMI_NAMES = list(KWAMI_LIST.keys())

COOLDOWN_SECONDS = 43200  # 12 часов

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
            "last_use": None,
            "ability_used": False,
            "coins": 0,
            "bee_stage": 0,
            "bee_stolen_kwami": None,
        }
        await players_collection.insert_one(user)
    elif username and user.get("username") != username:
        await players_collection.update_one(
            {"user_id": user_id}, {"$set": {"username": username}}
        )
        user["username"] = username

    # Миграция круассанов в камни
    if user.get("croissants") and not user.get("coins_migrated"):
        user["coins"] = user.get("croissants", 0)
        user["coins_migrated"] = True
        await players_collection.update_one(
            {"user_id": user_id},
            {"$set": {"coins": user["coins"], "coins_migrated": True}}
        )

    return user

async def save_user(user):
    await players_collection.update_one(
        {"user_id": user["user_id"]},
        {"$set": user},
        upsert=True
    )

def format_timer(seconds_left):
    hours = int(seconds_left // 3600)
    minutes = int((seconds_left % 3600) // 60)
    return f"{hours} ч {minutes} мин"

# =========================================================
# КОМАНДА /PROFILE
# =========================================================

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)

    kwami = user.get("kwami")
    if kwami:
        emoji = KWAMI_LIST[kwami]["emoji"]
        ability_text = "🧠 Способность активна"
    else:
        emoji = "🦋"
        ability_text = "🧠 Способность неактивна"

    coins = user.get("coins", 0)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Купить реликвию", callback_data="buy_relic")],
        [InlineKeyboardButton("📊 Моя статистика", callback_data="stats")]
    ])

    await update.message.reply_text(
        f"{emoji} Моя реликвия\n"
        f"💎 Камни чудес: {coins}\n"
        f"{ability_text}",
        reply_markup=keyboard
    )

# =========================================================
# КОМАНДА /POWER
# =========================================================

async def power(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)
    now = now_utc()

    # ---------- 1. Способность ещё не получена ----------
    if not user.get("kwami"):
        last_claim = to_naive_utc(user.get("last_claim"))
        if last_claim:
            time_diff = (now - last_claim).total_seconds()
            if time_diff < COOLDOWN_SECONDS:
                seconds_left = COOLDOWN_SECONDS - time_diff
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
                ])
                await update.message.reply_text(
                    f"⏳ Способность уже получена!\n"
                    f"⏳ Подожди: {format_timer(seconds_left)}",
                    reply_markup=keyboard
                )
                return

        kwami = random.choice(KWAMI_NAMES)
        user["kwami"] = kwami
        user["last_claim"] = now
        user["ability_used"] = False
        user["last_use"] = None
        user["bee_stage"] = 0
        user["bee_stolen_kwami"] = None
        await save_user(user)

        emoji = KWAMI_LIST[kwami]["emoji"]
        desc = KWAMI_LIST[kwami]["desc"]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Сменить реликвию", callback_data="change_relic")]
        ])
        await update.message.reply_text(
            f"{emoji} Реликвия получена!\n"
            f"🎲 Способность: {desc}",
            reply_markup=keyboard
        )
        return

    # ---------- 2. Способность уже применена — показываем таймер ----------
    if user.get("ability_used"):
        last_use = to_naive_utc(user.get("last_use"))
        if not last_use:
            last_use = now
            user["last_use"] = now
            await save_user(user)

        time_diff = (now - last_use).total_seconds()

        if time_diff < COOLDOWN_SECONDS:
            seconds_left = COOLDOWN_SECONDS - time_diff
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
            ])
            emoji = KWAMI_LIST[user["kwami"]]["emoji"]
            await update.message.reply_text(
                f"{emoji} Способность уже применена!\n"
                f"⏳ Подожди: {format_timer(seconds_left)}",
                reply_markup=keyboard
            )
            return
        else:
            # Кулдаун прошёл — сбрасываем флаг, продолжаем к применению
            user["ability_used"] = False
            user["last_use"] = None
            await save_user(user)

    # ---------- 3. Применение способности ----------
    kwami = user["kwami"]

    # Пчела — трёхэтапное применение
    if kwami == "Королева пчёл":
        stage = user.get("bee_stage", 0)

        if stage == 0:
            user["bee_stage"] = 1
            await save_user(user)
            await update.message.reply_text(
                "🐝 Ты активировала Королеву пчёл!\n"
                "🎯 Ответь на сообщение игрока, чью способность хочешь украсть."
            )
            return

        elif stage == 1:
            user["bee_stage"] = 2
            await save_user(user)
            await update.message.reply_text(
                "🐝 Теперь ответь на сообщение игрока, "
                "на кого хочешь использовать украденную способность."
            )
            return

        elif stage == 2:
            user["ability_used"] = True
            user["last_use"] = now
            user["bee_stage"] = 0
            await save_user(user)
            emoji = KWAMI_LIST[kwami]["emoji"]
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
            ])
            await update.message.reply_text(
                f"{emoji} Способность применена!\n"
                f"⏳ Подожди: {format_timer(COOLDOWN_SECONDS)}",
                reply_markup=keyboard
            )
            return

    # Обычные способности
    user["ability_used"] = True
    user["last_use"] = now
    await save_user(user)

    emoji = KWAMI_LIST[kwami]["emoji"]
    desc = KWAMI_LIST[kwami]["desc"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
    ])
    await update.message.reply_text(
        f"{emoji} Способность применена!\n"
        f"🎲 {desc}\n"
        f"⏳ Подожди: {format_timer(COOLDOWN_SECONDS)}",
        reply_markup=keyboard
    )

# =========================================================
# ОБРАБОТКА INLINE-КНОПОК
# =========================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "reset_timer":
        user = await get_user(query.from_user.id, query.from_user.username)
        user["ability_used"] = False
        user["last_use"] = None
        user["last_claim"] = None
        user["bee_stage"] = 0
        await save_user(user)
        await query.message.reply_text(
            "✅ Таймер сброшен! Теперь можно снова использовать /power."
        )
        return

    if query.data in ["buy_relic", "stats", "change_relic"]:
        await query.message.reply_text("⏳ Функция пока в разработке!")

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

    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("power", power))
    app.add_handler(CallbackQueryHandler(button_handler))

    thread = threading.Thread(target=run_flask)
    thread.daemon = True
    thread.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
