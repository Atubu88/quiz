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
from urllib.parse import parse_qs, urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from webapp.services.match_service import (
    _build_match_status_response,
    _ensure_match_quiz_assigned,
    _get_match_teams,
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
    _find_existing_team_for_user,
    _normalize_identifier,
)
from webapp.utils.cache import MATCH_STATUS_CACHE, MATCH_TEAM_CACHE, TEAM_READY_CACHE

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



async def _find_existing_team_for_user(user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the team the user already belongs to (if any)."""

    # Проверяем наличие записи в таблице участников.
    membership = await _fetch_single_record(
        "team_members",
        {"user_id": f"eq.{user['id']}"},
        select="team_id",
    )

    if membership and membership.get("team_id"):
        team = await _fetch_single_record("teams", {"id": f"eq.{membership['team_id']}"})
        if team:
            return team

    # На случай, если запись участника отсутствует, но пользователь значится капитаном.
    captain_team = await _fetch_single_record(
        "teams", {"captain_id": f"eq.{user['telegram_id']}"}
    )
    if captain_team:
        return captain_team

    return None


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


async def _fetch_team_scoreboard(match_id: str, quiz_id: Any) -> List[Dict[str, Any]]:
    """Возвращает таблицу результатов команд по матчу."""

    if not match_id or quiz_id in (None, ""):
        return []

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

    return scoreboard


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



@app.get("/team/{team_id}", response_class=HTMLResponse)
async def view_team(team_id: str, request: Request, user_id: Optional[int] = None) -> HTMLResponse:
    """Render the team page with the current member list."""

    team = await _fetch_team_with_members(team_id)

    user: Optional[Dict[str, Any]] = None
    member: Optional[Dict[str, Any]] = None

    if user_id is not None:
        try:
            user = await _ensure_user_exists(user_id)
        except HTTPException as exc:
            # Если пользователь не найден, просто отобразим команду без выделения капитана.
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
        else:
            member = next(
                (m for m in team.get("members", []) if m.get("id") == user.get("id")),
                None,
            )

    context = _build_team_context(
        request,
        team=team,
        user=user,
        member=member,
    )
    return templates.TemplateResponse("team.html", context)



@app.post("/team/create", response_class=HTMLResponse)
async def create_team(request: Request) -> HTMLResponse:
    """Создание команды с уникальным кодом и назначением капитана."""
    payload = await _parse_request_payload(request, CreateTeamRequest)
    user = await _ensure_user_exists(payload.user_id)

    # Если уже в команде — 409 с понятным текстом
    existing_team = await _find_existing_team_for_user(user)
    if existing_team:
        team_name = existing_team.get("name") or existing_team.get("code") or existing_team.get("id")
        message = f"Вы уже состоите в команде «{team_name}». Сначала покиньте текущую команду."
        raise HTTPException(status.HTTP_409_CONFLICT, detail=message)

    code = await _generate_unique_team_code()

    team_payload = {
        "name": payload.team_name,
        "code": code,
        "captain_id": user["id"],  # ✅ правильный PK
        "match_id": "demo-match",
    }

    # 1) Вставляем команду — пробрасываем исходные ошибки Supabase
    team_response = await _supabase_request(
        "POST",
        "teams",
        json_payload=team_payload,
        prefer="return=representation",
    )

    # team_response может быть списком или объектом
    team_data = team_response[0] if isinstance(team_response, list) and team_response else team_response
    if not isinstance(team_data, dict) or "id" not in team_data:
        logging.error("Team created but no ID in response: %s", team_response)
        raise HTTPException(status_code=500, detail=f"Team created but no ID in response")

    team_id = team_data["id"]
    normalized_team_id = _normalize_identifier(team_id)
    TEAM_READY_CACHE[normalized_team_id] = bool(team_data.get("ready"))
    match_id = _extract_match_id(team_data)
    if match_id and normalized_team_id:
        MATCH_TEAM_CACHE.setdefault(match_id, set()).add(normalized_team_id)

    # 2) Добавляем участника (капитана) — upsert, не падаем на дублях
    try:
        await _add_team_member(team_id, user["id"], is_captain=True)
    except HTTPException as e:
        # если это RLS/403 — отдадим понятную ошибку, но команда уже создана
        logging.error("Failed to add captain to team_members: %s", e.detail)
        # Можно продолжить и просто показать команду без списка участников
        # raise  # если хочешь жёстко падать — раскомментируй

    # 3) Собираем данные для отображения
    team_with_members = await _fetch_team_with_members(team_id)
    team_with_members.setdefault("code", code)

    if _is_json_request(request):
        # Возвращаем JSON-ответ + redirect для фронта
        redirect_url = f"/team/{team_id}?user_id={user['id']}"
        return JSONResponse({"team": team_with_members, "redirect": redirect_url})

    # HTML-ответ (рендер страницы команды)
    context = _build_team_context(
        request,
        team=team_with_members,
        user=user,
        last_response={"team": team_with_members},
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

    team_with_members = await _fetch_team_with_members(team["id"])
    member_entry = next(
        (m for m in team_with_members.get("members", []) if m.get("id") == user.get("id")),
        None,
    )

    if _is_json_request(request):
        redirect_url = f"/team/{team['id']}?user_id={user['id']}"
        return JSONResponse(
            {"team": team_with_members, "member": member_entry or existing_member, "redirect": redirect_url}
        )

    context = _build_team_context(
        request,
        team=team_with_members,
        user=user,
        member=member_entry or existing_member,
        last_response={"team": team_with_members, "member": member_entry or existing_member},
    )
    return templates.TemplateResponse("team.html", context)


@app.post("/team/start", response_class=HTMLResponse)
async def start_team(request: Request) -> HTMLResponse:
    # 1️⃣ Разбор данных запроса
    payload = await _parse_request_payload(request, StartTeamRequest)
    user = await _ensure_user_exists(payload.user_id)
    team = await _ensure_team_exists(payload.team_id)

    # 2️⃣ Проверяем, что нажал капитан
    member = await _fetch_team_member(team["id"], user["id"])
    if not member or not member.get("is_captain"):
        raise HTTPException(status_code=403, detail="Only the captain can start the quiz")

    team_id = _normalize_identifier(team.get("id"))

    # 3️⃣ Ставим готовность в кэше
    TEAM_READY_CACHE[team_id] = True
    team["ready"] = True

    # 4️⃣ ✅ Сохраняем готовность команды в Supabase
    try:
        await _supabase_request(
            "PATCH",
            "teams",
            params={"id": f"eq.{team_id}"},
            json_payload={"ready": True},
            prefer="return=representation",
        )
        logging.info(f"Team {team_id} marked ready in Supabase.")
    except HTTPException as e:
        logging.warning(f"Failed to update ready status for team {team_id}: {e.detail}")

    # 5️⃣ Добавляем команду в матч
    match_id = _extract_match_id(team)
    MATCH_TEAM_CACHE.setdefault(match_id, set()).add(team_id)

    # 6️⃣ Если все команды готовы — назначаем викторину
    all_ready = all(TEAM_READY_CACHE.get(tid) for tid in MATCH_TEAM_CACHE[match_id])
    if all_ready:
        await _ensure_match_quiz_assigned(match_id)

    # 7️⃣ Формируем ответ о состоянии матча
    match_response = await _build_match_status_response(match_id, fallback_team=team)

    # 8️⃣ Если JSON-запрос — сразу отдаём JSON
    if _is_json_request(request):
        return JSONResponse(match_response)

    # 9️⃣ Иначе рендерим шаблон team.html
    team_with_members = await _fetch_team_with_members(team_id)
    context = _build_team_context(
        request,
        team=team_with_members,
        user=user,
        member=member,
        last_response={"team": team_with_members},
    )
    context["match_status"] = match_response
    return templates.TemplateResponse("team.html", context)




@app.get("/match/status/{match_id}")
async def match_status(match_id: str) -> JSONResponse:
    cached = MATCH_STATUS_CACHE.get(match_id) or {}
    cached_teams = cached.get("teams")

    prefetched_teams: Optional[List[Dict[str, Any]]] = None
    if isinstance(cached_teams, list) and cached_teams:
        prefetched_teams = [
            {"id": team.get("id"), "name": team.get("name"), "ready": team.get("ready")}
            for team in cached_teams
            if team.get("id")
        ]

    fallback_team: Optional[Dict[str, Any]] = None
    if not prefetched_teams:
        try:
            fallback_team = await _fetch_single_record("teams", {"match_id": f"eq.{match_id}"})
        except HTTPException:
            fallback_team = None
        if not fallback_team:
            try:
                fallback_team = await _fetch_single_record("teams", {"id": f"eq.{match_id}"})
            except HTTPException:
                fallback_team = None

    response_data = await _build_match_status_response(
        match_id,
        fallback_team=fallback_team,
        prefetched_teams=prefetched_teams,
    )
    return JSONResponse(response_data)


@app.get("/game/status/{match_id}")
async def game_status(match_id: str, team_id: str, user_id: int) -> Dict[str, Any]:
    normalized_team_id = _normalize_identifier(team_id)
    if not normalized_team_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="team_id обязателен")

    team_with_members = await _fetch_team_with_members(normalized_team_id)
    team_match_id = _normalize_identifier(_extract_match_id(team_with_members))
    if team_match_id and team_match_id != _normalize_identifier(match_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Команда не участвует в этом матче")

    member_ids = {
        str(member.get("id"))
        for member in team_with_members.get("members", [])
        if member.get("id") is not None
    }
    if str(user_id) not in member_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Вы не состоите в этой команде")

    team_progress = await _ensure_team_progress(match_id, team_with_members)
    completed_members = len(team_progress.get("completed_members") or [])
    total_members = len(team_progress.get("member_ids") or [])

    response: Dict[str, Any] = {
        "team_completed": bool(team_progress.get("team_completed")),
        "team_members_completed": completed_members,
        "team_members_total": total_members,
    }

    if response["team_completed"]:
        response["team_score"] = team_progress.get("team_score")

    return response


@app.post("/team/leave", response_class=HTMLResponse)
async def leave_team(request: Request) -> HTMLResponse:
    payload = await _parse_request_payload(request, LeaveTeamRequest)
    user = await _ensure_user_exists(payload.user_id)
    team = await _ensure_team_exists(payload.team_id)

    member = await _fetch_team_member(team["id"], user["id"])
    if not member:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Вы не состоите в этой команде")
    if member.get("is_captain"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Капитан не может покинуть команду. Используйте удаление команды.",
        )

    await _remove_team_member(team["id"], user["id"])
    team_with_members = await _fetch_team_with_members(team["id"])
    message = "Вы покинули команду."

    if _is_json_request(request):
        return JSONResponse({"team": team_with_members, "redirect": "/", "message": message})

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/team/delete", response_class=HTMLResponse)
async def delete_team(request: Request) -> HTMLResponse:
    payload = await _parse_request_payload(request, DeleteTeamRequest)
    user = await _ensure_user_exists(payload.user_id)
    team = await _ensure_team_exists(payload.team_id)

    if team.get("captain_id") != user.get("id"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Удалять команду может только капитан")

    await _delete_team(team["id"])
    _clear_team_from_caches(team)
    message = "Команда удалена."

    if _is_json_request(request):
        return JSONResponse({"redirect": "/", "message": message})

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/me")
async def me(request: Request) -> Dict[str, Any]:
    # например, из сессии / токена
    user_id = request.headers.get("X-User-Id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    user = await _ensure_user_exists(int(user_id))
    return {"user": user}


@app.get("/team/of-user/{user_id}")
async def get_team_of_user(user_id: int):
    user = await _ensure_user_exists(user_id)
    team = await _find_existing_team_for_user(user)
    if not team:
        return JSONResponse({}, status_code=404)
    return team

@app.get("/game/{match_id}", response_class=HTMLResponse)
async def game_screen(request: Request, match_id: str):
    quiz_id = await _ensure_match_quiz_assigned(match_id)

    quizzes = await _supabase_request(
        "GET",
        "quizzes",
        params={
            "id": f"eq.{quiz_id}",
            "select": "id,title,description,questions(id,text,explanation,options(id,text,is_correct))",
        },
    )
    if not quizzes:
        raise HTTPException(404, detail="Quiz not found in database")

    quiz = quizzes[0]
    questions = quiz.get("questions") or []
    total_questions = len(questions)

    raw_question_index = request.query_params.get("question_index")
    try:
        submitted_index = int(raw_question_index) if raw_question_index is not None else 0
    except (TypeError, ValueError):
        submitted_index = 0

    if total_questions:
        submitted_index = max(0, min(submitted_index, total_questions - 1))
    else:
        submitted_index = 0

    team_id_param = request.query_params.get("team_id")
    user_id_param = request.query_params.get("user_id")

    team_id = _normalize_identifier(team_id_param)
    if not team_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="team_id обязателен для прохождения викторины")

    try:
        user_id = int(user_id_param) if user_id_param is not None else None
    except (TypeError, ValueError):
        user_id = None

    if user_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="user_id обязателен для прохождения викторины")

    team_with_members = await _fetch_team_with_members(team_id)
    team_match_id = _normalize_identifier(_extract_match_id(team_with_members))
    if team_match_id and team_match_id != _normalize_identifier(match_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Команда не участвует в этом матче")

    member_ids: Set[int] = set()
    for member in team_with_members.get("members", []):
        try:
            member_ids.add(int(member.get("id")))
        except (TypeError, ValueError):
            continue

    if user_id not in member_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Вы не состоите в этой команде")

    team_progress = await _ensure_team_progress(match_id, team_with_members, quiz.get("id"))
    _ensure_player_progress_entry(team_progress, user_id)

    selected_option_param = request.query_params.get("option")

    feedback: Optional[Dict[str, Any]] = None
    explanation: Optional[str] = None
    answered_question: Optional[Dict[str, Any]] = None
    selected_answer_text: Optional[str] = None

    next_index = submitted_index

    if selected_option_param is not None:
        if not questions:
            feedback = {
                "message": "Для этой викторины пока нет вопросов.",
                "status": "warning",
                "is_correct": False,
            }
        else:
            answered_question = questions[submitted_index]
            selected_option = next(
                (
                    option
                    for option in answered_question.get("options") or []
                    if str(option.get("id")) == str(selected_option_param)
                ),
                None,
            )

            if selected_option:
                selected_answer_text = selected_option.get("text")
                is_correct = bool(selected_option.get("is_correct"))
                _register_team_answer(
                    team_progress,
                    user_id,
                    answered_question.get("id"),
                    is_correct=is_correct,
                )
                feedback = {
                    "message": "Правильный ответ! Отличная работа." if is_correct else "Неправильный ответ. Попробуйте следующий вопрос!",
                    "status": "success" if is_correct else "danger",
                    "is_correct": is_correct,
                }
                explanation = answered_question.get("explanation")
            else:
                feedback = {
                    "message": "Не удалось определить выбранный вариант ответа.",
                    "status": "warning",
                    "is_correct": False,
                }

        next_index = min(submitted_index + 1, total_questions)

    quiz_finished = total_questions == 0 or next_index >= total_questions
    current_question: Optional[Dict[str, Any]] = None
    answers: List[Dict[str, Any]] = []
    team_scoreboard: List[Dict[str, Any]] = []
    winning_team: Optional[Dict[str, Any]] = None

    if not quiz_finished and questions:
        current_question = questions[next_index]
        answers = (current_question.get("options") or [])

    team_members_total = len(team_progress.get("member_ids") or [])
    team_members_completed = len(team_progress.get("completed_members") or [])
    team_waiting_for_members = False
    team_waiting_message: Optional[str] = None
    team_status_poll_url: Optional[str] = None

    if quiz_finished:
        if _mark_player_completed(team_progress, user_id):
            team_members_completed = len(team_progress.get("completed_members") or [])

        team_completed = await _finalize_team_if_ready(match_id, team_progress, team_id)
        team_waiting_for_members = not team_completed

        if team_waiting_for_members:
            team_waiting_message = (
                "Вы завершили викторину. Ожидайте, пока все участники команды закончат, чтобы увидеть общий результат."
            )
            if team_members_total:
                status_url = request.url_for("game_status", match_id=match_id)
                query = urlencode({"team_id": team_id, "user_id": user_id})
                team_status_poll_url = f"{status_url}?{query}" if query else str(status_url)
        else:
            team_scoreboard = await _fetch_team_scoreboard(match_id, quiz.get("id"))
            team_score_value = team_progress.get("team_score")
            if team_score_value is not None:
                normalized_team_id = _normalize_identifier(team_id)
                found = any(
                    _normalize_identifier(entry.get("team_id")) == normalized_team_id
                    for entry in team_scoreboard
                )
                if not found:
                    team_scoreboard.append(
                        {
                            "team_id": normalized_team_id,
                            "team_name": team_with_members.get("name") or normalized_team_id,
                            "score": team_score_value,
                            "time_taken": team_progress.get("time_taken"),
                        }
                    )
                    team_scoreboard.sort(
                        key=lambda item: (
                            -(item.get("score") or 0),
                            item.get("time_taken") if item.get("time_taken") is not None else float("inf"),
                            item.get("team_name") or "",
                        )
                    )

            if team_scoreboard:
                winning_team = team_scoreboard[0]
    else:
        team_waiting_for_members = False

    context = {
        "request": request,
        "match_id": match_id,
        "quiz": quiz,
        "questions": questions,
        "question": current_question,
        "answers": answers,
        "question_index": next_index,
        "total_questions": total_questions,
        "current_question_number": next_index + 1 if current_question else total_questions,
        "feedback": feedback,
        "answered_question": answered_question,
        "selected_answer_text": selected_answer_text,
        "explanation": explanation,
        "quiz_finished": quiz_finished,
        "team_scoreboard": team_scoreboard,
        "winning_team": winning_team,
        "team_waiting_for_members": team_waiting_for_members,
        "team_waiting_message": team_waiting_message,
        "team_members_total": team_members_total,
        "team_members_completed": team_members_completed,
        "team_status_poll_url": team_status_poll_url,
        "team_id": team_id,
        "current_user_id": user_id,
    }
    return templates.TemplateResponse("game.html", context)
