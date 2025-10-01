import os
import logging
from logging.config import fileConfig
from sqlalchemy import create_engine, pool
from sqlalchemy.ext.declarative import declarative_base
from alembic import context
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Читаем переменные для базы данных
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
DB_HOST = os.getenv("DB_HOST", "localhost")  # В Docker имя контейнера
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME", "aiogram_project")

# SQLAlchemy URL для подключения
SQLALCHEMY_URL = f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Настройки Alembic
config = context.config
config.set_main_option("sqlalchemy.url", SQLALCHEMY_URL)

# Логирование
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Импортируем метаданные моделей
from models import Base  # Убедись, что у тебя есть models.py
target_metadata = Base.metadata

def run_migrations_offline():
    """
    Запуск миграций в офлайн-режиме.
    """
    context.configure(
        url=SQLALCHEMY_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    """
    Запуск миграций в онлайн-режиме (с подключением к базе).
    """
    connectable = create_engine(SQLALCHEMY_URL, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata
        )
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    logging.info("Запуск миграций в офлайн-режиме...")
    run_migrations_offline()
else:
    logging.info("Запуск миграций в онлайн-режиме...")
    run_migrations_online()
