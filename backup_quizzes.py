import os
import json
import asyncio
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_API_KEY)

async def backup_quizzes():
    # Шаг 1: Загружаем все викторины
    quiz_resp = await asyncio.to_thread(
        supabase.table("quizzes").select("*").execute
    )
    quizzes = quiz_resp.data

    full_backup = []

    for quiz in quizzes:
        quiz_id = quiz["id"]
        title = quiz["title"]

        # Шаг 2: Загружаем все вопросы этой викторины
        question_resp = await asyncio.to_thread(
            supabase.table("questions").select("*").eq("quiz_id", quiz_id).execute
        )
        questions = question_resp.data

        full_questions = []
        for question in questions:
            question_id = question["id"]

            # Шаг 3: Загружаем все варианты ответа этого вопроса
            options_resp = await asyncio.to_thread(
                supabase.table("options").select("*").eq("question_id", question_id).execute
            )
            options = options_resp.data

            full_questions.append({
                "id": question["id"],
                "text": question["text"],
                "explanation": question.get("explanation"),
                "options": options
            })

        full_backup.append({
            "id": quiz_id,
            "title": title,
            "questions": full_questions
        })

    # Шаг 4: Сохраняем всё в JSON-файл
    with open("backup_quizzes.json", "w", encoding="utf-8") as f:
        json.dump(full_backup, f, ensure_ascii=False, indent=4)

    print("✅ Викторины экспортированы в backup_quizzes.json")

asyncio.run(backup_quizzes())
