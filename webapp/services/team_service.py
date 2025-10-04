import logging
from typing import Any, Dict, List, Optional, Set

from fastapi import HTTPException

from webapp.services.supabase_client import _fetch_single_record, _supabase_request
from webapp.utils.cache import (
    MATCH_CACHE,
    MATCH_STATUS_CACHE,
    MATCH_TEAM_CACHE,
    QUIZ_CACHE,
    TEAM_PROGRESS_CACHE,
    TEAM_READY_CACHE,
)


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


async def _ensure_team_exists(team_id: str) -> Dict[str, Any]:
    team = await _fetch_single_record("teams", {"id": f"eq.{team_id}"})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team


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
        user_id = r.get("user_id")
        u = users_map.get(user_id, {})
        name = (
            " ".join(p for p in [u.get("first_name"), u.get("last_name")] if p).strip()
            or u.get("username")
            or "Без имени"
        )
        members.append(
            {
                "id": u.get("id") or user_id,
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


def _clear_team_from_caches(team: Dict[str, Any]) -> None:
    team_id = _normalize_identifier(team.get("id"))
    match_id = _extract_match_id(team)

    if team_id:
        TEAM_READY_CACHE.pop(team_id, None)
        QUIZ_CACHE.pop(team_id, None)
        for match_progress in TEAM_PROGRESS_CACHE.values():
            if isinstance(match_progress, dict):
                match_progress.pop(team_id, None)

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
            TEAM_PROGRESS_CACHE.pop(match_id, None)


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
