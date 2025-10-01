import logging
import asyncio
import os
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_API_KEY)

admin_router = Router()
ADMIN_IDS = ['732402669', '7919126514']  # Список администраторов

def is_admin(user_id):
    return str(user_id) in ADMIN_IDS




# Команда /admin для открытия админ-панели
@admin_router.message(Command("admin"))
async def admin_panel(message: types.Message):
    if is_admin(message.from_user.id):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='➕ Добавить викторину', callback_data='add_quiz')],
            [InlineKeyboardButton(text='🗑 Удалить викторину', callback_data='delete_quiz')],
            [InlineKeyboardButton(text='🔄 Сбросить турнирную таблицу и общий рейтинг', callback_data='reset_tournament')]
        ])
        await message.answer("Добро пожаловать в админ-панель. Выберите действие:", reply_markup=keyboard)
    else:
        await message.answer("У вас нет прав для доступа к этой команде.")

# Обработка сброса турнирной таблицы (и общего рейтинга)
@admin_router.callback_query(F.data == 'reset_tournament')
async def reset_tournament_table(callback_query: types.CallbackQuery):
    if is_admin(callback_query.from_user.id):
        confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='✅ Подтвердить сброс', callback_data='confirm_reset_tournament')],
            [InlineKeyboardButton(text='❌ Отменить', callback_data='cancel')]
        ])
        await callback_query.message.answer("Вы уверены, что хотите сбросить турнирную таблицу и общий рейтинг?",
                                             reply_markup=confirm_keyboard)
    else:
        await callback_query.message.answer("У вас нет прав для этого действия.")

# Подтверждение сброса турнирной таблицы
@admin_router.callback_query(F.data == 'confirm_reset_tournament')
async def confirm_reset_tournament(callback_query: types.CallbackQuery):
    if is_admin(callback_query.from_user.id):
        await asyncio.to_thread(supabase.table("results").update({"score": 0}).neq("id", None).execute)
        await callback_query.message.answer("Все баллы обнулены. Участники остались в турнире.")
    else:
        await callback_query.message.answer("У вас нет прав для этого действия.")

# Обработка отмены действия
@admin_router.callback_query(F.data == 'cancel')
async def cancel_action(callback_query: types.CallbackQuery):
    await callback_query.message.answer("Действие отменено.")

# Переключение состояния таймера


# Обработка команды "Добавить викторину" – запрос текстового формата викторины
@admin_router.callback_query(F.data == 'add_quiz')
async def request_quiz_text(callback_query: types.CallbackQuery):
    if is_admin(callback_query.from_user.id):
        await callback_query.message.answer(
            "Отправьте викторину в текстовом формате. Важно соблюдать формат:\n"
            "1. Пустая строка после темы и между вопросами.\n"
            "2. Варианты ответов начинаются с '-'.\n"
            "3. Указывайте номер правильного ответа и пояснение (необязательно)."
        )
        await callback_query.message.answer(
            "Пример формата:\n"
            "Тема: Законы Вселенной\n\n"
            "1. Какой закон утверждает сохранение энергии?\n"
            "- Закон сохранения энергии\n"
            "- Закон притяжения\n"
            "- Закон кармы\n"
            "- Закон равновесия\n"
            "Ответ: 1\n"
            "Пояснение: Энергия может только переходить из одной формы в другую.\n"
        )
    else:
        await callback_query.message.answer("У вас нет прав для выполнения этой команды.")

# Обработка текстового формата викторины (начинается с "Тема:" или "TEMA:")
@admin_router.message(lambda message: message.text.startswith(("Тема:", "TEMA:", "Категория:")))
async def handle_text_quiz(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("У вас нет прав для выполнения этой команды.")
        return
    try:
        lines = message.text.splitlines()

        # 1. Проверяем наличие категории
        category_name = None
        if lines[0].startswith("Категория:"):
            category_name = lines[0].replace("Категория:", "").strip()
            lines = lines[1:]  # Удаляем первую строку, теперь первая — это "Тема:..."

        # 2. Определяем язык
        if lines[0].startswith("TEMA:"):
            language_keys = {"topic": "TEMA:", "answer": "Svar:", "explanation": "Förklaring:"}
        elif lines[0].startswith("Тема:"):
            language_keys = {"topic": "Тема:", "answer": "Ответ:", "explanation": "Пояснение:"}
        else:
            raise ValueError("Неизвестный язык или неверный формат викторины.")

        title = lines[0].replace(language_keys["topic"], "").strip()
        questions = []
        current_question = None

        for line in lines[1:]:
            if line.strip() == "":
                continue
            if line[0].isdigit():
                if current_question:
                    if not current_question["options"]:
                        raise ValueError(f"У вопроса '{current_question['question']}' нет вариантов ответа.")
                    if current_question["correct"] is None:
                        raise ValueError(f"У вопроса '{current_question['question']}' не указан правильный ответ.")
                    questions.append(current_question)
                parts = line.split(". ", 1)
                if len(parts) < 2:
                    raise ValueError("Неверный формат вопроса.")
                current_question = {
                    "question": parts[1].strip(),
                    "options": [],
                    "correct": None,
                    "explanation": None,
                }
            elif line.startswith(language_keys["answer"]):
                correct_option_id = int(line.replace(language_keys["answer"], "").strip()) - 1
                current_question["correct"] = correct_option_id
            elif line.startswith(language_keys["explanation"]):
                current_question["explanation"] = line.replace(language_keys["explanation"], "").strip()
            elif line.strip().startswith("-"):
                current_question["options"].append(line.strip().replace("-", "").strip())
            else:
                raise ValueError(f"Неверный формат строки: '{line}'")

        if current_question:
            questions.append(current_question)

        # 3. Обработка категории: создаём или находим
        category_id = None
        if category_name:
            category_resp = await asyncio.to_thread(
                supabase.table("categories")
                .select("id")
                .eq("name", category_name)
                .limit(1)
                .execute
            )
            if category_resp.data:
                category_id = category_resp.data[0]["id"]
            else:
                new_cat = await asyncio.to_thread(
                    supabase.table("categories")
                    .insert({"name": category_name, "is_active": True})
                    .execute
                )
                category_id = new_cat.data[0]["id"]

        # 4. Создаём викторину
        quiz_data = {"title": title, "is_active": True}
        if category_id:
            quiz_data["category_id"] = category_id

        quiz_response = await asyncio.to_thread(
            supabase.table("quizzes").insert(quiz_data).execute
        )
        quiz_id = quiz_response.data[0]["id"]

        # 5. Добавляем вопросы и варианты
        for q in questions:
            question_response = await asyncio.to_thread(
                supabase.table("questions").insert({
                    "text": q["question"],
                    "quiz_id": quiz_id,
                    "explanation": q.get("explanation")
                }).execute
            )
            question_id = question_response.data[0]["id"]
            options_data = [
                {
                    "text": opt,
                    "is_correct": (idx == q["correct"]),
                    "question_id": question_id
                }
                for idx, opt in enumerate(q["options"])
            ]
            await asyncio.to_thread(supabase.table("options").insert(options_data).execute)

        await message.answer(f"✅ Викторина «{title}» успешно добавлена!")

    except ValueError as e:
        await message.answer(f"❌ Ошибка в данных: {str(e)}")
    except Exception as e:
        await message.answer(f"❌ Неизвестная ошибка: {str(e)}")


# 👇 Начало логики удаления викторин с выбором категории

# 1. Показываем список категорий
@admin_router.callback_query(F.data == 'delete_quiz')
async def choose_category_to_delete_quiz(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.message.answer("У вас нет прав для этого действия.")
        return

    categories_resp = await asyncio.to_thread(
        supabase.table("categories").select("id, name").eq("is_active", True).execute
    )
    categories = categories_resp.data or []

    if not categories:
        await callback_query.message.answer("Нет доступных категорий.")
        return

    buttons = [
        [InlineKeyboardButton(text=f"📂 {cat['name']}", callback_data=f"admin_delete_category_{cat['id']}")]
        for cat in categories
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback_query.message.answer("Выберите категорию:", reply_markup=kb)


# 2. Показываем викторины внутри категории
@admin_router.callback_query(F.data.startswith("admin_delete_category_"))
async def choose_quiz_in_category_to_delete(callback_query: types.CallbackQuery):
    await callback_query.message.edit_reply_markup(reply_markup=None)

    category_id = int(callback_query.data.split("_")[-1])
    quizzes_resp = await asyncio.to_thread(
        supabase.table("quizzes").select("id, title").eq("category_id", category_id).execute
    )
    quizzes = quizzes_resp.data or []

    if not quizzes:
        await callback_query.message.answer("В этой категории нет викторин.")
        return

    buttons = [
        [InlineKeyboardButton(text=f"❌ {q['title']}", callback_data=f"admin_delete_quiz_{q['id']}")]
        for q in quizzes
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback_query.message.answer("Выберите викторину для удаления:", reply_markup=kb)


# 3. Подтверждение удаления викторины
@admin_router.callback_query(F.data.startswith("admin_delete_quiz_"))
async def confirm_deletion_quiz(callback_query: types.CallbackQuery):
    await callback_query.message.edit_reply_markup(reply_markup=None)

    quiz_id = int(callback_query.data.split("_")[-1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin_confirm_delete_{quiz_id}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")]
    ])
    await callback_query.message.answer(
        f"Удалить викторину с ID {quiz_id}? Это действие необратимо.",
        reply_markup=kb
    )


# 4. Удаляем викторину и, если нужно, категорию
@admin_router.callback_query(F.data.startswith("admin_confirm_delete_"))
async def admin_final_delete(callback_query: types.CallbackQuery):
    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
        quiz_id = int(callback_query.data.split("_")[-1])

        quiz_resp = await asyncio.to_thread(
            supabase.table("quizzes").select("category_id").eq("id", quiz_id).single().execute
        )
        if not quiz_resp.data:
            await callback_query.message.answer("❌ Викторина не найдена.")
            return

        category_id = quiz_resp.data["category_id"]

        await asyncio.to_thread(
            supabase.table("results").delete().eq("quiz_id", quiz_id).execute
        )
        await asyncio.to_thread(
            supabase.table("quizzes").delete().eq("id", quiz_id).execute
        )

        remaining_resp = await asyncio.to_thread(
            supabase.table("quizzes").select("id").eq("category_id", category_id).execute
        )
        remaining = remaining_resp.data or []

        if not remaining:
            await asyncio.to_thread(
                supabase.table("categories").delete().eq("id", category_id).execute
            )
            await callback_query.message.answer("✅ Викторина и её категория удалены.")
        else:
            await callback_query.message.answer("✅ Викторина успешно удалена.")

    except Exception as e:
        logging.exception(f"❌ Ошибка при удалении викторины: {e}")
        await callback_query.message.answer("⚠️ Произошла ошибка при удалении. Попробуйте позже.")


# 5. Отмена
@admin_router.callback_query(F.data == "cancel")
async def cancel_action(callback_query: types.CallbackQuery):
    await callback_query.message.edit_text("❌ Отменено.", reply_markup=None)



