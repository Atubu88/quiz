import asyncio
import logging
from aiogram import Router, types, F, Bot
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from supabase import create_client
from config import SUPABASE_URL, SUPABASE_API_KEY
from utils import build_leaderboard_message  # используем готовую функцию

# Подключаем Supabase
supabase = create_client(SUPABASE_URL, SUPABASE_API_KEY)

# Создаём НОВЫЙ роутер, не смешивая его с другими
competition_router = Router()

@competition_router.message(Command("send_post"))
async def send_competition_post(message: types.Message, bot: Bot):
    """
    Отправляет в канал пост с кнопками (остаётся навсегда).
    Только для админов (ADMIN_ID).
    """
    ADMIN_ID = 732402669
    CHANNEL_ID = -1002487599337  # Ваш канал

    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет прав для этой команды.")
        return

    text = (
        "🔥 <b>Почувствуй дух соревнования, испытай себя!</b>\n"
        "📊 <b>В нашей викторине есть ДВА рейтинга:</b>\n\n"
        "🥇 📋 <b>Турнирная таблица</b> – ТОП-10 лучших результатов в каждой викторине.\n"
        "🏆 🌟 <b>Общий рейтинг</b> – ТОП-10 лучших результатов по всем викторинам суммарно.\n\n"
        "⚡ <b>Отвечай быстро и точно!</b> Если два участника набрали одинаковое количество очков, "
        "выше окажется тот, кто прошёл викторину быстрее.\n\n"
        "🚀 <b>Проверь и обнови свои знания!</b>\n"
    )

    # Кнопки: перейти к боту, открыть турнирную таблицу, показать общий рейтинг
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀Перейти к боту 💪", url="https://t.me/islamikum_bot")],
            [InlineKeyboardButton(text="📋 Турнирная таблица", callback_data="open_leaderboard")],
            [InlineKeyboardButton(text="🌟 Общий рейтинг", callback_data="show_general_leaderboard")]
        ]
    )

    # Отправляем в канал пост
    await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard
    )

    # Сообщаем админу
    await message.answer("✅ Пост отправлен в канал (остаётся навсегда).")


@competition_router.callback_query(F.data == "open_leaderboard")
async def open_leaderboard_callback(callback_query: types.CallbackQuery, bot: Bot):
    """
    При нажатии "📋 Турнирная таблица" в канале:
      1) Отправляем список викторин
      2) Сообщение удалится через 30 секунд
    """
    await callback_query.answer()  # убираем "часики"

    try:
        response = await asyncio.to_thread(
            supabase.table("quizzes").select("id, title").execute
        )
        quizzes = response.data
    except Exception as e:
        logging.error(f"Ошибка получения викторин: {e}")
        msg = await callback_query.message.answer(
            "⚠️ Ошибка загрузки викторин.\n\n"
            "Чтоб не засорять чат, это сообщение автоматически удалится через 30 секунд."
        )
        asyncio.create_task(delete_message_after_delay(bot, msg.chat.id, msg.message_id, 30))
        return

    if not quizzes:
        msg = await callback_query.message.answer(
            "Нет доступных викторин.\n\n"
            "Чтоб не засорять чат, это сообщение автоматически удалится через 30 секунд."
        )
        asyncio.create_task(delete_message_after_delay(bot, msg.chat.id, msg.message_id, 30))
        return

    # Формируем кнопки с единообразным callback_data "leaderboard_<quiz_id>"
    inline_keyboard = []
    for quiz in quizzes:
        quiz_id = quiz["id"]
        quiz_title = quiz["title"]
        inline_keyboard.append([
            InlineKeyboardButton(text=quiz_title, callback_data=f"leaderboard_{quiz_id}")
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
    msg = await callback_query.message.answer(
        "Выберите викторину для отображения турнирной таблицы:\n\n"
        "Чтоб не засорять чат, это сообщение автоматически удалится через 30 секунд.",
        reply_markup=keyboard
    )
    asyncio.create_task(delete_message_after_delay(bot, msg.chat.id, msg.message_id, 30))


@competition_router.callback_query(F.data.startswith("leaderboard_"))
async def show_leaderboard_for_quiz(callback_query: types.CallbackQuery, bot: Bot):
    """
    При выборе конкретной викторины:
      - Загружаем данные викторины и результаты
      - Формируем сообщение таблицы через build_leaderboard_message
      - Сообщение удаляется через 30 секунд
    """
    await callback_query.answer()

    parts = callback_query.data.split("_")
    if len(parts) != 2:
        return  # некорректный формат callback_data

    try:
        quiz_id = int(parts[1])
    except ValueError:
        return

    # Получаем информацию о викторине
    try:
        quiz_resp = await asyncio.to_thread(
            supabase.table("quizzes").select("title").eq("id", quiz_id).single().execute
        )
        quiz_data = quiz_resp.data
    except Exception as e:
        logging.error(f"Ошибка получения викторины {quiz_id}: {e}")
        msg = await callback_query.message.answer(
            "⚠️ Ошибка при запросе викторины.\n\n"
            "Чтоб не засорять чат, это сообщение автоматически удалится через 30 секунд."
        )
        asyncio.create_task(delete_message_after_delay(bot, msg.chat.id, msg.message_id, 30))
        return

    if not quiz_data:
        msg = await callback_query.message.answer(
            "Викторина не найдена.\n\n"
            "Чтоб не засорять чат, это сообщение автоматически удалится через 30 секунд."
        )
        asyncio.create_task(delete_message_after_delay(bot, msg.chat.id, msg.message_id, 30))
        return

    quiz_title = quiz_data["title"]

    # Загружаем результаты для выбранной викторины
    try:
        result_resp = await asyncio.to_thread(
            supabase.table("results")
            .select("user_id, score, time_taken")
            .eq("quiz_id", quiz_id)
            .order("score", desc=True)    # очки по убыванию
            .order("time_taken", desc=False)  # время по возрастанию
            .limit(10)
            .execute
        )
        results = result_resp.data
    except Exception as e:
        logging.error(f"Ошибка загрузки результатов викторины {quiz_id}: {e}")
        msg = await callback_query.message.answer(
            "⚠️ Ошибка загрузки результатов.\n\n"
            "Чтоб не засорять чат, это сообщение автоматически удалится через 30 секунд."
        )
        asyncio.create_task(delete_message_after_delay(bot, msg.chat.id, msg.message_id, 30))
        return

    if not results:
        msg = await callback_query.message.answer(
            f"Пока нет результатов для «{quiz_title}».\n\n"
            "Чтоб не засорять чат, это сообщение автоматически удалится через 30 секунд."
        )
        asyncio.create_task(delete_message_after_delay(bot, msg.chat.id, msg.message_id, 30))
        return

    # Формируем текст таблицы через готовую функцию
    leaderboard_text = await build_leaderboard_message(results, supabase)

    msg = await callback_query.message.answer(
        f"🏆 Турнирная таблица для «{quiz_title}»:\n{leaderboard_text}\n\n"
        "Чтоб не засорять чат, это сообщение автоматически удалится через 30 секунд."
    )
    asyncio.create_task(delete_message_after_delay(bot, msg.chat.id, msg.message_id, 30))


@competition_router.callback_query(F.data == "show_general_leaderboard")
async def show_general_leaderboard_callback(callback_query: types.CallbackQuery, bot: Bot):
    """
    При нажатии "🌟 Общий рейтинг" загружаем общий рейтинг,
    форматируем через build_leaderboard_message и удаляем сообщение через 30 секунд.
    """
    await callback_query.answer()

    try:
        total_resp = await asyncio.to_thread(
            supabase.rpc("get_total_scores").execute
        )
        results = total_resp.data
    except Exception as e:
        logging.error(f"Ошибка загрузки общего рейтинга: {e}")
        msg = await callback_query.message.answer(
            "⚠️ Ошибка загрузки общего рейтинга.\n\n"
            "Чтоб не засорять чат, это сообщение автоматически удалится через 30 секунд."
        )
        asyncio.create_task(delete_message_after_delay(bot, msg.chat.id, msg.message_id, 30))
        return

    if not results:
        msg = await callback_query.message.answer(
            "Пока нет результатов.\n\n"
            "Чтоб не засорять чат, это сообщение автоматически удалится через 30 секунд."
        )
        asyncio.create_task(delete_message_after_delay(bot, msg.chat.id, msg.message_id, 30))
        return

    # Приводим данные к формату для build_leaderboard_message
    top_10 = []
    for row in results[:10]:
        top_10.append({
            "user_id": row["user_id"],
            "score": int(row["total_score"]),
            "time_taken": int(row["total_time"]) if row["total_time"] is not None else 0
        })

    leaderboard_text = await build_leaderboard_message(top_10, supabase)

    msg = await callback_query.message.answer(
        f"🌟 Общий рейтинг:\n{leaderboard_text}\n\n"
        "Чтоб не засорять чат, это сообщение автоматически удалится через 30 секунд."
    )
    asyncio.create_task(delete_message_after_delay(bot, msg.chat.id, msg.message_id, 30))


async def delete_message_after_delay(bot: Bot, chat_id: int, message_id: int, delay: int):
    """Удаляет сообщение из чата через delay секунд."""
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as e:
        logging.warning(f"Не удалось удалить сообщение {message_id} из чата {chat_id}: {e}")
