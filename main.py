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

def format_timer(seconds_left):
    if seconds_left < 0:
        seconds_left = 0
    hours = int(seconds_left // 3600)
    minutes = int((seconds_left % 3600) // 60)
    return f"{hours} часов {minutes} минут"

# =========================================================
# КВАМИ
# =========================================================

KWAMI_LIST = {
    "Божья коровка": {
        "emoji": "🐞",
        "desc": "Отменяет способность игрока"
    },
    "Чёрный кот": {
        "emoji": "🐈‍⬛",
        "desc": "Замораживает игрока на 12 часов"
    },
    "Рыжая лисица": {
        "emoji": "🦊",
        "desc": "Меняет реликвии между двумя игроками"
    },
    "Черепаха": {
        "emoji": "🐢",
        "desc": "Дает иммунитет к атакам на 12 часов"
    },
    "Павлин": {
        "emoji": "🦚",
        "desc": "Привязывается к любому игроку"
    },
    "Бражник": {
        "emoji": "🦋",
        "desc": "Выдаёт дополнительный ход"
    },
    "Пчёлка": {
        "emoji": "🐝",
        "desc": "Крадёт ход и использует его"
    },
}

KWAMI_NAMES = list(KWAMI_LIST.keys())

RELIC_DURATION = 24 * 3600
ABILITY_COOLDOWN = 11 * 3600 + 59 * 60

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
            "last_emoji": "🐞",
            "last_claim": None,
            "last_use": None,
            "ability_used": False,
            "frozen_until": None,
            "shield_active": False,
            "shield_until": None,
            "linked_to": None,
            "bee_stage": 0,
            "bee_stolen_user": None,
            "bee_target_user": None,
            "fox_stage": 0,
            "fox_first_target": None,
            "awaiting_target": False,
            "awaiting_kwami": None,
            "is_new": True,
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

def get_kwami_emoji(user):
    return user.get("last_emoji") or "🐞"

def get_relic_name_by_emoji(emoji):
    for name, data in KWAMI_LIST.items():
        if data["emoji"] == emoji:
            return name
    return None

# =========================================================
# ПРОВЕРКИ
# =========================================================

async def check_relic_expired(user):
    now = now_utc()
    if not user.get("kwami"):
        return False
    last_claim = to_naive_utc(user.get("last_claim"))
    if not last_claim:
        return False
    if (now - last_claim).total_seconds() >= RELIC_DURATION:
        user["kwami"] = None
        user["last_use"] = None
        user["ability_used"] = False
        user["bee_stage"] = 0
        user["bee_stolen_user"] = None
        user["bee_target_user"] = None
        user["fox_stage"] = 0
        user["fox_first_target"] = None
        user["awaiting_target"] = False
        user["awaiting_kwami"] = None
        await save_user(user)
        return True
    return False

def is_frozen(user):
    frozen_until = to_naive_utc(user.get("frozen_until"))
    if not frozen_until:
        return False
    return now_utc() < frozen_until

def frozen_seconds_left(user):
    frozen_until = to_naive_utc(user.get("frozen_until"))
    if not frozen_until:
        return 0
    return (frozen_until - now_utc()).total_seconds()

# =========================================================
# /PROFILE
# =========================================================

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)
    await check_relic_expired(user)

    now = now_utc()
    emoji = get_kwami_emoji(user)

    if user.get("ability_used") and user.get("last_use"):
        last_use = to_naive_utc(user["last_use"])
        diff = (now - last_use).total_seconds()
        if diff < ABILITY_COOLDOWN:
            relic_status = "Активна"
        else:
            relic_status = "Неактивна"
    else:
        relic_status = "Неактивна"

    last_claim = to_naive_utc(user.get("last_claim"))
    if last_claim:
        diff_claim = (now - last_claim).total_seconds()
    else:
        diff_claim = RELIC_DURATION + 1

    if relic_status == "Активна" and user.get("last_use"):
        last_use = to_naive_utc(user["last_use"])
        diff_use = (now - last_use).total_seconds()
        seconds_left = ABILITY_COOLDOWN - diff_use
        timer_line = f"⏳ Следующая способность через: {format_timer(seconds_left)}"
    else:
        if diff_claim < RELIC_DURATION:
            seconds_left = RELIC_DURATION - diff_claim
            timer_line = f"⏳ Следующая способность через: {format_timer(seconds_left)}"
        else:
            timer_line = "⏳ Следующая способность через: 23 часа 59 минут"

    text = f"{emoji} Реликвия: {relic_status}\n{timer_line}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Смена реликвии", callback_data="change_relic")]
    ])

    await update.message.reply_text(text, reply_markup=keyboard)

# =========================================================
# /POWER
# =========================================================

async def power(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)
    await check_relic_expired(user)
    now = now_utc()

    if is_frozen(user):
        seconds_left = frozen_seconds_left(user)
        await update.message.reply_text(
            f"🐈‍⬛ Вы заморожены!\n"
            f"⏳ Подожди: {format_timer(seconds_left)}"
        )
        return

    if not user.get("kwami"):
        last_claim = to_naive_utc(user.get("last_claim"))
        if last_claim:
            diff = (now - last_claim).total_seconds()
            if diff < RELIC_DURATION:
                seconds_left = RELIC_DURATION - diff
                emoji = get_kwami_emoji(user)
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
                ])
                await update.message.reply_text(
                    f"{emoji} Способность уже получена!\n"
                    f"⏳ Подожди: {format_timer(seconds_left)}",
                    reply_markup=keyboard
                )
                return

        if user.get("is_new"):
            kwami = "Божья коровка"
            user["is_new"] = False
        else:
            kwami = random.choice(KWAMI_NAMES)

        user["kwami"] = kwami
        user["last_claim"] = now
        user["last_use"] = None
        user["ability_used"] = False
        user["bee_stage"] = 0
        user["bee_stolen_user"] = None
        user["bee_target_user"] = None
        user["fox_stage"] = 0
        user["fox_first_target"] = None
        user["last_emoji"] = KWAMI_LIST[kwami]["emoji"]
        await save_user(user)

        emoji = KWAMI_LIST[kwami]["emoji"]
        desc = KWAMI_LIST[kwami]["desc"]
        await update.message.reply_text(
            f"{emoji} Реликвия получена!\n"
            f"🎲 Способность: {desc}"
        )
        return

    if user.get("ability_used") and user.get("last_use"):
        last_use = to_naive_utc(user["last_use"])
        diff = (now - last_use).total_seconds()
        if diff < ABILITY_COOLDOWN:
            seconds_left = ABILITY_COOLDOWN - diff
            emoji = KWAMI_LIST[user["kwami"]]["emoji"]
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
            user["ability_used"] = False
            user["last_use"] = None
            await save_user(user)

    kwami = user["kwami"]

    if kwami == "Пчёлка":
        user["bee_stage"] = 1
        user["awaiting_target"] = True
        user["awaiting_kwami"] = "Пчёлка"
        await save_user(user)
        await update.message.reply_text(
            "🐝 Ты активировала Пчёлку!\n"
            "🎯 Ответь на сообщение игрока, у кого хочешь украсть ход."
        )
        return

    if kwami == "Рыжая лисица":
        user["fox_stage"] = 1
        user["awaiting_target"] = True
        user["awaiting_kwami"] = "Рыжая лисица"
        await save_user(user)
        await update.message.reply_text(
            "🦊 Ты активировала Лису!\n"
            "🎯 Ответь на сообщение первого игрока для обмена."
        )
        return

    user["awaiting_target"] = True
    user["awaiting_kwami"] = kwami
    await save_user(user)

    emoji = KWAMI_LIST[kwami]["emoji"]
    await update.message.reply_text(
        f"{emoji} Способность активирована!\n"
        f"🎯 Ответь на сообщение игрока, на кого применить."
    )

# =========================================================
# ОБРАБОТЧИК REPLY
# =========================================================

async def handle_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return
    if not update.message.reply_to_message.from_user:
        return

    user = await get_user(update.effective_user.id, update.effective_user.username)

    if not user.get("awaiting_target"):
        return

    target_tg = update.message.reply_to_message.from_user

    if target_tg.is_bot:
        emoji = get_kwami_emoji(user)
        await update.message.reply_text(
            f"{emoji} Реликвия не применена!\n"
            f"🎲 Ответь командой в ответ на сообщение игрока"
        )
        return

    if target_tg.id == user["user_id"]:
        await update.message.reply_text("❌ Нельзя применить способность на себя!")
        return

    target = await get_user(target_tg.id, target_tg.username)
    kwami = user.get("awaiting_kwami") or user.get("kwami")
    now = now_utc()

    if kwami == "Пчёлка":
        stage = user.get("bee_stage", 0)
        if stage == 1:
            user["bee_stolen_user"] = target["user_id"]
            user["bee_stage"] = 2
            user["awaiting_target"] = True
            user["awaiting_kwami"] = "Пчёлка"
            await save_user(user)
            await update.message.reply_text(
                "🐝 Ты украла ход!\n"
                "🎯 Теперь ответь на сообщение игрока, на кого применить украденный ход."
            )
            return
        elif stage == 2:
            user["awaiting_target"] = False
            user["awaiting_kwami"] = None
            user["bee_stage"] = 0
            user["bee_stolen_user"] = None
            user["bee_target_user"] = None

            user["ability_used"] = True
            user["last_use"] = now
            await save_user(user)

            emoji = KWAMI_LIST["Пчёлка"]["emoji"]
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
            ])
            await update.message.reply_text(
                f"{emoji} Реликвия применена!\n"
                f"🎲 Крадёт ход и использует его\n\n"
                f"⏳ Подожди: 11 часов 59 минут",
                reply_markup=keyboard
            )
            return

    if kwami == "Рыжая лисица":
        stage = user.get("fox_stage", 0)
        if stage == 1:
            user["fox_first_target"] = target["user_id"]
            user["fox_stage"] = 2
            user["awaiting_target"] = True
            user["awaiting_kwami"] = "Рыжая лисица"
            await save_user(user)
            await update.message.reply_text(
                "🦊 Первый игрок выбран!\n"
                "🎯 Теперь ответь на сообщение второго игрока для обмена."
            )
            return
        elif stage == 2:
            first_id = user.get("fox_first_target")
            if first_id == target["user_id"]:
                await update.message.reply_text(
                    "❌ Нельзя выбрать одного и того же игрока дважды!"
                )
                return

            first_user = await players_collection.find_one({"user_id": first_id})
            if first_user:
                first_user["kwami"], target["kwami"] = target.get("kwami"), first_user.get("kwami")
                if first_user.get("kwami"):
                    first_user["last_emoji"] = KWAMI_LIST[first_user["kwami"]]["emoji"]
                if target.get("kwami"):
                    target["last_emoji"] = KWAMI_LIST[target["kwami"]]["emoji"]
                await save_user(first_user)
                await save_user(target)

            user["awaiting_target"] = False
            user["awaiting_kwami"] = None
            user["fox_stage"] = 0
            user["fox_first_target"] = None

            user["ability_used"] = True
            user["last_use"] = now
            await save_user(user)

            emoji = KWAMI_LIST["Рыжая лисица"]["emoji"]
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
            ])
            await update.message.reply_text(
                f"{emoji} Реликвия применена!\n"
                f"🎲 Меняет реликвии между двумя игроками\n\n"
                f"⏳ Подожди: 11 часов 59 минут",
                reply_markup=keyboard
            )
            return

    user["awaiting_target"] = False
    user["awaiting_kwami"] = None

    emoji = KWAMI_LIST[kwami]["emoji"] if kwami in KWAMI_LIST else "✨"

    if kwami == "Божья коровка":
        target["ability_used"] = False
        target["last_use"] = None
        await save_user(target)
        result_text = f"{emoji} Реликвия применена!\n🎲 Способность игрока отменена"

    elif kwami == "Чёрный кот":
        target["frozen_until"] = now + timedelta(hours=12)
        await save_user(target)
        result_text = f"{emoji} Реликвия применена!\n🎲 Игрок заморожен на 12 часов"

    elif kwami == "Черепаха":
        user["shield_active"] = True
        user["shield_until"] = now + timedelta(hours=12)
        await save_user(user)
        result_text = f"{emoji} Реликвия применена!\n🎲 Дает иммунитет к атакам на 12 часов"

    elif kwami == "Павлин":
        target["linked_to"] = user["user_id"]
        await save_user(target)
        result_text = f"{emoji} Реликвия применена!\n🎲 Привязывается к любому игроку"

    elif kwami == "Бражник":
        target["ability_used"] = False
        target["last_use"] = None
        await save_user(target)
        result_text = f"{emoji} Реликвия применена!\n🎲 Выдаёт дополнительный ход"

    else:
        result_text = f"{emoji} Реликвия применена!\n🎲 Неизвестная способность"

    user["ability_used"] = True
    user["last_use"] = now
    await save_user(user)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
    ])
    await update.message.reply_text(
        f"{result_text}\n\n"
        f"⏳ Подожди: 11 часов 59 минут",
        reply_markup=keyboard
    )

# =========================================================
# ОБРАБОТЧИК КНОПОК
# =========================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.data == "change_relic":
        await query.answer("🔄 Смена реликвии скоро будет доступна!", show_alert=True)
        return

    if query.data == "reset_timer":
        await query.answer(
            "⏳ Функция «🔄 Сбросить таймер» находится в разработке!",
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
    app.add_handler(MessageHandler(
        filters.REPLY & ~filters.COMMAND,
        handle_reply
    ))

    thread = threading.Thread(target=run_flask)
    thread.daemon = True
    thread.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
