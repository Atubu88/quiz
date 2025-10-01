import logging
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

class ErrorHandlerMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data):
        try:
            return await handler(event, data)
        except Exception as e:
            logging.exception("Необработанная ошибка в хендлере:")
            # Здесь можно уведомлять администратора или выполнять другие действия.
            raise e  # Или вернуть дефолтный ответ, если не хотите прерывать выполнение
