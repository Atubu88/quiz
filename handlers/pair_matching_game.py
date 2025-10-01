import random
import uuid
import logging
import time  # <-- добавили импорт
from aiogram import Router, Bot, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_API_KEY
from handlers.quiz_handler import start_quiz

# Инициализация Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_API_KEY)

matching_quiz_router = Router()

ADMIN_ID = 732402669
#CHANNEL_ID = -1002392900552
CHANNEL_ID = -1002487599337
# Глобальный словарь сессий для викторины "Найди пару"
matching_sessions = {}

@matching_quiz_router.message(Command("send_matching_quiz_post"))
async def send_matching_quiz_post(message: types.Message, bot: Bot):
    """Команда админа для отправки кнопок викторины в канал."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет прав для этой команды.")
        return

    response = supabase.table("matching_quizzes").select("id, title").execute()
    quizzes = response.data

    if not quizzes:
        await message.answer("⛔ Нет доступных викторин.")
        return

    buttons = []
    for quiz in quizzes:
        quiz_id = quiz["id"]
        quiz_title = quiz["title"]
        buttons.append([
            InlineKeyboardButton(
                text=quiz_title,
                callback_data=f"select_matching_quiz_{quiz_id}"
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Выберите викторину для отправки в канал:", reply_markup=keyboard)


@matching_quiz_router.callback_query(F.data.startswith("select_matching_quiz_"))
async def process_matching_quiz_selection(callback_query: types.CallbackQuery, bot: Bot):
    """Обработчик выбора викторины (нажатие кнопки), чтобы выслать кнопку в канал."""
    quiz_id = int(callback_query.data.split("_")[-1])
    await callback_query.answer("✅ Викторина выбрана, отправляем ссылку в канал...")

    bot_username = (await bot.me()).username

    quiz_resp = supabase.table("matching_quizzes").select("title, difficulty").eq("id", quiz_id).execute()
    if quiz_resp.data:
        quiz_title = quiz_resp.data[0]["title"]
        quiz_difficulty = quiz_resp.data[0].get("difficulty") or "не указана"
    else:
        quiz_title = "Без названия"
        quiz_difficulty = "неизвестна"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧠 Начать игру",
                    url=f"https://t.me/{bot_username}?start=matching_quiz_{quiz_id}"
                )
            ]
        ]
    )

    text_for_channel = (
        "❓ В этой викторине ваша задача – сопоставить элементы. "
        "Выберите по одной кнопке из левой и правой колонок, чтобы сформировать правильную пару.\n\n"
        f"<b>🧠 Нажмите кнопку, чтобы перейти к боту и начать викторину!</b>\n\n"
        f"<b>📌 Название:</b> «{quiz_title}»\n"
        f"<b>🔰 Уровень сложности:</b> {quiz_difficulty}\n\n"
        "⬇️ <b>Нажмите ниже, чтобы начать!</b>"
    )

    await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text_for_channel,
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@matching_quiz_router.message(Command("start"))
async def handle_matching_quiz_start(message: types.Message, bot: Bot):
    """Старт бота. Если аргумент /start matching_quiz_ID, запускаем Matching Quiz."""
    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "Привет! Наберите /send_matching_quiz_post (если вы админ) "
            "или перейдите по ссылке из канала, чтобы начать викторину."
        )
        return

    if args[1].startswith("matching_quiz_"):
        quiz_id_str = args[1].replace("matching_quiz_", "")
        if quiz_id_str.isdigit():
            quiz_id = int(quiz_id_str)
            logging.info(f"/start matching_quiz_{quiz_id} от {message.from_user.id}")
            await start_matching_quiz(message.chat.id, message.from_user.id, quiz_id, bot)
        else:
            await message.answer("⛔ Неверный формат quiz_ID!")
    else:
        await message.answer("⛔ Неизвестная команда /start!")


async def start_matching_quiz(chat_id: int, user_id: int, quiz_id: int, bot: Bot):
    """
    Запуск викторины с подбором пар.
    Получаем правильные пары из БД, случайно перемешиваем кнопки для левой и правой колонок.
    """
    quiz_resp = supabase.table("matching_quizzes").select("pairs, title").eq("id", quiz_id).execute()
    if not quiz_resp.data:
        await bot.send_message(chat_id, "⛔ Викторина не найдена.")
        return

    pairs = quiz_resp.data[0]["pairs"]
    if not pairs or not isinstance(pairs, list):
        await bot.send_message(chat_id, "⛔ Неверный формат данных викторины.")
        return

    left_buttons = {}
    right_buttons = {}
    correct_map = {}

    left_order = []
    right_order = []

    for pair in pairs:
        left_text = pair.get("left")
        right_text = pair.get("right")
        if not left_text or not right_text:
            continue
        left_id = str(uuid.uuid4())[:8]
        right_id = str(uuid.uuid4())[:8]
        left_buttons[left_id] = left_text
        right_buttons[right_id] = right_text
        correct_map[left_id] = right_id
        left_order.append(left_id)
        right_order.append(right_id)

    random.shuffle(left_order)
    random.shuffle(right_order)

    # Сохраняем в matching_sessions
    matching_sessions[user_id] = {
        "quiz_id": quiz_id,
        "left_buttons": left_buttons,
        "right_buttons": right_buttons,
        "left_order": left_order,
        "right_order": right_order,
        "correct_map": correct_map,
        "matched": set(),
        "current_selection": None,
        "error_count": 0,
        "start_time": time.time()  # <-- время запуска, чтобы потом вычислить time_taken
    }

    keyboard = build_matching_keyboard(matching_sessions[user_id])
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "🎯 Начинаем «Найди пару»!\n\n"
            "Нажмите кнопку слева, затем справа; совпало — увидите ✅.\n"
            "Время идёт! В рейтинге приоритет у безошибочности, при равенстве ошибок – меньшее время выше.\n"
            "При новой попытке результат обновляется."
        ),
        parse_mode="Markdown",
        reply_markup=keyboard
    )


def build_matching_keyboard(session: dict) -> InlineKeyboardMarkup:
    """Строим клавиатуру с двумя колонками (левая/правая) для сопоставления."""
    keyboard_rows = []
    left_order = session["left_order"]
    right_order = session["right_order"]
    left_buttons = session["left_buttons"]
    right_buttons = session["right_buttons"]
    current = session["current_selection"]
    matched = session["matched"]

    max_rows = max(len(left_order), len(right_order))
    for i in range(max_rows):
        row = []
        # Левая колонка
        if i < len(left_order):
            left_id = left_order[i]
            text = left_buttons[left_id]
            if left_id in matched:
                display_text = f"✅ {text}"
                cb_data = "already_matched"
            elif current and current["side"] == "left" and current["id"] == left_id:
                display_text = f"🔘 {text}"
                cb_data = f"match_left_{left_id}"
            else:
                display_text = text
                cb_data = f"match_left_{left_id}"
            row.append(InlineKeyboardButton(text=display_text, callback_data=cb_data))

        # Правая колонка
        if i < len(right_order):
            right_id = right_order[i]
            text = right_buttons[right_id]
            # Проверяем, не угадана ли уже эта кнопка
            found = any(right_id == session["correct_map"].get(lid) for lid in matched)
            if found:
                display_text = f"✅ {text}"
                cb_data = "already_matched"
            elif current and current["side"] == "right" and current["id"] == right_id:
                display_text = f"🔘 {text}"
                cb_data = f"match_right_{right_id}"
            else:
                display_text = text
                cb_data = f"match_right_{right_id}"
            row.append(InlineKeyboardButton(text=display_text, callback_data=cb_data))

        keyboard_rows.append(row)

    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


@matching_quiz_router.callback_query(F.data.startswith("match_left_") | F.data.startswith("match_right_"))
async def process_matching_choice(callback_query: types.CallbackQuery, bot: Bot):
    """Обработка нажатий на кнопки викторины (сопоставление пар)."""
    user_id = callback_query.from_user.id
    data = callback_query.data
    session = matching_sessions.get(user_id)

    if not session:
        await callback_query.answer("⛔ Сессия не найдена.", show_alert=True)
        return

    if data == "already_matched":
        await callback_query.answer("⚠️ Эта пара уже найдена!", show_alert=True)
        return

    if data.startswith("match_left_"):
        side = "left"
        button_id = data.replace("match_left_", "")
    elif data.startswith("match_right_"):
        side = "right"
        button_id = data.replace("match_right_", "")
    else:
        await callback_query.answer("⛔ Неверный выбор!", show_alert=True)
        return

    # Если сейчас нет выбора - это первая кнопка
    if session["current_selection"] is None:
        session["current_selection"] = {"side": side, "id": button_id}
        keyboard = build_matching_keyboard(session)
        try:
            await callback_query.message.edit_reply_markup(reply_markup=keyboard)
        except Exception as e:
            logging.error(f"Ошибка обновления клавиатуры: {e}")
        await callback_query.answer()
        return

    # Если выбор из той же колонки - меняем выбор
    if session["current_selection"]["side"] == side:
        session["current_selection"] = {"side": side, "id": button_id}
        keyboard = build_matching_keyboard(session)
        try:
            await callback_query.message.edit_reply_markup(reply_markup=keyboard)
        except Exception as e:
            logging.error(f"Ошибка обновления клавиатуры: {e}")
        await callback_query.answer("Выбран вариант обновлён!")
        return

    # Иначе проверяем, совпадают ли выбранные кнопки (left_id vs right_id)
    first_selection = session["current_selection"]
    if first_selection["side"] == "left":
        left_id = first_selection["id"]
        right_id = button_id
    else:
        left_id = button_id
        right_id = first_selection["id"]

    correct_right = session["correct_map"].get(left_id)
    if correct_right == right_id:
        session["matched"].add(left_id)
        await callback_query.answer("✅ Пара найдена!")
    else:
        session["error_count"] = session.get("error_count", 0) + 1
        await callback_query.answer("❌ Неверная пара, попробуйте ещё раз!")

    session["current_selection"] = None
    keyboard = build_matching_keyboard(session)
    try:
        await callback_query.message.edit_reply_markup(reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Ошибка обновления клавиатуры: {e}")

    # Проверяем, все ли пары угаданы
    total_pairs = len(session["correct_map"])
    if len(session["matched"]) == total_pairs:
        await finalize_matching_quiz(callback_query, bot, session)
        matching_sessions.pop(user_id, None)


import time
import logging
from aiogram import types, Bot


import time
import logging
from aiogram import types, Bot

async def finalize_matching_quiz(callback_query: types.CallbackQuery, bot: Bot, session: dict):
    user_id = callback_query.from_user.id
    quiz_id = session["quiz_id"]
    error_count = session.get("error_count", 0)

    # Узнаём время
    end_time = time.time()
    time_taken = round(end_time - session["start_time"], 2)

    # Получаем название викторины и ссылку на Telegraph
    quiz_resp = supabase.table("matching_quizzes").select("title", "telegraph_url").eq("id", quiz_id).execute()

    if not quiz_resp.data:
        logging.error(f"Викторина с ID {quiz_id} не найдена в базе!")
        return

    quiz_data = quiz_resp.data[0]
    quiz_title = quiz_data.get("title", "Без названия")
    telegraph_url = quiz_data.get("telegraph_url", "#")  # Если ссылки нет, ставим заглушку

    # -- Запись результата (upsert) --
    supabase.table("matching_quiz_results").upsert({
        "user_id": user_id,
        "quiz_id": quiz_id,
        "is_correct": True,
        "error_count": error_count,
        "time_taken": time_taken
    }, on_conflict="user_id,quiz_id").execute()

    # -- Формируем рейтинг --
    results_for_quiz = supabase.table("matching_quiz_results") \
        .select("user_id, time_taken, error_count") \
        .eq("quiz_id", quiz_id) \
        .eq("is_correct", True) \
        .execute().data

    def ranking_key(rec):
        """
        Обрабатываем случаи, когда time_taken или error_count отсутствуют.
        - Если time_taken отсутствует, ставим float('inf'), чтобы он был в конце списка.
        - Если error_count отсутствует, ставим большое число 999999.
        """
        t = rec["time_taken"] if rec["time_taken"] is not None else float('inf')
        e = rec["error_count"] if rec["error_count"] is not None else 999999
        return (e, t)

    ranking = sorted(results_for_quiz, key=ranking_key)

    rank = next((i for i, row in enumerate(ranking, start=1) if row["user_id"] == user_id), None)
    total_players = len(ranking)

    # Удаляем клавиатуру
    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        logging.error(f"Ошибка удаления клавиатуры: {e}")

    # Сообщение пользователю с ссылкой на Telegraph
    text = (
        f"🎉 Вы прошли викторину «{quiz_title}»!\n"
        f"🔹 Ошибок: {error_count}\n"
        f"⏳ Время: {time_taken} сек.\n\n"
        f"🏆 <b>Ваше место:</b> <b>{rank}</b> из <b>{total_players}</b>\n\n"
    )

    # Добавляем ссылку, только если она есть
    if telegraph_url and telegraph_url != "#":
        text += f"📄 <a href='{telegraph_url}'>КРАТКАЯ ИНФА ПО ВИКТОРИНЕ</a>"

    # Отправляем сообщение
    await bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=False
    )



