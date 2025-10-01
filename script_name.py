import psycopg2
from dotenv import load_dotenv
import os

# Загрузка переменных окружения из файла .env
load_dotenv()

# Получаем данные из переменных окружения
user = os.getenv("user")
password = os.getenv("password")
host = os.getenv("host")
port = os.getenv("port")
dbname = os.getenv("dbname")

# Подключаемся к базе данных
try:
    conn = psycopg2.connect(
        user=user,
        password=password,
        host=host,
        port=port,
        dbname=dbname
    )
    print("Соединение с базой данных установлено успешно!")

    # Создание курсора для выполнения SQL-запросов
    cursor = conn.cursor()

    # Пример выполнения запроса (например, получение списка таблиц)
    cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';")
    tables = cursor.fetchall()
    print("Список таблиц в базе данных:")
    for table in tables:
        print(table)

    # Закрытие курсора и соединения
    cursor.close()
    conn.close()

except Exception as e:
    print(f"Ошибка при подключении к базе данных: {e}")
