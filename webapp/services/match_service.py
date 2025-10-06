import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from webapp.services.supabase_client import _supabase_request
from webapp.services.team_service import _normalize_identifier
from webapp.utils.cache import (
    MATCH_STATUS_CACHE,
    MATCH_TEAM_CACHE,
    MATCH_QUIZ_CACHE,
    TEAM_PROGRESS_CACHE,
    TEAM_READY_CACHE,
)


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

        extra_fields: Dict[str, Any] = {}
        for key in (
            "status",
            "team_status",
            "team_completed",
            "completed",
            "finished",
            "start_time",
            "team_start_time",
        ):
            if key in team:
                extra_fields[key] = team.get(key)

        statuses.append({"id": team_id, "name": team_name, "ready": ready, **extra_fields})

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

    cached_team_ids = MATCH_TEAM_CACHE.setdefault(match_id, set())
    your_team_id = _normalize_identifier(fallback_team.get("id")) if fallback_team else None

    response_teams: list[dict[str, Any]] = []
    for status in statuses:
        team_id = status.get("id")
        if team_id:
            cached_team_ids.add(team_id)

        team_payload: Dict[str, Any] = {
            "id": team_id,
            "name": status.get("name") or team_id,
            "ready": bool(status.get("ready")),
            "is_yours": bool(your_team_id and team_id == your_team_id),
        }

        for extra_key in ("status", "team_status", "team_completed", "completed", "finished"):
            if extra_key in status:
                team_payload[extra_key] = status.get(extra_key)

        response_teams.append(team_payload)

    def _extract_first(source: Optional[Dict[str, Any]], keys: Tuple[str, ...]) -> Any:
        if not source:
            return None
        for key in keys:
            if key in source:
                return source.get(key)
        return None

    def _coerce_bool(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if not normalized:
                return None
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off"}:
                return False
        return None

    def _normalize_status(value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        if isinstance(value, str):
            return value.strip().lower() or None
        return str(value)

    fallback_team_completed = _coerce_bool(
        _extract_first(
            fallback_team,
            ("team_completed", "teamCompleted", "completed", "finished"),
        )
    )
    fallback_match_completed = _coerce_bool(
        _extract_first(
            fallback_team,
            ("match_team_completed", "matchTeamCompleted"),
        )
    )
    fallback_team_status = _normalize_status(
        _extract_first(
            fallback_team,
            ("team_status", "teamStatus", "status"),
        )
    )
    fallback_match_status = _normalize_status(
        _extract_first(
            fallback_team,
            ("match_status", "matchStatus"),
        )
    )
    fallback_results_url = _extract_first(
        fallback_team,
        ("results_url", "resultsUrl"),
    )
    fallback_redirect_url = _extract_first(
        fallback_team,
        ("redirect", "redirect_url", "redirectUrl"),
    )

    team_progress = None
    if match_id and your_team_id:
        match_progress = TEAM_PROGRESS_CACHE.get(match_id)
        if isinstance(match_progress, dict):
            team_progress = match_progress.get(your_team_id)

    cached_team_completed = None
    if isinstance(team_progress, dict):
        cached_team_completed = _coerce_bool(team_progress.get("team_completed"))
        if cached_team_completed is None and team_progress.get("team_completed") is not None:
            cached_team_completed = bool(team_progress.get("team_completed"))

    team_completed = (
        fallback_team_completed
        if fallback_team_completed is not None
        else cached_team_completed
    )

    team_started = False
    if isinstance(team_progress, dict) and team_progress.get("team_start_time"):
        team_started = True
    elif fallback_team and fallback_team.get("start_time"):
        team_started = True

    match_status_value = fallback_match_status
    if not match_status_value:
        if team_completed:
            match_status_value = "finished"
        elif team_started:
            match_status_value = "started"
        elif all_ready:
            match_status_value = "started"
        else:
            match_status_value = "waiting"

    team_status_value = fallback_team_status
    if not team_status_value:
        if team_completed:
            team_status_value = "finished"
        elif team_started:
            team_status_value = "started"
        elif fallback_team and fallback_team.get("ready"):
            team_status_value = "ready"
        else:
            team_status_value = "waiting"

    response: dict[str, Any] = {
        "status": match_status_value,
        "teams": response_teams,
        "match_id": match_id,
    }

    if team_status_value:
        response["team_status"] = team_status_value

    if team_completed is not None:
        response["team_completed"] = bool(team_completed)

    if fallback_match_completed is not None:
        response["match_team_completed"] = bool(fallback_match_completed)

    if fallback_results_url:
        response["results_url"] = fallback_results_url

    redirect_candidate = fallback_redirect_url
    if not redirect_candidate and (team_started or all_ready):
        redirect_candidate = f"/game/{match_id}"

    should_redirect = (
        redirect_candidate
        and match_status_value == "started"
        and not bool(team_completed)
        and not bool(fallback_match_completed)
    )

    response["redirect"] = redirect_candidate if should_redirect else None

    MATCH_STATUS_CACHE[match_id] = response
    return response
