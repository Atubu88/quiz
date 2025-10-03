"""FastAPI application that powers the quiz Telegram Mini App flow."""
from __future__ import annotations

import logging

from dotenv import load_dotenv
load_dotenv()

import hashlib
import hmac
import json
import os
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, TypeVar
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# ВАЖНО: убираем кавычки/пробелы у токена
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required for Telegram init data validation.")
if not SUPABASE_URL or not SUPABASE_API_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_API_KEY must be configured.")

QUIZ_CACHE: dict[str, dict[str, Any]] = {}

app = FastAPI(title="Quiz Mini App")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# ------------------- МОДЕЛИ -------------------

class LoginRequest(BaseModel):
    init_data: str = Field(alias="initData", description="Raw initData string passed from Telegram WebApp")


class CreateTeamRequest(BaseModel):
    user_id: int
    team_name: str = Field(..., min_length=1, max_length=128)


class JoinTeamRequest(BaseModel):
    user_id: int
    code: str = Field(..., min_length=3, max_length=12)


class StartTeamRequest(BaseModel):
    user_id: int
    team_id: str


# ------------------- ВСПОМОГАТЕЛЬНЫЕ -------------------

def _build_supabase_headers(prefer: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "apikey": SUPABASE_API_KEY,
        "Authorization": f"Bearer {SUPABASE_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


async def _supabase_request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_payload: Optional[Dict[str, Any] | List[Dict[str, Any]]] = None,
    prefer: Optional[str] = None
) -> Any:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = _build_supabase_headers(prefer)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.request(method, url, params=params, json=json_payload, headers=headers)
    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = {"message": response.text}
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "SupabaseError", "status": response.status_code, "detail": detail},
        )
    if response.status_code == status.HTTP_204_NO_CONTENT:
        return None
    return response.json()


async def _fetch_single_record(table: str, filters: Dict[str, str], select: str = "*") -> Optional[Dict[str, Any]]:
    params: Dict[str, Any] = {"select": select, **filters, "limit": 1}
    data = await _supabase_request("GET", table, params=params)
    return data[0] if data else None


def _calc_hmacs(token: str, data_check_string: str) -> Dict[str, str]:
    """Возвращает все варианты подписи: webapp/login."""
    # WebAppData-деривированный ключ
    secret_webapp = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    hash_webapp = hmac.new(secret_webapp, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    # Login Widget-совместимость
    secret_login = hashlib.sha256(token.encode("utf-8")).digest()
    hash_login = hmac.new(secret_login, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    return {"webapp": hash_webapp, "login": hash_login}


def _validate_init_data(init_data: str) -> Dict[str, Any]:
    if not init_data:
        raise HTTPException(status_code=400, detail="initData is required")

    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise HTTPException(status_code=500, detail="BOT_TOKEN is not set")

    print("RAW initData:", init_data)

    # Разбор query
    parsed = {k: v[0] for k, v in parse_qs(init_data, strict_parsing=True).items()}

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=400, detail="hash is missing from initData")

    # Сценарий 1: считаем ХЭШ по всем ключам (включая signature, если есть)
    data_check_string = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed.keys()))
    print("Data check string:", data_check_string)
    h1 = _calc_hmacs(token, data_check_string)

    # Сценарий 2 (legacy): на некоторых клиентах signature исторически не участвовал
    parsed_legacy = dict(parsed)
    if "signature" in parsed_legacy:
        parsed_legacy.pop("signature")
    data_check_string_legacy = "\n".join(f"{k}={parsed_legacy[k]}" for k in sorted(parsed_legacy.keys()))
    h2 = _calc_hmacs(token, data_check_string_legacy)

    print("Computed hash (webapp):", h1["webapp"])
    print("Computed hash (login):", h1["login"])
    print("Computed hash legacy (webapp):", h2["webapp"])
    print("Computed hash legacy (login):", h2["login"])
    print("Received hash:", received_hash)

    if received_hash not in {h1["webapp"], h1["login"], h2["webapp"], h2["login"]}:
        # Быстрая диагностика: какой бот у токена?
        try:
            r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
            bot_info = r.json()
        except Exception as e:
            bot_info = {"error": str(e)}
        print("getMe:", bot_info)
        raise HTTPException(
            status_code=401,
            detail="Invalid initData hash (ensure WebApp opened by the same bot whose token is used on server)",
        )

    # (Опционально) Проверка свежести initData
    try:
        auth_ts = int(parsed.get("auth_date", "0"))
        # 24 часа допуска
        if abs(datetime.now(timezone.utc).timestamp() - auth_ts) > 86400:
            # Не критично: можно сделать warning вместо жёсткого отказа
            print("Warning: initData auth_date is older than 24h (or too far in future).")
            # Если хочешь строго — раскомментируй следующую строку:
            # raise HTTPException(status_code=401, detail="initData is too old")
    except ValueError:
        pass

    # Разбор user
    user_raw = parsed.get("user")
    if not user_raw:
        raise HTTPException(status_code=400, detail="user payload is missing")

    try:
        user_payload = json.loads(user_raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid user JSON in initData")

    if "id" not in user_payload:
        raise HTTPException(status_code=400, detail="user.id is required in initData")

    print("Validated user:", user_payload)

    return {
        "auth_date": parsed.get("auth_date"),
        "query_id": parsed.get("query_id"),
        "user": user_payload,
    }


async def _get_or_create_user(user_payload: Dict[str, Any]) -> Dict[str, Any]:
    telegram_id = user_payload["id"]
    existing = await _fetch_single_record("users", {"telegram_id": f"eq.{telegram_id}"})
    if existing:
        return existing

    user_data = {
        "telegram_id": telegram_id,
        "username": user_payload.get("username"),
        "first_name": user_payload.get("first_name"),
        "last_name": user_payload.get("last_name"),
    }
    created = await _supabase_request("POST", "users", json_payload=user_data, prefer="return=representation")
    return created[0] if isinstance(created, list) else created


async def _generate_unique_team_code(length: int = 6, attempts: int = 10) -> str:
    characters = string.ascii_uppercase + string.digits
    for _ in range(attempts):
        code = "".join(secrets.choice(characters) for _ in range(length))
        existing = await _fetch_single_record("teams", {"code": f"eq.{code}"}, select="id")
        if not existing:
            return code
    raise HTTPException(status_code=500, detail="Unable to generate team code")


async def _ensure_user_exists(user_id: int) -> Dict[str, Any]:
    user = await _fetch_single_record("users", {"id": f"eq.{user_id}"})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def _ensure_team_exists(team_id: str) -> Dict[str, Any]:
    team = await _fetch_single_record("teams", {"id": f"eq.{team_id}"})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team


async def _fetch_team_member(team_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return await _fetch_single_record("team_members", {"team_id": f"eq.{team_id}", "user_id": f"eq.{user_id}"})


async def _add_team_member(team_id: str, user_id: int, is_captain: bool) -> Dict[str, Any]:
    payload = {"team_id": team_id, "user_id": user_id, "is_captain": is_captain}
    created = await _supabase_request("POST", "team_members", json_payload=payload, prefer="return=representation")
    return created[0] if isinstance(created, list) else created


TModel = TypeVar("TModel", bound=BaseModel)


async def _parse_request_payload(request: Request, model: Type[TModel]) -> TModel:
    """Extract data from JSON or form payload and validate it with the given model."""

    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/json":
        raw = await request.json()
    else:
        form = await request.form()
        raw = dict(form)
    return model.model_validate(raw)


def _is_json_request(request: Request) -> bool:
    return "application/json" in request.headers.get("content-type", "").lower()


async def _fetch_active_quiz() -> Dict[str, Any]:
    quiz = await _fetch_single_record("quizzes", {"is_active": "eq.true"}, select="id,title,description")
    if not quiz:
        raise HTTPException(status_code=404, detail="No active quiz configured")

    questions = await _supabase_request(
        "GET",
        "questions",
        params={
            "select": "id,quiz_id,text,explanation,options(id,question_id,text,is_correct)",
            "quiz_id": f"eq.{quiz['id']}",
            "order": "id.asc",
        },
    )
    quiz["questions"] = questions
    return quiz


async def _load_quiz_into_cache(team_id: str) -> Dict[str, Any]:
    quiz_payload = await _fetch_active_quiz()
    QUIZ_CACHE[team_id] = quiz_payload
    return quiz_payload


# ------------------- ЭНДПОИНТЫ -------------------

@app.on_event("startup")
async def startup_check():
    # Быстрый самотест токена бота
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe")
        print("Startup getMe:", r.text)
    except Exception as e:
        print("Startup getMe error:", repr(e))


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/debug/init")
async def debug_init(initData: str) -> Dict[str, Any]:
    """Отладка initData напрямую."""
    parsed = _validate_init_data(initData)
    return {"parsed": parsed}


def _build_member_representation(user: Dict[str, Any], *, is_captain: bool) -> Dict[str, Any]:
    """Prepare the minimal set of fields that `team.html` expects for a member."""

    return {
        "name": user.get("first_name")
        or user.get("last_name")
        or user.get("username")
        or "Без имени",
        "username": user.get("username"),
        "is_captain": is_captain,
    }


def _build_team_context(
    request: Request,
    *,
    team: Dict[str, Any],
    user: Dict[str, Any],
    member: Optional[Dict[str, Any]] = None,
    last_response: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Common helper for rendering the `team.html` template."""

    members: List[Dict[str, Any]] = []

    existing_members = team.get("members")
    if isinstance(existing_members, list):
        members = existing_members

    # Ensure the current user is present in the rendered list so that the template
    # always shows at least one participant.
    members_keys = {
        (m.get("username"), m.get("name")) for m in members if isinstance(m, dict)
    }

    user_key = (
        user.get("username"),
        user.get("first_name") or user.get("last_name") or user.get("username"),
    )

    inferred_is_captain = bool(
        member.get("is_captain")
        if member is not None
        else team.get("captain_id") == user.get("telegram_id")
    )

    if user_key not in members_keys:
        members.append(
            _build_member_representation(
                {
                    "first_name": user.get("first_name"),
                    "last_name": user.get("last_name"),
                    "username": user.get("username"),
                },
                is_captain=inferred_is_captain,
            )
        )

    context = {
        "request": request,
        "team": {**team, "members": members},
        "user_is_captain": inferred_is_captain,
        "captain_id": user.get("id"),
        "last_response": last_response,
    }
    return context


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request) -> HTMLResponse:
    payload = await _parse_request_payload(request, LoginRequest)
    init_payload = _validate_init_data(payload.init_data)
    user_record = await _get_or_create_user(init_payload["user"])
    user_payload = {
        "id": user_record["id"],
        "telegram_id": user_record["telegram_id"],
        "username": user_record.get("username"),
        "first_name": user_record.get("first_name"),
        "last_name": user_record.get("last_name"),
    }

    if _is_json_request(request):
        return JSONResponse({"user": user_payload, "redirect": "/"})

    context = {
        "request": request,
        "user": user_payload,
        "login_success": True,
    }
    return templates.TemplateResponse("index.html", context)



@app.post("/team/create", response_class=HTMLResponse)
async def create_team(request: Request) -> HTMLResponse:
    """Создание команды с уникальным кодом и назначением капитана."""
    payload = await _parse_request_payload(request, CreateTeamRequest)
    user = await _ensure_user_exists(payload.user_id)
    code = await _generate_unique_team_code()

    team_payload = {
        "name": payload.team_name,
        "code": code,
        "captain_id": user["telegram_id"],  # bigint FK
    }

    try:
        team_response = await _supabase_request(
            "POST",
            "teams",
            json_payload=team_payload,
            prefer="return=representation",
        )
    except Exception as e:
        logging.error("❌ Ошибка при запросе в Supabase: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail="Ошибка запроса к базе")

    team_data = team_response[0] if isinstance(team_response, list) else team_response
    if "id" not in team_data:
        raise HTTPException(status_code=500, detail="Team was created without ID")

    member = await _add_team_member(team_data["id"], user["id"], is_captain=True)

    if _is_json_request(request):
        return JSONResponse(
            {"team": team_data, "code": code, "redirect": f"/team/{team_data['id']}"}
        )

    context = _build_team_context(
        request,
        team={**team_data, "code": code, "members": [
            {
                "id": user["id"],
                "username": user.get("username"),
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "is_captain": True,
            }
        ]},
        user=user,
        member=member,
        last_response={"team": team_data, "code": code},
        user_is_captain=True,
        captain_id=user["id"],
    )
    return templates.TemplateResponse("team.html", context)




@app.post("/team/join", response_class=HTMLResponse)
async def join_team(request: Request) -> HTMLResponse:
    """Join an existing team using an invite code."""
    payload = await _parse_request_payload(request, JoinTeamRequest)
    user = await _ensure_user_exists(payload.user_id)
    team = await _fetch_single_record("teams", {"code": f"eq.{payload.code.upper()}"})
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team code not found")

    existing_member = await _fetch_team_member(team["id"], user["id"])
    if not existing_member:
        existing_member = await _add_team_member(team["id"], user["id"], is_captain=False)

    if _is_json_request(request):
        return JSONResponse({"team": team, "member": existing_member, "redirect": f"/team/{team['id']}"})

    context = _build_team_context(
        request,
        team=team,
        user=user,
        member=existing_member,
        last_response={"team": team, "member": existing_member},
    )
    return templates.TemplateResponse("team.html", context)


@app.post("/team/start", response_class=HTMLResponse)
async def start_team(request: Request) -> HTMLResponse:
    """Mark the team as started and load quiz data into cache for fast access."""
    payload = await _parse_request_payload(request, StartTeamRequest)
    user = await _ensure_user_exists(payload.user_id)
    team = await _ensure_team_exists(payload.team_id)

    member = await _fetch_team_member(team["id"], user["id"])
    if not member or not member.get("is_captain"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the captain can start the quiz")

    if team.get("start_time"):
        quiz_payload = QUIZ_CACHE.get(team["id"]) or await _load_quiz_into_cache(team["id"])
    else:
        start_time = datetime.now(timezone.utc).isoformat()
        updated_team = await _supabase_request(
            "PATCH",
            "teams",
            params={"id": f"eq.{team['id']}"},
            json_payload={"start_time": start_time},
            prefer="return=representation",
        )
        if isinstance(updated_team, list):
            team = updated_team[0]
        else:
            team = updated_team
        quiz_payload = await _load_quiz_into_cache(team["id"])

    if _is_json_request(request):
        return JSONResponse({"team": team, "quiz": quiz_payload, "redirect": f"/quiz/{team['id']}"})

    first_question = None
    if isinstance(quiz_payload, dict):
        questions = quiz_payload.get("questions") or []
        if questions:
            first_question = questions[0]

    context = {
        "request": request,
        "team": team,
        "quiz": quiz_payload,
        "question": first_question,
        "answers": first_question.get("options") if isinstance(first_question, dict) else None,
        "game_id": team.get("id"),
        "last_response": {"team": team, "quiz": quiz_payload},
    }
    return templates.TemplateResponse("quiz.html", context)


@app.get("/me")
async def me(request: Request) -> Dict[str, Any]:
    # например, из сессии / токена
    user_id = request.headers.get("X-User-Id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    user = await _ensure_user_exists(int(user_id))
    return {"user": user}