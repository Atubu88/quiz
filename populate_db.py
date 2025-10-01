import logging
import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from database import SessionLocal  # Используем SessionLocal (НЕ AsyncSessionLocal)
from models import Quiz, Question, Option

# 🔥 Данные викторины
QUIZ_DATA = {
    "title": "Жизнь Пророка Мухаммада",
    "questions": [
        {
            "question": "Когда родился Пророк Мухаммад (мир ему и благословение)?",
            "options": ["570 год", "580 год", "590 год", "600 год"],
            "correct": 0
        },
        {
            "question": "Как звали мать Пророка Мухаммада (мир ему и благословение)?",
            "options": ["Амина бинт Вахб", "Хадиджа бинт Хувайлид", "Фатима бинт Асад", "Асия бинт Музахим"],
            "correct": 0
        },
        {
            "question": "Какой ангел передал Пророку Мухаммаду (мир ему и благословение) первые откровения от Аллаха?",
            "options": ["Ангел Джибрил", "Ангел Микаил", "Ангел Исрафил", "Ангел Малик"],
            "correct": 0
        },
        {
            "question": "Как называлась пещера, в которой Пророку Мухаммаду (мир ему и благословение) были даны первые откровения?",
            "options": ["Хира", "Тур", "Сафра", "Худ"],
            "correct": 0
        },
        {
            "question": "Сколько лет было Пророку Мухаммаду (мир ему и благословение), когда он получил первые откровения?",
            "options": ["40 лет", "30 лет", "25 лет", "50 лет"],
            "correct": 0
        }
    ]
}


async def add_quiz():
    """Добавляет викторину, вопросы и варианты ответов в Supabase через SQLAlchemy."""
    async with SessionLocal() as session:  # 🛠 Используем SessionLocal (НЕ AsyncSessionLocal)
        try:
            # 🔹 Проверяем, есть ли уже такая викторина
            result = await session.execute(
                select(Quiz).where(Quiz.title == QUIZ_DATA["title"])
            )
            existing_quiz = result.scalars().first()

            if existing_quiz:
                logging.info("⚠️ Викторина уже существует в базе.")
                return

            # 🔥 Добавляем викторину
            quiz = Quiz(title=QUIZ_DATA["title"])
            session.add(quiz)
            await session.commit()
            await session.refresh(quiz)

            # 🔥 Добавляем вопросы и варианты ответов
            for q in QUIZ_DATA["questions"]:
                question = Question(text=q["question"], quiz_id=quiz.id)
                session.add(question)
                await session.commit()
                await session.refresh(question)

                for index, option_text in enumerate(q["options"]):
                    option = Option(
                        text=option_text,
                        is_correct=(index == q["correct"]),
                        question_id=question.id
                    )
                    session.add(option)

            # ✅ Подтверждаем изменения
            await session.commit()
            logging.info("✅ Викторина успешно добавлена в базу!")

        except Exception as e:
            logging.error(f"❌ Ошибка при добавлении викторины: {e}")
            await session.rollback()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(add_quiz())
