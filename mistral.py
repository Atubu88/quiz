# mistral.py

import aiohttp
import os
import logging
from dotenv import load_dotenv
import asyncio

load_dotenv()

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

async def ask_mistral_with_history(messages: list[dict]) -> str:
    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": "mistral-tiny",  # Можно заменить на "mistral-small" или "mistral-medium"
        "messages": messages,
        "temperature": 0.5,
        "max_tokens": 400
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(MISTRAL_API_URL, headers=headers, json=data) as resp:
                if resp.status == 200:
                    json_response = await resp.json()
                    return json_response["choices"][0]["message"]["content"].strip()
                else:
                    error_text = await resp.text()
                    logging.error(f"Mistral API error {resp.status}: {error_text}")
                    return "⚠️ Mistral не смог ответить."
    except Exception as e:
        logging.exception("Ошибка Mistral:")
        return "⚠️ Ошибка запроса к Mistral."


async def safe_mistral_request(messages: list[dict], retries: int = 3, delay: float = 1.2) -> str:
    for attempt in range(retries):
        reply = await ask_mistral_with_history(messages)
        if "⚠️" not in reply:
            return reply
        await asyncio.sleep(delay)
    return "⚠️ Превышен лимит запросов к Mistral. Попробуйте чуть позже."