import random
import uuid
import logging
from aiogram import Router, Bot, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_API_KEY
import time
# Инициализация Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_API_KEY)
prophets_quiz_router = Router()

ADMIN_ID = 732402669
#CHANNEL_ID = -1002392900552
CHANNEL_ID = -1002487599337
# Глобальный словарь
quiz_sessions = {}

@prophets_quiz_router.message(Command("send_quiz_post"))
async def send_quiz_post(message: types.Message, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет прав для этой команды.")
        return

    response = supabase.table("quizzes_new").select("id, title").execute()
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
                callback_data=f"select_quiz_{quiz_id}"
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Выберите викторину для отправки в канал:", reply_markup=keyboard)

@prophets_quiz_router.callback_query(F.data.startswith("select_quiz_"))
async def process_quiz_selection(callback_query: types.CallbackQuery, bot: Bot):
    quiz_id = int(callback_query.data.split("_")[2])
    await callback_query.answer("✅ Викторина выбрана, отправляем ссылку в канал...")

    bot_username = (await bot.me()).username

    quiz_resp = supabase.table("quizzes_new").select("title, difficulty").eq("id", quiz_id).execute()
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
                    url=f"https://t.me/{bot_username}?start=quiz_{quiz_id}"
                )
            ]
        ]
    )

    text_for_channel = (
        f"<b>🧠 Нажмите кнопку, чтобы перейти к боту и начать викторину!</b> 🎯\n\n"
        f"<b>📌 Название:</b> «{quiz_title}»\n"
        f"<b>🔰 Уровень сложности:</b> {quiz_difficulty}\n\n"
        "❓ В этой викторине вам нужно расположить элементы в правильном порядке. "
        "<b>Количество попыток неограничено</b> 🔥\n\n"
        "⬇️ <b>Нажмите ниже, чтобы начать!</b>"
    )

    await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text_for_channel,
        reply_markup=keyboard,
        parse_mode="HTML"
    )


async def start_quiz(chat_id: int, user_id: int, quiz_id: int, bot: Bot):
    """
    Запуск викторины. Сохраняем start_time, чтобы потом вычислить время прохождения.
    """
    quiz_resp = supabase.table("quizzes_new").select("correct_order").eq("id", quiz_id).execute()
    if not quiz_resp.data:
        await bot.send_message(chat_id, "⛔ Викторина не найдена.")
        return

    correct_order = quiz_resp.data[0]["correct_order"]
    shuffled_list = correct_order.copy()
    random.shuffle(shuffled_list)

    # Ручной upsert в user_attempts (количество выбранных)
    existing_resp = supabase.table("user_attempts").select("*") \
        .eq("user_id", user_id).eq("quiz_id", quiz_id).execute()
    if existing_resp.data:
        supabase.table("user_attempts").update({"selected_count": 0}) \
            .eq("user_id", user_id).eq("quiz_id", quiz_id).execute()
    else:
        supabase.table("user_attempts").insert({
            "user_id": user_id,
            "quiz_id": quiz_id,
            "selected_count": 0
        }).execute()

    # (Новая часть) Запоминаем время начала
    start_time = time.time()

    # Готовим словарь невыбранных
    unselected_dict = {}
    for item in shuffled_list:
        key = str(uuid.uuid4())[:8]
        unselected_dict[key] = item

    quiz_sessions[user_id] = {
        "quiz_id": quiz_id,
        "correct_order": correct_order,
        "unselected_dict": unselected_dict,
        "selected_prophets": [],
        "start_time": start_time  # (Новая часть) Записали в сессию
    }

    keyboard = build_keyboard(selected_list=[], unselected_dict=unselected_dict)
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "🎯 *Вы начали викторину!*\n\n"
            "📌 Нажимайте на элементы в _правильном порядке_ – от первого до последнего.\n"
            "✅ После выбора элемент _поднимется наверх_ с галочкой.\n\n"
            "Время уже пошло!"
        ),
        parse_mode="Markdown",
        reply_markup=keyboard
    )

def build_keyboard(selected_list: list, unselected_dict: dict) -> InlineKeyboardMarkup:
    rows = []
    # Сначала кнопки выбранных (сверху вниз)
    for item in selected_list:
        btn_text = f"✅ {item}"
        btn_data = "already_chosen"
        rows.append([InlineKeyboardButton(text=btn_text, callback_data=btn_data)])

    # Затем кнопки невыбранных
    for key_uuid, full_text in unselected_dict.items():
        btn_data = f"choose_{key_uuid}"
        rows.append([InlineKeyboardButton(text=full_text, callback_data=btn_data)])

    return InlineKeyboardMarkup(inline_keyboard=rows)

@prophets_quiz_router.callback_query(F.data.startswith("choose_") | (F.data == "already_chosen"))
async def process_choice(callback_query: types.CallbackQuery, bot: Bot):
    logging.info(f"Callback data = '{callback_query.data}'")

    user_id = callback_query.from_user.id
    data = callback_query.data

    if data == "already_chosen":
        await callback_query.answer("⚠️ Вы уже выбрали этот элемент!")
        return

    uuid_key = data.replace("choose_", "")

    session = quiz_sessions.get(user_id)
    if not session:
        await callback_query.answer("⛔ Сессия не найдена.", show_alert=True)
        return

    unselected_dict = session["unselected_dict"]
    selected_list = session["selected_prophets"]
    quiz_id = session["quiz_id"]

    if uuid_key not in unselected_dict:
        await callback_query.answer("⛔ Этот элемент уже был выбран или кнопка устарела!", show_alert=True)
        return

    chosen_item = unselected_dict.pop(uuid_key)
    selected_list.append(chosen_item)

    # Обновляем selected_count в user_attempts
    new_count = len(selected_list)
    supabase.table("user_attempts").update({"selected_count": new_count}) \
        .eq("user_id", user_id).eq("quiz_id", quiz_id).execute()

    # Проверяем, все ли выбраны
    if new_count == len(session["correct_order"]):
        await finalize_quiz(callback_query, bot, session)
    else:
        keyboard = build_keyboard(selected_list, unselected_dict)
        await callback_query.message.edit_reply_markup(reply_markup=keyboard)
        await callback_query.answer("✅ Выбор сохранён!")


import time
import logging
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

async def finalize_quiz(callback_query: types.CallbackQuery, bot: Bot, session: dict):
    """
    Завершает викторину:
    1) Считает, правильно ли пользователь расставил порядок
    2) Записывает в quiz_results (is_correct, time_taken)
    3) Если правильно, показывает место (среди is_correct, сортируем по time_taken)
    4) Выводит сравнение ответов
    5) Отображает полезную ссылку (extra_link), если она есть
    6) Добавляет кнопку "Вернуться в канал"
    """
    user_id = callback_query.from_user.id
    quiz_id = session["quiz_id"]
    correct_order = session["correct_order"]
    selected_prophets = session["selected_prophets"]

    # 1) Считаем затраченное время
    end_time = time.time()
    time_taken = round(end_time - session["start_time"], 2)  # сек.

    # 2) Проверяем правильность
    is_correct = (selected_prophets == correct_order)

    # 3) Записываем в quiz_results (upsert)
    existing_resp = supabase.table("quiz_results").select("*") \
        .eq("user_id", user_id).eq("quiz_id", quiz_id).execute()
    if existing_resp.data:
        supabase.table("quiz_results").update({
            "is_correct": is_correct,
            "time_taken": time_taken
        }).eq("user_id", user_id).eq("quiz_id", quiz_id).execute()
    else:
        supabase.table("quiz_results").insert({
            "user_id": user_id,
            "quiz_id": quiz_id,
            "is_correct": is_correct,
            "time_taken": time_taken
        }).execute()

    # 4) Удаляем сессию из памяти
    quiz_sessions.pop(user_id, None)

    # 5) Берём ссылку (extra_link) из quizzes_new (если есть)
    quiz_resp = supabase.table("quizzes_new").select("extra_link") \
        .eq("id", quiz_id).execute()
    if quiz_resp.data and quiz_resp.data[0].get("extra_link"):
        extra_link = quiz_resp.data[0]["extra_link"]
    else:
        extra_link = None

    # Считаем общее число участников и число правильных ответов
    total_resp = supabase.table("quiz_results").select("*", count="exact").eq("quiz_id", quiz_id).execute()
    total_count = total_resp.count or 0

    correct_resp = supabase.table("quiz_results").select("*", count="exact") \
        .eq("quiz_id", quiz_id).eq("is_correct", True).execute()
    correct_count = correct_resp.count or 0

    correct_pct = round(correct_count / total_count * 100, 2) if total_count > 0 else 0

    # Если ответ правильный, считаем место по time_taken
    place_text = ""
    if is_correct:
        all_correct = supabase.table("quiz_results") \
            .select("user_id, time_taken") \
            .eq("quiz_id", quiz_id) \
            .eq("is_correct", True) \
            .execute().data

        # Сортируем по time_taken (меньше = выше место)
        def time_key(rec):
            return rec["time_taken"] if rec["time_taken"] is not None else float('inf')

        ranking = sorted(all_correct, key=time_key)

        rank = None
        for i, record in enumerate(ranking, start=1):
            if record["user_id"] == user_id:
                rank = i
                break

        total_correct_players = len(ranking)
        place_text = (
            f"\n\nВы заняли {rank}-е место из {total_correct_players} (среди ответивших верно).\n"
            f"Затраченное время: {time_taken} сек."
        )

    # Формируем сравнение ответов
    comparison_lines = []
    for sel, cor in zip(selected_prophets, correct_order):
        if sel == cor:
            comparison_lines.append(f"{sel} ✅")
        else:
            comparison_lines.append(f"{sel} ❌ должно быть: {cor}")
    comparison_text = "\n".join(comparison_lines)

    # -- Формируем кнопку(и) --
    if is_correct:
        # Если порядок верный – одна кнопка "Вернуться в канал"
        header = "🎉 *Правильный порядок!*"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Вернуться в канал",
                        url="https://t.me/islamquizes"
                    )
                ]
            ]
        )
    else:
        # Если неверно – две кнопки: "Попробовать снова" и "Вернуться в канал"
        header = "❌ Неправильный порядок!"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Попробовать снова",
                        callback_data=f"retry_quiz_{quiz_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Вернуться в канал",
                        url="https://t.me/islamquizes"
                    )
                ]
            ]
        )

    # Основной текст для пользователя
    text = (
        f"{header}\n\n"
        f"Ваш ответ:\n{comparison_text}\n\n"
        f"Всего участников: {total_count}, из них правильно: {correct_count} ({correct_pct}%)."
        f"{place_text}"
    )

    # Если есть ссылка - добавим в конец
    if extra_link:
        text += f"\n\n📄 [КРАТКАЯ ИНФА ПО ВИКТОРИНЕ]({extra_link})"

    # Отправляем сообщение
    await bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode="Markdown",
        disable_web_page_preview=False,  # 🔹 Отключает предпросмотр ссылки
        reply_markup = keyboard
    )



@prophets_quiz_router.callback_query(F.data.startswith("retry_quiz_"))
async def retry_quiz(callback_query: types.CallbackQuery, bot: Bot):
    quiz_id_str = callback_query.data.replace("retry_quiz_", "")
    if quiz_id_str.isdigit():
        quiz_id = int(quiz_id_str)
        # Перезапускаем викторину: chat_id = user_id
        await start_quiz(callback_query.from_user.id, callback_query.from_user.id, quiz_id, bot)
        await callback_query.answer("🔄 Викторина перезапущена!")
    else:
        await callback_query.answer("⛔ Ошибка! Неверный ID викторины.", show_alert=True)

