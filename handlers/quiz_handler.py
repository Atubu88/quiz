import logging
import os
import asyncio
import time

from aiogram import Router, types, Bot, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramAPIError
from supabase import create_client
from dotenv import load_dotenv

# Подключаем ваш utils и keyboards
from keyboards import quiz_list_keyboard  # Можете оставить, если ещё нужно
from utils import build_leaderboard_message

load_dotenv()

# Подключение к Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_API_KEY)

quiz_router = Router()
logger = logging.getLogger(__name__)


class QuizState(StatesGroup):
    waiting_for_quiz_selection = State()
    answering_questions = State()

class GPTDialog(StatesGroup):
    waiting_for_question_number = State()
    waiting_for_user_question = State()


from mistral import  safe_mistral_request  # подключай свою функцию из utils

@quiz_router.callback_query(F.data == "ask_gpt")
async def ask_gpt_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()  # Удаляем сообщение с кнопкой
    await callback.message.answer("🔢 Введите номер вопроса викторины, по которому хотите задать вопрос:")
    await state.set_state(GPTDialog.waiting_for_question_number)


@quiz_router.message(GPTDialog.waiting_for_question_number)
async def handle_question_number(message: types.Message, state: FSMContext):
    text = message.text.strip()

    # ⛔ Прерывание: команды и "Список викторин"
    if text == "📋 Список викторин" or text.startswith("/"):
        await message.answer("🔄 Возвращаемся к списку викторин.")
        await state.clear()
        await list_quizzes(message, state)
        return

    # 🔢 Проверка на номер
    if not text.isdigit():
        await message.answer("❗ Пожалуйста, введите номер вопроса (например: 3)")
        return

    number = int(text)
    data = await state.get_data()
    quiz = data.get("quiz")

    if not quiz or number < 1 or number > len(quiz["questions"]):
        await message.answer("❗ Нет вопроса с таким номером.")
        return

    question_data = quiz["questions"][number - 1]
    question_text = question_data["text"]
    options = question_data["options"]
    explanation = question_data.get("explanation") or "Нет пояснения."
    correct_option = next((opt["text"] for opt in options if opt["is_correct"]), "неизвестно")

    # 🧩 Формируем текст с контекстом
    options_text = ""
    for i, opt in enumerate(options):
        bullet = "🔹"
        options_text += f"{bullet} {opt['text']}\n"

    full_question_text = (
        f"Вопрос №{number}:\n"
        f"{question_text}\n\n"
        f"Варианты:\n{options_text}\n"
        f"✅ Правильный ответ: {correct_option}\n"
        f"ℹ️ Пояснение: {explanation}"
    )

    # 💾 Обновляем FSM
    await state.update_data(
        selected_question_text=full_question_text,
        gpt_question_count=0,
        chat_history=[
            {
                "role": "system",
                "content": (
                    "Ты помощник для школьников. "
                    "Отвечай очень просто и понятно, как будто ты объясняешь другу из 6 класса. "
                    "Используй простые слова, короткие предложения, никакой научной терминологии. "
                    "Не используй сложные выражения, термины, определения. "
                    "Объясняй так, чтобы понял даже тот, кто учится на тройки. "
                    "Ты можешь использовать примеры из жизни. "
                    "Отвечай только на вопросы по викторине. "
                    "Если вопрос не по теме — скажи: 'Пожалуйста, задайте вопрос, связанный с выбранным вопросом викторины.'"
                )
            },
            {
                "role": "user",
                "content": f"Вопрос из викторины:\n{question_text}\n\nВарианты:\n" + "\n".join(
                    f"- {opt['text']}" for opt in options)
            },
            {
                "role": "assistant",
                "content": f"✅ Правильный ответ: {correct_option}. ℹ️ Пояснение: {explanation}"
            }
        ]

    )

    await message.answer(
        f"✅ Вопрос №{number} выбран!\n\n"
        f"*{question_text}*\n\n"
        f"✍️ Напишите ваш вопрос по этой теме:",
        parse_mode="Markdown"
    )

    await state.set_state(GPTDialog.waiting_for_user_question)





@quiz_router.message(GPTDialog.waiting_for_user_question)
async def handle_user_gpt_question(message: types.Message, state: FSMContext):
    user_question = message.text.strip()

    # 🛑 Прерывание: если это список или команда
    if user_question in ["📋 Список викторин"] or user_question.startswith("/"):
        await message.answer("🔄 Возвращаемся к списку викторин.")
        await state.clear()
        await list_quizzes(message, state)
        return

    data = await state.get_data()
    chat_history = data.get("chat_history", [])
    question_count = data.get("gpt_question_count", 0)

    if not chat_history:
        await message.answer("⚠️ История диалога не найдена. Начните заново.")
        await state.clear()
        return

    chat_history.append({"role": "user", "content": user_question})
    await message.answer("🤖 GPT думает...")

    reply = await safe_mistral_request(chat_history)
    chat_history.append({"role": "assistant", "content": reply})

    question_count += 1
    await state.update_data(chat_history=chat_history, gpt_question_count=question_count)

    await message.answer(f"💬 GPT:\n\n{reply}")

    if question_count >= 5:
        await message.answer("✅ Вы задали 5 вопросов по этому пункту. Диалог с GPT завершён.")
        await state.clear()

    else:
        await message.answer("✍️ Можете задать ещё один вопрос или напишите /stop, чтобы закончить.")




async def get_db_user_id_by_telegram_id(telegram_id: int):
    """
    Получаем внутренний ID пользователя (db_user_id) из таблицы 'users'
    по реальному Telegram ID (telegram_id).
    Возвращает None, если пользователь не найден.
    """
    try:
        response = await asyncio.to_thread(
            supabase.table("users")
            .select("id")
            .eq("telegram_id", telegram_id)
            .single()
            .execute
        )
        return response.data["id"] if response.data else None
    except Exception as e:
        logging.error(f"Ошибка получения db_user_id: {e}")
        return None


async def get_quiz_by_id(quiz_id: int):
    """Получаем викторину по ID с вопросами и статусом активности."""
    try:
        response = await asyncio.to_thread(
            supabase.table("quizzes")
            .select("id, title, is_active, questions(id, text, explanation, options(text, is_correct))")
            .eq("id", quiz_id)
            .single()
            .execute
        )
        return response.data if response.data else None
    except Exception as e:
        logging.error(f"Ошибка получения викторины: {e}")
        return None


async def auto_finish_quiz(chat_id: int, state: FSMContext, bot: Bot):
    """Автоматически завершает викторину через 10 минут."""
    await asyncio.sleep(600)  # ждем 600 секунд (10 минут)
    current_state = await state.get_state()
    if current_state == QuizState.answering_questions.state:
        await bot.send_message(chat_id, "⏰ Время викторины истекло. Завершаем викторину.")
        await finish_quiz(chat_id, state, bot)


# -------------------- НОВЫЙ КОЛБЭК для выбора категории --------------------
@quiz_router.callback_query(F.data.startswith("category_"))
async def show_quizzes_in_category(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    category_id_str = callback_query.data.split("_", maxsplit=1)[1]

    if not category_id_str.isdigit():
        await callback_query.message.answer("Некорректная категория.")
        return

    category_id = int(category_id_str)

    try:
        # Получаем название категории
        category_resp = await asyncio.to_thread(
            supabase.table("categories")
            .select("name")
            .eq("id", category_id)
            .single()
            .execute
        )
        category_name = category_resp.data["name"] if category_resp.data else "неизвестная категория"

        # Загружаем викторины
        quizzes_resp = await asyncio.to_thread(
            supabase.table("quizzes")
            .select("id, title, is_active")
            .eq("category_id", category_id)
            .eq("is_active", True)
            .execute
        )
        quizzes = quizzes_resp.data or []

        if not quizzes:
            await callback_query.message.edit_text(f"В категории '{category_name}' пока нет активных викторин.")
            return

        keyboard_buttons = [
            [InlineKeyboardButton(
                text=f"📝 {q['title']}",
                callback_data=f"quiz_{q['id']}"
            )]
            for q in quizzes
        ]
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="⬅ Назад к категориям",
                callback_data="return_to_quizzes"
            )
        ])

        kb = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

        await callback_query.message.edit_text(
            f"📋 Выберите викторину из категории: *{category_name}*",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        await state.set_state(QuizState.waiting_for_quiz_selection)

    except Exception as e:
        logging.error(f"Ошибка при получении викторин категории {category_id}: {e}")
        await callback_query.message.answer("⚠️ Произошла ошибка при загрузке викторин.")





@quiz_router.callback_query(F.data.startswith("quiz_"), StateFilter(QuizState.waiting_for_quiz_selection))
async def start_quiz(callback_query: types.CallbackQuery, state: FSMContext):
    try:
        quiz_id = int(callback_query.data.split("_")[1])
        telegram_id = callback_query.from_user.id

        # Получаем викторину
        quiz = await get_quiz_by_id(quiz_id)
        if not quiz:
            await callback_query.answer("⚠️ Ошибка: викторина не найдена!", show_alert=True)
            return

        # Проверяем, активна ли викторина
        if not quiz["is_active"]:
            await callback_query.answer("🔒 Эта викторина временно недоступна.", show_alert=True)
            return

        # Удаляем сообщение с кнопками выбора викторины
        await callback_query.message.delete()

        # Уведомляем пользователя о выборе викторины
        await callback_query.message.answer(
            f"✅ Вы выбрали викторину: *{quiz['title']}*.",
            parse_mode="Markdown"
        )

        # Получаем пользователя из Supabase
        db_user_id = await get_db_user_id_by_telegram_id(telegram_id)
        if not db_user_id:
            logging.error(f"❌ Ошибка: Пользователь Telegram ID={telegram_id} не найден в Supabase.")
            await callback_query.message.answer(
                "⚠️ Ошибка: ваш профиль не найден.\nПопробуйте заново /start или перезапустите бота.")
            return

        chat_id = callback_query.message.chat.id

        await state.update_data(
            quiz_id=quiz_id,
            chat_id=chat_id,
            telegram_id=telegram_id,
            db_user_id=db_user_id,
            current_question_index=0,
            correct_answers=0,
            start_time=time.time(),
            quiz_finished=False,
            quiz=quiz
        )
        await state.set_state(QuizState.answering_questions)

        # Запускаем таймер автоматического завершения через 10 минут (600 секунд)
        asyncio.create_task(auto_finish_quiz(chat_id, state, callback_query.bot))

        # Отправляем первый вопрос
        await send_question(chat_id, state, callback_query.bot)

    except Exception as e:
        logger.exception(f"❌ Ошибка в start_quiz: {e}")
        await callback_query.message.answer("⚠️ Ошибка при запуске викторины. Попробуйте снова.")
        await state.clear()


@quiz_router.message(F.text == "📋 Список викторин")
@quiz_router.callback_query(F.data == "return_to_quizzes")
async def list_quizzes(event: types.Message | types.CallbackQuery, state: FSMContext):
    """
    Показываем список активных категорий викторин.
    """
    try:
        # 1. Удаляем сообщение, если это callback_query и сообщение было отправлено ботом
        if isinstance(event, types.CallbackQuery):
            try:
                await event.message.delete()
            except TelegramAPIError as e:
                logger.warning(f"❗ Не удалось удалить сообщение: {e}")
            await event.answer()

        # 2. Загружаем категории
        cat_resp = await asyncio.to_thread(
            supabase.table("categories")
            .select("id, name")
            .eq("is_active", True)
            .execute
        )
        categories = cat_resp.data or []

        if not categories:
            msg = "Нет доступных категорий викторин."
            if isinstance(event, types.CallbackQuery):
                await event.message.answer(msg)
            else:
                await event.answer(msg)
            return

        # 3. Кнопки с категориями
        keyboard_buttons = [
            [InlineKeyboardButton(
                text=f"📂 {cat['name']}",
                callback_data=f"category_{cat['id']}"
            )]
            for cat in categories
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

        # 4. Показываем сообщение
        msg_text = "📚 Выберите <b>категорию</b> викторин:"
        if isinstance(event, types.CallbackQuery):
            await event.message.answer(msg_text, reply_markup=kb, parse_mode="HTML")
        elif isinstance(event, types.Message):
            await event.bot.send_message(event.chat.id, msg_text, reply_markup=kb, parse_mode="HTML")

        # 5. Обновляем состояние
        await state.set_state(QuizState.waiting_for_quiz_selection)

    except Exception as e:
        logging.error(f"Ошибка в list_quizzes (показ категорий): {e}")
        error_msg = "⚠️ Ошибка загрузки категорий."
        if isinstance(event, types.CallbackQuery):
            await event.message.answer(error_msg)
        else:
            await event.answer(error_msg)




async def send_question(chat_id: int, state: FSMContext, bot: Bot):
    """Отправка вопроса викторины с обратным отсчетом и нумерацией."""
    try:
        data = await state.get_data()
        quiz = data.get("quiz")

        if not quiz or "questions" not in quiz:
            await bot.send_message(chat_id, "⚠️ Ошибка: викторина не найдена или не содержит вопросов.")
            return

        questions = quiz["questions"]
        current_index = data.get("current_question_index", 0)

        if current_index >= len(questions):
            await finish_quiz(chat_id, state, bot)
            return

        question = questions[current_index]
        options = question["options"]
        correct_index = next((i for i, opt in enumerate(options) if opt["is_correct"]), None)

        # Отсчёт перед первым вопросом
        if current_index == 0:
            countdown = ["3️⃣", "2️⃣", "1️⃣"]
            for num in countdown:
                msg = await bot.send_message(chat_id, f"⏳ {num}")
                await asyncio.sleep(1)
                await bot.delete_message(chat_id, msg.message_id)

        # 🔢 Добавляем номер к тексту вопроса
        question_text = f"{current_index + 1}. {question['text']}"

        poll_message = await bot.send_poll(
            chat_id=chat_id,
            question=question_text,
            options=[opt["text"] for opt in options],
            type="quiz",
            correct_option_id=correct_index,
            is_anonymous=False,
        )

        await state.update_data(poll_id=poll_message.poll.id)

    except Exception as e:
        logger.error(f"Ошибка в send_question: {e}")
        await bot.send_message(chat_id, "⚠️ Ошибка отправки вопроса.")
        await state.clear()



@quiz_router.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer, state: FSMContext):
    """Обрабатывает ответ пользователя (quiz Poll)."""
    try:
        data = await state.get_data()
        chat_id = data.get("chat_id")
        quiz = data.get("quiz")  # Используем уже загруженные вопросы

        if not chat_id or not quiz or "questions" not in quiz:
            logging.warning("⚠️ Ошибка: chat_id или викторина не найдены в FSM.")
            return

        questions = quiz["questions"]
        current_question_index = data.get("current_question_index", 0)

        if current_question_index >= len(questions):
            await poll_answer.bot.send_message(chat_id, "⚠️ Вопросов больше нет.")
            return

        question = questions[current_question_index]
        options = question["options"]

        if not poll_answer.option_ids:
            await poll_answer.bot.send_message(chat_id, "⚠️ Вы не выбрали вариант.")
            return

        selected_option_id = poll_answer.option_ids[0]
        selected_option = options[selected_option_id]

        # Проверяем, верно ли отвечено
        if selected_option["is_correct"]:
            correct_answers = data.get("correct_answers", 0) + 1
            await state.update_data(correct_answers=correct_answers)
            await poll_answer.bot.send_message(chat_id, "✅ Верно!")
        else:
            await poll_answer.bot.send_message(chat_id, "❌ Неверно.")

        # Выводим пояснение (если есть)
        explanation = question.get("explanation")
        if explanation:
            await poll_answer.bot.send_message(chat_id, f"ℹ️ Пояснение: {explanation}")

        # Переходим к следующему вопросу
        await state.update_data(current_question_index=current_question_index + 1)

        if current_question_index + 1 >= len(questions):
            await finish_quiz(chat_id, state, poll_answer.bot)
        else:
            await send_question(chat_id, state, poll_answer.bot)

    except Exception as e:
        logger.error(f"Ошибка в handle_poll_answer: {e}")
        if state:
            await state.clear()
        await poll_answer.bot.send_message(poll_answer.user.id, "⚠️ Ошибка обработки ответа.")


async def finish_quiz(chat_id: int, state: FSMContext, bot: Bot):
    """🏆 Завершение викторины и показ турнирной таблицы."""
    try:
        data = await state.get_data()
        quiz_id = data["quiz_id"]
        db_user_id = data["db_user_id"]
        correct_answers = data["correct_answers"]
        time_taken = int(time.time() - data["start_time"])
        quiz_data = data.get("quiz")  # <-- нужно сохранить quiz для GPT после clear()

        # Проверяем, существует ли уже результат
        existing_result = await asyncio.to_thread(
            supabase.table("results")
            .select("user_id", "score", "time_taken")
            .eq("user_id", db_user_id)
            .eq("quiz_id", quiz_id)
            .limit(1)
            .execute
        )

        if existing_result.data:
            await bot.send_message(chat_id, "Вы уже проходили эту викторину, ваш результат сохранён.")
        else:
            result_data = {
                "user_id": db_user_id,
                "quiz_id": quiz_id,
                "score": correct_answers,
                "time_taken": time_taken
            }
            response = await asyncio.to_thread(
                supabase.table("results").insert(result_data).execute
            )
            if response.data is None:
                logging.error("❌ Ошибка при сохранении результата.")
                await bot.send_message(chat_id, "⚠️ Ошибка при сохранении результата.")
                return
            await bot.send_message(chat_id, "✅ Ваш результат сохранён.")

        # Загружаем все результаты
        leaderboard_response = await asyncio.to_thread(
            supabase.table("results")
            .select("user_id", "score", "time_taken")
            .eq("quiz_id", quiz_id)
            .order("score", desc=True)
            .order("time_taken", desc=False)
            .execute
        )
        leaderboard = leaderboard_response.data or []

        total_participants = len(leaderboard)
        user_position = next((idx + 1 for idx, res in enumerate(leaderboard)
                              if res["user_id"] == db_user_id), None)

        result_message = (
            f"🏆 Викторина завершена!\n\n"
            f"🔹 Ваш результат: {correct_answers} правильных ответов\n"
            f"🕒 Время: {time_taken} сек\n"
            f"📊 Ваше место в рейтинге: {user_position}/{total_participants}"
        )

        await bot.send_message(chat_id, result_message)

        # Топ-10
        if leaderboard:
            top_results = leaderboard[:10]
            leaderboard_message = await build_leaderboard_message(top_results, supabase)
            await bot.send_message(chat_id, leaderboard_message)

        await asyncio.sleep(2)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Список викторин", callback_data="return_to_quizzes")]
            ]
        )
        await bot.send_message(chat_id, "📋 Вы можете вернуться к выбору викторин:", reply_markup=keyboard)

        # Кнопка GPT
        gpt_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🤖 Спросить GPT", callback_data="ask_gpt")]
            ]
        )

        # Очистить state и сохранить quiz для GPT FSM
        await state.clear()
        await state.update_data(quiz=quiz_data)

        await bot.send_message(chat_id, "❓ Хотите задать вопрос GPT по какому-то пункту викторины?",
                               reply_markup=gpt_keyboard)

    except Exception as e:
        logging.error(f"❌ Ошибка в finish_quiz: {e}")
        await bot.send_message(chat_id, "⚠️ Ошибка завершения викторины.")
        await state.clear()

