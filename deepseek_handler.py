# deepseek_handler.py

import os
import httpx
from aiogram import Router, types
from aiogram.filters import Command
from dotenv import load_dotenv
import logging
import asyncio

# Загружаем переменные окружения из файла .env
load_dotenv()

# Настройка логирования
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,  # Уровень логирования DEBUG для подробного вывода
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Создаем роутер для DeepSeek
deepseek_router = Router(name="deepseek_router")

# Словарь для хранения истории переписки (контекста) для каждого пользователя
user_context = {}

# Конфигурация API
API_KEY = "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"
MODEL = "deepseek/deepseek-r1:free"
SITE_URL = os.getenv("SITE_URL")      # Опционально: URL вашего сайта
SITE_NAME = os.getenv("SITE_NAME")    # Опционально: Название вашего сайта

# Проверяем наличие необходимых переменных окружения
if not API_KEY:
    logger.error("Не установлен OPENROUTER_API_KEY в переменных окружения.")
    raise ValueError("Не установлен OPENROUTER_API_KEY в переменных окружения.")

# Функция для отправки запроса к API OpenRouter.ai с учетом контекста
async def fetch_completion_with_context(conversation: list):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    # Добавляем опциональные заголовки, если они предоставлены
    if SITE_URL:
        headers["HTTP-Referer"] = SITE_URL
    if SITE_NAME:
        headers["X-Title"] = SITE_NAME

    data = {
        "model": MODEL,
        "messages": conversation,
        "temperature": 0.7,
        "max_tokens": 1000,
        "top_p": 0.9,
        "frequency_penalty": 0.5,
        "presence_penalty": 0.5
    }

    # Добавляем таймаут для избежания долгого ожидания ответа
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        try:
            response = await client.post(url, headers=headers, json=data)
            logger.debug(f"HTTP Response Status: {response.status_code}")
            logger.debug(f"HTTP Response Body: {response.text}")
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Ошибка API: {response.status_code} - {response.text}")
                return None
        except httpx.RequestError as e:
            logger.error(f"HTTP запрос завершился ошибкой: {e}")
            return None

@deepseek_router.message(Command("deepseek"))
async def activate_deepseek_mode(message: types.Message):
    """
    Активирует режим DeepSeek и инициализирует историю переписки для пользователя.
    """
    user_id = message.from_user.id
    # Инициализируем историю переписки. Можно добавить системное сообщение для настройки поведения модели.
    user_context[user_id] = [
        {
            "role": "system",
            "content": "Ты бот DeepSeek. Пожалуйста, помогай пользователям, отвечая на их вопросы."
        }
    ]
    await message.answer("🔮 Режим DeepSeek активирован. Задавайте вопросы!")
from aiogram import F
@deepseek_router.message(F.text.exists() & ~F.text.startswith("/"))
async def handle_deepseek_question(message: types.Message):
    # Дополнительная защита: если вдруг текст начинается с "/" — выходим.
    if message.text.startswith("/"):
        return

    user_id = message.from_user.id
    # Если пользователь не активировал режим DeepSeek, игнорируем сообщение
    if user_id not in user_context:
        return

    try:
        user_message = message.text.strip()
        logger.info(f"Запрос от пользователя {user_id}: {user_message}")

        # Получаем историю переписки и добавляем новое сообщение пользователя
        conversation = user_context.get(user_id, [])
        conversation.append({"role": "user", "content": user_message})

        # Повторяем запрос, если ответ пустой
        max_retries = 3
        for attempt in range(max_retries):
            response = await fetch_completion_with_context(conversation)
            logger.debug(f"Попытка {attempt + 1}: Ответ от API: {response}")

            if response and "choices" in response and len(response["choices"]) > 0:
                content = response["choices"][0].get("message", {}).get("content", "").strip()
                if content:
                    # Добавляем ответ модели в историю переписки
                    conversation.append({"role": "assistant", "content": content})
                    user_context[user_id] = conversation  # Обновляем историю
                    await message.answer(content)
                    return  # Успешный ответ, выходим из цикла

            logger.warning(f"Пустой ответ от модели. Попытка {attempt + 1} из {max_retries}")

        # Если все попытки неудачны
        await message.answer("🤖 Не удалось получить ответ. Попробуйте переформулировать вопрос.")

    except Exception as e:
        logger.error(f"Ошибка при обработке запроса: {str(e)}")
        await message.answer("🚫 Произошла ошибка. Попробуйте позже.")

