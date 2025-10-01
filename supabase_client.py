from supabase import create_client, Client
import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env
load_dotenv()

# Получаем URL и ключ из переменных окружения
supabase_url = os.getenv('SUPABASE_URL')
supabase_key = os.getenv('SUPABASE_API_KEY')

# Создаем клиент для подключения к Supabase
supabase: Client = create_client(supabase_url, supabase_key)

