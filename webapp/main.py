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
from typing import Any, Dict, List, Optional, Set, Tuple, Type, TypeVar
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from webapp.services.match_service import (
    _ensure_match_quiz_assigned,
)
from webapp.services.quiz_service import (
    _ensure_player_progress_entry,
    _ensure_team_progress,
    _finalize_team_if_ready,
    _mark_player_completed,
    _register_team_answer,
)
from webapp.services.supabase_client import (
    _fetch_single_record,
    _supabase_request,
)
from webapp.services.team_service import (
    _clear_team_from_caches,
    _ensure_team_exists,
    _extract_match_id,
    _fetch_team_with_members,
    _normalize_identifier,
)

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


class LeaveTeamRequest(BaseModel):
    user_id: int
    team_id: str


class DeleteTeamRequest(BaseModel):
    user_id: int
    team_id: str


# ------------------- ВСПОМОГАТЕЛЬНЫЕ -------------------


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


async def _fetch_team_member(team_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return await _fetch_single_record("team_members", {"team_id": f"eq.{team_id}", "user_id": f"eq.{user_id}"})


async def _add_team_member(team_id: str, user_id: int, is_captain: bool = False):
    payload = {
        "team_id": team_id,
        "user_id": user_id,      # ✅ именно PK из users.id
        "is_captain": is_captain,
    }

    response = await _supabase_request(
        "POST",
        "team_members",
        json_payload=payload,
        prefer="return=representation",
    )

    if not response:
        raise HTTPException(status_code=500, detail="Не удалось добавить участника")
    return response[0] if isinstance(response, list) else response


async def _remove_team_member(team_id: str, user_id: int) -> None:
    await _supabase_request(
        "DELETE",
        "team_members",
        params={"team_id": f"eq.{team_id}", "user_id": f"eq.{user_id}"},
    )


async def _delete_team(team_id: str) -> None:
    await _supabase_request("DELETE", "team_members", params={"team_id": f"eq.{team_id}"})
    await _supabase_request("DELETE", "teams", params={"id": f"eq.{team_id}"})




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


async def _fetch_team_scoreboard(match_id: str, quiz_id: Any) -> tuple[List[Dict[str, Any]], bool]:
    """Возвращает таблицу результатов команд по матчу и признак, что все результаты готовы."""

    if not match_id or quiz_id in (None, ""):
        return [], False

    try:
        teams = await _supabase_request(
            "GET",
            "teams",
            params={
                "match_id": f"eq.{match_id}",
                "select": "id,name",
            },
        ) or []
    except HTTPException as exc:
        logging.info("Failed to fetch teams for scoreboard %s: %s", match_id, exc.detail)
        teams = []

    team_lookup: Dict[str, str] = {}
    for team in teams:
        team_id = _normalize_identifier(team.get("id"))
        if not team_id:
            continue
        team_lookup[team_id] = team.get("name") or team_id

    try:
        results = await _supabase_request(
            "GET",
            "team_results",
            params={
                "quiz_id": f"eq.{quiz_id}",
                "select": "team_id,score,time_taken",
                "order": "score.desc,time_taken.asc",
            },
        ) or []
    except HTTPException as exc:
        logging.info("Failed to fetch team results for match %s: %s", match_id, exc.detail)
        results = []

    scoreboard: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    for result in results:
        team_id = _normalize_identifier(result.get("team_id"))
        if team_id is None:
            continue
        if team_lookup and team_id not in team_lookup:
            continue

        score_raw = result.get("score")
        try:
            score = int(score_raw)
        except (TypeError, ValueError):
            score = 0

        entry = {
            "team_id": team_id,
            "team_name": team_lookup.get(team_id, team_id),
            "score": score,
            "time_taken": result.get("time_taken"),
        }
        scoreboard.append(entry)
        seen.add(team_id)

    expected_team_ids = {team_id for team_id in team_lookup if team_id}
    all_results_reported = bool(expected_team_ids) and expected_team_ids.issubset(seen)

    for team_id, team_name in team_lookup.items():
        if team_id in seen:
            continue
        scoreboard.append(
            {
                "team_id": team_id,
                "team_name": team_name,
                "score": 0,
                "time_taken": None,
            }
        )

    scoreboard.sort(
        key=lambda item: (
            -(item.get("score") or 0),
            item.get("time_taken") if item.get("time_taken") is not None else float("inf"),
            item.get("team_name") or "",
        )
    )

    return scoreboard, all_results_reported


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
    user: Optional[Dict[str, Any]] = None,
    member: Optional[Dict[str, Any]] = None,
    last_response: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Common helper for rendering the `team.html` template."""

    members: List[Dict[str, Any]] = []

    existing_members = team.get("members")
    if isinstance(existing_members, list):
        members = list(existing_members)

    inferred_is_captain = False
    captain_form_user_id: Optional[int] = None

    if user:
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
            else team.get("captain_id") == user.get("id")
        )

        if member is not None and user_key not in members_keys:
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

        captain_form_user_id = user.get("id")

    context = {
        "request": request,
        "team": {**team, "members": members},
        "user_is_captain": inferred_is_captain,
        "captain_id": captain_form_user_id,
        "last_response": last_response,
        "current_user": user,
        "current_member": member,
    }
    return context


# ------------------- РОУТЕРЫ -------------------

from webapp.routers.game import router as game_router
from webapp.routers.matches import router as matches_router
from webapp.routers.teams import router as teams_router

app.include_router(game_router)
app.include_router(teams_router)
app.include_router(matches_router)


@app.on_event("startup")
async def startup_check():
    # Быстрый самотест токена бота
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe")
        print("Startup getMe:", r.text)
    except Exception as e:
        print("Startup getMe error:", repr(e))

























