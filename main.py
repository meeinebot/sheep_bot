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

RELIC_DURATION = 24 * 3600                # 24 часа — реликвия
ABILITY_COOLDOWN = 11 * 3600 + 59 * 60    # 11ч 59м — кулдаун способности

# =========================================================
# РАБОТА С БАЗОЙ
# =========================================================

async def get_user(user_id, username):
    user = await players_collection.find_one({"user_id": user_id})
    if not user:
        user = {
            "user_id": user_id,
            "username": username,
            "kwami": None,                  # реликвии нет изначально
            "last_emoji": "🦋",             # дефолт — 🦋 (до первой реликвии)
            "last_claim": None,
            "last_use": None,
            "ability_used": False,
            "frozen_until": None,
            "shield_active": False,
            "shield_until": None,
            "linked_to": None,
            "bee_stage": 0,
            "bee_stolen_kwami": None,
            "bee_target_kwami": None,
            "bee_target_user": None,
            "awaiting_target": False,
            "awaiting_kwami": None,
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
    """Эмодзи последней реликвии. НЕ сбрасывается при истечении."""
    return user.get("last_emoji") or "🦋"

def get_relic_name_by_emoji(emoji):
    for name, data in KWAMI_LIST.items():
        if data["emoji"] == emoji:
            return name
    return None

# =========================================================
# ПРОВЕРКА РЕЛИКВИИ
# =========================================================

async def check_relic_expired(user):
    """Если реликвия истекла — сбрасываем kwami, но last_emoji и last_claim НЕ трогаем."""
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
        user["bee_stolen_kwami"] = None
        user["bee_target_kwami"] = None
        user["bee_target_user"] = None
        user["awaiting_target"] = False def
        user["awaiting_kwami"] = None
        # last_emoji и last_claim НЕ трогаем!
        await save_user(user)
        return True
    return False

# =========================================================
# /PROFILE
# =========================================================

async profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)
    await check_relic_expired(user)

    now = now_utc()
    emoji = get_kwami_emoji(user)
    relic_name = get_relic_name_by_emoji(emoji)

    if user.get("kwami") and relic_name:
        header = f"{emoji} Реликвия: {relic_name}"
    else:
        header = f"{emoji} Реликвия: Неактивна"

    # Таймер следующей способности/реликвии
    last_claim = to_naive_utc(user.get("last_claim"))
    if last_claim:
        diff_claim = (now - last_claim).total_seconds()
    else:
        diff_claim = RELIC_DURATION + 1  # нет реликвии — сразу можно получить

    if user.get("kwami") and user.get("ability_used") and user.get("last_use"):
        # Кулдаун способности идёт
        last_use = to_naive_utc(user["last_use"])
        diff_use = (now - last_use).total_seconds()
        if diff_use < ABILITY_COOLDOWN:
            seconds_left = ABILITY_COOLDOWN - diff_use
            timer_line = f"🧠 Следующая способность через: {format_timer(seconds_left)}"
        else:
            timer_line = "🧠 Способность готова к применению!"
    elif user.get("kwami") and not user.get("ability_used"):
        timer_line = "🧠 Способность готова к применению!"
    else:
        # Реликвии нет — показываем таймер до новой реликвии
        if diff_claim < RELIC_DURATION:
            seconds_left = RELIC_DURATION - diff_claim
            timer_line = f"🧠 Следующая способность через: {format_timer(seconds_left)}"
        else:
            timer_line = "🧠 Способность готова к применению!"

    text = f"{header}\n{timer_line}"

    await update.message.reply_text(text)

# =========================================================
# /POWER
# =========================================================

async def power(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id, update.effective_user.username)
    await check_relic_expired(user)
    now = now_utc()

    # --- Случай 1: реликвии нет — выдаём новую ---
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

        kwami = random.choice(KWAMI_NAMES)
        user["kwami"] = kwami
        user["last_claim"] = now
        user["last_use"] = None
        user["ability_used"] = False
        user["bee_stage"] = 0
        user["bee_stolen_kwami"] = None
        user["bee_target_kwami"] = None
        user["bee_target_user"] = None
        user["last_emoji"] = KWAMI_LIST[kwami]["emoji"]
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

    # --- Случай 2: кулдаун способности ---
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

    # --- Случай 3: применение способности ---
    kwami = user["kwami"]

    # Пчёлка — трёхэтапная
    if kwami == "Пчёлка":
        stage = user.get("bee_stage", 0)
        if stage == 0:
            user["bee_stage"] = 1
            user["awaiting_target"] = True
            user["awaiting_kwami"] = "Пчёлка"
            await save_user(user)
            await update.message.reply_text(
                "🐝 Ты активировала Пчёлку!\n"
                "🎯 Ответь на сообщение игрока, чью способность хочешь украсть."
            )
            return

    # Обычные способности — просим ответить на цель
    user["awaiting_target"] = True
    user["awaiting_kwami"] = kwami
    await save_user(user)

    emoji = KWAMI_LIST[kwami]["emoji"]
    await update.message.reply_text(
        f"{emoji} Способность активирована!\n"
        f"🎯 Ответь на сообщение игрока, на кого применить."
    )

# =========================================================
# ОБРАБОТЧИК REPLY — АТАКИ НА ИГРОКОВ
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
    if target_tg.id == user["user_id"]:
        await update.message.reply_text("❌ Нельзя применить способность на себя!")
        return

    target = await get_user(target_tg.id, target_tg.username)
    kwami = user.get("awaiting_kwami") or user.get("kwami")
    now = now_utc()

    attacker_name = user.get("username") or "игрок"
    target_name = target.get("username") or "игрок"

    # --- Пчёлка (двухэтапная) ---
    if kwami == "Пчёлка":
        stage = user.get("bee_stage", 0)
        if stage == 1:
            # Крадём квами цели
            if not target.get("kwami"):
                await update.message.reply_text(
                    "❌ У этого игрока нет активной реликвии — красть нечего!"
                )
                user["awaiting_target"] = False
                user["awaiting_kwami"] = None
                user["bee_stage"] = 0
                await save_user(user)
                return
            user["bee_stolen_kwami"] = target["kwami"]
            user["bee_target_user"] = target["user_id"]
            user["bee_stage"] = 2
            user["awaiting_target"] = True
            user["awaiting_kwami"] = "Пчёлка"
            await save_user(user)
            stolen_emoji = KWAMI_LIST[target["kwami"]]["emoji"]
            await update.message.reply_text(
                f"🐝 Ты украла способность {stolen_emoji} @{target_name}!\n"
                f"🎯 Теперь ответь на сообщение игрока, на кого применить украденную способность."
            )
            return
        elif stage == 2:
            # Применяем украденную способность
            stolen = user.get("bee_stolen_kwami")
            user["awaiting_target"] = False
            user["awaiting_kwami"] = None
            user["bee_stage"] = 0
            user["bee_stolen_kwami"] = None
            user["bee_target_user"] = None

            text = await apply_ability(user, target, stolen, attacker_name, target_name, now)
            user["ability_used"] = True
            user["last_use"] = now
            await save_user(user)

            emoji = KWAMI_LIST[user["kwami"]]["emoji"] if user.get("kwami") else "🐝"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
            ])
            await update.message.reply_text(
                f"{text}\n\n"
                f"{emoji} Способность применена!\n"
                f"⏳ Подожди: 11 часов 59 минут",
                reply_markup=keyboard
            )
            return

    # --- Обычные способности ---
    user["awaiting_target"] = False
    user["awaiting_kwami"] = None

    # Черепаха — цель не нужна, но защищаем себя
    text = await apply_ability(user, target, kwami, attacker_name, target_name, now)

    user["ability_used"] = True
    user["last_use"] = now
    await save_user(user)

    emoji = KWAMI_LIST[kwami]["emoji"] if kwami in KWAMI_LIST else "✨"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Сбросить таймер", callback_data="reset_timer")]
    ])
    await update.message.reply_text(
        f"{text}\n\n"
        f"{emoji} Способность применена!\n"
        f"⏳ Подожди: 11 часов 59 минут",
        reply_markup=keyboard
    )

# =========================================================
# ПРИМЕНЕНИЕ СПОСОБНОСТИ
# =========================================================

async def apply_ability(user, target, kwami, attacker_name, target_name, now):
    """Применяет способность, возвращает текст результата."""
    if kwami == "Божья коровка":
        target["ability_used"] = False
        target["last_use"] = None
        await save_user(target)
        return (
            f"🐞 @{attacker_name} отменил способность @{target_name}!\n"
            f"✨ Способность цели восстановлена."
        )

    elif kwami == "Чёрный кот":
        target["frozen_until"] = now + timedelta(hours=12)
        await save_user(target)
        return (
            f"🐈‍⬛ @{attacker_name} применил Катастрофу на @{target_name}!\n"
            f"❄️ Цель заморожена на 12 часов."
        )

    elif kwami == "Черепаха":
        user["shield_active"] = True
        user["shield_until"] = now + timedelta(hours=12)
        await save_user(user)
        return (
            f"🐢 @{attacker_name} активировал щит Черепахи!\n"
            f"🛡️ Иммунитет на 12 часов."
        )

    elif kwami == "Рыжая лисица":
        user["kwami"], target["kwami"] = target.get("kwami"), user.get("kwami")
        # Обновляем эмодзи
        if target.get("kwami"):
            target["last_emoji"] = KWAMI_LIST[target["kwami"]]["emoji"]
        if user.get("kwami"):
            user["last_emoji"] = KWAMI_LIST[user["kwami"]]["emoji"]
        await save_user(target)
        await save_user(user)
        return (
            f"🦊 @{attacker_name} обменялся реликвиями с @{target_name}!\n"
            f"🔄 Способности поменялись местами."
        )

    elif kwami == "Павлин":
        target["linked_to"] = user["user_id"]
        await save_user(target)
        return (
            f"🦚 @{attacker_name} привязался к @{target_name}!\n"
            f"🔗 Теперь вы связаны."
        )

    elif kwami == "Мотылёк":
        target["ability_used"] = False
        target["last_use"] = None
        await save_user(target)
        return (
            f"🦋 @{attacker_name} удвоил способность @{target_name}!\n"
            f"✨ Цель может применить способность ещё раз."
        )

    return f"❓ @{attacker_name} применил неизвестную способность."

# =========================================================
# ОБРАБОТЧИК КНОПОК
# =========================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

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

    if query.data == "stats":
        await query.answer(
            "⏳ Функция «📊 Моя статистика» находится в разработке!",
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
