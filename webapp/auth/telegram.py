"""Telegram authentication helpers and API endpoints."""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from config import BOT_TOKEN
from supabase_client import supabase

logger = logging.getLogger(__name__)

router = APIRouter()


class TelegramLoginPayload(BaseModel):
    """Payload received from the Telegram Login Widget."""

    id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    hash: str
    auth_date: Optional[int] = None
    last_name: Optional[str] = None
    photo_url: Optional[str] = None


def verify_telegram_signature(
    data: Dict[str, Any],
    bot_token: Optional[str] = None,
    *,
    expected_hash: str,
) -> bool:
    """Verify Telegram Login Widget payload signature using HMAC-SHA256."""

    token = bot_token or BOT_TOKEN
    if not token:
        raise RuntimeError("BOT_TOKEN is not configured for Telegram authentication")

    filtered_data = {k: v for k, v in data.items() if k != "hash"}
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(filtered_data.items())
    )

    secret_key = hashlib.sha256(token.encode()).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed_hash, expected_hash)


async def get_user_from_supabase(user_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve a user record from Supabase by Telegram identifier."""

    def _query() -> Optional[Dict[str, Any]]:
        response = (
            supabase.table("users")
            .select("id,username,first_name")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        return response.data[0]

    try:
        return await run_in_threadpool(_query)
    except Exception as exc:  # pragma: no cover - network errors
        logger.exception("Failed to fetch user from Supabase", exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Не удалось получить данные пользователя",
        ) from exc


async def get_or_create_user(
    user_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Get a user from Supabase or create a new record if absent."""

    existing = await get_user_from_supabase(user_id)
    if existing is not None:
        return existing

    payload = {"id": user_id}
    if username is not None:
        payload["username"] = username
    if first_name is not None:
        payload["first_name"] = first_name

    def _insert() -> Dict[str, Any]:
        response = supabase.table("users").insert(payload).execute()
        if response.data:
            return response.data[0]
        return payload

    try:
        return await run_in_threadpool(_insert)
    except Exception as exc:  # pragma: no cover - network errors
        logger.exception("Failed to create user in Supabase", exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Не удалось сохранить пользователя",
        ) from exc


@router.post("/login")
async def login(payload: TelegramLoginPayload, response: Response) -> Dict[str, Any]:
    """Authenticate user via Telegram Login Widget data."""

    payload_dict = payload.model_dump(exclude_none=True)
    provided_hash = payload_dict.pop("hash", None)
    if not provided_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Отсутствует подпись hash",
        )

    if not verify_telegram_signature(payload_dict, expected_hash=provided_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Некорректная подпись Telegram",
        )

    user = await get_or_create_user(
        user_id=payload.id,
        username=payload.username,
        first_name=payload.first_name,
    )

    response.set_cookie(
        key="user_id",
        value=str(user["id"]),
        httponly=True,
        max_age=60 * 60 * 24 * 30,
        samesite="lax",
    )

    return {"status": "ok", "user": user}


@router.get("/me")
async def read_current_user(request: Request) -> Dict[str, Any]:
    """Return the current authenticated user based on the cookie."""

    raw_user_id = request.cookies.get("user_id")
    if raw_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Пользователь не авторизован",
        )

    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError) as exc:
        logger.warning("Invalid user_id cookie received", exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Некорректный идентификатор пользователя",
        ) from exc

    user = await get_user_from_supabase(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Пользователь не найден",
        )

    return {"user": user}
