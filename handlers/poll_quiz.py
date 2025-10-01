import asyncio
import time
import os
import random
from aiogram import Router, F
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from supabase import create_client
from keyboards import start_keyboard  # Импорт стандартной клавиатуры главного меню

# Подключение к Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_API_KEY)

# Роутер для режима викторины
poll_quiz_router = Router()

# Сессии пользователей (без FSM)
sessions = {}

def load_questions():
    """
    Загружает все вопросы из таблицы poll_quiz_questions.
    Ожидается, что каждая запись имеет следующие поля:
      - id
      - question (текст вопроса)
      - options (массив строк)
      - correct_answer (индекс правильного варианта)
      - explanation (пояснение)
      - theme (необязательно)
    """
    response = supabase.table("poll_quiz_questions").select("*").execute()
    if response.data:
        return response.data
    return []

def poll_quiz_menu_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Начать игру")],
            [KeyboardButton(text="Назад в меню")]
        ],
        resize_keyboard=True
    )

restart_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔄 Начать заново")],
        [KeyboardButton(text="Назад в меню")]
    ],
    resize_keyboard=True
)

def build_keyboard(user_id: int, question_index: int) -> InlineKeyboardMarkup:
    """Собирает инлайн-клавиатуру для текущего вопроса."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    # Извлекаем вопрос из сессии
    current_q = sessions[user_id]["questions"][question_index]
    for i, option in enumerate(current_q["options"]):
        cb_data = f"quiz:{user_id}:{question_index}:{i}"
        keyboard.inline_keyboard.append([InlineKeyboardButton(text=option, callback_data=cb_data)])
    return keyboard

@poll_quiz_router.message(F.text == "🔄 Начать заново")
async def restart_poll_game(message: Message):
    await start_poll_quiz(message)

@poll_quiz_router.message(F.text == "⏳ Выживание")
async def poll_quiz_mode_entry(message: Message):
    """Обработка выбора режима «выживание»."""
    await message.answer(
        " <b>Добро пожаловать в режим 'Выживание'!</b> 🔥\n\n"
        "📜 <b>Правила игры:</b>\n"
        "🟢 У вас <b>3</b> игровых жизни (❤️❤️❤️).\n"
        "❓ Викторина состоит из 25 вопросов — Чтобы выиграть, ответьте правильно на все.\n"
        "⏳ Время на ответ сокращается:\n"
        "   • Вопросы 1–10: 15 секунд на каждый.\n"
        "   • Вопросы 11–20: 10 секунд на каждый.\n"
        "   • Вопросы 21–25: 7 секунд на каждый.\n"
        "❌ За неверный ответ или если время истекает, вы теряете 1 игровую жизнь.\n"
        "⚡️ Чем быстрее вы отвечаете, тем выше окажетесь в таблице рейтинга!\n"
        "Готовы проверить свои знания и скорость? Нажмите <b>'Начать игру'</b>!",
        reply_markup=poll_quiz_menu_keyboard(),
        parse_mode="HTML"
    )

@poll_quiz_router.message(F.text == "Назад в меню")
async def back_to_menu(message: Message):
    user_id = message.from_user.id
    sessions.pop(user_id, None)
    await message.answer("🔙 Вы вернулись в главное меню.", reply_markup=start_keyboard())

@poll_quiz_router.message(F.text == "Начать игру")
async def start_poll_quiz(message: Message):
    user_id = message.from_user.id
    if user_id in sessions and sessions[user_id]["active"]:
        await message.answer("⚠️ Вы уже играете! Завершите текущую игру перед началом новой.")
        return

    # Загружаем все вопросы из базы
    questions = load_questions()
    if not questions:
        await message.answer("⚠️ Не удалось загрузить вопросы. Попробуйте позже.")
        return

    # Перемешиваем список вопросов
    random.shuffle(questions)
    # Если вопросов больше 25, оставляем только первые 25
    if len(questions) > 25:
        questions = questions[:25]

    sessions[user_id] = {
        "lives": 3,
        "question_index": 0,
        "score": 0,
        "active": True,
        "start_time": time.time(),
        "timer_task": None,
        "current_msg_id": None,
        "questions": questions  # сохраняем 25 случайных вопросов в сессию
    }

    await message.answer("🎮 Игра началась! У вас 3 игровых жизни (❤️❤️❤️). Отвечайте правильно, чтобы пройти уровень.")
    await send_question(message)

async def send_question(message: Message, user_id: int = None):
    """
    Отправляем текущий вопрос с инлайн-кнопками и запускаем видимый таймер.
    Если user_id не передан, берется из message.from_user.id.
    """
    if user_id is None:
        user_id = message.from_user.id
    session = sessions[user_id]

    if session["question_index"] >= len(session["questions"]):
        await message.answer("🎉 Поздравляем! Вы прошли все уровни!")
        return await finalize_game(message, user_id)

    current_level = session["question_index"] + 1
    current_q = session["questions"][session["question_index"]]
    # Заменяем эмодзи энергии на эмодзи сердечек (игровая жизнь)
    lives_display = "❤️" * session["lives"]

    keyboard = build_keyboard(user_id, session["question_index"])

    # Определяем длительность таймера по номеру вопроса:
    # первые 10 вопросов – 15 секунд, с 11 по 20 – 10 секунд, далее – 7 секунд.
    if session["question_index"] < 10:
        timer_duration = 15
    elif session["question_index"] < 20:
        timer_duration = 10
    else:
        timer_duration = 7

    text = (f"🆙 Уровень {current_level}:\n{current_q['question']}\n"
            f"⚡ Игровая жизнь: {lives_display}\n⏳ Осталось {timer_duration} секунд")
    sent_msg = await message.answer(text, reply_markup=keyboard)
    session["current_msg_id"] = sent_msg.message_id

    # Запускаем видимый таймер с нужной длительностью
    loop = asyncio.get_running_loop()
    task = loop.create_task(countdown_timer(message, session["question_index"], user_id, timer_duration))
    session["timer_task"] = task

async def countdown_timer(message: Message, question_idx: int, user_id: int, total_time: int = 40):
    """
    Обновляет текст сообщения каждую секунду, показывая оставшееся время.
    Если время истекает, списывает игровую жизнь и повторяет тот же вопрос.
    """
    for remaining in range(total_time, 0, -1):
        await asyncio.sleep(1)
        session = sessions.get(user_id)
        if not session or not session["active"] or session["question_index"] != question_idx:
            return  # если состояние изменилось, прекращаем обновление
        current_level = session["question_index"] + 1
        current_q = session["questions"][question_idx]
        lives_display = "❤️" * session["lives"]
        text = (f"🆙 Уровень {current_level}:\n{current_q['question']}\n"
                f"⚡ Игровая жизнь: {lives_display}\n⏳ Осталось {remaining} секунд")
        keyboard = build_keyboard(user_id, question_idx)
        try:
            await message.bot.edit_message_text(
                text,
                chat_id=message.chat.id,
                message_id=session["current_msg_id"],
                reply_markup=keyboard
            )
        except Exception as e:
            if "message is not modified" in str(e):
                pass  # Игнорируем, если новое содержимое совпадает с текущим
            else:
                print("Ошибка редактирования текста:", e)
    # Если цикл завершился (таймер истёк)
    session = sessions.get(user_id)
    if not session or not session["active"] or session["question_index"] != question_idx:
        return
    session["lives"] -= 1
    try:
        await message.bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=session["current_msg_id"],
            reply_markup=None
        )
    except Exception:
        pass
    await message.answer("⏳ Время вышло! Попробуйте ответить быстрее.")
    if session["lives"] <= 0:
        await message.answer("❌ У вас закончилась игровая жизнь. Игра окончена.")
        return await finalize_game(message, user_id)
    else:
        # Вопрос остаётся тем же для повторной попытки
        return await send_question(message, user_id)

@poll_quiz_router.callback_query(lambda c: c.data and c.data.startswith("quiz:"))
async def quiz_answer_callback(callback: CallbackQuery):
    """
    Обрабатываем нажатие на инлайн-кнопку с ответом.
    Формат callback_data: quiz:user_id:question_index:option_index
    """
    parts = callback.data.split(":")
    if len(parts) != 4:
        return await callback.answer("Некорректные данные!", show_alert=True)
    _, user_id_str, q_idx_str, opt_idx_str = parts
    try:
        user_id = int(user_id_str)
        q_idx = int(q_idx_str)
        opt_idx = int(opt_idx_str)
    except ValueError:
        return await callback.answer("Ошибка данных!", show_alert=True)

    if callback.from_user.id != user_id:
        return await callback.answer("Вы не участник этой игры.", show_alert=True)

    session = sessions.get(user_id)
    if not session or not session["active"]:
        return await callback.answer("Игра уже не активна.", show_alert=True)

    if q_idx != session["question_index"]:
        return await callback.answer("Этот вопрос уже неактивен!", show_alert=True)

    if session["timer_task"]:
        session["timer_task"].cancel()
        session["timer_task"] = None

    try:
        await callback.message.edit_reply_markup(None)
    except Exception:
        pass

    current_q = session["questions"][q_idx]
    correct_option = current_q["correct_answer"]
    if opt_idx == correct_option:
        session["score"] += 1
        feedback = "✅ Верно!\n\nℹ️ " + current_q["explanation"]
        # Переходим к следующему вопросу только если ответ верный
        session["question_index"] += 1
    else:
        session["lives"] -= 1
        feedback = "❌ Неверно!"  # Только сообщение, без пояснения

    await callback.message.answer(feedback)

    if session["lives"] <= 0:
        await callback.message.answer("❌ У вас закончилась игровая жизнь. Игра окончена.")
        return await finalize_game(callback.message, user_id)

    await send_question(callback.message, user_id)
    await callback.answer()

async def finalize_game(message: Message, user_id: int = None):
    """
    Завершаем игру: выводим результаты, сохраняем их в Supabase и очищаем сессию.
    """
    if user_id is None:
        user_id = message.from_user.id
    session = sessions.pop(user_id, None)
    if not session:
        return
    elapsed_time = time.time() - session["start_time"]
    minutes, seconds = divmod(int(elapsed_time), 60)
    first_name = message.from_user.first_name or ""
    username = message.from_user.username or ""
    display_name = first_name or username or "Аноним"
    score = session["score"]
    time_spent = int(elapsed_time)

    # Сохраняем результат в Supabase
    existing_record = supabase.table("poll_quiz_results") \
                              .select("id", "score", "time_spent") \
                              .eq("user_id", user_id).execute()
    if existing_record.data:
        supabase.table("poll_quiz_results").update({
            "score": score,
            "time_spent": time_spent
        }).eq("user_id", user_id).execute()
    else:
        supabase.table("poll_quiz_results").insert({
            "user_id": user_id,
            "username": display_name,
            "score": score,
            "time_spent": time_spent
        }).execute()

    # Измененный запрос: сортируем по score (убывание) и по time_spent (возрастание) через desc=False
    result = supabase.table("poll_quiz_results") \
                     .select("user_id", "score") \
                     .order("score", desc=True) \
                     .order("time_spent", desc=False) \
                     .execute()
    all_results = result.data
    total_players = len(all_results)
    position = next((i + 1 for i, res in enumerate(all_results)
                     if res["user_id"] == user_id), "N/A")

    await message.answer(
        f"🏁 Игра завершена! 📊\n"
        f"✅ Пройденных уровней: {score}\n"
        f"⏱ Время игры: {minutes} мин {seconds} сек.\n"
        f"🏆 Ты занял *{position}-е место* из {total_players} участников!",
        reply_markup=restart_keyboard,
        parse_mode="Markdown"
    )
