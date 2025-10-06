from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from webapp.main import (
    CreateTeamRequest,
    DeleteTeamRequest,
    JoinTeamRequest,
    LeaveTeamRequest,
    LoginRequest,
    SelectQuizRequest,
    StartTeamRequest,
    _add_team_member,
    _build_team_context,
    _delete_team,
    _ensure_user_exists,
    _fetch_team_member,
    _generate_unique_team_code,
    _get_or_create_user,
    _is_json_request,
    _parse_request_payload,
    _remove_team_member,
    _validate_init_data,
    templates,
)
from webapp.services.match_service import _build_match_status_response, _ensure_match_quiz_assigned
from webapp.services.supabase_client import (
    _fetch_quiz_options,
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
from webapp.utils.cache import MATCH_QUIZ_CACHE, MATCH_TEAM_CACHE, QUIZ_CACHE, TEAM_PROGRESS_CACHE, TEAM_READY_CACHE


async def _augment_team_context_with_quizzes(context: Dict[str, Any]) -> None:
    team = context.get("team") or {}
    match_id = team.get("match_id") or team.get("id")

    selected_quiz_id = team.get("quiz_id")
    if isinstance(selected_quiz_id, str) and not selected_quiz_id.strip():
        selected_quiz_id = None

    if not selected_quiz_id and match_id:
        cached_quiz_id = MATCH_QUIZ_CACHE.get(match_id)
        if cached_quiz_id not in (None, ""):
            selected_quiz_id = cached_quiz_id

    available_quizzes: List[Dict[str, Any]] = []
    quiz_error: Optional[str] = None

    if context.get("user_is_captain"):
        try:
            available_quizzes = await _fetch_quiz_options()
        except HTTPException as exc:
            quiz_error = exc.detail if isinstance(exc.detail, str) else "Не удалось загрузить список викторин"
        except Exception:
            quiz_error = "Не удалось загрузить список викторин"

    selected_quiz: Optional[Dict[str, Any]] = None
    if selected_quiz_id not in (None, ""):
        for quiz in available_quizzes:
            if str(quiz.get("id")) == str(selected_quiz_id):
                selected_quiz = quiz
                break
        if selected_quiz is None:
            try:
                selected_quiz = await _fetch_single_record(
                    "quizzes",
                    {"id": f"eq.{selected_quiz_id}"},
                    select="id,title",
                )
            except HTTPException:
                selected_quiz = None

    selected_quiz_id_str = None
    if selected_quiz_id not in (None, ""):
        selected_quiz_id_str = str(selected_quiz_id)

    context["available_quizzes"] = available_quizzes
    context["selected_quiz_id"] = selected_quiz_id
    context["selected_quiz_id_str"] = selected_quiz_id_str
    context["selected_quiz"] = selected_quiz
    if quiz_error:
        context["quiz_error"] = quiz_error


def _apply_team_completion_state(context: Dict[str, Any]) -> None:
    team = context.get("team")
    if not isinstance(team, dict):
        return

    normalized_team_id = _normalize_identifier(team.get("id"))
    if not normalized_team_id:
        return

    match_id = _normalize_identifier(_extract_match_id(team))

    team_progress = None
    if match_id:
        match_progress = TEAM_PROGRESS_CACHE.get(match_id) or {}
        candidate_progress = match_progress.get(normalized_team_id)
        if isinstance(candidate_progress, dict):
            team_progress = candidate_progress

    if team_progress is None:
        for match_progress in TEAM_PROGRESS_CACHE.values():
            if not isinstance(match_progress, dict):
                continue
            candidate_progress = match_progress.get(normalized_team_id)
            if isinstance(candidate_progress, dict):
                team_progress = candidate_progress
                if not match_id:
                    candidate_match_id = candidate_progress.get("match_id")
                    match_id = _normalize_identifier(candidate_match_id)
                break

    team_completed = bool(team_progress and team_progress.get("team_completed"))

    if team_completed:
        team["team_completed"] = True
        if team.get("status") != "finished":
            team["status"] = "finished"

    match_status = context.get("match_status")
    if not isinstance(match_status, dict):
        if team_completed and match_status is None:
            context["match_status"] = {"team_status": "finished", "team_completed": True}
        return

    if team_completed:
        match_status["team_status"] = "finished"
        match_status["team_completed"] = True
        match_status.pop("redirect", None)

    if not match_id:
        return

    match_progress_map = TEAM_PROGRESS_CACHE.get(match_id) or {}
    relevant_team_ids = {
        _normalize_identifier(team_entry.get("id"))
        for team_entry in match_status.get("teams", [])
        if isinstance(team_entry, dict) and team_entry.get("id") is not None
    }

    relevant_team_ids = {team_id for team_id in relevant_team_ids if team_id}

    if not relevant_team_ids:
        return

    all_completed = True
    for team_id in relevant_team_ids:
        progress = match_progress_map.get(team_id)
        if not isinstance(progress, dict) or not progress.get("team_completed"):
            all_completed = False
            break

    if all_completed:
        match_status["status"] = "finished"

router = APIRouter()


@router.post("/login", response_class=HTMLResponse)
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


@router.get("/team/{team_id}", response_class=HTMLResponse)
async def view_team(team_id: str, request: Request, user_id: Optional[int] = None) -> HTMLResponse:
    team = await _fetch_team_with_members(team_id)

    user: Optional[Dict[str, Any]] = None
    member: Optional[Dict[str, Any]] = None

    if user_id is not None:
        try:
            user = await _ensure_user_exists(user_id)
        except HTTPException as exc:
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
    await _augment_team_context_with_quizzes(context)
    _apply_team_completion_state(context)
    return templates.TemplateResponse("team.html", context)


@router.post("/team/create", response_class=HTMLResponse)
async def create_team(request: Request) -> HTMLResponse:
    payload = await _parse_request_payload(request, CreateTeamRequest)
    user = await _ensure_user_exists(payload.user_id)

    existing_team = await _find_existing_team_for_user(user)
    if existing_team:
        team_name = existing_team.get("name") or existing_team.get("code") or existing_team.get("id")
        message = f"Вы уже состоите в команде «{team_name}». Сначала покиньте текущую команду."
        raise HTTPException(status.HTTP_409_CONFLICT, detail=message)

    code = await _generate_unique_team_code()

    team_payload = {
        "name": payload.team_name,
        "code": code,
        "captain_id": user["id"],
        "match_id": "demo-match",
        "ready": False,
    }

    team_response = await _supabase_request(
        "POST",
        "teams",
        json_payload=team_payload,
        prefer="return=representation",
    )

    team_data = team_response[0] if isinstance(team_response, list) and team_response else team_response
    if not isinstance(team_data, dict) or "id" not in team_data:
        raise HTTPException(status_code=500, detail="Team created but no ID in response")

    team_id = team_data["id"]
    normalized_team_id = _normalize_identifier(team_id)
    TEAM_READY_CACHE[normalized_team_id] = bool(team_data.get("ready"))
    match_id = _extract_match_id(team_data)
    if match_id and normalized_team_id:
        MATCH_TEAM_CACHE.setdefault(match_id, set()).add(normalized_team_id)

    try:
        await _add_team_member(team_id, user["id"], is_captain=True)
    except HTTPException:
        pass

    team_with_members = await _fetch_team_with_members(team_id)
    team_with_members.setdefault("code", code)

    if _is_json_request(request):
        redirect_url = f"/team/{team_id}?user_id={user['id']}"
        return JSONResponse({"team": team_with_members, "redirect": redirect_url})

    context = _build_team_context(
        request,
        team=team_with_members,
        user=user,
        last_response={"team": team_with_members},
    )
    await _augment_team_context_with_quizzes(context)
    _apply_team_completion_state(context)
    return templates.TemplateResponse("team.html", context)


@router.post("/team/join", response_class=HTMLResponse)
async def join_team(request: Request) -> HTMLResponse:
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
    await _augment_team_context_with_quizzes(context)
    _apply_team_completion_state(context)
    return templates.TemplateResponse("team.html", context)


@router.post("/team/start", response_class=HTMLResponse)
async def start_team(request: Request) -> HTMLResponse:
    payload = await _parse_request_payload(request, StartTeamRequest)
    user = await _ensure_user_exists(payload.user_id)
    team = await _ensure_team_exists(payload.team_id)

    member = await _fetch_team_member(team["id"], user["id"])
    if not member or not member.get("is_captain"):
        raise HTTPException(status_code=403, detail="Only the captain can start the quiz")

    team_id = _normalize_identifier(team.get("id"))

    TEAM_READY_CACHE[team_id] = True
    team["ready"] = True

    try:
        await _supabase_request(
            "PATCH",
            "teams",
            params={"id": f"eq.{team_id}"},
            json_payload={"ready": True},
            prefer="return=representation",
        )
    except HTTPException:
        pass

    match_id = _extract_match_id(team)
    MATCH_TEAM_CACHE.setdefault(match_id, set()).add(team_id)

    all_ready = all(TEAM_READY_CACHE.get(tid) for tid in MATCH_TEAM_CACHE[match_id])
    if all_ready:
        await _ensure_match_quiz_assigned(match_id)

    match_response = await _build_match_status_response(match_id, fallback_team=team)

    if _is_json_request(request):
        return JSONResponse(match_response)

    team_with_members = await _fetch_team_with_members(team_id)
    context = _build_team_context(
        request,
        team=team_with_members,
        user=user,
        member=member,
        last_response={"team": team_with_members},
    )
    context["match_status"] = match_response
    await _augment_team_context_with_quizzes(context)
    _apply_team_completion_state(context)
    return templates.TemplateResponse("team.html", context)


@router.post("/team/select-quiz", response_class=HTMLResponse)
async def select_quiz(request: Request) -> HTMLResponse:
    payload = await _parse_request_payload(request, SelectQuizRequest)
    user = await _ensure_user_exists(payload.user_id)
    team = await _ensure_team_exists(payload.team_id)

    member = await _fetch_team_member(team["id"], user["id"])
    if not member or not member.get("is_captain"):
        raise HTTPException(status_code=403, detail="Только капитан может выбрать викторину")

    normalized_team_id = _normalize_identifier(team.get("id"))
    match_id = _extract_match_id(team)

    try:
        update_response = await _supabase_request(
            "PATCH",
            "teams",
            params={"id": f"eq.{normalized_team_id}"},
            json_payload={"quiz_id": payload.quiz_id},
            prefer="return=representation",
        )
    except HTTPException:
        raise

    if isinstance(update_response, list) and update_response:
        team = {**team, **update_response[0]}
    elif isinstance(update_response, dict):
        team = {**team, **update_response}
    else:
        team = {**team, "quiz_id": payload.quiz_id}

    if match_id:
        MATCH_QUIZ_CACHE[match_id] = payload.quiz_id
        QUIZ_CACHE.pop(match_id, None)
        TEAM_PROGRESS_CACHE.pop(match_id, None)
    if normalized_team_id:
        QUIZ_CACHE.pop(normalized_team_id, None)

    team_with_members = await _fetch_team_with_members(team["id"])
    team_with_members["quiz_id"] = team.get("quiz_id")

    if _is_json_request(request):
        return JSONResponse({"team": team_with_members, "quiz_id": payload.quiz_id})

    context = _build_team_context(
        request,
        team=team_with_members,
        user=user,
        member=member,
        last_response={"team": team_with_members, "quiz_id": payload.quiz_id},
    )
    await _augment_team_context_with_quizzes(context)
    _apply_team_completion_state(context)
    return templates.TemplateResponse("team.html", context)


@router.post("/team/leave", response_class=HTMLResponse)
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

    if _is_json_request(request):
        return JSONResponse({"team": team_with_members, "redirect": "/", "message": "Вы покинули команду."})

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/team/delete", response_class=HTMLResponse)
async def delete_team(request: Request) -> HTMLResponse:
    payload = await _parse_request_payload(request, DeleteTeamRequest)
    user = await _ensure_user_exists(payload.user_id)
    team = await _ensure_team_exists(payload.team_id)

    if team.get("captain_id") != user.get("id"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Удалять команду может только капитан")

    await _delete_team(team["id"])
    _clear_team_from_caches(team)

    if _is_json_request(request):
        return JSONResponse({"redirect": "/", "message": "Команда удалена."})

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/me")
async def me(request: Request) -> Dict[str, Any]:
    user_id = request.headers.get("X-User-Id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    user = await _ensure_user_exists(int(user_id))
    return {"user": user}


@router.get("/team/of-user/{user_id}")
async def get_team_of_user(user_id: int):
    user = await _ensure_user_exists(user_id)
    team = await _find_existing_team_for_user(user)
    if not team:
        return JSONResponse({}, status_code=404)
    return team