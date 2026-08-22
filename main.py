import asyncio
import os
import random
import time
import threading

import motor.motor_asyncio
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
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
db = mongo_client["ninety_nine_nights"]
players_collection = db["players"]


# =========================================================
# FLASK
# =========================================================

flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "99 nights bot is running"


def run_flask():
    port = int(os.getenv("PORT", 10000))

    flask_app.run(
        host="0.0.0.0",
        port=port
    )


# =========================================================
# НАСТРОЙКИ ИГРЫ
# =========================================================

MAX_PLAYERS = 5

REGISTRATION_SECONDS = 30

RESOURCE_SECONDS = 179

SATIETY_TICK_SECONDS = 5
FIRE_TICK_SECONDS = 5

DAY_SECONDS = 180
NIGHT_SECONDS = 90


# =========================================================
# СОСТОЯНИЕ РЕГИСТРАЦИИ
# =========================================================

registration_players = set()

registration_task = None

current_round_id = 0


# =========================================================
# DATABASE / MONGODB
# =========================================================

async def create_player(user_id, username):

    existing = await players_collection.find_one({
        "user_id": user_id
    })

    if existing:
        return

    await players_collection.insert_one({
        "user_id": user_id,
        "username": username,

        "class_name": "🏹 Разведчик",
        "gems": 0,

        "in_round": False,
        "in_registration": False,

        "round_id": None,

        "satiety": 100,

        "fire": 0,
        "fire_started": False,

        "food": 1,
        "food_type": "stew",

        "logs": 0,
        "metal": 0,

        "phase": "day",
        "day_number": 1,

        "forest_started_at": None
    })


async def get_player(user_id):

    return await players_collection.find_one({
        "user_id": user_id
    })


async def update_player(user_id, **values):

    if not values:
        return

    await players_collection.update_one(
        {
            "user_id": user_id
        },
        {
            "$set": values
        }
    )


# =========================================================
# КЛАВИАТУРЫ
# =========================================================

def start_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👤 Профиль",
                callback_data="profile"
            )
        ],
        [
            InlineKeyboardButton(
                "🎲 Перейти в раунд",
                callback_data="round"
            )
        ],
        [
            InlineKeyboardButton(
                "🌐 Язык",
                callback_data="language"
            )
        ]
    ])


def profile_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 Покупка класса",
                callback_data="class_shop"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 Моя статистика",
                callback_data="statistics"
            )
        ]
    ])


def round_start_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🚀 Начать регистрацию",
                callback_data="start_registration"
            )
        ]
    ])


def registration_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🤝 Присоединиться",
                callback_data="join_registration"
            )
        ]
    ])


def round_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🌳 Отправиться в лес",
                callback_data="forest"
            )
        ],
        [
            InlineKeyboardButton(
                "🍗 Поесть",
                callback_data="eat"
            ),
            InlineKeyboardButton(
                "🪵 Огонь",
                callback_data="fire"
            )
        ]
    ])


def forest_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🍃 Добыча ресурсов",
                callback_data="resources"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data="back_round"
            ),
            InlineKeyboardButton(
                "➡️ Далее",
                callback_data="forest_next"
            )
        ]
    ])


def language_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🇷🇺 Русский",
                callback_data="lang_ru"
            ),
            InlineKeyboardButton(
                "🇺🇸 English",
                callback_data="lang_en"
            )
        ]
    ])


# =========================================================
# ТЕКСТ "РАУНД НЕ СОЗДАН"
# =========================================================

ROUND_NOT_CREATED_TEXT = (
    "🎲 Раунд еще не создан!\n"
    "Нажми, чтобы начать регистрацию:"
)


# =========================================================
# /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    await create_player(
        user.id,
        user.username or user.first_name
    )

    await update.message.reply_text(
        "🎲 99 ночей",
        reply_markup=start_keyboard()
    )


# =========================================================
# /PROFILE
# =========================================================

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    await create_player(
        user.id,
        user.username or user.first_name
    )

    player = await get_player(user.id)

    text = (
        f"{player['class_name']}\n"
        f"💎 Самоцветы: {player['gems']}\n"
        f"⏳ Находится в раунде"
    )

    if update.callback_query:

        await update.callback_query.edit_message_text(
            text,
            reply_markup=profile_keyboard()
        )

    else:

        await update.message.reply_text(
            text,
            reply_markup=profile_keyboard()
        )


# =========================================================
# /ROUND
# =========================================================

async def round_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    await create_player(
        user.id,
        user.username or user.first_name
    )

    player = await get_player(user.id)

    if player["in_round"]:

        await update.message.reply_text(
            f"🌞 День: {player['day_number']}\n"
            f"🌿 Сытость: {player['satiety']}%",
            reply_markup=round_keyboard()
        )

        return

    await update.message.reply_text(
        ROUND_NOT_CREATED_TEXT,
        reply_markup=round_start_keyboard()
    )


# =========================================================
# КНОПКА "ПЕРЕЙТИ В РАУНД"
# =========================================================

async def round_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    user = query.from_user

    await create_player(
        user.id,
        user.username or user.first_name
    )

    player = await get_player(user.id)

    await query.answer()

    if player["in_round"]:

        await query.edit_message_text(
            f"🌞 День: {player['day_number']}\n"
            f"🌿 Сытость: {player['satiety']}%",
            reply_markup=round_keyboard()
        )

        return

    await query.edit_message_text(
        ROUND_NOT_CREATED_TEXT,
        reply_markup=round_start_keyboard()
    )


# =========================================================
# НАЧАТЬ РЕГИСТРАЦИЮ
# =========================================================

async def start_registration(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global registration_players
    global registration_task

    query = update.callback_query

    user = query.from_user

    await create_player(
        user.id,
        user.username or user.first_name
    )

    if registration_task and not registration_task.done():

        if user.id in registration_players:

            await query.answer()

            return

        if len(registration_players) >= MAX_PLAYERS:

            return

        registration_players.add(user.id)

        await update_player(
            user.id,
            in_registration=True
        )

        await query.answer()

        return

    registration_players = set()

    registration_players.add(user.id)

    await update_player(
        user.id,
        in_registration=True
    )

    await query.answer()

    message = await query.edit_message_text(
        "🎲 Набор в раунд\n"
        "⏳ У вас 30 секунд",
        reply_markup=registration_keyboard()
    )

    registration_task = asyncio.create_task(
        registration_timer(
            context,
            message.chat_id,
            message.message_id
        )
    )


# =========================================================
# ПРИСОЕДИНИТЬСЯ
# =========================================================

async def join_registration(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    user = query.from_user

    if not registration_task or registration_task.done():

        return

    if len(registration_players) >= MAX_PLAYERS:

        return

    if user.id in registration_players:

        await query.answer()

        return

    await create_player(
        user.id,
        user.username or user.first_name
    )

    registration_players.add(user.id)

    await update_player(
        user.id,
        in_registration=True
    )

    await query.answer()


# =========================================================
# ТАЙМЕР РЕГИСТРАЦИИ
# =========================================================

async def registration_timer(
    context,
    chat_id,
    message_id
):

    global current_round_id
    global registration_players

    await asyncio.sleep(
        REGISTRATION_SECONDS
    )

    current_round_id += 1

    players = list(
        registration_players
    )

    try:

        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=message_id
        )

    except Exception:
        pass

    for user_id in players:

        await update_player(
            user_id,

            in_registration=False,
            in_round=True,

            round_id=current_round_id,

            satiety=100,

            fire=0,
            fire_started=False,

            food=1,
            food_type="stew",

            logs=0,
            metal=0,

            phase="day",
            day_number=1,

            forest_started_at=None
        )

    for user_id in players:

        try:

            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "🌞 День: 1\n"
                    "🌿 Сытость: 100%"
                ),
                reply_markup=round_keyboard()
            )

        except Exception:
            pass

    asyncio.create_task(
        round_loop(
            context,
            current_round_id
        )
    )


# =========================================================
# ЛЕС
# =========================================================

async def forest(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    user_id = query.from_user.id

    player = await get_player(user_id)

    if not player or not player["in_round"]:

        return

    await query.answer()

    await query.edit_message_text(
        "🌳 Чем займёмся в лесу?\n"
        "Выбери действие:",
        reply_markup=forest_keyboard()
    )


# =========================================================
# ДОБЫЧА РЕСУРСОВ
# =========================================================

async def resources(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    user_id = query.from_user.id

    player = await get_player(user_id)

    if not player or not player["in_round"]:

        return

    now = time.time()

    if player["forest_started_at"] is not None:

        elapsed = (
            now - player["forest_started_at"]
        )

        if elapsed < RESOURCE_SECONDS:

            await query.answer(
                "🍃 Гуляем и добываем ресурсы. ⏳ Процесс займёт: 2 мин. 59 сек",
                show_alert=True
            )

            return

        if random.choice([True, False]):

            await update_player(
                user_id,

                logs=player["logs"] + 5,
                food=player["food"] + 2,

                food_type="stew",

                forest_started_at=None
            )

            await query.answer(
                "🍃 Добыча ресурсов окончена! Получено: 🪵 5 брёвен, 🍗 2 еда",
                show_alert=True
            )

        else:

            await update_player(
                user_id,

                metal=player["metal"] + 4,
                food=player["food"] + 2,

                food_type="stew",

                forest_started_at=None
            )

            await query.answer(
                "🍃 Добыча ресурсов окончена! Получено: 🔩 4 металла, 🍗 2 еда",
                show_alert=True
            )

        return

    await update_player(
        user_id,
        forest_started_at=now
    )

    await query.answer(
        "🍃 Гуляем и добываем ресурсы. ⏳ Процесс займёт: 2 мин. 59 сек",
        show_alert=True
    )


# =========================================================
# ПОЕСТЬ
# =========================================================

async def eat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    user_id = query.from_user.id

    player = await get_player(user_id)

    if not player or not player["in_round"]:

        return

    if player["food"] <= 0:

        await query.answer(
            f"🍗 Нет порций еды! 🌿 Текущий уровень: {player['satiety']}%",
            show_alert=True
        )

        return

    new_satiety = min(
        100,
        player["satiety"] + 50
    )

    await update_player(
        user_id,

        food=player["food"] - 1,
        satiety=new_satiety
    )

    if player["food_type"] == "stew":

        await query.answer(
            f"🥣 Съедена 1 порция еды! 🌿 Текущий уровень: {new_satiety}%",
            show_alert=True
        )

    else:

        await query.answer(
            f"🍗 Съедена 1 порция еды! 🌿 Текущий уровень: {new_satiety}%",
            show_alert=True
        )


# =========================================================
# ОГОНЬ
# =========================================================

async def fire(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    user_id = query.from_user.id

    player = await get_player(user_id)

    if not player or not player["in_round"]:

        return

    # Первый бесплатный розжиг
    if not player["fire_started"]:

        await update_player(
            user_id,

            fire=100,
            fire_started=True
        )

        await query.answer(
            "🪵 Ты разжёг огонь! 🔥 Текущий уровень: 100%",
            show_alert=True
        )

        return

    # Нет брёвен
    if player["logs"] <= 0:

        await query.answer(
            f"🪵 Нет брёвен! 🔥 Текущий уровень: {player['fire']}%",
            show_alert=True
        )

        return

    # 1 бревно = +10% огня
    new_fire = min(
        100,
        player["fire"] + 10
    )

    await update_player(
        user_id,

        fire=new_fire,
        logs=player["logs"] - 1
    )

    await query.answer(
        f"🪵 Огонь поддержан! 🔥 Текущий уровень: {new_fire}%",
        show_alert=True
    )


# =========================================================
# НАЗАД
# =========================================================

async def back_round(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    user_id = query.from_user.id

    player = await get_player(user_id)

    if not player or not player["in_round"]:

        return

    await query.answer()

    await query.edit_message_text(
        f"🌞 День: {player['day_number']}\n"
        f"🌿 Сытость: {player['satiety']}%",
        reply_markup=round_keyboard()
    )


# =========================================================
# ДАЛЕЕ
# =========================================================

async def forest_next(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()


# =========================================================
# ПОКУПКА КЛАССА
# =========================================================

async def class_shop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer(
        "⏳ Функция на стадии разработки!",
        show_alert=True
    )


# =========================================================
# СТАТИСТИКА
# =========================================================

async def statistics(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer(
        "⏳ Функция на стадии разработки!",
        show_alert=True
    )


# =========================================================
# ЯЗЫК
# =========================================================

async def language(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    await query.edit_message_text(
        "🇷🇺 Выбрать язык | Choose language",
        reply_markup=language_keyboard()
    )


async def lang_ru(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()


async def lang_en(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer(
        "⏳ Функция на стадии разработки!",
        show_alert=True
    )


# =========================================================
# /LEAVE
# =========================================================

async def leave(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    player = await get_player(user_id)

    if not player:

        return

    if not player["in_round"]:

        return

    await update_player(
        user_id,

        in_round=False,
        in_registration=False,

        round_id=None,

        satiety=100,

        fire=0,
        fire_started=False,

        food=0,

        logs=0,
        metal=0,

        phase="day",
        day_number=1,

        forest_started_at=None
    )

    await update.message.reply_text(
        "🎲 Раунд покинут!"
    )


# =========================================================
# УДАЛЕНИЕ ИГРОКА ИЗ РАУНДА
# =========================================================

async def player_died(
    context,
    user_id
):

    await update_player(
        user_id,

        in_round=False,
        in_registration=False,

        round_id=None,

        satiety=100,

        fire=0,
        fire_started=False,

        food=0,

        logs=0,
        metal=0,

        phase="day",
        day_number=1,

        forest_started_at=None
    )

    try:

        await context.bot.send_message(
            chat_id=user_id,

            text=ROUND_NOT_CREATED_TEXT,

            reply_markup=round_start_keyboard()
        )

    except Exception:
        pass


# =========================================================
# ИГРОВОЙ ЦИКЛ
# =========================================================

async def round_loop(
    context,
    round_id
):

    day_number = 1

    while True:

        players_cursor = players_collection.find({
            "in_round": True,
            "round_id": round_id
        })

        users = []

        async for player in players_cursor:

            users.append(
                player["user_id"]
            )

        if not users:

            return

        # =================================================
        # ДЕНЬ
        # =================================================

        await players_collection.update_many(
            {
                "in_round": True,
                "round_id": round_id
            },
            {
                "$set": {
                    "phase": "day",
                    "day_number": day_number
                }
            }
        )

        day_start = time.time()

        while (
            time.time() - day_start
            < DAY_SECONDS
        ):

            await asyncio.sleep(
                SATIETY_TICK_SECONDS
            )

            current_users = list(users)

            for user_id in current_users:

                player = await get_player(user_id)

                if not player:

                    continue

                if not player["in_round"]:

                    if user_id in users:
                        users.remove(user_id)

                    continue

                if player["round_id"] != round_id:

                    if user_id in users:
                        users.remove(user_id)

                    continue

                new_satiety = max(
                    0,
                    player["satiety"] - 1
                )

                await update_player(
                    user_id,
                    satiety=new_satiety
                )

                if new_satiety <= 0:

                    await player_died(
                        context,
                        user_id
                    )

                    if user_id in users:
                        users.remove(user_id)

        # =================================================
        # ПЕРЕХОД В НОЧЬ
        # =================================================

        current_users = list(users)

        for user_id in current_users:

            player = await get_player(user_id)

            if not player:

                continue

            if not player["in_round"]:

                continue

            if not player["fire_started"]:

                await player_died(
                    context,
                    user_id
                )

                if user_id in users:
                    users.remove(user_id)

                continue

            if player["fire"] <= 0:

                await player_died(
                    context,
                    user_id
                )

                if user_id in users:
                    users.remove(user_id)

                continue

            await update_player(
                user_id,
                phase="night"
            )

        # =================================================
        # НОЧЬ
        # =================================================

        night_start = time.time()

        while (
            time.time() - night_start
            < NIGHT_SECONDS
        ):

            await asyncio.sleep(
                FIRE_TICK_SECONDS
            )

            current_users = list(users)

            for user_id in current_users:

                player = await get_player(user_id)

                if not player:

                    continue

                if not player["in_round"]:

                    if user_id in users:
                        users.remove(user_id)

                    continue

                new_satiety = max(
                    0,
                    player["satiety"] - 1
                )

                new_fire = max(
                    0,
                    player["fire"] - 1
                )

                await update_player(
                    user_id,

                    satiety=new_satiety,
                    fire=new_fire
                )

                if new_satiety <= 0:

                    await player_died(
                        context,
                        user_id
                    )

                    if user_id in users:
                        users.remove(user_id)

                    continue

                if new_fire <= 0:

                    await player_died(
                        context,
                        user_id
                    )

                    if user_id in users:
                        users.remove(user_id)

        # =================================================
        # НОВЫЙ ДЕНЬ
        # =================================================

        day_number += 1

        current_users = list(users)

        for user_id in current_users:

            player = await get_player(user_id)

            if not player:

                continue

            if not player["in_round"]:

                continue

            await update_player(
                user_id,

                phase="day",
                day_number=day_number
            )

            try:

                await context.bot.send_message(
                    chat_id=user_id,

                    text=(
                        f"🌞 День: {day_number}\n"
                        f"🌿 Сытость: {player['satiety']}%"
                    ),

                    reply_markup=round_keyboard()
                )

            except Exception:
                pass


# =========================================================
# ОБРАБОТЧИК КНОПОК
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    data = query.data

    if data == "profile":

        await profile(
            update,
            context
        )

    elif data == "round":

        await round_button(
            update,
            context
        )

    elif data == "start_registration":

        await start_registration(
            update,
            context
        )

    elif data == "join_registration":

        await join_registration(
            update,
            context
        )

    elif data == "class_shop":

        await class_shop(
            update,
            context
        )

    elif data == "statistics":

        await statistics(
            update,
            context
        )

    elif data == "language":

        await language(
            update,
            context
        )

    elif data == "lang_ru":

        await lang_ru(
            update,
            context
        )

    elif data == "lang_en":

        await lang_en(
            update,
            context
        )

    elif data == "forest":

        await forest(
            update,
            context
        )

    elif data == "resources":

        await resources(
            update,
            context
        )

    elif data == "eat":

        await eat(
            update,
            context
        )

    elif data == "fire":

        await fire(
            update,
            context
        )

    elif data == "back_round":

        await back_round(
            update,
            context
        )

    elif data == "forest_next":

        await forest_next(
            update,
            context
        )


# =========================================================
# MAIN
# =========================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "Не найден TELEGRAM_BOT_TOKEN"
        )

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True
    )

    flask_thread.start()

    application = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "profile",
            profile
        )
    )

    application.add_handler(
        CommandHandler(
            "round",
            round_command
        )
    )

    application.add_handler(
        CommandHandler(
            "leave",
            leave
        )
    )

    application.add_handler(
        CommandHandler(
            "language",
            language
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    print(
        "99 ночей запущен."
    )

    application.run_polling()


if __name__ == "__main__":

    main()
