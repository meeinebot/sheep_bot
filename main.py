import asyncio
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
FOX_SWAP_SECONDS = 12 * 60 * 60

pending_butterfly_choices = {}


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
        "name": "Бражник",
        "emoji": "🦋",
        "ability": "Акуматизация будет доступна позже",
    },
    "peacock": {
        "name": "Павлин",
        "emoji": "🦚",
        "ability": "Создаёшь цепь с игроком на 12 часов",
    },
    "fox": {
        "name": "Лисица",
        "emoji": "🦊",
        "ability": "Меняешь талисманы двух игроков местами на 12 часов",
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

        # Первый талисман игрока — Божья коровка.
        "talisman": FIRST_TALISMAN,

        # Таймер способности игрока.
        "ability_used": False,
        "ability_used_at": None,

        # Отдельный таймер смены талисмана.
        "last_talisman_change_at": None,

        # Заморозка от Чёрного кота.
        "frozen_until": None,
        "frozen_by": None,
        "cat_frozen_target_id": None,

        # Цепь Павлина.
        "peacock_target_id": None,
        "peacock_until": None,

        # Первый выбранный игрок для способности Лисицы.
        "fox_pending_first_id": None,
    }


def normalize_talisman(value) -> str:
    """Поддерживает названия талисманов из старых версий базы."""

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
        "Бражник": "butterfly",
        "Мотылёк": "butterfly",
        "Мотылек": "butterfly",

        "peacock": "peacock",
        "Павлин": "peacock",

        "fox": "fox",
        "Лисица": "fox",
        "Рыжая лисица": "fox",
    }

    return aliases.get(value, FIRST_TALISMAN)


def save_player_in_db(db, player: dict) -> None:
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

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS fox_swaps (
                swap_id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_user_id INTEGER NOT NULL,
                second_user_id INTEGER NOT NULL,
                first_original_talisman TEXT NOT NULL,
                second_original_talisman TEXT NOT NULL,
                expires_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )

        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_fox_swaps_first_user
            ON fox_swaps(first_user_id, active)
            """
        )

        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_fox_swaps_second_user
            ON fox_swaps(second_user_id, active)
            """
        )

        db.commit()


def restore_swap_sync(swap_id: int) -> None:
    """Возвращает участникам обмена их исходные талисманы."""

    with get_db() as db:
        swap = db.execute(
            """
            SELECT *
            FROM fox_swaps
            WHERE swap_id = ? AND active = 1
            """,
            (swap_id,),
        ).fetchone()

        if swap is None:
            return

        players_to_restore = [
            (
                int(swap["first_user_id"]),
                swap["first_original_talisman"],
            ),
            (
                int(swap["second_user_id"]),
                swap["second_original_talisman"],
            ),
        ]

        for user_id, original_talisman in players_to_restore:
            row = db.execute(
                "SELECT data FROM players WHERE user_id = ?",
                (user_id,),
            ).fetchone()

            if row is None:
                continue

            try:
                player = json.loads(row["data"])
            except (TypeError, json.JSONDecodeError):
                continue

            player["talisman"] = normalize_talisman(
                original_talisman
            )

            save_player_in_db(db, player)

        db.execute(
            """
            UPDATE fox_swaps
            SET active = 0
            WHERE swap_id = ?
            """,
            (swap_id,),
        )

        db.commit()


def expire_player_swaps_sync(user_id: int) -> None:
    """Завершает обмены Лисицы, срок которых истёк."""

    with get_db() as db:
        rows = db.execute(
            """
            SELECT swap_id
            FROM fox_swaps
            WHERE active = 1
              AND expires_at <= ?
              AND (first_user_id = ? OR second_user_id = ?)
            """,
            (time.time(), user_id, user_id),
        ).fetchall()

    for row in rows:
        restore_swap_sync(int(row["swap_id"]))


def find_active_swap_sync(user_id: int):
    """Возвращает активный обмен игрока или None."""

    expire_player_swaps_sync(user_id)

    with get_db() as db:
        return db.execute(
            """
            SELECT *
            FROM fox_swaps
            WHERE active = 1
              AND expires_at > ?
              AND (first_user_id = ? OR second_user_id = ?)
            LIMIT 1
            """,
            (time.time(), user_id, user_id),
        ).fetchone()


def create_fox_swap_sync(
    first_user_id: int,
    second_user_id: int,
) -> bool:
    """
    Меняет талисманы двух игроков на 12 часов.

    Таймеры способности и личной смены талисмана
    остаются у игроков и не сбрасываются.
    """

    if first_user_id == second_user_id:
        return False

    expire_player_swaps_sync(first_user_id)
    expire_player_swaps_sync(second_user_id)

    with get_db() as db:
        existing = db.execute(
            """
            SELECT swap_id
            FROM fox_swaps
            WHERE active = 1
              AND expires_at > ?
              AND (
                    first_user_id IN (?, ?)
                    OR second_user_id IN (?, ?)
              )
            LIMIT 1
            """,
            (
                time.time(),
                first_user_id,
                second_user_id,
                first_user_id,
                second_user_id,
            ),
        ).fetchone()

        if existing is not None:
            return False

        first_row = db.execute(
            "SELECT data FROM players WHERE user_id = ?",
            (first_user_id,),
        ).fetchone()

        second_row = db.execute(
            "SELECT data FROM players WHERE user_id = ?",
            (second_user_id,),
        ).fetchone()

        if first_row is None or second_row is None:
            return False

        try:
            first_player = json.loads(first_row["data"])
            second_player = json.loads(second_row["data"])
        except (TypeError, json.JSONDecodeError):
            return False

        first_original = normalize_talisman(
            first_player.get("talisman")
        )
        second_original = normalize_talisman(
            second_player.get("talisman")
        )

        db.execute(
            """
            INSERT INTO fox_swaps (
                first_user_id,
                second_user_id,
                first_original_talisman,
                second_original_talisman,
                expires_at,
                active
            )
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (
                first_user_id,
                second_user_id,
                first_original,
                second_original,
                time.time() + FOX_SWAP_SECONDS,
            ),
        )

        # Меняются только талисманы.
        # Таймеры игроков остаются без изменений.
        first_player["talisman"] = second_original
        second_player["talisman"] = first_original

        save_player_in_db(db, first_player)
        save_player_in_db(db, second_player)
        db.commit()

    return True


def save_player_sync(player: dict) -> None:
    with get_db() as db:
        save_player_in_db(db, player)
        db.commit()


async def save_player(player: dict) -> None:
    save_player_sync(player)


def get_player_sync(
    user_id: int,
    username: Optional[str] = None,
) -> dict:
    expire_player_swaps_sync(user_id)

    with get_db() as db:
        row = db.execute(
            "SELECT data FROM players WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            player = default_player(user_id, username)

            # Первую смену талисмана новый игрок
            # сможет сделать через 24 часа.
            player["last_talisman_change_at"] = time.time()

            save_player_in_db(db, player)
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

        normalized = normalize_talisman(
            player.get("talisman")
        )

        if player.get("talisman") != normalized:
            player["talisman"] = normalized
            changed = True

        if changed:
            save_player_in_db(db, player)
            db.commit()

        return player


async def get_player(
    user_id: int,
    username: Optional[str] = None,
) -> dict:
    return get_player_sync(user_id, username)


# =========================================================
# ТАЙМЕРЫ И СОСТОЯНИЯ
# =========================================================

def now() -> float:
    return time.time()


def format_timer(seconds_left: float) -> str:
    seconds_left = max(0, int(seconds_left))

    hours = seconds_left // 3600
    minutes = (seconds_left % 3600) // 60

    return f"{hours} часов {minutes} минут"


def talisman_emoji(player: dict) -> str:
    talisman = normalize_talisman(
        player.get("talisman")
    )
    return TALISMANS[talisman]["emoji"]


def ability_time_left(player: dict) -> float:
    if not player.get("ability_used"):
        return 0

    used_at = player.get("ability_used_at")

    if not used_at:
        return 0

    return max(
        0,
        ABILITY_COOLDOWN_SECONDS
        - (now() - float(used_at)),
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


def mark_ability_used(player: dict) -> None:
    player["ability_used"] = True
    player["ability_used_at"] = now()


async def get_peacock_chain_target(player: dict):
    target_id = player.get("peacock_target_id")
    chain_until = player.get("peacock_until")

    if not target_id or not chain_until:
        return None

    if now() >= float(chain_until):
        player["peacock_target_id"] = None
        player["peacock_until"] = None
        await save_player(player)
        return None

    return await get_player(int(target_id))


async def resolve_effect_target(target: dict) -> dict:
    """
    Если действие направлено на Павлина с действующей цепью,
    оно применяется к игроку, связанному с ним.
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
            "⏳ Следующая способность через: "
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
# ОТВЕТЫ ДЛЯ /POWER
# =========================================================

async def reply_already_used(
    update: Update,
    player: dict,
) -> None:
    await update.message.reply_text(
        f"{talisman_emoji(player)} Талисман уже использован!\n"
        "⏳ Подожди: "
        f"{format_timer(ability_time_left(player))}"
    )


async def reply_not_used(
    update: Update,
    player: dict,
) -> None:
    await update.message.reply_text(
        f"{talisman_emoji(player)} Талисман не использован!\n"
        "🎲 Ответь командой в ответ на сообщение игрока"
    )


async def reply_frozen(
    update: Update,
    player: dict,
) -> None:
    await update.message.reply_text(
        "🐈‍⬛ Заморозка действует!\n"
        "⏳ Подожди: "
        f"{format_timer(frozen_time_left(player))}"
    )


def butterfly_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "❄️ Непогода",
                callback_data="butterfly_weather",
            ),
            InlineKeyboardButton(
                "❌ Отказ",
                callback_data="butterfly_decline",
            ),
        ]]
    )


async def delete_butterfly_prompt(
    bot,
    chat_id: int,
    message_id: int,
) -> None:
    await asyncio.sleep(30)

    try:
        await bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
    except Exception:
        logger.exception(
            "Не удалось удалить сообщение выбора способности Бражника."
        )
    finally:
        pending_butterfly_choices.pop(
            (chat_id, message_id),
            None,
        )


async def handle_butterfly_choice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query.message is None:
        await query.answer(
            "⌛️ Время выбора истекло!",
            show_alert=True,
        )
        return

    message_key = (
        query.message.chat.id,
        query.message.message_id,
    )
    choice = pending_butterfly_choices.get(message_key)

    if choice is None or now() >= choice["expires_at"]:
        pending_butterfly_choices.pop(message_key, None)
        await query.answer(
            "⌛️ Время выбора истекло!",
            show_alert=True,
        )
        return

    if query.from_user.id != choice["target_user_id"]:
        await query.answer(
            "Эта кнопка предназначена игроку, на чьё сообщение ответили.",
            show_alert=True,
        )
        return

    if choice.get("selected"):
        await query.answer(
            "🦋 Акуматизация будет доступна позже!",
            show_alert=True,
        )
        return

    choice["selected"] = True
    await query.answer(
        "🦋 Акуматизация будет доступна позже!",
        show_alert=True,
    )

    try:
        await query.edit_message_reply_markup(
            reply_markup=None,
        )
    except Exception:
        logger.exception(
            "Не удалось убрать кнопки выбора способности Бражника."
        )


# =========================================================
# СПОСОБНОСТИ ЛЕДИ БАГ
# =========================================================

async def cancel_cat_freeze(cat: dict) -> None:
    """
    Снимает заморозку, созданную Чёрным котом.
    Таймер способности кота не сбрасывается.
    """

    frozen_target_id = cat.get("cat_frozen_target_id")

    if not frozen_target_id:
        return

    frozen_target = await get_player(int(frozen_target_id))

    if frozen_target.get("frozen_by") == cat["user_id"]:
        frozen_target["frozen_until"] = None
        frozen_target["frozen_by"] = None
        await save_player(frozen_target)

    cat["cat_frozen_target_id"] = None
    await save_player(cat)


async def cancel_current_freeze(target: dict) -> None:
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
# СПОСОБНОСТЬ ЛИСИЦЫ
# =========================================================

async def use_fox_power(
    update: Update,
    player: dict,
    selected_player: dict,
) -> None:
    first_id = player.get("fox_pending_first_id")
    selected_id = int(selected_player["user_id"])

    if first_id is None:
        if find_active_swap_sync(selected_id):
            await update.message.reply_text(
                "🦊 Талисман не использован!\n"
                "🎲 Этот игрок уже участвует во временном обмене"
            )
            return

        player["fox_pending_first_id"] = selected_id
        await save_player(player)

        await update.message.reply_text(
            "🦊 Талисман не использован!\n"
            "🎲 Ответь на сообщение другого игрока, "
            "для смены способностей"
        )
        return

    if selected_id == int(first_id):
        await update.message.reply_text(
            "🦊 Талисман не использован!\n"
            "🎲 Ответь на сообщение другого игрока, "
            "для смены способностей"
        )
        return

    if find_active_swap_sync(int(first_id)):
        player["fox_pending_first_id"] = None
        await save_player(player)

        await update.message.reply_text(
            "🦊 Талисман не использован!\n"
            "🎲 Первый выбранный игрок уже участвует "
            "во временном обмене"
        )
        return

    if find_active_swap_sync(selected_id):
        await update.message.reply_text(
            "🦊 Талисман не использован!\n"
            "🎲 Этот игрок уже участвует во временном обмене"
        )
        return

    success = create_fox_swap_sync(
        int(first_id),
        selected_id,
    )

    if not success:
        await update.message.reply_text(
            "🦊 Талисман не использован!\n"
            "🎲 Не удалось выполнить обмен. "
            "Выбери двух игроков, которые не участвуют "
            "в другом обмене"
        )
        return

    # Перечитываем данные: владелец Лисицы мог быть
    # одним из выбранных для обмена игроков.
    player = await get_player(player["user_id"])

    player["fox_pending_first_id"] = None
    mark_ability_used(player)
    await save_player(player)

    await update.message.reply_text(
        "🦊 Талисман использован!\n"
        "🎲 Способность: Меняешь талисманы двух игроков "
        "местами на 12 часов"
    )


# =========================================================
# /POWER
# Команду нужно отправлять ответом на сообщение игрока.
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

    if is_frozen(player):
        await reply_frozen(update, player)
        return

    if ability_time_left(player) > 0:
        await reply_already_used(update, player)
        return

    reply = message.reply_to_message

    if reply is None or reply.from_user is None:
        await reply_not_used(update, player)
        return

    target_user = reply.from_user

    if target_user.is_bot:
        await reply_not_used(update, player)
        return

    direct_target = await get_player(
        target_user.id,
        target_user.username,
    )

    talisman = normalize_talisman(
        player.get("talisman")
    )

    # Лисица выбирает двух игроков.
    if talisman == "fox":
        await use_fox_power(
            update,
            player,
            direct_target,
        )
        return

    if target_user.id == player["user_id"]:
        await message.reply_text(
            f"{talisman_emoji(player)} Талисман не использован!\n"
            "🎲 Нельзя использовать способность на себя"
        )
        return

    effect_target = await resolve_effect_target(direct_target)
    current_time = now()

    # =====================================================
    # БОЖЬЯ КОРОВКА
    # =====================================================

    if talisman == "ladybug":
        # Божья коровка отменяет временный обмен Лисицы,
        # если отвечает на сообщение одного из участников.
        active_swap = find_active_swap_sync(
            direct_target["user_id"]
        )

        if active_swap:
            restore_swap_sync(int(active_swap["swap_id"]))

            direct_target = await get_player(
                target_user.id,
                target_user.username,
            )
            effect_target = await resolve_effect_target(
                direct_target
            )

        # Ответ на сообщение Чёрного кота отменяет
        # его действующую заморозку.
        if normalize_talisman(
            direct_target.get("talisman")
        ) == "black_cat":
            await cancel_cat_freeze(direct_target)

        # Если эффект перенаправлен цепью Павлина,
        # снимаем заморозку со связанного игрока.
        if effect_target.get("frozen_by"):
            await cancel_current_freeze(effect_target)

        # Если обмен вернул владельцу другой талисман,
        # сохраняем его текущее состояние перед записью.
        player = await get_player(player["user_id"])
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
        if effect_target["user_id"] == player["user_id"]:
            await reply_not_used(update, player)
            return

        effect_target["frozen_until"] = (
            current_time + FREEZE_DURATION_SECONDS
        )
        effect_target["frozen_by"] = player["user_id"]

        player["cat_frozen_target_id"] = effect_target["user_id"]
        await save_player(effect_target)

        player = await get_player(player["user_id"])
        player["cat_frozen_target_id"] = effect_target["user_id"]
        mark_ability_used(player)
        await save_player(player)

        await message.reply_text(
            "🐈‍⬛ Талисман использован!\n"
            "🎲 Способность: Заморозка игрока на 12 часов"
        )
        return

    # =====================================================
    # БРАЖНИК
    # =====================================================

    if talisman == "butterfly":
        # Пока Бражник не применяет эффект к цели: игроку
        # предлагается выбрать способность или отказаться.
        # Сообщение отправляется ответом на сообщение цели,
        # чтобы было понятно, кому адресован выбор.
        prompt = await reply.reply_text(
            "🎲 Выбери способность:\n"
            "⏳ У вас 30 секунд!",
            reply_markup=butterfly_choice_keyboard(),
        )

        prompt_key = (prompt.chat.id, prompt.message_id)
        pending_butterfly_choices[
            prompt_key
        ] = {
            "target_user_id": target_user.id,
            "expires_at": now() + 30,
            "selected": False,
        }

        context.application.create_task(
            delete_butterfly_prompt(
                context.bot,
                prompt.chat.id,
                prompt.message_id,
            )
        )

        player = await get_player(player["user_id"])
        mark_ability_used(player)
        await save_player(player)
        return

    # =====================================================
    # ПАВЛИН
    # =====================================================

    if talisman == "peacock":
        player["peacock_target_id"] = direct_target["user_id"]
        player["peacock_until"] = (
            current_time + PEACOCK_CHAIN_SECONDS
        )

        mark_ability_used(player)
        await save_player(player)

        await message.reply_text(
            "🦚 Талисман использован!\n"
            "🎲 Способность: Создаёшь цепь "
            "с игроком на 12 часов"
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

    # Пока игрок участвует в обмене Лисицы,
    # собственную смену талисмана выполнить нельзя.
    active_swap = find_active_swap_sync(player["user_id"])

    if active_swap:
        await query.answer(
            "🦊 Талисманы временно поменяны местами. "
            "Подожди окончания обмена!",
            show_alert=True,
        )
        return

    last_change = player.get("last_talisman_change_at")

    if last_change is not None:
        elapsed = now() - float(last_change)

        if elapsed < TALISMAN_CHANGE_COOLDOWN_SECONDS:
            await query.answer(
                "⏳ Менять талисман можно раз в 24 часа!",
                show_alert=True,
            )
            return

    current_talisman = normalize_talisman(
        player.get("talisman")
    )

    choices = [
        talisman
        for talisman in TALISMANS
        if talisman != current_talisman
    ]

    new_talisman = random.choice(choices)

    player["talisman"] = new_talisman
    player["last_talisman_change_at"] = now()

    # Смена талисмана не сбрасывает таймер способности.
    player["fox_pending_first_id"] = None

    await save_player(player)

    emoji = TALISMANS[new_talisman]["emoji"]
    alert_text = (
        f"{emoji} Талисман изменён.\n"
        "⏳ Следующая смена через 24 часа!"
    )

    await query.answer(
        alert_text,
        show_alert=True,
    )

    try:
        await query.edit_message_text(
            profile_text(player),
            reply_markup=profile_keyboard(),
        )
    except Exception:
        logger.info(
            "Не удалось обновить сообщение профиля после смены."
        )


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
# ОБРАБОТКА КНОПОК
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query.data in {
        "butterfly_weather",
        "butterfly_decline",
    }:
        await handle_butterfly_choice(update, context)
        return

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
