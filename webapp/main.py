from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from supabase_client import supabase

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(
    title="Quiz WebApp",
    description="Мини-приложение Telegram для викторины",
    version="0.1.0",
)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Главная страница с перечнем доступных викторин."""
    load_error: str | None = None
    try:
        response = supabase.table("quizzes").select("id,title").order("title").execute()
        quizzes: list[dict[str, Any]] = response.data or []
    except Exception:  # pragma: no cover - network/runtime guard
        logger.exception("Failed to load quizzes from Supabase")
        quizzes = []
        load_error = "Не удалось загрузить список викторин. Проверьте подключение к базе данных."
    context = {
        "request": request,
        "quizzes": quizzes,
        "load_error": load_error,
    }
    return templates.TemplateResponse("index.html", context)


@app.get("/quiz/{quiz_id}", response_class=HTMLResponse)
async def quiz_detail(quiz_id: int, request: Request) -> HTMLResponse:
    """Возвращает содержимое викторины для HTMX-запросов."""
    try:
        quiz_response = supabase.table("quizzes").select("id,title").eq("id", quiz_id).limit(1).execute()
        quiz = (quiz_response.data or [{}])[0] if quiz_response.data else None
        if not quiz:
            raise HTTPException(status_code=404, detail="Викторина не найдена")

        questions_response = (
            supabase.table("questions")
            .select("id,text,explanation")
            .eq("quiz_id", quiz_id)
            .order("id")
            .execute()
        )
        questions = questions_response.data or []
        question_ids = [question["id"] for question in questions]

        options_by_question: dict[int, list[dict[str, Any]]] = {}
        if question_ids:
            options_response = (
                supabase.table("options")
                .select("id,question_id,text,is_correct")
                .in_("question_id", question_ids)
                .order("id")
                .execute()
            )
            for option in options_response.data or []:
                options_by_question.setdefault(option["question_id"], []).append(option)

        for question in questions:
            question["options"] = options_by_question.get(question["id"], [])

        context = {
            "request": request,
            "quiz": quiz,
            "questions": questions,
            "error": None,
        }
    except HTTPException:
        raise
    except Exception:  # pragma: no cover - network/runtime guard
        logger.exception("Failed to load quiz from Supabase", extra={"quiz_id": quiz_id})
        context = {
            "request": request,
            "quiz": None,
            "questions": [],
            "error": "Не удалось загрузить викторину. Попробуйте обновить страницу позже.",
        }

    return templates.TemplateResponse("partials/quiz_detail.html", context)


@app.get("/health", response_class=HTMLResponse)
async def healthcheck() -> HTMLResponse:
    return HTMLResponse("ok")
