import asyncio
import os
import random
import threading
from datetime import datetime, timedelta

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
    elif user.get("username") != username:
        await players_collection.update_one({"user_id": user_id}, {"$set": {"username": username}})
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
    await players_collection.update_one({"user_id": user["user_id"]}, {"$set": user})

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
    now = datetime.now()

    # Если способность не получена — выдаём
    if not user.get("kwami"):
        # Проверка кулдауна на получение
        if user.get("last_claim"):
            time_diff = (now - user["last_claim"]).total_seconds()
            if time_diff < 43200:
                hours = int((43200 - time_diff) // 3600)
                minutes = int(((43200 - time_diff) % 3600) // 60)
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
                ])
                await update.message.reply_text(
                    f"⏳ Способность уже получена!\n"
                    f"⏳ Подожди: {hours} часов {minutes} минут",
                    reply_markup=keyboard
                )
                return

        kwami = random.choice(KWAMI_NAMES)
        user["kwami"] = kwami
        user["last_claim"] = now
        user["ability_used"] = False
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

    # Если способность уже использована — показываем таймер
    if user.get("ability_used"):
        # Таймер считается от last_use, а не от last_claim
        if user.get("last_use"):
            time_diff = (now - user["last_use"]).total_seconds()
            if time_diff < 43200:
                hours = int((43200 - time_diff) // 3600)
                minutes = int(((43200 - time_diff) % 3600) // 60)
            else:
                # Таймер истёк — сбрасываем ability_used
                user["ability_used"] = False
                await save_user(user)
                hours = 0
                minutes = 0
        else:
            hours = 12
            minutes = 0

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
        ])
        emoji = KWAMI_LIST[user["kwami"]]["emoji"]
        await update.message.reply_text(
            f"{emoji} Способность уже применена!\n"
            f"⏳ Подожди: {hours} часов {minutes} минут",
            reply_markup=keyboard
        )
        return

    # Применение способности
    kwami = user["kwami"]

    # Пчела — трёхэтапное применение
    if kwami == "Королева пчёл":
        if user.get("bee_stage") == 0:
            user["bee_stage"] = 1
            await save_user(user)
            await update.message.reply_text(
                "🐝 Ты активировала Королеву пчёл!\n"
                "🎯 Ответь на сообщение игрока, чью способность хочешь украсть."
            )
            return
        elif user.get("bee_stage") == 1:
            user["bee_stage"] = 2
            await save_user(user)
            await update.message.reply_text(
                "🐝 Теперь ответь на сообщение игрока, на кого хочешь использовать украденную способность."
            )
            return
        elif user.get("bee_stage") == 2:
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
                f"⏳ Подожди: 12 часов 0 минут",
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
        f"⏳ Подожди: 12 часов 0 минут",
        reply_markup=keyboard
    )

# =========================================================
# ОБРАБОТКА INLINE-КНОПОК (ЗАГЛУШКИ)
# =========================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data in ["buy_relic", "stats", "reset_timer", "change_relic"]:
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
