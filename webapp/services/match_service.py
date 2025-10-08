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


TEAM_WAITING_MESSAGE = "🏁 Ваша команда завершила игру. Ожидаем вторую команду…"


def _summarize_match_result(
    teams: List[Dict[str, Any]],
    match_progress_map: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a lightweight summary of the final match result."""

    scoreboard: List[Dict[str, Any]] = []

    for team in teams:
        if not isinstance(team, dict):
            continue

        raw_team_id = team.get("id")
        team_id = _normalize_identifier(raw_team_id)
        if not team_id:
            continue

        progress = match_progress_map.get(team_id) or {}

        score_raw = progress.get("team_score")
        try:
            score_value = int(score_raw)
        except (TypeError, ValueError):
            score_value = 0

        time_taken_raw = progress.get("time_taken")
        try:
            time_taken_value = float(time_taken_raw)
        except (TypeError, ValueError):
            time_taken_value = None

        name = team.get("name") or (team_id if isinstance(team_id, str) else "Команда")

        scoreboard.append(
            {
                "team_id": team_id,
                "team_name": name,
                "score": score_value,
                "time_taken": time_taken_value,
            }
        )

    scoreboard.sort(
        key=lambda item: (
            -(item.get("score") or 0),
            item.get("time_taken") if item.get("time_taken") is not None else float("inf"),
            item.get("team_name") or "",
        )
    )

    summary: Dict[str, Any] = {"scoreboard": scoreboard}
    if not scoreboard:
        return summary

    top_score = scoreboard[0].get("score") or 0
    winners = [entry for entry in scoreboard if (entry.get("score") or 0) == top_score]

    if len(winners) == 1:
        winner = winners[0]
        runner_score = scoreboard[1].get("score") if len(scoreboard) > 1 else 0
        score_text = f"{winner.get('score', 0)}:{runner_score or 0}"
        winner_name = winner.get("team_name") or winner.get("team_id") or "Команда"
        message = (
            "✅ Игра завершена. Результат: команда "
            f"«{winner_name}» победила со счётом {score_text}."
        )
        summary.update(
            {
                "winner": winner,
                "runner_score": runner_score or 0,
                "score_text": score_text,
                "message": message,
            }
        )
        return summary

    winner_names = ", ".join(
        f"«{entry.get('team_name') or entry.get('team_id') or 'Команда'}»" for entry in winners
    )
    message = (
        "✅ Игра завершена. Результат: ничья — "
        f"{winner_names} набрали по {top_score} очков."
    )
    summary.update({"score_text": None, "message": message, "winner": None})
    return summary


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

    normalized_match_id = _normalize_identifier(match_id)
    match_progress_map = TEAM_PROGRESS_CACHE.get(normalized_match_id or match_id) or {}
    completed_flags: Dict[str, bool] = {}
    for progress_team_id, progress in match_progress_map.items():
        normalized_team_id = _normalize_identifier(progress_team_id)
        if not normalized_team_id:
            continue
        if isinstance(progress, dict) and progress.get("team_completed"):
            completed_flags[normalized_team_id] = True

    cached_team_ids = MATCH_TEAM_CACHE.setdefault(match_id, set())
    your_team_id = _normalize_identifier(fallback_team.get("id")) if fallback_team else None

    response_teams: list[dict[str, Any]] = []
    for status in statuses:
        team_id = status.get("id")
        if team_id:
            cached_team_ids.add(team_id)

        normalized_team_id = _normalize_identifier(team_id)
        if normalized_team_id:
            cached_team_ids.add(normalized_team_id)
        team_completed = bool(normalized_team_id and completed_flags.get(normalized_team_id))

        team_entry: dict[str, Any] = {
            "id": team_id,
            "name": status.get("name") or team_id,
            "ready": bool(status.get("ready")),
            "is_yours": bool(your_team_id and team_id == your_team_id),
        }

        if team_completed:
            team_entry["team_completed"] = True
            team_entry["status"] = "finished"

        response_teams.append(team_entry)

    response: dict[str, Any] = {
        "status": "ready" if all_ready else "waiting",
        "teams": response_teams,
        "match_id": match_id,
    }

    your_team_completed = bool(your_team_id and completed_flags.get(your_team_id))

    relevant_team_ids = {
        _normalize_identifier(status.get("id"))
        for status in statuses
        if _normalize_identifier(status.get("id"))
    }

    cached_normalized_team_ids = {
        _normalize_identifier(team_id)
        for team_id in cached_team_ids
        if _normalize_identifier(team_id)
    }

    progress_team_ids = {
        _normalize_identifier(team_id)
        for team_id in match_progress_map.keys()
        if _normalize_identifier(team_id)
    }

    if not relevant_team_ids:
        relevant_team_ids = cached_normalized_team_ids or progress_team_ids

    if not relevant_team_ids and your_team_id:
        relevant_team_ids = {your_team_id}

    all_teams_completed = bool(relevant_team_ids) and all(
        bool(completed_flags.get(team_id)) for team_id in relevant_team_ids
    )

    if your_team_completed:
        response["team_status"] = "finished"
        response["team_completed"] = True

    if all_teams_completed:
        response["status"] = "finished"
        summary = _summarize_match_result(response_teams, match_progress_map)
        if summary.get("message"):
            response["message"] = summary["message"]
        response["result_summary"] = summary
        response.pop("redirect", None)
    elif all_ready and not your_team_completed:
        response["redirect"] = f"/game/{match_id}"
    elif all_ready:
        response["status"] = "started"
    elif your_team_completed:
        response["message"] = TEAM_WAITING_MESSAGE
        response.pop("redirect", None)

    MATCH_STATUS_CACHE[match_id] = response
    return response