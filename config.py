import os
from dotenv import load_dotenv

# Загружаем переменные окружения из файла .env
load_dotenv()

# Токен бота
BOT_TOKEN = os.getenv("BOT_TOKEN")


# Параметры для подключения к базе данных
USER = os.getenv("user")
PASSWORD = os.getenv("password")
HOST = os.getenv("host")
PORT = os.getenv("port")
DBNAME = os.getenv("dbname")

# Строка подключения для SQLAlchemy
DB_URI = "postgresql+asyncpg://postgres.cluwvupuextcpxcoouba:klopikijuy00@aws-0-eu-north-1.pooler.supabase.com:5432/postgres"


# Данные для подключения к Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")



