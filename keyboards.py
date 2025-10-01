from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncio
import os
from supabase import create_client

# Инициализируем клиента Supabase (убедитесь, что переменные окружения настроены)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_API_KEY)


from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

def start_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Список викторин"), KeyboardButton(text="🏆 Турнирная таблица")],
            [KeyboardButton(text="🌟 Общий рейтинг"), KeyboardButton(text="⏳ Выживание")],
            [KeyboardButton(text="🌙 Рамадан-Квест"), ]
        ],
        resize_keyboard=True
    )
    return keyboard



async def quiz_list_keyboard():
    response = await asyncio.to_thread(
        supabase.table("quizzes").select("id, title, is_active").order("id").execute
    )

    quizzes = response.data or []
    keyboard_builder = InlineKeyboardBuilder()

    for quiz in quizzes:
        title = quiz["title"]
        if not quiz["is_active"] and not title.startswith("🔒"):
            title = f"🔒 {title}"  # Добавляем замок, если его ещё нет

        keyboard_builder.button(
            text=title,
            callback_data=f"quiz_{quiz['id']}"
        )

    keyboard_builder.adjust(1)  # Одна кнопка в ряду
    return keyboard_builder.as_markup()


