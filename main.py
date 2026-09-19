import asyncio
import logging
import os
import random
import threading
from datetime import datetime, timedelta, timezone

import motor.motor_asyncio
from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

MONGO_URL = (
    os.getenv("MONGO_URL")
    or os.getenv("MONGODB_URI")
    or os.getenv("MONGO_URI")
)

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

if not MONGO_URL:
    raise RuntimeError(
        "MONGO_URL, MONGODB_URI or MONGO_URI is not configured"
    )


mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)

db = mongo_client["miraculous_battle"]
players_collection = db["players"]


# =========================================================
# НАСТРОЙКИ
# =========================================================

RELIC_DURATION = 24 * 60 * 60
ABILITY_COOLDOWN = 12 * 60 * 60


# =========================================================
# ВРЕМЯ
# =========================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(
                tzinfo=None
            )

        return value

    return None


def format_timer(seconds_left: float) -> str:
    seconds_left = max(0, int(seconds_left))

    hours = seconds_left // 3600
    minutes = (seconds_left % 3600) // 60

    return f"{hours} часов {minutes} минут"


# =========================================================
# РЕЛИКВИИ
# =========================================================

KWAMI_LIST = {
    "Леди Баг": {
        "emoji": "🐞",
        "desc": "Отменяет способность игрока",
    },
    "Чёрный кот": {
        "emoji": "🐈‍⬛",
        "desc": "Замораживает игрока на 12 часов",
    },
    "Рыжая лисица": {
        "emoji": "🦊",
        "desc": "Меняет реликвии между двумя игроками",
    },
    "Черепаха": {
        "emoji": "🐢",
        "desc": "Даёт иммунитет к атакам на 12 часов",
    },
    "Павлин": {
        "emoji": "🦚",
        "desc": "Привязывается к любому игроку",
    },
    "Бражник": {
        "emoji": "🦋",
        "desc": "Выдаёт дополнительный ход",
    },
    "Пчёлка": {
        "emoji": "🐝",
        "desc": "Крадёт ход и использует его",
    },
}


# Первая реликвия всегда Леди Баг.
FIRST_RELIC = "Леди Баг"

# После первой реликвии случайно может выпасть любая реликвия,
# включая Леди Баг.
RANDOM_RELIC_NAMES = list(KWAMI_LIST.keys())


# =========================================================
# ПОЛЬЗОВАТЕЛИ
# =========================================================

def default_user(user_id: int, username: str | None) -> dict:
    return {
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


async def get_user(
    user_id: int,
    username: str | None,
) -> dict:
    user = await players_collection.find_one(
        {"user_id": user_id}
    )

    if not user:
        user = default_user(user_id, username)
        await players_collection.insert_one(user)
        return user

    changed = False

    if username and user.get("username") != username:
        user["username"] = username
        changed = True

    # Совместимость со старым названием реликвии.
    if user.get("kwami") == "Божья коровка":
        user["kwami"] = "Леди Баг"
        user["last_emoji"] = "🐞"
        changed = True

    if "is_new" not in user:
        user["is_new"] = True
        changed = True

    if changed:
        await save_user(user)

    return user


async def save_user(user: dict) -> None:
    # _id нельзя отправлять внутрь $set.
    data = {
        key: value
        for key, value in user.items()
        if key != "_id"
    }

    await players_collection.update_one(
        {"user_id": user["user_id"]},
        {"$set": data},
        upsert=True,
    )


def relic_emoji(user: dict) -> str:
    kwami = user.get("kwami")

    if kwami in KWAMI_LIST:
        return KWAMI_LIST[kwami]["emoji"]

    return user.get("last_emoji") or "🐞"


# =========================================================
# ПРОВЕРКА ИСТЕЧЕНИЯ РЕЛИКВИИ
# =========================================================

async def check_relic_expired(user: dict) -> bool:
    kwami = user.get("kwami")
    last_claim = to_naive_utc(user.get("last_claim"))

    if not kwami or not last_claim:
        return False

    elapsed = (now_utc() - last_claim).total_seconds()

    if elapsed < RELIC_DURATION:
        return False

    user["kwami"] = None
    user["last_claim"] = None
    user["last_use"] = None
    user["ability_used"] = False

    user["bee_stage"] = 0
    user["bee_stolen_user"] = None
    user["bee_target_user"] = None

    user["fox_stage"] = 0
    user["fox_first_target"] = None

    user["awaiting_target"] = False
    user["awaiting_kwami"] = None

    user["shield_active"] = False
    user["shield_until"] = None

    await save_user(user)

    return True


def is_frozen(user: dict) -> bool:
    frozen_until = to_naive_utc(user.get("frozen_until"))

    if not frozen_until:
        return False

    return now_utc() < frozen_until


def frozen_seconds_left(user: dict) -> float:
    frozen_until = to_naive_utc(user.get("frozen_until"))

    if not frozen_until:
        return 0

    return max(
        0,
        (frozen_until - now_utc()).total_seconds(),
    )


# =========================================================
# /PROFILE
# =========================================================

async def profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    user = await get_user(
        update.effective_user.id,
        update.effective_user.username,
    )

    await check_relic_expired(user)

    if not user.get("kwami"):
        await update.message.reply_text(
            "⌛️ Реликвия готова к получению!"
        )
        return

    now = now_utc()
    emoji = relic_emoji(user)

    if user.get("ability_used") and user.get("last_use"):
        last_use = to_naive_utc(user["last_use"])
        elapsed = (now - last_use).total_seconds()

        if elapsed < ABILITY_COOLDOWN:
            timer_text = (
                "⏳ Следующая способность через: "
                f"{format_timer(ABILITY_COOLDOWN - elapsed)}"
            )
        else:
            timer_text = "✨ Способность готова к активации!"
    else:
        timer_text = "✨ Способность готова к активации!"

    text = (
        f"{emoji} Реликвия: Активна\n"
        f"{timer_text}"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Смена реликвии",
                    callback_data="change_relic",
                )
            ]
        ]
    )

    await update.message.reply_text(
        text,
        reply_markup=keyboard,
    )


# =========================================================
# /POWER
# =========================================================

async def power(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    user = await get_user(
        update.effective_user.id,
        update.effective_user.username,
    )

    await check_relic_expired(user)

    # Если команда /power написана ответом
    # на сообщение игрока, выбираем цель.
    if (
        update.message.reply_to_message
        and user.get("awaiting_target")
    ):
        await handle_reply(update, context)
        return

    if is_frozen(user):
        seconds_left = frozen_seconds_left(user)

        await update.message.reply_text(
            "🐈‍⬛ Вы заморожены!\n"
            f"⏳ Подожди: {format_timer(seconds_left)}"
        )
        return

    now = now_utc()

    # Если реликвии нет, выдаём новую.
    if not user.get("kwami"):
        if user.get("is_new"):
            kwami = FIRST_RELIC
            user["is_new"] = False
        else:
            kwami = random.choice(RANDOM_RELIC_NAMES)

        user["kwami"] = kwami
        user["last_claim"] = now
        user["last_use"] = None
        user["ability_used"] = False

        user["bee_stage"] = 0
        user["bee_stolen_user"] = None
        user["bee_target_user"] = None

        user["fox_stage"] = 0
        user["fox_first_target"] = None

        user["awaiting_target"] = False
        user["awaiting_kwami"] = None

        user["last_emoji"] = KWAMI_LIST[kwami]["emoji"]

        await save_user(user)

        emoji = KWAMI_LIST[kwami]["emoji"]
        description = KWAMI_LIST[kwami]["desc"]

        await update.message.reply_text(
            f"{emoji} Реликвия получена!\n"
            f"🎲 Способность: {description}"
        )
        return

    # Проверяем таймер способности.
    if user.get("ability_used") and user.get("last_use"):
        last_use = to_naive_utc(user["last_use"])
        elapsed = (now - last_use).total_seconds()

        if elapsed < ABILITY_COOLDOWN:
            emoji = relic_emoji(user)

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Сбросить таймер",
                            callback_data="reset_timer",
                        )
                    ]
                ]
            )

            await update.message.reply_text(
                f"{emoji} Способность уже применена!\n"
                f"⏳ Подожди: "
                f"{format_timer(ABILITY_COOLDOWN - elapsed)}",
                reply_markup=keyboard,
            )
            return

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
            "🎯 Ответь командой /power на сообщение игрока, "
            "у которого хочешь украсть ход."
        )
        return

    if kwami == "Рыжая лисица":
        user["fox_stage"] = 1
        user["awaiting_target"] = True
        user["awaiting_kwami"] = "Рыжая лисица"

        await save_user(user)

        await update.message.reply_text(
            "🦊 Ты активировала Лису!\n"
            "🎯 Ответь командой /power на сообщение первого игрока "
            "для обмена."
        )
        return

    user["awaiting_target"] = True
    user["awaiting_kwami"] = kwami

    await save_user(user)

    emoji = KWAMI_LIST[kwami]["emoji"]

    await update.message.reply_text(
        f"{emoji} Способность активирована!\n"
        "🎯 Ответь командой /power на сообщение игрока, "
        "на кого применить способность."
    )


# =========================================================
# ОБРАБОТКА ОТВЕТОВ НА ИГРОКОВ
# =========================================================

async def handle_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    if not update.message.reply_to_message:
        return

    if not update.message.reply_to_message.from_user:
        return

    user = await get_user(
        update.effective_user.id,
        update.effective_user.username,
    )

    await check_relic_expired(user)

    if not user.get("awaiting_target"):
        return

    target_tg = update.message.reply_to_message.from_user

    if target_tg.is_bot:
        emoji = relic_emoji(user)

        await update.message.reply_text(
            f"{emoji} Реликвия не применена!\n"
            "🎯 Ответь командой /power на сообщение игрока."
        )
        return

    if target_tg.id == user["user_id"]:
        await update.message.reply_text(
            "❌ Нельзя применить способность на себя!"
        )
        return

    target = await get_user(
        target_tg.id,
        target_tg.username,
    )

    kwami = user.get("awaiting_kwami") or user.get("kwami")
    now = now_utc()

    # =====================================================
    # ПЧЁЛКА
    # =====================================================

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
                "🎯 Теперь ответь командой /power "
                "на сообщение игрока, на кого применить "
                "украденный ход."
            )
            return

        if stage == 2:
            user["awaiting_target"] = False
            user["awaiting_kwami"] = None
            user["bee_stage"] = 0
            user["bee_stolen_user"] = None
            user["bee_target_user"] = None
            user["ability_used"] = True
            user["last_use"] = now

            await save_user(user)

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Сбросить таймер",
                            callback_data="reset_timer",
                        )
                    ]
                ]
            )

            await update.message.reply_text(
                "🐝 Реликвия применена!\n"
                "🎲 Крадёт ход и использует его\n\n"
                f"⏳ Подожди: {format_timer(ABILITY_COOLDOWN)}",
                reply_markup=keyboard,
            )
            return

    # =====================================================
    # РЫЖАЯ ЛИСИЦА
    # =====================================================

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
                "🎯 Теперь ответь командой /power "
                "на сообщение второго игрока для обмена."
            )
            return

        if stage == 2:
            first_id = user.get("fox_first_target")

            if first_id == target["user_id"]:
                await update.message.reply_text(
                    "❌ Нельзя выбрать одного и того же игрока дважды!"
                )
                return

            first_user = await players_collection.find_one(
                {"user_id": first_id}
            )

            if first_user:
                first_kwami = first_user.get("kwami")
                target_kwami = target.get("kwami")

                first_user["kwami"] = target_kwami
                target["kwami"] = first_kwami

                if first_user.get("kwami") in KWAMI_LIST:
                    first_user["last_emoji"] = KWAMI_LIST[
                        first_user["kwami"]
                    ]["emoji"]

                if target.get("kwami") in KWAMI_LIST:
                    target["last_emoji"] = KWAMI_LIST[
                        target["kwami"]
                    ]["emoji"]

                await save_user(first_user)
                await save_user(target)

            user["awaiting_target"] = False
            user["awaiting_kwami"] = None
            user["fox_stage"] = 0
            user["fox_first_target"] = None
            user["ability_used"] = True
            user["last_use"] = now

            await save_user(user)

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Сбросить таймер",
                            callback_data="reset_timer",
                        )
                    ]
                ]
            )

            await update.message.reply_text(
                "🦊 Реликвия применена!\n"
                "🎲 Меняет реликвии между двумя игроками\n\n"
                f"⏳ Подожди: {format_timer(ABILITY_COOLDOWN)}",
                reply_markup=keyboard,
            )
            return

    # =====================================================
    # ОСТАЛЬНЫЕ РЕЛИКВИИ
    # =====================================================

    user["awaiting_target"] = False
    user["awaiting_kwami"] = None

    emoji = KWAMI_LIST.get(kwami, {}).get("emoji", "✨")

    if kwami == "Леди Баг":
        target["ability_used"] = False
        target["last_use"] = None
        await save_user(target)

        result_text = (
            f"{emoji} Реликвия применена!\n"
            "🎲 Способность игрока отменена"
        )

    elif kwami == "Чёрный кот":
        target["frozen_until"] = now + timedelta(hours=12)
        await save_user(target)

        result_text = (
            f"{emoji} Реликвия применена!\n"
            "🎲 Игрок заморожен на 12 часов"
        )

    elif kwami == "Черепаха":
        user["shield_active"] = True
        user["shield_until"] = now + timedelta(hours=12)
        await save_user(user)

        result_text = (
            f"{emoji} Реликвия применена!\n"
            "🎲 Даёт иммунитет к атакам на 12 часов"
        )

    elif kwami == "Павлин":
        target["linked_to"] = user["user_id"]
        await save_user(target)

        result_text = (
            f"{emoji} Реликвия применена!\n"
            "🎲 Привязывается к любому игроку"
        )

    elif kwami == "Бражник":
        target["ability_used"] = False
        target["last_use"] = None
        await save_user(target)

        result_text = (
            f"{emoji} Реликвия применена!\n"
            "🎲 Выдаёт дополнительный ход"
        )

    else:
        result_text = (
            f"{emoji} Реликвия применена!\n"
            "🎲 Неизвестная способность"
        )

    user["ability_used"] = True
    user["last_use"] = now

    await save_user(user)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Сбросить таймер",
                    callback_data="reset_timer",
                )
            ]
        ]
    )

    await update.message.reply_text(
        f"{result_text}\n\n"
        f"⏳ Подожди: {format_timer(ABILITY_COOLDOWN)}",
        reply_markup=keyboard,
    )


# =========================================================
# КНОПКИ
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query.data == "change_relic":
        await query.answer(
            "🔄 Смена реликвии скоро будет доступна!",
            show_alert=True,
        )
        return

    if query.data == "reset_timer":
        await query.answer(
            "⏳ Функция «🔄 Сбросить таймер» находится в разработке!",
            show_alert=True,
        )
        return

    await query.answer()


# =========================================================
# FLASK
# =========================================================

flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "Miraculous Battle Bot is running!"


def run_flask():
    port = int(os.getenv("PORT", "10000"))

    flask_app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )


# =========================================================
# ЗАПУСК
# =========================================================

async def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(
        CommandHandler("profile", profile)
    )

    application.add_handler(
        CommandHandler("power", power)
    )

    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    # Обычные ответы текстом на сообщения игроков.
    # Ответ командой /power обрабатывается внутри power().
    application.add_handler(
        MessageHandler(
            filters.REPLY & ~filters.COMMAND,
            handle_reply,
        )
    )

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
    )
    flask_thread.start()

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
