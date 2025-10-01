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

DEFAULT_QUIZ_TYPE_SLUG = "classic"
FALLBACK_QUIZ_TYPES: tuple[dict[str, Any], ...] = (
    {
        "id": "classic",
        "slug": "classic",
        "title": "Классическая викторина",
        "description": "Вопросы с вариантами ответов и объяснениями.",
    },
    {
        "id": "self_report",
        "slug": "self_report",
        "title": "Самооценочные тесты",
        "description": "Тесты с развернутыми ответами и рекомендациями.",
    },
)


def _normalize_quiz_type(raw_type: dict[str, Any]) -> dict[str, Any]:
    slug = raw_type.get("slug") or raw_type.get("code") or raw_type.get("id")
    if slug is None:
        slug = DEFAULT_QUIZ_TYPE_SLUG
    slug_str = str(slug)
    title = (
        raw_type.get("title")
        or raw_type.get("name")
        or raw_type.get("label")
        or slug_str.capitalize()
    )
    description = raw_type.get("description") or raw_type.get("details")
    return {
        "id": raw_type.get("id"),
        "slug": slug_str,
        "title": title,
        "description": description,
    }


def load_quiz_types() -> tuple[list[dict[str, Any]], str | None]:
    try:
        base_query = (
            supabase.table("quiz_types")
            .select("id,slug,title,name,description,is_active,sort_order")
            .eq("is_active", True)
        )
        try:
            response = base_query.order("sort_order").order("id").execute()
        except Exception:  # pragma: no cover - network/runtime guard
            logger.debug(
                "Retrying quiz types loading without sort_order column",
                exc_info=True,
            )
            response = (
                supabase.table("quiz_types")
                .select("id,slug,title,name,description,is_active")
                .eq("is_active", True)
                .order("id")
                .execute()
            )
        raw_types = response.data or []
        if not raw_types:
            return [dict(item) for item in FALLBACK_QUIZ_TYPES], None
        return [_normalize_quiz_type(item) for item in raw_types], None
    except Exception:  # pragma: no cover - network/runtime guard
        logger.exception("Failed to load quiz types from Supabase")
        return (
            [dict(item) for item in FALLBACK_QUIZ_TYPES],
            "Не удалось загрузить список видов викторин. Отображены доступные по умолчанию разделы.",
        )


def load_categories() -> tuple[list[dict[str, Any]], str | None]:
    try:
        response = (
            supabase.table("categories")
            .select("id,name")
            .eq("is_active", True)
            .order("name")
            .execute()
        )
        categories = [
            {"id": int(category["id"]), "name": category.get("name", "Без названия")}
            for category in response.data or []
            if category.get("id") is not None
        ]
        return categories, None
    except Exception:  # pragma: no cover - network/runtime guard
        logger.exception("Failed to load quiz categories from Supabase")
        return (
            [],
            "Не удалось загрузить категории классической викторины. Попробуйте обновить страницу позже.",
        )


def load_quizzes(category_id: int) -> tuple[list[dict[str, Any]], str | None]:
    try:
        response = (
            supabase.table("quizzes")
            .select("id,title,is_active")
            .eq("category_id", category_id)
            .eq("is_active", True)
            .order("title")
            .execute()
        )
        quizzes = [
            {"id": int(quiz["id"]), "title": quiz.get("title", "Без названия")}
            for quiz in response.data or []
            if quiz.get("id") is not None
        ]
        return quizzes, None
    except Exception:  # pragma: no cover - network/runtime guard
        logger.exception(
            "Failed to load quizzes for category from Supabase",
            extra={"category_id": category_id},
        )
        return (
            [],
            "Не удалось загрузить викторины выбранной категории. Попробуйте обновить страницу позже.",
        )


def build_quiz_type_context(
    request: Request, quiz_type_slug: str | None, category_id: int | None
) -> dict[str, Any]:
    quiz_types, types_error = load_quiz_types()
    active_type = next(
        (quiz_type for quiz_type in quiz_types if quiz_type["slug"] == quiz_type_slug),
        None,
    )
    if active_type is None and quiz_types:
        active_type = next(
            (
                quiz_type
                for quiz_type in quiz_types
                if quiz_type["slug"] == DEFAULT_QUIZ_TYPE_SLUG
            ),
            quiz_types[0],
        )
    elif active_type is None:
        active_type = dict(FALLBACK_QUIZ_TYPES[0])

    categories: list[dict[str, Any]] = []
    categories_error: str | None = None
    category_notice: str | None = None
    quizzes: list[dict[str, Any]] = []
    quizzes_error: str | None = None
    selected_category: dict[str, Any] | None = None

    if active_type["slug"] == "classic":
        categories, categories_error = load_categories()
        if categories and category_id is not None:
            selected_category = next(
                (category for category in categories if category["id"] == category_id),
                None,
            )
            if selected_category is None:
                category_notice = "Выбранная категория не найдена или недоступна."
                category_id = None
        if selected_category is not None:
            quizzes, quizzes_error = load_quizzes(selected_category["id"])
    else:
        category_id = None

    context = {
        "request": request,
        "quiz_types": quiz_types,
        "active_type": active_type,
        "types_error": types_error,
        "categories": categories,
        "categories_error": categories_error,
        "category_notice": category_notice,
        "selected_category": selected_category,
        "quizzes": quizzes,
        "quizzes_error": quizzes_error,
    }
    return context


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    quiz_type: str | None = None,
    category_id: int | None = None,
) -> HTMLResponse:
    """Главная страница с выбором вида викторины и доступных категорий."""
    context = build_quiz_type_context(request, quiz_type, category_id)
    return templates.TemplateResponse("index.html", context)


@app.get("/quiz-type/{quiz_type_slug}", response_class=HTMLResponse)
async def load_quiz_type(
    quiz_type_slug: str,
    request: Request,
    category_id: int | None = None,
) -> HTMLResponse:
    """Возвращает панель с категориями и викторинами для выбранного вида."""
    context = build_quiz_type_context(request, quiz_type_slug, category_id)
    return templates.TemplateResponse("partials/quiz_type_panel.html", context)


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
