import os
import logging
import asyncio
import gc
import psutil
import sentry_sdk
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN

# 🔹 Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)

logger = logging.getLogger(__name__)

SENTRY_DSN = os.getenv("SENTRY_DSN")

if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        send_default_pii=True,
        traces_sample_rate=1.0,
        ignore_errors=[KeyboardInterrupt]
    )
    logging.info("✅ Sentry подключён!")
else:
    logging.warning("⚠️ SENTRY_DSN не найден! Логирование в Sentry отключено.")

# ✅ Создаём `bot` и `dp` ГЛОБАЛЬНО
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# Импортируем маршрутизаторы

from handlers.start_handler import start_router
from handlers.prophets_quiz import prophets_quiz_router
from handlers.quiz_handler import quiz_router
from handlers.leaderboard_handler import leaderboard_router
from handlers.admin import admin_router
from deepseek_handler import deepseek_router
from handlers.competition_router import competition_router
from handlers.pair_matching_game import matching_quiz_router
from handlers.poll_quiz import poll_quiz_router
from handlers.survival import survival_router

# Подключаем маршрутизаторы

dp.include_router(start_router)
dp.include_router(matching_quiz_router)
dp.include_router(prophets_quiz_router)
dp.include_router(quiz_router)
dp.include_router(leaderboard_router)
dp.include_router(admin_router)
dp.include_router(deepseek_router)
dp.include_router(competition_router)
dp.include_router(poll_quiz_router)
dp.include_router(survival_router)

# 🔹 Очистка памяти
async def memory_cleanup():
    while True:
        process = psutil.Process()
        mem_usage = process.memory_info().rss / (1024 * 1024)

        if mem_usage > 150:
            gc.collect()
            logging.info(f"🧹 Очистка памяти! Использование RAM: {mem_usage:.2f} MB")

        await asyncio.sleep(600)

async def on_startup():
    logging.info("✅ Бот запущен")
    sentry_sdk.capture_message("🚀 Бот успешно запущен!")

async def on_shutdown():
    global bot  # Указываем, что `bot` глобальный
    logging.info("🛑 Остановка бота...")
    await bot.session.close()  # Закрываем соединение
    await asyncio.sleep(1)
    del bot  # Удаляем `bot` из памяти
    gc.collect()

async def main():
    await on_startup()
    asyncio.create_task(memory_cleanup())

    # Удаляем Webhook, если был установлен ранее
    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"❌ Ошибка: {e}")
        sentry_sdk.capture_exception(e)
    finally:
        await on_shutdown()


if __name__ == "__main__":
    asyncio.run(main())




