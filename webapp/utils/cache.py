from __future__ import annotations

from typing import Any, Dict, List, Set

QUIZ_CACHE: Dict[str, Dict[str, Any]] = {}
MATCH_CACHE: Dict[str, Dict[str, Any]] = {}
MATCH_STATUS_CACHE: Dict[str, Dict[str, Any]] = {}
MATCH_TEAM_CACHE: Dict[str, Set[str]] = {}
TEAM_READY_CACHE: Dict[str, bool] = {}
TEAM_PROGRESS_CACHE: Dict[str, Dict[str, Any]] = {}
MATCH_QUIZ_CACHE: Dict[str, str] = {}
_matches_ready: Dict[str, List[str]] = {}

__all__ = [
    "QUIZ_CACHE",
    "MATCH_CACHE",
    "MATCH_STATUS_CACHE",
    "MATCH_TEAM_CACHE",
    "TEAM_READY_CACHE",
    "TEAM_PROGRESS_CACHE",
    "MATCH_QUIZ_CACHE",
    "_matches_ready",
]
