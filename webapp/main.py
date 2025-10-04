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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
MATCH_CACHE: dict[str, Dict[str, Any]] = {}
MATCH_STATUS_CACHE: dict[str, Dict[str, Any]] = {}
MATCH_TEAM_CACHE: dict[str, Set[str]] = {}
TEAM_READY_CACHE: dict[str, bool] = {}

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
_matches_ready: dict[str, list[str]] = {}

# Кэш для матчей и их викторин
MATCH_QUIZ_CACHE: dict[str, str] = {}


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

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.request(method, url, params=params, json=json_payload, headers=headers)
    except Exception as e:
        logging.exception("❌ Network error to Supabase: %s", e)
        # 502 только для сетевых ошибок
        raise HTTPException(status_code=502, detail=f"Supabase network error: {str(e)}")

    # ЛОГИРУЕМ ВСЁ
    logging.debug(
        "Supabase [%s %s] %s -> %s\nparams=%s\npayload=%s\nresp=%s",
        method, path, response.status_code, url, params, json_payload, response.text
    )

    # Пробрасываем ИСХОДНЫЙ статус Supabase + текст
    if response.status_code >= 400:
        # Пытаемся вытащить json, иначе отдаём сырой текст
        try:
            detail = response.json()
        except ValueError:
            detail = {"message": response.text}
        raise HTTPException(
            status_code=response.status_code,
            detail={
                "source": "Supabase",
                "status": response.status_code,
                "path": path,
                "params": params,
                "payload": json_payload,
                "detail": detail,
            },
        )

    if response.status_code == status.HTTP_204_NO_CONTENT:
        return None

    try:
        return response.json()
    except ValueError:
        # бывает пустой ответ/текст; возвращаем как есть
        return response.text



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


async def _fetch_team_members(team_id: str) -> List[Dict[str, Any]]:
    """Возвращает список участников команды с данными пользователя (без join в select)."""
    rows = await _supabase_request(
        "GET",
        "team_members",
        params={
            "team_id": f"eq.{team_id}",
            "select": "id,user_id,is_captain,joined_at",
            "order": "joined_at.asc",
        },
    ) or []

    user_ids = sorted({r.get("user_id") for r in rows if r.get("user_id") is not None})
    users_map: Dict[int, Dict[str, Any]] = {}

    if user_ids:
        in_list = ",".join(str(uid) for uid in user_ids)
        users = await _supabase_request(
            "GET",
            "users",
            params={"id": f"in.({in_list})", "select": "id,telegram_id,username,first_name,last_name"},
        ) or []
        users_map = {u["id"]: u for u in users}

    members: List[Dict[str, Any]] = []
    for r in rows:
        u = users_map.get(r["user_id"], {})
        name = " ".join(p for p in [u.get("first_name"), u.get("last_name")] if p).strip() or u.get("username") or "Без имени"
        members.append(
            {
                "id": u.get("id") or r["user_id"],
                "telegram_id": u.get("telegram_id"),
                "username": u.get("username"),
                "name": name,
                "is_captain": bool(r.get("is_captain")),
                "joined_at": r.get("joined_at"),
            }
        )
    return members


async def _fetch_team_with_members(team_id: str) -> Dict[str, Any]:
    team = await _ensure_team_exists(team_id)
    try:
        members = await _fetch_team_members(team_id)
    except HTTPException as e:
        logging.error("fetch_team_members failed: %s", e.detail)
        members = []
    return {**team, "members": members}


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


def _normalize_identifier(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _extract_match_id(team: Dict[str, Any]) -> Optional[str]:
    match_id = team.get("match_id")
    if isinstance(match_id, str) and match_id:
        return match_id
    return _normalize_identifier(team.get("id"))


async def _get_match_teams(
    match_id: Optional[str],
    fallback_team: Optional[Dict[str, Any]] = None,
    prefetched_teams: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    teams: List[Dict[str, Any]] = []

    if prefetched_teams:
        teams = prefetched_teams
    elif match_id:
        try:
            teams = await _supabase_request(
                "GET",
                "teams",
                params={
                    "match_id": f"eq.{match_id}",
                    "select": "id,name,ready,match_id",
                },
            ) or []
        except HTTPException as exc:
            logging.info("Failed to fetch teams for match %s: %s", match_id, exc.detail)
            teams = []

    if not teams and fallback_team:
        teams = [fallback_team]

    return teams


def _collect_match_team_statuses(teams: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    statuses: List[Dict[str, Any]] = []
    # матчу нужно минимум 2 команды
    all_ready = len(teams) >= 2

    for team in teams:
        team_id = _normalize_identifier(team.get("id"))
        team_name = team.get("name")
        cached_ready = TEAM_READY_CACHE.get(team_id) if team_id else None

        if cached_ready is None:
            ready = bool(team.get("ready"))
            if team_id:
                TEAM_READY_CACHE.setdefault(team_id, ready)
        else:
            ready = bool(cached_ready)

        statuses.append({"id": team_id, "name": team_name, "ready": ready})

        if not ready:
            all_ready = False

    return statuses, all_ready



async def _ensure_match_started(match_id: str, teams: List[Dict[str, Any]]) -> Dict[str, Any]:
    statuses, _ = _collect_match_team_statuses(teams)

    match_entry = MATCH_CACHE.get(match_id)
    if not match_entry:
        quiz_payload = QUIZ_CACHE.get(match_id)
        if quiz_payload is None:
            quiz_payload = await _fetch_active_quiz()
            QUIZ_CACHE[match_id] = quiz_payload

        match_entry = {
            "match_id": match_id,
            "teams": statuses,
            "redirect": f"/game/{match_id}",
        }
        MATCH_CACHE[match_id] = match_entry

    else:
        match_entry["teams"] = statuses

    if "started_at" not in match_entry:
        start_time = datetime.now(timezone.utc).isoformat()
        match_entry["started_at"] = start_time

        team_ids = [status["id"] for status in statuses if status.get("id")]
        for team_id in team_ids:
            try:
                await _supabase_request(
                    "PATCH",
                    "teams",
                    params={"id": f"eq.{team_id}"},
                    json_payload={"start_time": start_time},
                    prefer="return=representation",
                )
            except HTTPException as exc:
                logging.warning("Failed to update start_time for team %s: %s", team_id, exc.detail)

    match_entry["quiz"] = QUIZ_CACHE.get(match_id)
    return match_entry


async def _build_match_status_response(
    match_id: str,
    fallback_team: dict | None = None,
    prefetched_teams: Optional[List[Dict[str, Any]]] = None,
) -> dict:
    """
    Возвращает статус матча:
    - список команд с пометкой "готова/нет"
    - если все готовы → редирект на игру
    """

    if not match_id and fallback_team:
        match_id = fallback_team.get("match_id") or fallback_team.get("id")

    if not match_id:
        return {"status": "error", "message": "Не удалось определить матч"}

    teams = await _get_match_teams(match_id, fallback_team, prefetched_teams)
    statuses, all_ready = _collect_match_team_statuses(teams)

    cached_team_ids = MATCH_TEAM_CACHE.setdefault(match_id, set())
    your_team_id = _normalize_identifier(fallback_team.get("id")) if fallback_team else None

    response_teams: list[dict[str, Any]] = []
    for status in statuses:
        team_id = status.get("id")
        if team_id:
            cached_team_ids.add(team_id)

        response_teams.append(
            {
                "id": team_id,
                "name": status.get("name") or team_id,
                "ready": bool(status.get("ready")),
                "is_yours": bool(your_team_id and team_id == your_team_id),
            }
        )

    response: dict[str, Any] = {
        "status": "ready" if all_ready else "waiting",
        "teams": response_teams,
        "match_id": match_id,
    }

    if all_ready:
        response["redirect"] = f"/game/{match_id}"

    MATCH_STATUS_CACHE[match_id] = response
    return response


def _clear_team_from_caches(team: Dict[str, Any]) -> None:
    team_id = _normalize_identifier(team.get("id"))
    match_id = _extract_match_id(team)

    if team_id:
        TEAM_READY_CACHE.pop(team_id, None)
        QUIZ_CACHE.pop(team_id, None)

    if not match_id:
        return

    teams = MATCH_TEAM_CACHE.get(match_id)
    if teams and team_id:
        teams.discard(team_id)
        if not teams:
            MATCH_TEAM_CACHE.pop(match_id, None)
            MATCH_STATUS_CACHE.pop(match_id, None)
            MATCH_CACHE.pop(match_id, None)
            QUIZ_CACHE.pop(match_id, None)

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
    if all_ready and match_id not in MATCH_QUIZ_CACHE:
        try:
            quizzes = await _supabase_request("GET", "quizzes", params={"limit": 1})
            if quizzes:
                quiz_id = quizzes[0]["id"]
                MATCH_QUIZ_CACHE[match_id] = quiz_id
                logging.info(f"Match {match_id} assigned quiz {quiz_id}")
            else:
                raise HTTPException(404, "No quizzes found in database")
        except Exception as e:
            logging.error(f"Failed to fetch quiz for match {match_id}: {e}")
            raise HTTPException(500, "Unable to assign quiz")

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
    quiz_id = MATCH_QUIZ_CACHE.get(match_id)
    if not quiz_id:
        raise HTTPException(404, detail="Quiz not found for this match")

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
    current_question = questions[0] if questions else None
    answers = (current_question.get("options") if current_question else None) or []

    context = {
        "request": request,
        "match_id": match_id,
        "quiz": quiz,
        "questions": questions,
        "question": current_question,
        "answers": answers,
    }
    return templates.TemplateResponse("game.html", context)


def _extract_match_id(team: dict):
    return team.get("match_id") or team.get("id")

def _mark_team_ready(match_id: str, team_name: str):
    """Отмечаем команду как готовую в кэше"""
    if match_id not in _matches_ready:
        _matches_ready[match_id] = []
    if team_name not in _matches_ready[match_id]:
        _matches_ready[match_id].append(team_name)