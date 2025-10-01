from aiogram import types, Router, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import json

# Загружаем тестовые данные
with open("test_data.json", encoding="utf-8") as f:
    test_data = json.load(f)

test_router = Router()

# Глобальная переменная для хранения данных пользователя и идентификаторов тестов
user_data = {}
test_ids = {str(i): test_name for i, test_name in enumerate(test_data.keys())}  # Короткие идентификаторы


# Класс для управления тестом
class SelfReportTestHandler:
    def __init__(self, test_name):
        self.test = test_data.get(test_name)
        self.current_question = 0
        self.total_score = 0
        self.num_questions = len(self.test['questions']) if self.test else 0

    def get_question(self):
        if self.current_question < self.num_questions:
            question_data = self.test['questions'][self.current_question]
            return question_data['question'], question_data['options']
        return None, None

    def submit_answer(self, selected_option):
        question_data = self.test['questions'][self.current_question]
        score = question_data['options'][selected_option].get('score', 0)
        self.total_score += score
        self.current_question += 1
        return score

    def is_finished(self):
        return self.current_question >= self.num_questions

    def get_result(self):
        max_score = sum(max(option['score'] for option in q['options']) for q in self.test['questions'])
        for result in self.test['results']:
            range_min, range_max = map(int, result['score_range'].split('–'))
            if range_min <= self.total_score <= range_max:
                return result['level'], result['description'], self.total_score, max_score
        return None, None, self.total_score, max_score


# Показ списка доступных тестов с короткими `callback_data`
@test_router.message(F.text == "📝 Тест Самооценки")
async def show_tests_list(message: types.Message):
    # Создаем клавиатуру со списком тестов с использованием коротких идентификаторов
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=test_name, callback_data=f"start_test:{test_id}")]
                         for test_id, test_name in test_ids.items()]
    )
    await message.answer("Выберите тест для прохождения:", reply_markup=keyboard)


# Начало теста после выбора
@test_router.callback_query(F.data.startswith("start_test:"))
async def start_test(callback_query: types.CallbackQuery):
    test_id = callback_query.data.split(":")[1]  # Извлечение идентификатора теста
    test_name = test_ids.get(test_id)  # Получаем полное название теста по идентификатору

    handler = SelfReportTestHandler(test_name)

    # Если у теста есть описание, показываем его перед началом теста
    description = handler.test.get("description", "")
    intro_text = f"{test_name}\n\n{description}\n\n"

    question, options = handler.get_question()
    if question:
        # Добавляем текст с первым вопросом и ответами после описания
        answer_text = f"{intro_text}{question}\n\n"
        answer_text += "\n".join([f"{i + 1}. {option['text']}" for i, option in enumerate(options)])

        # Создаем инлайн-клавиатуру с кнопками для выбора ответа
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=str(i + 1), callback_data=f"answer:{i}")]
                             for i in range(len(options))]  # Каждому варианту соответствует кнопка с номером
        )
        await callback_query.message.edit_text(answer_text, reply_markup=keyboard)

    # Сохраняем handler для пользователя в глобальной переменной user_data
    user_data[callback_query.from_user.id] = handler



# Обработка ответов пользователя
@test_router.callback_query(F.data.startswith("answer:"))
async def handle_answer(callback_query: types.CallbackQuery):
    handler = user_data.get(callback_query.from_user.id)

    if handler is None:
        await callback_query.answer("Пожалуйста, начните тест заново.")
        return

    try:
        selected_option = int(callback_query.data.split(":")[1])
        handler.submit_answer(selected_option)
    except (ValueError, IndexError):
        await callback_query.answer("Неверный выбор.")
        return

    if not handler.is_finished():
        question, options = handler.get_question()

        # Обновляем текст с новым вопросом и ответами
        answer_text = f"{question}\n\n"
        answer_text += "\n".join([f"{i + 1}. {option['text']}" for i, option in enumerate(options)])

        # Обновляем инлайн-клавиатуру с кнопками-номерами
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=str(i + 1), callback_data=f"answer:{i}")]
                             for i in range(len(options))]
        )
        await callback_query.message.edit_text(answer_text, reply_markup=keyboard)
    else:
        level, description, total_score, max_score = handler.get_result()
        result_text = (
            f"Результат теста:\nУровень: {level}\n{description}\n\n"
            f"Вы получили {total_score} из {max_score} баллов."
        )
        await callback_query.message.edit_text(result_text)
        # Удаление handler после завершения теста
        del user_data[callback_query.from_user.id]

    # Уведомление об обработке callback
    await callback_query.answer()
