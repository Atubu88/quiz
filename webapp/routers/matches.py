from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from webapp.services.match_service import _build_match_status_response
from webapp.services.supabase_client import _fetch_single_record
from webapp.utils.cache import MATCH_STATUS_CACHE

router = APIRouter()


@router.get("/match/status/{match_id}")
async def match_status(match_id: str) -> JSONResponse:
    cached = MATCH_STATUS_CACHE.get(match_id) or {}
    cached_teams = cached.get("teams")

    prefetched_teams: Optional[List[Dict[str, Any]]] = None
    if isinstance(cached_teams, list) and cached_teams:
        prefetched_teams = []
        for team in cached_teams:
            team_id = team.get("id")
            if not team_id:
                continue

            prefetched_teams.append(
                {
                    "id": team_id,
                    "name": team.get("name"),
                    "ready": team.get("ready"),
                    "status": team.get("status") or team.get("team_status"),
                    "team_status": team.get("team_status"),
                    "team_completed": team.get("team_completed"),
                    "is_yours": team.get("is_yours"),
                }
            )

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
