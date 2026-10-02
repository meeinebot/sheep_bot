import json
import logging
import os
import random
import sqlite3
import threading
import time
from typing import Optional

from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)


# =========================================================
# НАСТРОЙКИ
# =========================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "miraculous_game.db")

ABILITY_COOLDOWN_SECONDS = 12 * 60 * 60
TALISMAN_CHANGE_COOLDOWN_SECONDS = 24 * 60 * 60
FREEZE_DURATION_SECONDS = 12 * 60 * 60
PEACOCK_CHAIN_SECONDS = 12 * 60 * 60


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


# =========================================================
# ТАЛИСМАНЫ
# =========================================================

TALISMANS = {
    "ladybug": {
        "name": "Божья коровка",
        "emoji": "🐞",
        "ability": "Отменяешь действие игрока",
    },
    "black_cat": {
        "name": "Чёрный кот",
        "emoji": "🐈‍⬛",
        "ability": "Заморозка игрока на 12 часов",
    },
    "butterfly": {
        "name": "Мотылёк",
        "emoji": "🦋",
        "ability": "Сбрасываешь таймер ожидания способности",
    },
    "peacock": {
        "name": "Павлин",
        "emoji": "🦚",
        "ability": "Создаёшь цепь с игроком на 12 часов",
    },
}

FIRST_TALISMAN = "ladybug"


# =========================================================
# SQLITE
# =========================================================

def get_db():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def default_player(user_id: int, username: Optional[str]) -> dict:
    return {
        "user_id": user_id,
        "username": username,

        # Первый талисман всегда Божья коровка.
        "talisman": FIRST_TALISMAN,

        # Таймер способности.
        "ability_used": False,
        "ability_used_at": None,

        # Таймер смены талисмана.
        "last_talisman_change_at": None,

        # Заморозка.
        "frozen_until": None,
        "frozen_by": None,

        # Последняя цель Чёрного кота.
        "cat_frozen_target_id": None,

        # Цепь Павлина.
        "peacock_target_id": None,
        "peacock_until": None,
    }


def init_db() -> None:
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                data TEXT NOT NULL
            )
            """
        )
        db.commit()


def normalize_talisman(value) -> str:
    """Преобразует сохранённые названия из старых версий в ID."""

    aliases = {
        "ladybug": "ladybug",
        "Леди Баг": "ladybug",
        "Божья коровка": "ladybug",

        "black_cat": "black_cat",
        "Чёрный кот": "black_cat",
        "Черный кот": "black_cat",
        "Супер кот": "black_cat",
        "Супер-кот": "black_cat",

        "butterfly": "butterfly",
        "Мотылёк": "butterfly",
        "Мотылек": "butterfly",
        "Бражник": "butterfly",

        "peacock": "peacock",
        "Павлин": "peacock",
    }

    return aliases.get(value, FIRST_TALISMAN)


def save_player_sync(player: dict) -> None:
    with get_db() as db:
        db.execute(
            """
            INSERT INTO players (user_id, username, data)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                data = excluded.data
            """,
            (
                player["user_id"],
                player.get("username"),
                json.dumps(player, ensure_ascii=False),
            ),
        )
        db.commit()


async def save_player(player: dict) -> None:
    save_player_sync(player)


async def get_player(
    user_id: int,
    username: Optional[str] = None,
) -> dict:
    with get_db() as db:
        row = db.execute(
            "SELECT data FROM players WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            player = default_player(user_id, username)

            # Новому игроку первая смена доступна через 24 часа.
            player["last_talisman_change_at"] = time.time()

            db.execute(
                """
                INSERT INTO players (user_id, username, data)
                VALUES (?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    json.dumps(player, ensure_ascii=False),
                ),
            )
            db.commit()
            return player

        try:
            player = json.loads(row["data"])
        except (TypeError, json.JSONDecodeError):
            player = default_player(user_id, username)

        changed = False
        defaults = default_player(user_id, username)

        for key, value in defaults.items():
            if key not in player:
                player[key] = value
                changed = True

        player["user_id"] = user_id

        if username and player.get("username") != username:
            player["username"] = username
            changed = True

        # Переводим старые названия талисманов на стабильные ID.
        normalized = normalize_talisman(player.get("talisman"))

        if player.get("talisman") != normalized:
            player["talisman"] = normalized
            changed = True

        # Для старых записей без таймера смены разрешаем сменить сейчас.
        if "last_talisman_change_at" not in player:
            player["last_talisman_change_at"] = None
            changed = True

        if changed:
            save_player_sync(player)

        return player


# =========================================================
# ВРЕМЯ И СОСТОЯНИЕ
# =========================================================

def now() -> float:
    return time.time()


def format_timer(seconds_left: float) -> str:
    seconds_left = max(0, int(seconds_left))

    hours = seconds_left // 3600
    minutes = (seconds_left % 3600) // 60

    return f"{hours} часов {minutes} минут"


def talisman_emoji(player: dict) -> str:
    talisman = normalize_talisman(player.get("talisman"))
    return TALISMANS[talisman]["emoji"]


def ability_time_left(player: dict) -> float:
    if not player.get("ability_used"):
        return 0

    used_at = player.get("ability_used_at")

    if not used_at:
        return 0

    return max(
        0,
        ABILITY_COOLDOWN_SECONDS - (now() - float(used_at)),
    )


def is_frozen(player: dict) -> bool:
    frozen_until = player.get("frozen_until")

    if not frozen_until:
        return False

    return now() < float(frozen_until)


def frozen_time_left(player: dict) -> float:
    frozen_until = player.get("frozen_until")

    if not frozen_until:
        return 0

    return max(0, float(frozen_until) - now())


async def get_peacock_chain_target(player: dict):
    """Возвращает активную цель цепи Павлина или None."""

    target_id = player.get("peacock_target_id")
    until = player.get("peacock_until")

    if not target_id or not until:
        return None

    if now() >= float(until):
        player["peacock_target_id"] = None
        player["peacock_until"] = None
        await save_player(player)
        return None

    return await get_player(int(target_id))


async def resolve_effect_target(target: dict) -> dict:
    """
    Если выбранный игрок — Павлин с активной цепью,
    действие применяется к игроку, соединённому с ним.
    """

    linked_target = await get_peacock_chain_target(target)

    if linked_target is None:
        return target

    if linked_target["user_id"] == target["user_id"]:
        return target

    return linked_target


# =========================================================
# ПРОФИЛЬ
# =========================================================

def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Сменить талисман",
                    callback_data="change_talisman",
                )
            ],
            [
                InlineKeyboardButton(
                    "🛒 Перейти в магазин",
                    callback_data="shop",
                )
            ],
        ]
    )


def profile_text(player: dict) -> str:
    emoji = talisman_emoji(player)
    remaining = ability_time_left(player)

    if remaining > 0:
        return (
            f"{emoji} Талисман: Активен\n"
            f"⏳ Следующая способность через: "
            f"{format_timer(remaining)}"
        )

    return (
        f"{emoji} Талисман: Неактивен\n"
        "⌛️ Способность готова к активации!"
    )


async def show_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    player = await get_player(
        update.effective_user.id,
        update.effective_user.username,
    )

    text = profile_text(player)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=profile_keyboard(),
        )
    elif update.message:
        await update.message.reply_text(
            text,
            reply_markup=profile_keyboard(),
        )


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await show_profile(update, context)


async def profile_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await show_profile(update, context)


# =========================================================
# ОБЩИЕ ОТВЕТЫ ДЛЯ /POWER
# =========================================================

async def reply_already_used(
    update: Update,
    player: dict,
) -> None:
    emoji = talisman_emoji(player)

    await update.message.reply_text(
        f"{emoji} Талисман уже использован!\n"
        f"⏳ Подожди: {format_timer(ability_time_left(player))}"
    )


async def reply_not_used(
    update: Update,
    player: dict,
) -> None:
    emoji = talisman_emoji(player)

    await update.message.reply_text(
        f"{emoji} Талисман не использован!\n"
        "🎲 Ответь командой в ответ на сообщение игрока"
    )


async def reply_frozen(
    update: Update,
    player: dict,
) -> None:
    await update.message.reply_text(
        "🐈‍⬛ Заморозка действует!\n"
        f"⏳ Подожди: {format_timer(frozen_time_left(player))}"
    )


def mark_ability_used(player: dict) -> None:
    player["ability_used"] = True
    player["ability_used_at"] = now()


# =========================================================
# СПОСОБНОСТЬ ЛЕДИ БАГ
# =========================================================

async def cancel_cat_freeze(cat: dict) -> None:
    """
    Снимает заморозку, созданную этим Чёрным котом.
    Таймер способности кота не сбрасывается.
    """

    frozen_target_id = cat.get("cat_frozen_target_id")

    if not frozen_target_id:
        return

    frozen_target = await get_player(int(frozen_target_id))

    # Не снимаем чужую заморозку, если цель позже заморозил
    # другой Чёрный кот.
    if frozen_target.get("frozen_by") == cat["user_id"]:
        frozen_target["frozen_until"] = None
        frozen_target["frozen_by"] = None
        await save_player(frozen_target)

    cat["cat_frozen_target_id"] = None
    await save_player(cat)


async def cancel_current_freeze(target: dict) -> None:
    """Снимает заморозку с выбранной цели, если она есть."""

    frozen_by = target.get("frozen_by")

    if not frozen_by:
        return

    cat = await get_player(int(frozen_by))

    if cat.get("cat_frozen_target_id") == target["user_id"]:
        cat["cat_frozen_target_id"] = None
        await save_player(cat)

    target["frozen_until"] = None
    target["frozen_by"] = None
    await save_player(target)


# =========================================================
# /POWER
# Способность используется командой в ответ на сообщение
# другого игрока.
# =========================================================

async def power(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.message

    if message is None:
        return

    player = await get_player(
        update.effective_user.id,
        update.effective_user.username,
    )

    # Замороженный игрок не может применить способность.
    if is_frozen(player):
        await reply_frozen(update, player)
        return

    # Таймер способности ещё не завершился.
    if ability_time_left(player) > 0:
        await reply_already_used(update, player)
        return

    # Для применения команды нужно ответить на сообщение игрока.
    reply = message.reply_to_message

    if reply is None or reply.from_user is None:
        await reply_not_used(update, player)
        return

    target_user = reply.from_user

    if target_user.is_bot:
        await reply_not_used(update, player)
        return

    if target_user.id == player["user_id"]:
        await message.reply_text(
            f"{talisman_emoji(player)} Талисман не использован!\n"
            "🎲 Нельзя использовать способность на себя"
        )
        return

    direct_target = await get_player(
        target_user.id,
        target_user.username,
    )

    talisman = normalize_talisman(player.get("talisman"))
    effect_target = await resolve_effect_target(direct_target)

    # =====================================================
    # ЛЕДИ БАГ
    # =====================================================

    if talisman == "ladybug":
        # Ответ на сообщение Чёрного кота отменяет
        # его действующую заморозку.
        if normalize_talisman(direct_target.get("talisman")) == "black_cat":
            await cancel_cat_freeze(direct_target)

        # Если цель действия перенаправлена цепью Павлина,
        # Леди Баг снимает заморозку с связанного игрока.
        if effect_target.get("frozen_by"):
            await cancel_current_freeze(effect_target)

        mark_ability_used(player)
        await save_player(player)

        await message.reply_text(
            "🐞 Талисман использован!\n"
            "🎲 Способность: Отменяешь действие игрока"
        )
        return

    # =====================================================
    # ЧЁРНЫЙ КОТ
    # =====================================================

    if talisman == "black_cat":
        effect_target["frozen_until"] = (
            now() + FREEZE_DURATION_SECONDS
        )
        effect_target["frozen_by"] = player["user_id"]

        player["cat_frozen_target_id"] = effect_target["user_id"]

        await save_player(effect_target)

        mark_ability_used(player)
        await save_player(player)

        await message.reply_text(
            "🐈‍⬛ Талисман использован!\n"
            "🎲 Способность: Заморозка игрока на 12 часов"
        )
        return

    # =====================================================
    # МОТЫЛЁК / БАБОЧКА
    # =====================================================

    if talisman == "butterfly":
        # Сбрасываем таймер способности цели.
        # Если цель — Павлин с действующей цепью,
        # сброс применяется к связанному игроку.
        effect_target["ability_used"] = False
        effect_target["ability_used_at"] = None

        await save_player(effect_target)

        mark_ability_used(player)
        await save_player(player)

        await message.reply_text(
            "🦋 Талисман использован!\n"
            "🎲 Способность: Сбрасываешь таймер ожидания способности"
        )
        return

    # =====================================================
    # ПАВЛИН
    # =====================================================

    if talisman == "peacock":
        # Павлин создаёт цепь непосредственно с игроком,
        # на сообщение которого ответил.
        player["peacock_target_id"] = direct_target["user_id"]
        player["peacock_until"] = now() + PEACOCK_CHAIN_SECONDS

        mark_ability_used(player)
        await save_player(player)

        await message.reply_text(
            "🦚 Талисман использован!\n"
            "🎲 Способность: Создаёшь цепь с игроком на 12 часов"
        )
        return


# =========================================================
# СМЕНА ТАЛИСМАНА
# =========================================================

async def change_talisman(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    player = await get_player(query.from_user.id)

    last_change = player.get("last_talisman_change_at")

    if last_change is not None:
        elapsed = now() - float(last_change)

        if elapsed < TALISMAN_CHANGE_COOLDOWN_SECONDS:
            await query.answer(
                "⏳ Менять талисман можно раз в 24 часа!",
                show_alert=True,
            )
            return

    current_talisman = normalize_talisman(player.get("talisman"))

    choices = [
        talisman
        for talisman in TALISMANS
        if talisman != current_talisman
    ]

    new_talisman = random.choice(choices)

    player["talisman"] = new_talisman
    player["last_talisman_change_at"] = now()

    # Таймер способности не меняется при смене талисмана.
    # Цепь Павлина и уже действующая заморозка также не сбрасываются.

    await save_player(player)

    emoji = TALISMANS[new_talisman]["emoji"]

    alert_text = (
        f"{emoji} Талисман изменён. "
        "⌛️ Следующая смена через 24 часа!"
    )

    await query.answer(
        alert_text,
        show_alert=True,
    )

    # Обновляем профиль и показываем текущий таймер способности.
    try:
        await query.edit_message_text(
            profile_text(player),
            reply_markup=profile_keyboard(),
        )
    except Exception:
        # Например, сообщение уже было изменено или удалено.
        logger.info("Не удалось обновить сообщение профиля после смены.")


# =========================================================
# МАГАЗИН
# =========================================================

async def open_shop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.callback_query.answer(
        "🛒 Магазин будет доступен в следующем обновлении!",
        show_alert=True,
    )


# =========================================================
# CALLBACK-КНОПКИ
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query.data == "change_talisman":
        await change_talisman(update, context)
        return

    if query.data == "shop":
        await open_shop(update, context)
        return

    await query.answer()


# =========================================================
# FLASK ДЛЯ HEALTH CHECK
# =========================================================

flask_app = Flask(__name__)


@flask_app.route("/")
def health():
    return "Miraculous Bot is running!"


def run_flask() -> None:
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

def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "Не найден TELEGRAM_BOT_TOKEN"
        )

    init_db()

    application = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("profile", profile_command)
    )

    application.add_handler(
        CommandHandler("power", power)
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
    )
    flask_thread.start()

    application.run_polling()


if __name__ == "__main__":
    main()
