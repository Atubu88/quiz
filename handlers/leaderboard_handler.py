import logging
import asyncio
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from supabase import create_client
from config import SUPABASE_URL, SUPABASE_API_KEY
from utils import build_leaderboard_message

# Подключаем Supabase
supabase = create_client(SUPABASE_URL, SUPABASE_API_KEY)

leaderboard_router = Router()

class LeaderboardState(StatesGroup):
    waiting_for_quiz_selection = State()


@leaderboard_router.message(F.text == "🏆 Турнирная таблица")
async def select_quiz_for_leaderboard(message: types.Message, state: FSMContext):
    """
    Получение списка викторин из Supabase и отправка пользователю.
    """
    try:
        response = await asyncio.to_thread(
            supabase.table("quizzes").select("id, title").order("id").execute
        )
        quizzes = response.data

        if not quizzes:
            await message.answer("Нет доступных викторин.")
            return

        inline_keyboard = [
            [InlineKeyboardButton(text=quiz["title"], callback_data=f"leaderboard_{quiz['id']}")]
            for quiz in quizzes
        ]

        keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
        await message.answer("Выберите викторину для отображения турнирной таблицы:", reply_markup=keyboard)
        await state.set_state(LeaderboardState.waiting_for_quiz_selection)

    except Exception as e:
        logging.error(f"Ошибка получения викторин: {e}")
        await message.answer("⚠️ Ошибка загрузки викторин. Попробуйте позже.")



@leaderboard_router.callback_query(
    F.data.startswith("leaderboard_"),
    StateFilter(LeaderboardState.waiting_for_quiz_selection)
)
async def show_leaderboard(callback_query: types.CallbackQuery, state: FSMContext):
    """
    Отображение турнирной таблицы для выбранной викторины.
    """
    quiz_id = int(callback_query.data.split("_")[1])

    try:
        quiz_response = await asyncio.to_thread(
            supabase.table("quizzes").select("title").eq("id", quiz_id).single().execute
        )
        quiz = quiz_response.data

        if not quiz:
            await callback_query.message.answer("Викторина не найдена.")
            return

        # Загружаем результаты (возможно, все, но выводим только 10)
        result_response = await asyncio.to_thread(
            supabase.table("results")
            .select("user_id, score, time_taken")
            .eq("quiz_id", quiz_id)
            .order("score", desc=True)
            .order("time_taken", desc=False)
            .execute
        )
        results = result_response.data

        if not results:
            await callback_query.message.answer("Пока нет результатов для этой викторины.")
            return

        top_10 = results[:10]

        # Формируем сам текст Топ-10 через функцию
        leaderboard_text = await build_leaderboard_message(top_10, supabase)

        quiz_title = quiz["title"]
        # Добавляем шапку
        final_text = f"🏆 Турнирная таблица для викторины \"{quiz_title}\":\n{leaderboard_text}"

        await callback_query.message.answer(final_text)

    except Exception as e:
        logging.error(f"Ошибка получения турнирной таблицы: {e}")
        await callback_query.message.answer("⚠️ Ошибка загрузки таблицы. Попробуйте позже.")




@leaderboard_router.message(F.text == "🌟 Общий рейтинг")
async def show_general_leaderboard(message: types.Message):
    try:
        # Вызываем вашу хранимую процедуру get_total_scores
        # которая возвращает [{"user_id":..., "total_score":..., "total_time":...}, ...]
        total_results_response = await asyncio.to_thread(
            supabase.rpc("get_total_scores").execute
        )
        results = total_results_response.data

        if not results:
            await message.answer("Пока нет результатов.")
            return

        # Приводим к формату "user_id", "score", "time_taken"
        # Предположим, нужно только ТОП-10
        top_10 = []
        for row in results[:10]:
            top_10.append({
                "user_id": row["user_id"],
                "score": int(row["total_score"]),
                "time_taken": int(row["total_time"])
            })

        # Вызываем нашу функцию, чтобы получить оформленный текст
        leaderboard_text = await build_leaderboard_message(top_10, supabase)

        # Добавим заголовок "🌟 Общий рейтинг:\n"
        final_text = f"🌟 Общий рейтинг:\n{leaderboard_text}"

        await message.answer(final_text)

    except Exception as e:
        logging.error(f"Ошибка загрузки общего рейтинга: {e}")
        await message.answer("⚠️ Ошибка загрузки рейтинга. Попробуйте позже.")




