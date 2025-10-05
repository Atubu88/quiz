from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from webapp.main import _fetch_team_scoreboard, _validate_init_data, templates
from webapp.services.match_service import _ensure_match_quiz_assigned
from webapp.services.quiz_service import (
    _ensure_player_progress_entry,
    _ensure_team_progress,
    _finalize_team_if_ready,
    _mark_player_completed,
    _register_team_answer,
)
from webapp.services.supabase_client import _supabase_request
from webapp.services.team_service import (
    _extract_match_id,
    _fetch_team_with_members,
    _normalize_identifier,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def read_root(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@router.get("/debug/init")
async def debug_init(initData: str) -> Dict[str, Any]:
    parsed = _validate_init_data(initData)
    return {"parsed": parsed}


@router.get("/game/status/{match_id}")
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

        quiz_id = team_progress.get("quiz_id")
        if quiz_id not in (None, ""):
            _, all_results_reported = await _fetch_team_scoreboard(match_id, quiz_id)
            response["all_teams_completed"] = all_results_reported
        else:
            response["all_teams_completed"] = False

    return response


@router.get("/game/{match_id}", response_class=HTMLResponse)
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
    answered_question: Optional[Dict[str, Any]] = None
    selected_answer_text: Optional[str] = None
    explanation: Optional[str] = None
    feedback: Optional[Dict[str, Any]] = None

    next_index = submitted_index

    if selected_option_param is not None:
        if submitted_index >= total_questions:
            feedback = {
                "message": "Вы уже прошли все вопросы этой викторины.",
                "status": "info",
                "is_correct": None,
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
    waiting_for_other_teams = False
    waiting_for_other_teams_message: Optional[str] = None

    if quiz_finished:
        if _mark_player_completed(team_progress, user_id):
            team_members_completed = len(team_progress.get("completed_members") or [])

        team_completed = await _finalize_team_if_ready(match_id, team_progress, team_id)
        team_waiting_for_members = not team_completed

        if team_waiting_for_members:
            team_waiting_message = (
                "Вы завершили викторину. Ожидайте, пока все участники команды закончат, чтобы увидеть общий результат."
            )
            status_url = request.url_for("game_status", match_id=match_id)
            query = urlencode({"team_id": team_id, "user_id": user_id})
            team_status_poll_url = f"{status_url}?{query}" if query else str(status_url)
        else:
            team_scoreboard_data, all_teams_completed = await _fetch_team_scoreboard(
                match_id, quiz.get("id")
            )

            if not all_teams_completed:
                waiting_for_other_teams = True
                waiting_for_other_teams_message = (
                    "Ожидайте, пока все команды завершат игру, чтобы увидеть результаты."
                )
                status_url = request.url_for("game_status", match_id=match_id)
                query = urlencode({"team_id": team_id, "user_id": user_id})
                team_status_poll_url = f"{status_url}?{query}" if query else str(status_url)
            else:
                team_scoreboard = team_scoreboard_data
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
                                item.get("time_taken")
                                if item.get("time_taken") is not None
                                else float("inf"),
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
        "waiting_for_other_teams": waiting_for_other_teams,
        "waiting_for_other_teams_message": waiting_for_other_teams_message,
        "team_id": team_id,
        "current_user_id": user_id,
    }
    return templates.TemplateResponse("game.html", context)
