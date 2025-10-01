# create_tables.py
import asyncio
from database import create_tables  # Импорт функции для создания таблиц

# Функция для запуска создания таблиц
async def main():
    await create_tables()  # Вызовем функцию для создания таблиц

# Запуск создания таблиц
if __name__ == "__main__":
    asyncio.run(main())  # Запускаем основной процесс для создания таблиц
