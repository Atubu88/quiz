# populate_self_report_tests.py
import json
from models import Base, SelfReportTest  # Импортируем модели и базовый класс
from database import session, engine  # Импортируем сессию и движок из database.py

# Создаем таблицы в базе данных (если их ещё нет)
Base.metadata.create_all(engine)

# Функция для загрузки данных из JSON файла и добавления в базу данных
def load_self_report_test(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        data = json.load(file)

        for test_name, test_content in data.items():
            # Проверяем, существует ли тест уже в базе данных, чтобы не дублировать
            existing_test = session.query(SelfReportTest).filter_by(title=test_name).first()
            if existing_test:
                print(f"Тест '{test_name}' уже существует, пропускаем.")
                continue

            # Создаем новый объект SelfReportTest
            new_test = SelfReportTest(
                title=test_name,
                description=test_content.get("description"),
                questions=test_content.get("questions"),
                results=test_content.get("results")
            )
            session.add(new_test)
            print(f"Тест '{test_name}' добавлен.")

        # Сохраняем изменения в базе данных
        session.commit()
        print("Данные успешно добавлены в базу данных.")

# Запуск функции загрузки с указанием пути к JSON файлу
# Запуск функции загрузки с указанием пути к JSON файлу
load_self_report_test('test_data.json')


