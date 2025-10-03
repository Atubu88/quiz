"""FastAPI application that powers the quiz Telegram Mini App flow."""
from __future__ import annotations
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

BOT_TOKEN = os.getenv("BOT_TOKEN")
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


async def _supabase_request(method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                            json_payload: Optional[Dict[str, Any] | List[Dict[str, Any]]] = None,
                            prefer: Optional[str] = None) -> Any:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = _build_supabase_headers(prefer)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.request(method, url, params=params, json=json_payload, headers=headers)
    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = {"message": response.text}
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail={"error": "SupabaseError", "status": response.status_code, "detail": detail})
    if response.status_code == status.HTTP_204_NO_CONTENT:
        return None
    return response.json()


async def _fetch_single_record(table: str, filters: Dict[str, str], select: str = "*") -> Optional[Dict[str, Any]]:
    params: Dict[str, Any] = {"select": select, **filters, "limit": 1}
    data = await _supabase_request("GET", table, params=params)
    return data[0] if data else None


def _validate_init_data(init_data: str) -> Dict[str, Any]:
    if not init_data:
        raise HTTPException(status_code=400, detail="initData is required")

    print("RAW initData:", init_data)

    # Парсим query string
    parsed = {key: values[0] for key, values in parse_qs(init_data, strict_parsing=True).items()}

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=400, detail="hash is missing from initData")

    # Убираем signature — оно не должно участвовать в проверке
    parsed.pop("signature", None)

    # Формируем data_check_string из оставшихся полей, отсортированных по ключу
    data_check_string = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed.keys()))

    # Точный способ: secret_key = HMAC_SHA256(key="WebAppData", message=BOT_TOKEN)
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()

    # Теперь вычисляем хэш: HMAC_SHA256(secret_key, data_check_string)
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    print("BOT_TOKEN:", BOT_TOKEN)
    print("Data check string:", data_check_string)
    print("Computed hash:", computed_hash)
    print("Received hash:", received_hash)

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid initData hash")

    # Теперь извлекаем user
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

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/debug/init")
async def debug_init(initData: str) -> Dict[str, Any]:
    """Отладка initData напрямую"""
    parsed = _validate_init_data(initData)
    return {"parsed": parsed}


@app.post("/login")
async def login(payload: LoginRequest) -> Dict[str, Any]:
    init_payload = _validate_init_data(payload.init_data)
    user_record = await _get_or_create_user(init_payload["user"])
    return {
        "user": {
            "id": user_record["id"],
            "telegram_id": user_record["telegram_id"],
            "username": user_record.get("username"),
            "first_name": user_record.get("first_name"),
            "last_name": user_record.get("last_name"),
        },
        "redirect": "/",
    }

# --- /team/create, /team/join, /team/start, /quiz/{team_id}, /me остаются как в твоем коде ---


@app.post("/team/create")
async def create_team(payload: CreateTeamRequest) -> Dict[str, Any]:
    """Create a team with a unique code and assign the captain."""

    user = await _ensure_user_exists(payload.user_id)
    code = await _generate_unique_team_code()
    team_payload = {
        "name": payload.team_name,
        "code": code,
        "captain_id": user["telegram_id"],
    }
    team_response = await _supabase_request(
        "POST",
        "teams",
        json_payload=team_payload,
        prefer="return=representation",
    )
    team_data = team_response[0] if isinstance(team_response, list) else team_response
    await _add_team_member(team_data["id"], user["id"], is_captain=True)
    return {"team": team_data, "code": code, "redirect": f"/team/{team_data['id']}"}


@app.post("/team/join")
async def join_team(payload: JoinTeamRequest) -> Dict[str, Any]:
    """Join an existing team using an invite code."""

    user = await _ensure_user_exists(payload.user_id)
    team = await _fetch_single_record("teams", {"code": f"eq.{payload.code.upper()}"})
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team code not found")

    existing_member = await _fetch_team_member(team["id"], user["id"])
    if not existing_member:
        existing_member = await _add_team_member(team["id"], user["id"], is_captain=False)

    return {"team": team, "member": existing_member, "redirect": f"/team/{team['id']}"}


@app.post("/team/start")
async def start_team(payload: StartTeamRequest) -> Dict[str, Any]:
    """Mark the team as started and load quiz data into cache for fast access."""

    user = await _ensure_user_exists(payload.user_id)
    team = await _ensure_team_exists(payload.team_id)

    member = await _fetch_team_member(team["id"], user["id"])
    if not member or not member.get("is_captain"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the captain can start the quiz")

    if team.get("start_time"):
        quiz_payload = QUIZ_CACHE.get(team["id"]) or await _load_quiz_into_cache(team["id"])
        return {"team": team, "quiz": quiz_payload, "redirect": f"/quiz/{team['id']}"}

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
    return {"team": updated_team, "quiz": quiz_payload, "redirect": f"/quiz/{team['id']}"}


@app.get("/me")
async def me(request: Request) -> Dict[str, Any]:
    # например, из сессии / токена
    user_id = request.headers.get("X-User-Id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    user = await _ensure_user_exists(int(user_id))
    return {"user": user}
