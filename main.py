import json
import logging
import os
import sqlite3
import threading
import time

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


# =========================================================
# НАСТРОЙКИ
# =========================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "miraculous_game.db")

ABILITY_COOLDOWN_SECONDS = 12 * 60 * 60
TALISMAN_CHANGE_COOLDOWN_SECONDS = 2 * 24 * 60 * 60
FREEZE_DURATION_SECONDS = 12 * 60 * 60


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


# =========================================================
# ТАЛИСМАНЫ
# =========================================================

TALISMANS = {
    "Леди Баг": {
        "emoji": "🐞",
        "ability": "Отменяешь действие игрока",
    },
    "Чёрный кот": {
        "emoji": "🐈‍⬛",
        "ability": "Заморозка игрока на 12 часов",
    },
}

FIRST_TALISMAN = "Леди Баг"
RANDOM_TALISMANS = ["Леди Баг", "Чёрный кот"]


# =========================================================
# SQLITE
# =========================================================

def get_db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def default_player(user_id: int, username: str | None) -> dict:
    return {
        "user_id": user_id,
        "username": username,

        # Текущий талисман сохраняется постоянно.
        "talisman": FIRST_TALISMAN,
        "last_talisman_emoji": "🐞",

        # Таймер способности.
        "ability_used": False,
        "ability_used_at": None,

        # Таймер смены талисмана.
        "last_talisman_change_at": None,

        # Заморозка игрока.
        "frozen_until": None,
        "frozen_by": None,

        # Кого заморозил Чёрный кот.
        "cat_frozen_target": None,

        # Ожидание ответа на сообщение игрока.
        "awaiting_target": False,
        "awaiting_talisman": None,
    }


def init_db():
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


async def get_player(
    user_id: int,
    username: str | None = None,
) -> dict:
    with get_db() as db:
        row = db.execute(
            "SELECT data FROM players WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            player = default_player(user_id, username)

            # Для нового игрока смена доступна не сразу,
            # а через 2 дня после получения первой Леди Баг.
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

        # Добавляем поля, которых могло не быть в старой базе.
        for key, value in default_player(user_id, username).items():
            if key not in player:
                player[key] = value
                changed = True

        player["user_id"] = user_id

        if username and player.get("username") != username:
            player["username"] = username
            changed = True

        # Исправление старого значения.
        if player.get("talisman") == "Божья коровка":
            player["talisman"] = "Леди Баг"
            player["last_talisman_emoji"] = "🐞"
            changed = True

        # Если старая версия уже очистила talisman,
        # восстанавливаем его по сохранённому смайлику.
        if player.get("talisman") not in TALISMANS:
            old_emoji = player.get("last_talisman_emoji")

            if old_emoji == "🐈‍⬛":
                player["talisman"] = "Чёрный кот"
            else:
                player["talisman"] = "Леди Баг"

            player["last_talisman_emoji"] = TALISMANS[
                player["talisman"]
            ]["emoji"]

            changed = True

        # Если у старого игрока не было времени смены,
        # разрешаем ему сменить талисман сейчас.
        # После этого таймер будет установлен на 2 дня.
        if "last_talisman_change_at" not in player:
            player["last_talisman_change_at"] = None
            changed = True

        if changed:
            save_player_sync(player)

        return player


async def save_player(player: dict) -> None:
    save_player_sync(player)


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
    talisman = player.get("talisman")

    if talisman in TALISMANS:
        return TALISMANS[talisman]["emoji"]

    return "🐞"


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

    return max(
        0,
        float(frozen_until) - now(),
    )


def clear_pending_action(player: dict) -> None:
    player["awaiting_target"] = False
    player["awaiting_talisman"] = None


# =========================================================
# КЛАВИАТУРА ПРОФИЛЯ
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


# =========================================================
# ПРОФИЛЬ
# =========================================================

async def profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    player = await get_player(
        update.effective_user.id,
        update.effective_user.username,
    )

    emoji = talisman_emoji(player)
    ability_left = ability_time_left(player)

    if ability_left > 0:
        text = (
            f"{emoji} Талисман: Активен\n"
            "⏳ Следующая способность через: "
            f"{format_timer(ability_left)}"
        )
    else:
        text = (
            f"{emoji} Талисман: Неактивен\n"
            "⌛️ Способность готова к активации!"
        )

    await update.message.reply_text(
        text,
        reply_markup=profile_keyboard(),
    )


# =========================================================
# ПОДГОТОВКА СПОСОБНОСТИ
# =========================================================

def prepare_ability(player: dict) -> str:
    player["awaiting_target"] = True
    player["awaiting_talisman"] = player["talisman"]

    emoji = talisman_emoji(player)

    return (
        f"{emoji} Способность активирована!\n"
        "🎯 Ответь командой /power в ответ "
        "на сообщение игрока."
    )


async def send_already_used(
    update: Update,
    player: dict,
) -> None:
    emoji = talisman_emoji(player)
    left = ability_time_left(player)

    await update.message.reply_text(
        f"{emoji} Талисман уже использован!\n"
        f"⏳ Подожди: {format_timer(left)}"
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

    player = await get_player(
        update.effective_user.id,
        update.effective_user.username,
    )

    # Замороженный игрок не может использовать способность.
    if is_frozen(player):
        await update.message.reply_text(
            "🐈‍⬛ Заморозка действует!\n"
            f"⏳ Подожди: "
            f"{format_timer(frozen_time_left(player))}"
        )
        return

    ability_left = ability_time_left(player)

    if ability_left > 0:
        await send_already_used(update, player)
        return

    # Если старый таймер закончился,
    # способность снова становится доступной.
    if player.get("ability_used"):
        player["ability_used"] = False
        player["ability_used_at"] = None
        await save_player(player)

    # /power сразу в ответ на сообщение игрока.
    if update.message.reply_to_message:
        player["awaiting_target"] = True
        player["awaiting_talisman"] = player["talisman"]

        await save_player(player)
        await apply_ability_to_reply(update, context)
        return

    # Обычная активация способности.
    prompt = prepare_ability(player)
    await save_player(player)

    await update.message.reply_text(prompt)


# =========================================================
# ПРИМЕНЕНИЕ СПОСОБНОСТИ
# =========================================================

async def apply_ability_to_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    reply = update.message.reply_to_message

    if reply is None or reply.from_user is None:
        return

    player = await get_player(
        update.effective_user.id,
        update.effective_user.username,
    )

    if not player.get("awaiting_target"):
        return

    target_telegram_user = reply.from_user

    if target_telegram_user.is_bot:
        emoji = talisman_emoji(player)

        await update.message.reply_text(
            f"{emoji} Талисман не использован!\n"
            "🎲 Ответь командой /power в ответ "
            "на сообщение игрока"
        )
        return

    if target_telegram_user.id == player["user_id"]:
        await update.message.reply_text(
            "❌ Нельзя применить способность на себя!"
        )
        return

    target = await get_player(
        target_telegram_user.id,
        target_telegram_user.username,
    )

    talisman = player.get("awaiting_talisman")
    current_time = now()

    # =====================================================
    # ЛЕДИ БАГ
    # =====================================================

    if talisman == "Леди Баг":
        # Если Леди Баг отвечает на сообщение Чёрного кота,
        # она отменяет его текущую заморозку.
        if target.get("talisman") == "Чёрный кот":
            frozen_target_id = target.get("cat_frozen_target")

            if frozen_target_id:
                frozen_player = await get_player(
                    int(frozen_target_id)
                )

                frozen_player["frozen_until"] = None
                frozen_player["frozen_by"] = None

                await save_player(frozen_player)

                target["cat_frozen_target"] = None

            # Таймер способности Чёрного кота не сбрасывается.
            clear_pending_action(target)
            await save_player(target)
        else:
            # Обычная отмена действия игрока.
            clear_pending_action(target)
            await save_player(target)

        clear_pending_action(player)

        player["ability_used"] = True
        player["ability_used_at"] = current_time

        await save_player(player)

        await update.message.reply_text(
            "🐞 Талисман использован!\n"
            "🎲 Способность: Отменяешь действие игрока"
        )
        return

    # =====================================================
    # ЧЁРНЫЙ КОТ
    # =====================================================

    if talisman == "Чёрный кот":
        target["frozen_until"] = (
            current_time + FREEZE_DURATION_SECONDS
        )
        target["frozen_by"] = player["user_id"]

        player["cat_frozen_target"] = target["user_id"]

        await save_player(target)

        clear_pending_action(player)

        player["ability_used"] = True
        player["ability_used_at"] = current_time

        await save_player(player)

        await update.message.reply_text(
            "🐈‍⬛ Талисман использован!\n"
            "🎲 Способность: Заморозка игрока на 12 часов"
        )
        return


# =========================================================
# КНОПКА СМЕНЫ ТАЛИСМАНА
# =========================================================

async def change_talisman_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    player = await get_player(query.from_user.id)

    last_change = player.get("last_talisman_change_at")

    if last_change:
        seconds_passed = now() - float(last_change)

        if seconds_passed < TALISMAN_CHANGE_COOLDOWN_SECONDS:
            await query.answer(
                "⌛️ Талисман можно получать раз в 2 дня. Подожди!",
                show_alert=True,
            )
            return

    current_talisman = player.get("talisman")

    if current_talisman == "Леди Баг":
        new_talisman = "Чёрный кот"
    else:
        new_talisman = "Леди Баг"

    player["talisman"] = new_talisman
    player["last_talisman_emoji"] = TALISMANS[
        new_talisman
    ]["emoji"]

    # Смена способности не сбрасывает текущий таймер способности.
    # Игрок продолжает использовать текущую способность.
    player["last_talisman_change_at"] = now()

    clear_pending_action(player)

    await save_player(player)

    emoji = TALISMANS[new_talisman]["emoji"]

    alert_text = (
        f"{emoji} Талисман изменён. "
        "⌛️ Следующая смена через 2 дня!"
    )

    await query.answer(
        alert_text,
        show_alert=True,
    )

    # После смены сразу показываем обычный профиль.
    ability_left = ability_time_left(player)

    if ability_left > 0:
        profile_text = (
            f"{emoji} Талисман: Активен\n"
            "⏳ Следующая способность через: "
            f"{format_timer(ability_left)}"
        )
    else:
        profile_text = (
            f"{emoji} Талисман: Неактивен\n"
            "⌛️ Способность готова к активации!"
        )

    try:
        await query.edit_message_text(
            profile_text,
            reply_markup=profile_keyboard(),
        )
    except Exception:
        pass


# =========================================================
# КНОПКА МАГАЗИНА
# =========================================================

async def shop_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    await query.answer(
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

    if query.data == "change_talisman":
        await change_talisman_callback(update, context)
        return

    if query.data == "shop":
        await shop_callback(update, context)
        return

    await query.answer()


# =========================================================
# FLASK ДЛЯ RENDER
# =========================================================

flask_app = Flask(__name__)


@flask_app.route("/")
def health():
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

def main():
    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured"
        )

    init_db()

    application = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", profile)
    )

    application.add_handler(
        CommandHandler("profile", profile)
    )

    application.add_handler(
        CommandHandler("power", power)
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.REPLY & ~filters.COMMAND,
            apply_ability_to_reply,
        )
    )

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
    )
    flask_thread.start()

    application.run_polling()


if __name__ == "__main__":
    main()
