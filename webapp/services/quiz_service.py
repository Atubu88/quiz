import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from fastapi import HTTPException, status

from webapp.services.match_service import _collect_match_team_statuses
from webapp.services.supabase_client import _fetch_active_quiz, _fetch_single_record, _supabase_request
from webapp.services.team_service import _fetch_team_members, _normalize_identifier
from webapp.utils.cache import MATCH_CACHE, QUIZ_CACHE, TEAM_PROGRESS_CACHE


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            if candidate.endswith("Z"):
                candidate = candidate[:-1] + "+00:00"
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    return None


async def _load_quiz_into_cache(team_id: str) -> Dict[str, Any]:
    quiz_payload = await _fetch_active_quiz()
    QUIZ_CACHE[team_id] = quiz_payload
    return quiz_payload


async def _ensure_team_progress(
    match_id: str,
    team: Dict[str, Any],
    quiz_id: Any = None,
) -> Dict[str, Any]:
    """Готовит структуру для отслеживания прогресса команды в викторине."""

    normalized_match_id = _normalize_identifier(match_id)
    normalized_team_id = _normalize_identifier(team.get("id"))

    if not normalized_match_id or not normalized_team_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Некорректный идентификатор матча или команды")

    match_progress = TEAM_PROGRESS_CACHE.setdefault(normalized_match_id, {})
    team_progress = match_progress.get(normalized_team_id)

    members: List[Dict[str, Any]] = []
    if isinstance(team.get("members"), list):
        members = list(team["members"])
    else:
        try:
            members = await _fetch_team_members(normalized_team_id)
        except HTTPException:
            members = []
    member_ids = {str(m.get("id")) for m in members if m.get("id") is not None}

    if not team_progress:
        team_progress = {
            "team_id": normalized_team_id,
            "match_id": normalized_match_id,
            "quiz_id": quiz_id,
            "member_ids": member_ids,
            "answers": {},
            "completed_members": set(),
            "team_completed": False,
            "team_score": None,
            "team_start_time": team.get("start_time"),
            "time_taken": None,
        }
        match_progress[normalized_team_id] = team_progress
    else:
        if quiz_id not in (None, "") and team_progress.get("quiz_id") in (None, ""):
            team_progress["quiz_id"] = quiz_id
        if member_ids:
            team_progress["member_ids"] = member_ids
        if not team_progress.get("team_start_time") and team.get("start_time"):
            team_progress["team_start_time"] = team.get("start_time")

    return team_progress


def _ensure_player_progress_entry(team_progress: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    players: Dict[str, Dict[str, Any]] = team_progress.setdefault("answers", {})
    key = str(user_id)
    entry = players.get(key)
    if not entry:
        entry = {"score": 0, "answered_questions": set(), "completed": False}
        players[key] = entry
    else:
        answered = entry.get("answered_questions")
        if not isinstance(answered, set):
            entry["answered_questions"] = set(answered or [])
    return entry


def _register_team_answer(
    team_progress: Dict[str, Any],
    user_id: int,
    question_id: Any,
    *,
    is_correct: bool,
) -> None:
    if question_id in (None, ""):
        return

    entry = _ensure_player_progress_entry(team_progress, user_id)
    answered_questions: Set[str] = entry.setdefault("answered_questions", set())
    question_key = str(question_id)
    if question_key in answered_questions:
        return

    answered_questions.add(question_key)
    if is_correct:
        entry["score"] = int(entry.get("score", 0)) + 1


def _mark_player_completed(team_progress: Dict[str, Any], user_id: int) -> bool:
    entry = _ensure_player_progress_entry(team_progress, user_id)
    if entry.get("completed"):
        return False

    entry["completed"] = True
    completed: Set[str] = team_progress.setdefault("completed_members", set())
    completed.add(str(user_id))
    return True


async def _upsert_team_result(
    team_id: str,
    quiz_id: Any,
    score: int,
    *,
    time_taken: Optional[float] = None,
) -> None:
    payload: Dict[str, Any] = {
        "team_id": team_id,
        "quiz_id": quiz_id,
        "score": score,
    }
    if time_taken is not None:
        payload["time_taken"] = time_taken

    existing = await _fetch_single_record(
        "team_results",
        {"team_id": f"eq.{team_id}", "quiz_id": f"eq.{quiz_id}"},
        select="id",
    )

    if existing and existing.get("id"):
        await _supabase_request(
            "PATCH",
            "team_results",
            params={"id": f"eq.{existing['id']}"},
            json_payload=payload,
            prefer="return=representation",
        )
    else:
        await _supabase_request(
            "POST",
            "team_results",
            json_payload=payload,
            prefer="return=representation",
        )


async def _finalize_team_if_ready(
    match_id: str,
    team_progress: Dict[str, Any],
    team_id: str,
) -> bool:
    if team_progress.get("team_completed"):
        return True

    member_ids: Set[str] = set(team_progress.get("member_ids") or set())
    completed_members: Set[str] = set(team_progress.get("completed_members") or set())

    if not member_ids:
        return False

    if not member_ids.issubset(completed_members):
        return False

    if team_progress.get("finalizing"):
        return bool(team_progress.get("team_completed"))

    team_progress["finalizing"] = True
    try:
        total_score = sum(int(entry.get("score", 0)) for entry in team_progress.get("answers", {}).values())
        team_progress["team_score"] = total_score

        quiz_id = team_progress.get("quiz_id")
        start_dt = _parse_iso_datetime(team_progress.get("team_start_time"))
        time_taken: Optional[float] = None
        if start_dt:
            time_taken = max((datetime.now(timezone.utc) - start_dt).total_seconds(), 0.0)
            team_progress["time_taken"] = time_taken

        if quiz_id not in (None, ""):
            try:
                await _upsert_team_result(team_id, quiz_id, total_score, time_taken=time_taken)
            except HTTPException as exc:
                logging.warning(
                    "Failed to store team result for %s in match %s: %s",
                    team_id,
                    match_id,
                    exc.detail,
                )
            except Exception as exc:  # pragma: no cover - defensive logging
                logging.exception(
                    "Unexpected error while storing team result for %s in match %s: %s",
                    team_id,
                    match_id,
                    exc,
                )

        team_progress["team_completed"] = True
    finally:
        team_progress.pop("finalizing", None)

    return bool(team_progress.get("team_completed"))


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
