"""Мини-пример FastAPI + HTMX для викторин."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="HTMX Quiz Preview", version="0.1.0")

CATEGORIES: list[dict[str, Any]] = [
    {
        "id": 1,
        "name": "История",
        "description": "Проверьте знания ключевых событий и дат.",
    },
    {
        "id": 2,
        "name": "Наука",
        "description": "От физики до биологии — широкий кругозор.",
    },
    {
        "id": 3,
        "name": "Спорт",
        "description": "Самые памятные матчи и рекорды.",
    },
]

QUIZZES_BY_CATEGORY: dict[int, list[dict[str, Any]]] = {
    1: [
        {"id": 101, "title": "Мировая история"},
        {"id": 102, "title": "История России"},
    ],
    2: [
        {"id": 201, "title": "Великие ученые"},
        {"id": 202, "title": "Открытия XX века"},
    ],
    3: [
        {"id": 301, "title": "Олимпийские игры"},
        {"id": 302, "title": "Футбол"},
    ],
}

QUIZ_DETAILS: dict[int, dict[str, Any]] = {
    101: {
        "title": "Мировая история",
        "category_id": 1,
        "questions": [
            {
                "text": "Какая цивилизация построила пирамиду Хеопса?",
                "options": ["Древний Египет", "Майя", "Инки", "Римляне"],
                "answer": "Древний Египет",
            },
            {
                "text": "Когда началась Первая мировая война?",
                "options": ["1812", "1914", "1939", "1945"],
                "answer": "1914",
            },
        ],
    },
    102: {
        "title": "История России",
        "category_id": 1,
        "questions": [
            {
                "text": "В каком году была крещение Руси?",
                "options": ["862", "988", "1240", "1380"],
                "answer": "988",
            },
            {
                "text": "Кто правил страной в период Петровских реформ?",
                "options": ["Иван Грозный", "Петр I", "Екатерина II", "Александр I"],
                "answer": "Петр I",
            },
        ],
    },
    201: {
        "title": "Великие ученые",
        "category_id": 2,
        "questions": [
            {
                "text": "Кто открыл закон всемирного тяготения?",
                "options": ["Галилей", "Ньютон", "Коперник", "Эйнштейн"],
                "answer": "Ньютон",
            },
            {
                "text": "Кто разработал теорию относительности?",
                "options": ["Эйнштейн", "Фейнман", "Максвелл", "Боров"],
                "answer": "Эйнштейн",
            },
        ],
    },
    202: {
        "title": "Открытия XX века",
        "category_id": 2,
        "questions": [
            {
                "text": "В каком году впервые запустили искусственный спутник Земли?",
                "options": ["1945", "1957", "1961", "1969"],
                "answer": "1957",
            },
            {
                "text": "Кто открыл структуру ДНК?",
                "options": [
                    "Розалинд Франклин",
                    "Уотсон и Крик",
                    "Дарвин",
                    "Менделеев",
                ],
                "answer": "Уотсон и Крик",
            },
        ],
    },
    301: {
        "title": "Олимпийские игры",
        "category_id": 3,
        "questions": [
            {
                "text": "Где прошли первые современные Олимпийские игры?",
                "options": ["Афины", "Париж", "Лондон", "Берлин"],
                "answer": "Афины",
            },
            {
                "text": "Как часто проводятся летние Олимпийские игры?",
                "options": ["Каждый год", "Раз в два года", "Раз в четыре года", "Раз в пять лет"],
                "answer": "Раз в четыре года",
            },
        ],
    },
    302: {
        "title": "Футбол",
        "category_id": 3,
        "questions": [
            {
                "text": "Сколько игроков в команде на поле в официальном матче?",
                "options": ["7", "9", "11", "13"],
                "answer": "11",
            },
            {
                "text": "Какая страна выиграла ЧМ-2018?",
                "options": ["Германия", "Аргентина", "Франция", "Бразилия"],
                "answer": "Франция",
            },
        ],
    },
}


@app.get("/", response_class=HTMLResponse)
async def read_categories(request: Request) -> HTMLResponse:
    context = {
        "request": request,
        "categories": CATEGORIES,
        "active_view": "categories",
    }
    return templates.TemplateResponse("index.html", context)


def _is_hx(request: Request) -> bool:
    return request.headers.get("Hx-Request", "false").lower() == "true"


@app.get("/category/{category_id}", response_class=HTMLResponse)
async def read_category(category_id: int, request: Request) -> HTMLResponse:
    category = next((item for item in CATEGORIES if item["id"] == category_id), None)
    if category is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")

    quizzes = QUIZZES_BY_CATEGORY.get(category_id, [])
    context = {
        "request": request,
        "categories": CATEGORIES,
        "active_view": "category",
        "current_category": category,
        "quizzes": quizzes,
    }
    if _is_hx(request):
        return templates.TemplateResponse("category.html", context)
    return templates.TemplateResponse("index.html", context)


@app.get("/quiz/{quiz_id}", response_class=HTMLResponse)
async def read_quiz(quiz_id: int, request: Request) -> HTMLResponse:
    quiz = QUIZ_DETAILS.get(quiz_id)
    if quiz is None:
        raise HTTPException(status_code=404, detail="Викторина не найдена")

    category = next(
        (item for item in CATEGORIES if item["id"] == quiz["category_id"]),
        None,
    )
    if category is None:
        raise HTTPException(status_code=404, detail="Категория викторины не найдена")
    context = {
        "request": request,
        "categories": CATEGORIES,
        "active_view": "quiz",
        "current_category": category,
        "quiz": quiz,
    }
    if _is_hx(request):
        return templates.TemplateResponse("quiz.html", context)
    return templates.TemplateResponse("index.html", context)
