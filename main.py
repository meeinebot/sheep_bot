import asyncio
import os
import random
import threading
from datetime import datetime, timedelta, timezone

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
# УТИЛИТЫ ВРЕМЕНИ
# =========================================================

def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)

def to_naive_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

# =========================================================
# КВАМИ
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
    "Пчёлка": {
        "emoji": "🐝",
        "desc": "Использует чужую способность."
    },
}

KWAMI_NAMES = list(KWAMI_LIST.keys())

# Кулдауны
RELIC_DURATION = 24 * 3600       # 24 часа — реликвия
ABILITY_COOLDOWN = 11 * 3600 + 59 * 60  # 11ч 59м — кулдаун способности

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
    return user

async def save_user(user):
    await players_collection.update_one(
        {"user_id": user["user_id"]},
        {"$set": user},
        upsert=True
    )

def format_timer(seconds_left):
    """Форматирует секунды в часы и минуты."""
    if seconds_left < 0:
        seconds_left = 0
    hours = int(seconds_left // 3600)
    minutes = int((seconds_left % 3600) // 60)
    return f"{hours} часов {minutes} минут"

def get_kwami_emoji(user):
    """Возвращает эмодзи текущей реликвии или 🦋 по умолчанию."""
    kwami = user.get("kwami")
    if kwami and kwami in KWAMI_LIST:
        return KWAMI_LIST[kwami]["emoji"]
    return "🦋"

# =========================================================
# ПРОВЕРКА РЕЛИКВИИ (24ч)
# =========================================================

async def check_relic_expired(user):
    """
    Проверяет, не истекла ли реликвия (24 часа).
    Если истекла — сбрасывает kwami и ability_used.
    Возвращает True, если реликвия истекла.
    """
    now = now_utc()
    last_claim = to_naive_utc(user.get("last_claim"))

    if not last_claim:
        return False

    if (now - last_claim).total_seconds() >= RELIC_DURATION:
        # Реликвия истекла — сбрасываем всё
        user["kwami"] = None
        user["last_claim"] = None
        user["last_use"] = None
        user["ability_used"] = False
        user["bee_stage"] = 0
        user["bee_stolen_kwami"] = None
        await save_user(user)
        return True

    return False

# =========================================================
# /PROFILE
# =========================================================

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)
    await check_relic_expired(user)

    now = now_utc()
    kwami = user.get("kwami")

    # ─── Текст профиля ───
    if kwami:
        emoji = KWAMI_LIST[kwami]["emoji"]
        # Реликвия активна
        # Определяем таймер
        if user.get("ability_used") and user.get("last_use"):
            last_use = to_naive_utc(user["last_use"])
            diff = (now - last_use).total_seconds()
            if diff < ABILITY_COOLDOWN:
                seconds_left = ABILITY_COOLDOWN - diff
                timer_line = f"🧠 Следующая способность через: {format_timer(seconds_left)}"
            else:
                timer_line = "🧠 Способность готова!"
        else:
            timer_line = "🧠 Способность готова!"

        relic_status = "Активна"
        header = f"{emoji} Реликвия: {relic_status}"
    else:
        # Реликвия неактивна — показываем таймер до новой
        last_claim = to_naive_utc(user.get("last_claim"))
        if last_claim:
            diff = (now - last_claim).total_seconds()
            seconds_left = max(0, RELIC_DURATION - diff)
            timer_line = f"🧠 Следующая способность через: {format_timer(seconds_left)}"
        else:
            timer_line = "🧠 Способность не получена"

        header = "🦋 Реликвия: Неактивна"

    text = (
        f"{header}\n"
        f"💎 Кристаллы: {user.get('coins', 0)}\n"
        f"{timer_line}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Моя статистика", callback_data="stats")]
    ])

    await update.message.reply_text(text, reply_markup=keyboard)

# =========================================================
# /POWER
# =========================================================

async def power(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)

    # Сначала проверяем, не истекла ли реликвия
    await check_relic_expired(user)

    now = now_utc()

    # ─── Случай 1: Реликвии нет — выдаём новую ───
    if not user.get("kwami"):
        last_claim = to_naive_utc(user.get("last_claim"))
        if last_claim:
            diff = (now - last_claim).total_seconds()
            if diff < RELIC_DURATION:
                # Ещё рано — показываем таймер
                seconds_left = RELIC_DURATION - diff
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
                ])
                await update.message.reply_text(
                    f"⏳ Способность уже получена!\n"
                    f"⏳ Подожди: {format_timer(seconds_left)}",
                    reply_markup=keyboard
                )
                return

        # Выдаём новую реликвию
        kwami = random.choice(KWAMI_NAMES)
        user["kwami"] = kwami
        user["last_claim"] = now
        user["last_use"] = None
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

    # ─── Случай 2: Способность уже использована — показываем таймер ───
    if user.get("ability_used") and user.get("last_use"):
        last_use = to_naive_utc(user["last_use"])
        diff = (now - last_use).total_seconds()

        if diff < ABILITY_COOLDOWN:
            seconds_left = ABILITY_COOLDOWN - diff
            emoji = get_kwami_emoji(user)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
            ])
            await update.message.reply_text(
                f"{emoji} Способность уже применена!\n"
                f"⏳ Подожди: {format_timer(seconds_left)}",
                reply_markup=keyboard
            )
            return
        else:
            # Кулдаун прошёл — сбрасываем флаг
            user["ability_used"] = False
            user["last_use"] = None
            await save_user(user)

    # ─── Случай 3: Применяем способность ───
    kwami = user["kwami"]

    # Пчела — трёхэтапное применение (заглушка)
    if kwami == "Пчёлка":
        stage = user.get("bee_stage", 0)
        if stage == 0:
            user["bee_stage"] = 1
            await save_user(user)
            await update.message.reply_text(
                "🐝 Ты активировала Пчёлку!\n"
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
                f"⏳ Подожди: 11 часов 59 минут",
                reply_markup=keyboard
            )
            return

    # Обычные способности
    user["ability_used"] = True
    user["last_use"] = now
    await save_user(user)

    emoji = KWAMI_LIST[kwami]["emoji"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
    ])
    await update.message.reply_text(
        f"{emoji} Способность применена!\n"
        f"⏳ Подожди: 11 часов 59 минут",
        reply_markup=keyboard
    )

# =========================================================
# ОБРАБОТЧИК КНОПОК
# =========================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # закрываем "часики" на кнопке

    if query.data == "stats":
        await query.answer(
            "⏳ Функция «📊 Моя статистика» находится в разработке!",
            show_alert=True
        )
        return

    if query.data == "reset_timer":
        await query.answer(
            "⏳ Функция «🔄 Сбросить таймер» находится в разработке!",
            show_alert=True
        )
        return

    if query.data == "change_relic":
        await query.answer(
            "⏳ Функция «🔄 Смена реликвии» находится в разработке!",
            show_alert=True
        )
        return

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
