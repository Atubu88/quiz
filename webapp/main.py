"""FastAPI application that powers the quiz Telegram Mini App flow."""
from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()

import hashlib
import hmac
import json
from json import JSONDecodeError
import os
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
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


# ------------------- СЕРИАЛИЗАЦИЯ -------------------

def _serialize_user(user_record: Dict[str, Any]) -> Dict[str, Any]:
    full_name_parts = [user_record.get("first_name"), user_record.get("last_name")]
    full_name = " ".join(part for part in full_name_parts if part).strip()
    display_name = full_name or user_record.get("username") or f"Игрок #{user_record['id']}"
    return {
        "id": user_record["id"],
        "telegram_id": user_record.get("telegram_id"),
        "username": user_record.get("username"),
        "first_name": user_record.get("first_name"),
        "last_name": user_record.get("last_name"),
        "display_name": display_name,
    }


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
    except JSONDecodeError:
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


async def _fetch_team_members_with_profiles(team_id: str) -> List[Dict[str, Any]]:
    members_raw = await _supabase_request(
        "GET",
        "team_members",
        params={"team_id": f"eq.{team_id}"},
    ) or []

    members: List[Dict[str, Any]] = []
    for member in members_raw:
        user = await _fetch_single_record("users", {"id": f"eq.{member['user_id']}"})
        full_name_parts = []
        if user:
            if user.get("first_name"):
                full_name_parts.append(user.get("first_name"))
            if user.get("last_name"):
                full_name_parts.append(user.get("last_name"))
        full_name = " ".join(full_name_parts).strip()

        members.append(
            {
                "id": member.get("id"),
                "user_id": member.get("user_id"),
                "is_captain": member.get("is_captain", False),
                "username": user.get("username") if user else None,
                "name": full_name or (user.get("username") if user else None),
            }
        )

    return members


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


# ------------------- ПАРСИНГ -------------------

async def _extract_payload(request: Request) -> Dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            return await request.json()
        except JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}") from exc
    form = await request.form()
    return dict(form)


async def _parse_model(request: Request, model: type[BaseModel]) -> BaseModel:
    raw_payload = await _extract_payload(request)
    normalized_payload: Dict[str, Any] = {}
    for key, value in raw_payload.items():
        normalized_key = "initData" if key == "init_data" else key
        normalized_payload[normalized_key] = value
    validator = getattr(model, "model_validate", None)
    if callable(validator):
        return validator(normalized_payload)
    return model.parse_obj(normalized_payload)


async def _build_team_template_context(
    team: Dict[str, Any],
    *,
    current_user_id: int,
    status_message: Optional[str] = None,
) -> Dict[str, Any]:
    members = await _fetch_team_members_with_profiles(team["id"])
    captain_member = next((member for member in members if member.get("is_captain")), None)
    user_is_captain = any(
        member.get("user_id") == current_user_id and member.get("is_captain") for member in members
    )

    team_payload = dict(team)
    team_payload["members"] = members

    return {
        "team": team_payload,
        "user_is_captain": user_is_captain,
        "captain_id": captain_member.get("user_id") if captain_member else current_user_id,
        "status_message": status_message,
    }


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


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request) -> HTMLResponse:
    payload = await _parse_model(request, LoginRequest)
    init_payload = _validate_init_data(payload.init_data)
    user_record = await _get_or_create_user(init_payload["user"])
    user_serialized = _serialize_user(user_record)

    context = {
        "request": request,
        "user": user_serialized,
        "prefill_user_id": user_serialized["id"],
        "status_message": "Вы успешно вошли в систему!",
    }
    return templates.TemplateResponse("index.html", context)


@app.post("/team/create", response_class=HTMLResponse)
async def create_team(request: Request) -> HTMLResponse:
    """Create a team with a unique code and assign the captain."""
    payload = await _parse_model(request, CreateTeamRequest)
    user = await _ensure_user_exists(payload.user_id)
    code = await _generate_unique_team_code()
    team_payload = {
        "name": payload.team_name,
        "code": code,
        "captain_id": user["telegram_id"],  # если у тебя captian_id = users.telegram_id
    }
    team_response = await _supabase_request(
        "POST",
        "teams",
        json_payload=team_payload,
        prefer="return=representation",
    )
    team_data = team_response[0] if isinstance(team_response, list) else team_response
    await _add_team_member(team_data["id"], user["id"], is_captain=True)
    context = await _build_team_template_context(
        team_data,
        current_user_id=user["id"],
        status_message="Команда успешно создана! Поделитесь кодом с участниками.",
    )
    context.update(
        {
            "request": request,
            "user": _serialize_user(user),
        }
    )
    return templates.TemplateResponse("team.html", context)


@app.post("/team/join", response_class=HTMLResponse)
async def join_team(request: Request) -> HTMLResponse:
    """Join an existing team using an invite code."""
    payload = await _parse_model(request, JoinTeamRequest)
    user = await _ensure_user_exists(payload.user_id)
    team = await _fetch_single_record("teams", {"code": f"eq.{payload.code.upper()}"})
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team code not found")

    existing_member = await _fetch_team_member(team["id"], user["id"])
    if not existing_member:
        existing_member = await _add_team_member(team["id"], user["id"], is_captain=False)

    context = await _build_team_template_context(
        team,
        current_user_id=user["id"],
        status_message="Вы присоединились к команде!",
    )
    context.update(
        {
            "request": request,
            "user": _serialize_user(user),
            "last_response": {"member": existing_member},
        }
    )
    return templates.TemplateResponse("team.html", context)


@app.post("/team/start", response_class=HTMLResponse)
async def start_team(request: Request) -> HTMLResponse:
    """Mark the team as started and load quiz data into cache for fast access."""
    payload = await _parse_model(request, StartTeamRequest)
    user = await _ensure_user_exists(payload.user_id)
    team = await _ensure_team_exists(payload.team_id)

    member = await _fetch_team_member(team["id"], user["id"])
    if not member or not member.get("is_captain"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the captain can start the quiz")

    if team.get("start_time"):
        quiz_payload = QUIZ_CACHE.get(team["id"]) or await _load_quiz_into_cache(team["id"])
        first_question = (quiz_payload.get("questions") or [None])[0] if quiz_payload else None
        context = {
            "request": request,
            "team": team,
            "quiz": quiz_payload,
            "question": first_question,
            "answers": (first_question or {}).get("options") if first_question else None,
            "game_id": team["id"],
            "user": _serialize_user(user),
            "status_message": "Игра уже была начата ранее — продолжаем!",
        }
        return templates.TemplateResponse("quiz.html", context)

    start_time = datetime.now(timezone.utc).isoformat()
    updated_team = await _supabase_request(
        "PATCH",
        "teams",
        params={"id": f"eq.{team['id']}"},
        json_payload={"start_time": start_time},
        prefer="return=representation",
    )
    if isinstance(updated_team, list):
        updated_team = updated_team[0]
    quiz_payload = await _load_quiz_into_cache(team["id"])
    first_question = (quiz_payload.get("questions") or [None])[0] if quiz_payload else None
    context = {
        "request": request,
        "team": updated_team,
        "quiz": quiz_payload,
        "question": first_question,
        "answers": (first_question or {}).get("options") if first_question else None,
        "game_id": team["id"],
        "user": _serialize_user(user),
        "status_message": "Командная игра началась! Удачи!",
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
