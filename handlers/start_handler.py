import logging
import os
import asyncio
from aiogram import Router, types, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, FSInputFile, ReplyKeyboardMarkup, KeyboardButton
import asyncpg  # Если нужна обработка специфических ошибок PostgreSQL
from supabase import create_client
from keyboards import start_keyboard
from handlers.pair_matching_game import start_matching_quiz
from handlers.prophets_quiz import start_quiz

# Создаём роутер
start_router = Router()

# Путь до приветственной картинки (если есть)
MEDIA_PATH = os.path.join(os.getcwd(), "media", "welcome1.png")

# Подключаемся к Supabase через переменные окружения
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_API_KEY)


async def upsert_user_supabase(user_data: dict):
    """
    Пишем (upsert) пользователя в таблицу "users" Supabase
    по полю "telegram_id". Предполагаем, что telegram_id UNIQUE.
    """
    try:
        # Выполняем upsert, указывая on_conflict="telegram_id"
        response = await asyncio.to_thread(
            supabase.table("users")
            .upsert(user_data, on_conflict="telegram_id")
            .execute
        )
        # Проверяем, нет ли ошибки
        if response.data is None:
            # Если data=None, значит что-то пошло не так
            logging.error(
                f"Ошибка upsert_user_supabase: status_code={response.status_code}, "
                f"error_message={response.error_message}"
            )
        else:
            logging.info(
                f"✅ Пользователь {user_data['telegram_id']} ({user_data['username']}) "
                "успешно upsert в Supabase."
            )
    except Exception as e:
        logging.error(f"⚠️ Ошибка в upsert_user_supabase: {e}")
        # Здесь можно добавить retry‑логику, если необходимо


@start_router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    """
    Хендлер на /start. Если /start содержит параметр quiz_..., запускаем викторину.
    Если нет - показываем 'приветственное' меню.
    """
    user = message.from_user
    args = message.text.split()
    logging.info(f"🔹 /start от {user.id}, args={args}")
    logging.info(f"🔹 Аргументы команды: {args}")

    # Проверяем, есть ли deep link, например /start quiz_5
    if len(args) > 1:
        if args[1].startswith("quiz_"):
            quiz_id_str = args[1].replace("quiz_", "")
            if quiz_id_str.isdigit():
                quiz_id = int(quiz_id_str)
                logging.info(f"Deep link на викторину quiz_{quiz_id}")
                await start_quiz(message.chat.id, user.id, quiz_id, bot)
                return
            else:
                await message.answer("⛔ Неверный формат quiz_ID!")
                return
        elif args[1].startswith("matching_quiz_"):
            quiz_id_str = args[1].replace("matching_quiz_", "")
            if quiz_id_str.isdigit():
                quiz_id = int(quiz_id_str)
                logging.info(f"Deep link на викторину matching_quiz_{quiz_id}")
                await start_matching_quiz(message.chat.id, user.id, quiz_id, bot)
                return
            else:
                await message.answer("⛔ Неверный формат quiz_ID!")
                return


        else:
            await message.answer("⛔ Неизвестный параметр для /start.")
            return

    # ----- Если сюда дошли, значит аргументов нет -> обычный /start -----
    loading_msg = await message.answer("⏳ Загружаем данные...")

    # Готовим данные для Supabase
    user_data = {
        "telegram_id": user.id,
        "username": user.username or "",
        "first_name": user.first_name or "",
        "last_name": user.last_name or ""
    }
    # Запускаем upsert асинхронно
    asyncio.create_task(upsert_user_supabase(user_data))

    # Удаляем сообщение "Загрузка..."
    await loading_msg.delete()



    # Пробуем отправить фото (если есть), иначе – просто текст
    if os.path.exists(MEDIA_PATH):
        try:
            photo_file = FSInputFile(MEDIA_PATH)
            await message.answer_photo(
                photo=photo_file,
                caption="Добро пожаловать! 🎉\nВыбери викторину и начинай играть! 🎮",
                reply_markup=start_keyboard()  # вызываем функцию для получения разметки
            )
        except Exception as e:
            logging.warning(f"⚠️ Ошибка при отправке фото: {e}")
            await message.answer(
                "Добро пожаловать! Выбери викторину из меню 🎮",
                reply_markup=start_keyboard()  # вызываем функцию для получения разметки
            )
    else:
        await message.answer(
            "Добро пожаловать! Выбери викторину из меню 🎮",
            reply_markup=start_keyboard()  # вызываем функцию для получения разметки
        )


@start_router.message(Command("reset"))
async def cmd_reset(message: types.Message, state: FSMContext):
    """
    Сброс состояния FSM командой /reset.
    """
    await state.clear()
    await message.answer("✅ Состояние бота сброшено. Попробуйте снова /start")
