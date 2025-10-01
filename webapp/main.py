"""Мини-пример FastAPI + HTMX для викторин с реальными данными из Supabase."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from supabase_client import supabase

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="HTMX Quiz Preview", version="0.2.0")

logger = logging.getLogger(__name__)


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def get_categories() -> tuple[list[dict[str, Any]], str | None]:
    """Загружает активные категории викторин."""

    def _load() -> list[dict[str, Any]]:
        response = (
            supabase.table("categories")
            .select("id,name,description,is_active")
            .eq("is_active", True)
            .order("name")
            .execute()
        )
        categories: list[dict[str, Any]] = []
        for raw in response.data or []:
            category_id = _to_int(raw.get("id"))
            if category_id is None:
                continue
            categories.append(
                {
                    "id": category_id,
                    "name": raw.get("name") or f"Категория №{category_id}",
                    "description": raw.get("description")
                    or "Описание категории появится позже.",
                }
            )
        return categories

    try:
        categories = await run_in_threadpool(_load)
        return categories, None
    except Exception:
        logger.exception("Failed to load categories from Supabase")
        return (
            [],
            "Не удалось загрузить категории викторин. Попробуйте обновить страницу позже.",
        )


async def get_category_by_id(category_id: int) -> dict[str, Any] | None:
    """Возвращает одну категорию по идентификатору."""

    def _load() -> dict[str, Any] | None:
        response = (
            supabase.table("categories")
            .select("id,name,description")
            .eq("id", category_id)
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        raw = response.data[0]
        return {
            "id": category_id,
            "name": raw.get("name") or f"Категория №{category_id}",
            "description": raw.get("description")
            or "Описание категории появится позже.",
        }

    try:
        return await run_in_threadpool(_load)
    except Exception:
        logger.exception(
            "Failed to load category from Supabase", extra={"category_id": category_id}
        )
        return None


async def get_quizzes(category_id: int) -> tuple[list[dict[str, Any]], str | None]:
    """Загружает список активных викторин категории."""

    def _load() -> list[dict[str, Any]]:
        response = (
            supabase.table("quizzes")
            .select("id,title,category_id,is_active")
            .eq("category_id", category_id)
            .eq("is_active", True)
            .order("title")
            .execute()
        )
        quizzes: list[dict[str, Any]] = []
        for raw in response.data or []:
            quiz_id = _to_int(raw.get("id"))
            if quiz_id is None:
                continue
            quizzes.append(
                {
                    "id": quiz_id,
                    "title": raw.get("title") or f"Викторина №{quiz_id}",
                    "category_id": category_id,
                }
            )
        return quizzes

    try:
        quizzes = await run_in_threadpool(_load)
        return quizzes, None
    except Exception:
        logger.exception(
            "Failed to load quizzes for category", extra={"category_id": category_id}
        )
        return (
            [],
            "Не удалось загрузить викторины выбранной категории. Попробуйте обновить страницу позже.",
        )


async def get_quiz_detail(quiz_id: int) -> tuple[dict[str, Any] | None, str | None]:
    """Возвращает подробности викторины и связанные вопросы."""

    class _QuizNotFound(Exception):
        pass

    def _load() -> dict[str, Any]:
        quiz_response = (
            supabase.table("quizzes")
            .select("id,title,category_id")  # ✅ description убрал
            .eq("id", quiz_id)
            .limit(1)
            .execute()
        )
        if not quiz_response.data:
            raise _QuizNotFound
        quiz_raw = quiz_response.data[0]
        category_id = _to_int(quiz_raw.get("category_id"))

        questions_response = (
            supabase.table("questions")
            .select("id,text,explanation")
            .eq("quiz_id", quiz_id)
            .order("id")
            .execute()
        )
        questions: list[dict[str, Any]] = []
        question_ids: list[int] = []
        for raw_question in questions_response.data or []:
            question_id = _to_int(raw_question.get("id"))
            if question_id is None:
                continue
            question_ids.append(question_id)
            questions.append(
                {
                    "id": question_id,
                    "text": raw_question.get("text") or "Вопрос без текста",
                    "explanation": raw_question.get("explanation"),
                    "options": [],
                    "correct_answer": None,
                }
            )

        options_by_question: dict[int, list[dict[str, Any]]] = {qid: [] for qid in question_ids}
        if question_ids:
            options_response = (
                supabase.table("options")
                .select("id,question_id,text,is_correct")
                .in_("question_id", question_ids)
                .order("id")
                .execute()
            )
            for raw_option in options_response.data or []:
                question_id = _to_int(raw_option.get("question_id"))
                option_text = raw_option.get("text")
                if question_id is None or option_text is None:
                    continue
                option_payload = {
                    "text": option_text,
                    "is_correct": bool(raw_option.get("is_correct")),
                }
                options_by_question.setdefault(question_id, []).append(option_payload)

        for question in questions:
            options = options_by_question.get(question["id"], [])
            question["options"] = options
            question["correct_answer"] = next(
                (option["text"] for option in options if option["is_correct"]),
                None,
            )

        return {
            "id": quiz_id,
            "title": quiz_raw.get("title") or f"Викторина №{quiz_id}",
            "description": None,  # заглушка
            "category_id": category_id,
            "questions": questions,
        }

    try:
        quiz = await run_in_threadpool(_load)
        return quiz, None
    except _QuizNotFound:
        return None, None
    except Exception:
        logger.exception("Failed to load quiz from Supabase", extra={"quiz_id": quiz_id})
        return None, "Не удалось загрузить викторину. Попробуйте обновить страницу позже."


def _is_hx(request: Request) -> bool:
    return request.headers.get("Hx-Request", "false").lower() == "true"


@app.get("/", response_class=HTMLResponse)
async def read_categories(request: Request) -> HTMLResponse:
    categories, categories_error = await get_categories()
    context = {
        "request": request,
        "categories": categories,
        "categories_error": categories_error,
        "active_view": "categories",
    }
    return templates.TemplateResponse("index.html", context)


@app.get("/category/{category_id}", response_class=HTMLResponse)
async def read_category(category_id: int, request: Request) -> HTMLResponse:
    categories, categories_error = await get_categories()
    category = next((item for item in categories if item["id"] == category_id), None)
    if category is None:
        category = await get_category_by_id(category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")

    quizzes, quizzes_error = await get_quizzes(category["id"])
    context = {
        "request": request,
        "categories": categories,
        "categories_error": categories_error,
        "active_view": "category",
        "current_category": category,
        "quizzes": quizzes,
        "quizzes_error": quizzes_error,
    }
    if _is_hx(request):
        return templates.TemplateResponse("category.html", context)
    return templates.TemplateResponse("index.html", context)


@app.get("/quiz/{quiz_id}", response_class=HTMLResponse)
async def read_quiz(quiz_id: int, request: Request) -> HTMLResponse:
    quiz, quiz_error = await get_quiz_detail(quiz_id)
    if quiz is None and quiz_error is None:
        raise HTTPException(status_code=404, detail="Викторина не найдена")

    current_category = None
    if quiz and quiz.get("category_id") is not None:
        current_category = await get_category_by_id(quiz["category_id"])
        if current_category is None:
            current_category = {
                "id": quiz["category_id"],
                "name": f"Категория №{quiz['category_id']}",
                "description": None,
            }

    context = {
        "request": request,
        "active_view": "quiz",
        "current_category": current_category,
        "quiz": quiz,
        "quiz_error": quiz_error,
    }
    if _is_hx(request):
        return templates.TemplateResponse("quiz.html", context)
    return templates.TemplateResponse("index.html", context)
