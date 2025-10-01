# utils.py



# utils/leaderboard_utils.py
import asyncio
import logging
from supabase import Client

async def build_leaderboard_message(top_results: list[dict], supabase_client: Client) -> str:
    """
    Формирует текст "🔥 Топ-10 участников:\n" с медалями для первых 3 мест.
    """
    if not top_results:
        return "Пока нет участников."

    max_width = 50
    leaderboard_text = "🔥 Топ-10 участников 🔥".center(max_width) + "\n"

    for idx, res in enumerate(top_results, start=1):
        # Определяем медаль для первых трех мест
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        medal = medals.get(idx, "")

        # Формируем нумерацию и выравниваем (чтобы было ровно)
        place_text = f"{idx}.".ljust(3)

        user_id = res["user_id"]
        score = res["score"]
        time_taken = res["time_taken"]

        # Перевод времени в минуты и секунды
        minutes = time_taken // 60
        seconds = time_taken % 60
        time_str = f"{minutes} мин {seconds} сек" if minutes > 0 else f"{seconds} сек"

        # Подгружаем имя пользователя из Supabase
        try:
            user_info = await asyncio.to_thread(
                supabase_client.table("users")
                .select("username, first_name")
                .eq("id", user_id)
                .single()
                .execute
            )
            user_data = user_info.data
        except Exception as e:
            logging.error(f"Ошибка загрузки имени для user_id={user_id}: {e}")
            user_data = None

        # Если нет ни username, ни first_name, подставим "Аноним"
        username = (user_data.get("first_name") or user_data.get("username")) if user_data else "Аноним"

        # Формируем строку рейтинга с выравниванием
        leaderboard_text += f"{place_text} {username} – {score} очков ({time_str}) {medal}\n"

    return leaderboard_text


