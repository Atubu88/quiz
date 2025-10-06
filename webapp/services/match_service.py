import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from webapp.services.supabase_client import _fetch_single_record, _supabase_request
from webapp.services.team_service import _normalize_identifier
from webapp.utils.cache import (
    MATCH_STATUS_CACHE,
    MATCH_TEAM_CACHE,
    MATCH_QUIZ_CACHE,
    TEAM_READY_CACHE,
)


def _normalize_bool_flag(value: Any) -> Optional[bool]:
    """Convert various truthy/falsy representations to bool."""

    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "t", "1", "yes", "y"}:
            return True
        if lowered in {"false", "f", "0", "no", "n"}:
            return False
    return None


def _normalize_status_value(value: Any) -> Optional[str]:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return None


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

        raw_team_status = team.get("team_status") or team.get("status")
        team_status = _normalize_status_value(raw_team_status)

        team_completed = _normalize_bool_flag(team.get("team_completed"))
        if team_completed is None:
            team_completed = _normalize_bool_flag(team.get("completed"))
        if team_completed is None:
            team_completed = _normalize_bool_flag(team.get("finished"))

        statuses.append(
            {
                "id": team_id,
                "name": team_name,
                "ready": ready,
                "status": team_status,
                "team_status": team_status,
                "team_completed": team_completed,
                "is_yours": bool(team.get("is_yours")),
            }
        )

        if not ready:
            all_ready = False

    return statuses, all_ready


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


async def _ensure_match_quiz_assigned(match_id: str) -> str:
    """Return quiz id for a match, fetching it from Supabase if needed."""

    quiz_id = MATCH_QUIZ_CACHE.get(match_id)
    if quiz_id:
        return quiz_id

    try:
        teams = await _supabase_request(
            "GET",
            "teams",
            params={
                "match_id": f"eq.{match_id}",
                "select": "id,quiz_id",
            },
        ) or []
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive logging
        logging.error("Failed to load teams for match %s: %s", match_id, exc)
        raise HTTPException(500, detail="Unable to assign quiz") from exc

    for team in teams:
        team_quiz_id = team.get("quiz_id") if isinstance(team, dict) else None
        if team_quiz_id not in (None, ""):
            quiz_id = team_quiz_id
            MATCH_QUIZ_CACHE[match_id] = quiz_id
            logging.info("Match %s reused quiz %s from team %s", match_id, quiz_id, team.get("id"))
            return quiz_id

    try:
        quizzes = await _supabase_request(
            "GET",
            "quizzes",
            params={"limit": 1, "order": "id.asc"},
        )
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive logging
        logging.error("Failed to load quiz for match %s: %s", match_id, exc)
        raise HTTPException(500, detail="Unable to assign quiz") from exc

    if not quizzes:
        raise HTTPException(404, detail="Quiz not found in database")

    quiz_id = quizzes[0].get("id")
    if not quiz_id:
        logging.error("Supabase returned quiz without id for match %s", match_id)
        raise HTTPException(500, detail="Unable to assign quiz")

    MATCH_QUIZ_CACHE[match_id] = quiz_id
    logging.info("Match %s assigned quiz %s", match_id, quiz_id)
    return quiz_id


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

    previous_response = MATCH_STATUS_CACHE.get(match_id) or {}

    match_record: Optional[Dict[str, Any]] = None
    try:
        match_record = await _fetch_single_record("matches", {"id": f"eq.{match_id}"})
    except HTTPException as exc:
        if exc.status_code != 404:
            logging.info("Failed to fetch match %s: %s", match_id, exc.detail)
        match_record = None
    except Exception as exc:  # pragma: no cover - defensive logging
        logging.exception("Unexpected error while fetching match %s: %s", match_id, exc)
        match_record = None

    match_status_value = _normalize_status_value(previous_response.get("status"))
    results_url = previous_response.get("results_url")
    new_game_url = previous_response.get("new_game_url")
    team_status_value = _normalize_status_value(previous_response.get("team_status"))
    team_completed_flag = _normalize_bool_flag(previous_response.get("team_completed"))

    if match_record:
        record_status = _normalize_status_value(match_record.get("status"))
        if record_status:
            match_status_value = record_status

        record_team_status = _normalize_status_value(
            match_record.get("team_status") or match_record.get("team_state")
        )
        if record_team_status:
            team_status_value = record_team_status

        record_team_completed = _normalize_bool_flag(match_record.get("team_completed"))
        if record_team_completed is None:
            record_team_completed = _normalize_bool_flag(match_record.get("team_finished"))
        if record_team_completed is not None:
            team_completed_flag = record_team_completed

        if match_record.get("results_url") is not None:
            results_url = match_record.get("results_url")
        if match_record.get("new_game_url") is not None:
            new_game_url = match_record.get("new_game_url")

    if fallback_team:
        fallback_team_status = _normalize_status_value(
            fallback_team.get("team_status") or fallback_team.get("status")
        )
        if fallback_team_status:
            team_status_value = fallback_team_status

        fallback_team_completed = _normalize_bool_flag(fallback_team.get("team_completed"))
        if fallback_team_completed is None:
            fallback_team_completed = _normalize_bool_flag(fallback_team.get("completed"))
        if fallback_team_completed is None:
            fallback_team_completed = _normalize_bool_flag(fallback_team.get("finished"))
        if fallback_team_completed is not None:
            team_completed_flag = fallback_team_completed

    cached_team_ids = MATCH_TEAM_CACHE.setdefault(match_id, set())
    your_team_id = _normalize_identifier(fallback_team.get("id")) if fallback_team else None

    response_teams: list[dict[str, Any]] = []
    for status in statuses:
        team_id = status.get("id")
        if team_id:
            cached_team_ids.add(team_id)

        is_yours = bool(status.get("is_yours")) or bool(your_team_id and team_id == your_team_id)

        response_teams.append(
            {
                "id": team_id,
                "name": status.get("name") or team_id,
                "ready": bool(status.get("ready")),
                "is_yours": is_yours,
                "status": status.get("status"),
                "team_status": status.get("team_status"),
                "team_completed": status.get("team_completed"),
            }
        )

        if is_yours:
            status_team_status = _normalize_status_value(status.get("team_status") or status.get("status"))
            if status_team_status:
                team_status_value = status_team_status

            status_team_completed = _normalize_bool_flag(status.get("team_completed"))
            if status_team_completed is not None:
                team_completed_flag = status_team_completed

    if team_status_value is None and team_completed_flag:
        team_status_value = "finished"

    team_completed_bool = bool(team_completed_flag) if team_completed_flag is not None else False

    match_status_lower = match_status_value.lower() if isinstance(match_status_value, str) else ""
    team_status_lower = team_status_value.lower() if isinstance(team_status_value, str) else ""

    match_finished = match_status_lower == "finished"
    team_finished = team_completed_bool or team_status_lower == "finished"

    computed_status: Optional[str] = None
    if match_status_value:
        computed_status = match_status_value
    if team_finished:
        computed_status = "finished"
    if not computed_status:
        computed_status = "ready" if all_ready else "waiting"

    response: dict[str, Any] = {
        "status": computed_status,
        "teams": response_teams,
        "match_id": match_id,
    }

    if team_status_value:
        response["team_status"] = team_status_value
    elif "team_status" in previous_response:
        response["team_status"] = previous_response.get("team_status")

    if team_completed_flag is not None:
        response["team_completed"] = team_completed_bool
    elif "team_completed" in previous_response:
        response["team_completed"] = previous_response.get("team_completed")

    if results_url is not None:
        response["results_url"] = results_url
    if new_game_url is not None:
        response["new_game_url"] = new_game_url

    should_redirect = False
    if match_status_value:
        should_redirect = match_status_lower == "started"
    else:
        should_redirect = all_ready

    if should_redirect and not match_finished and not team_finished:
        response["redirect"] = f"/game/{match_id}"
    else:
        response["redirect"] = None

    MATCH_STATUS_CACHE[match_id] = response
    return response
