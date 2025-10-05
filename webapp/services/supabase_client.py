from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx
from fastapi import HTTPException, status

from config import SUPABASE_API_KEY, SUPABASE_URL

if not SUPABASE_URL or not SUPABASE_API_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_API_KEY must be configured.")


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
    prefer: Optional[str] = None,
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
        method,
        path,
        response.status_code,
        url,
        params,
        json_payload,
        response.text,
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


async def _fetch_quiz_options(select: str = "id,title") -> List[Dict[str, Any]]:
    """Возвращает список викторин с указанными полями (по умолчанию id и title)."""

    params = {"select": select, "order": "title.asc"}
    quizzes = await _supabase_request("GET", "quizzes", params=params) or []
    normalized: List[Dict[str, Any]] = []
    for quiz in quizzes:
        if not isinstance(quiz, dict):
            continue
        if quiz.get("id") in (None, ""):
            continue
        normalized.append(quiz)
    return normalized


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


__all__ = [
    "_supabase_request",
    "_fetch_single_record",
    "_fetch_active_quiz",
    "_fetch_quiz_options",
]
