import asyncio
import time
import os
from aiogram import Router, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from supabase import create_client
from keyboards import start_keyboard
# Подключение к Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_API_KEY)
# Создаём роутер для выживания
survival_router = Router()
sessions = {}

# Список вопросов
survival_questions = [
    # 📖 Чтение Корана
    {
        "question": "1. Ты решил читать Коран в Рамадан по 10 страниц в день. Однако в последние 10 дней месяца ты планируешь увеличить количество до 20 страниц в день. Сколько страниц ты прочитаешь за весь Рамадан (30 дней)?",
        "answer": "400",
        "explanation": "Ты правильно посчитал! Пророк (ﷺ) сказал: «Кто читает одну букву из Книги Аллаха, тому записывается награда, а награда умножается в 10 раз» (ат-Тирмизи). Представь, сколько награды ты получишь за 400 страниц!"
    },

    # 📚 Покупки и расчёты
    {
        "question": "2. Ты купил 3 книги по  350 рублей, но продавец сделал скидку 100 рублей на всю покупку. Сколько ты заплатил?",
        "answer": "950",  # (3 × 350) - 100 = 950
        "explanation": "МашаЛлах! Хороший расчёт. Покупка знаний — это инвестиция в вечность, ведь знания в Исламе — это великая ценность."
    },

    # 🕌 Приглашение в мечеть
    {
        "question": "3. Ты пригласил 3 друзей в мечеть. Каждый из них пригласил ещё 2 человек. Сколько людей пришло в мечеть посредтсвом  тебя?",
        "answer": "9",  # 3 + (3 × 2) = 9
        "explanation": "АльхамдулиЛлях! Пророк (ﷺ) сказал: «Тот, кто зовёт к благу, получит такую же награду, как и тот, кто последовал за ним» (Муслим)."
    },

    # 🌙 Пост в Рамадан
    {
        "question": "4. В Рамадан ты постишься 15 часов в день. Сколько часов ты проведёшь в посте за 30 дней, если в последний день ты пропустил из-за болезни?",
        "answer": "435",  # (15 × 29) = 435
        "explanation": "Ты правильно рассчитал! Пророк (ﷺ) сказал: «Тот, кто постится в Рамадан с верой и надеждой на награду, тому простятся прежние грехи» (Бухари, Муслим)."
    },

    # 💰 Благотворительность
    {
        "question": "5. Ты решил отдать сборщику фитр садака 20% от своей суммы . У тебя было 5000 рублей. Сколько ты отдал ?",
        "answer": "1000",  # 5000 × 0.2 = 1000
        "explanation": "БаракАллаху фика! Пророк (ﷺ) сказал: «Садака никогда не уменьшает богатство» (Муслим). Наоборот, она приумножает благословение в твоем имуществе!"
    },

    # 🛒 Покупки перед ифтаром
    {
        "question": "6. Ты готовишься к ифтару и пошёл в магазин. Ты купил 2 кг фиников по 150 рублей за кг, 3 упаковки сока по 100 рублей каждая и хлеб за 50 рублей. Сколько денег ты потратил?",
        "answer": "650",  # (2×150) + (3×100) + 50 = 600
        "explanation": "Правильный ответ! Финики — это сунна для разговения. Пророк (ﷺ) начинал ифтар с фиников или воды."
    },

    # 📚 Распространение знаний
    {
        "question": "7. Ты рассказал другу о важности сунны утреннего намаза. Он передал это двум своим друзьям, а каждый из них рассказал ещё трём людям. Сколько всего человек узнало об этом, включая тебя?",
        "answer": "10", # 1 (ты) + 1×2 + 2×3 = 10
        "explanation": "СубханАллах! Пророк (ﷺ) сказал: «Тот, кто научит кого-то благому, получит такую же награду, как и тот, кто следует этому» (Муслим)."
    },

    # 🤲 Распространение информации о посте
    {
        "question": "8. Ты рассказал 4 друзьям о награде за пост в понедельник и четверг. Если каждый из них поделился этой информацией ещё с 3 людьми, сколько всего человек узнало об этом?",
        "answer": "16",  # 4 + (4 × 3) = 16
        "explanation": "Ты правильно посчитал! Пророк (ﷺ) сказал: «Делай пост в понедельник и четверг, ведь в эти дни дела представляются Аллаху» (Тирмизи)."
    },

    # 🕌 Количество людей в мечети
    {
        "question": "9. Ты молился в мечети, где было 10 человек. Затем пришли ещё X человек. Теперь в мечети 25 человек. Чему равно X?",
        "answer": "15",  # 10 + X = 25 → X = 15
        "explanation": "Отлично! Посещение мечети увеличивает твою награду. Пророк (ﷺ) сказал: «Намаз в мечети умножается в 27 раз по сравнению с индивидуальной молитвой» (Бухари, Муслим)."
    },

    # 💵 Долг и выплаты
    {
        "question": "10. Ты взял в долг некую сумму и договорился отдавать по 500 рублей в месяц в течение года. Сколько денег ты взял в долг?",
        "answer": "6000",  # 500 × 12 = 6000
        "explanation": "Верно! В Исламе важно выплачивать долги вовремя. Пророк (ﷺ) сказал: лучшим из вас является тот, кто возвращает долги наилучшим образом”». Аль-Бухари, 2306 и Муслим, 1601. (Бухари)."
    }
]

# Клавиатура для режима Выживания
def survival_menu_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Начать игру")], [KeyboardButton(text="Назад в меню")]],
        resize_keyboard=True
    )

# Клавиатура для завершения игры
restart_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🔄 Начать заново")], [KeyboardButton(text="Назад в меню")]],
    resize_keyboard=True
)

@survival_router.message(F.text == "🔄 Начать заново")
async def restart_game(message: Message):
    await start_game(message)


@survival_router.message(F.text == "🌙 Рамадан-Квест")
async def survival_mode_entry(message: Message):
    await message.answer(
        "🔋 <b>Добро пожаловать в режим 'Рамадан Квест'!</b> 🔥\n\n"
        "📜 <b>Правила игры:</b>\n"
        "🟢 У вас <b>3</b> 🔋 энергии.\n"
        "❓ Каждый <b>вопрос</b> – новый <b>уровень</b>.\n"
        "⏳ На ответ даётся <b>40 секунд</b>.\n"
        "❌ За <b>неверный ответ</b> или <b>истечение времени</b> – минус <b>1</b> 🔋.\n"
        "⚡ <b>Готовы испытать свои силы?</b>\n"
        "🎮 Нажмите <b>'▶ Начать игру'</b>, чтобы начать! 🚀",
        reply_markup=survival_menu_keyboard(),
        parse_mode="HTML"
    )


@survival_router.message(F.text == "Назад в меню")
async def back_to_menu(message: Message):
    user_id = message.from_user.id
    sessions.pop(user_id, None)
    await message.answer("🔙 Вы вернулись в главное меню.", reply_markup=start_keyboard())

@survival_router.message(F.text == "Начать игру")
async def start_game(message: Message):
    user_id = message.from_user.id
    if user_id in sessions and sessions[user_id]["active"]:
        await message.answer("⚠️ Вы уже играете! Завершите текущую игру перед началом новой.")
        return

    sessions[user_id] = {
        "lives": 3,
        "question_index": 0,
        "score": 0,
        "waiting_future": None,
        "active": True,
        "start_time": time.time()  # Запоминаем время начала игры
    }

    await message.answer("🎮 Игра началась! У вас 3 🔋. Отвечайте правильно, чтобы пройти уровень.")
    await game_loop(message)

async def countdown_timer(message: Message, countdown_msg: Message, total_time: int, waiting_future: asyncio.Future):
    for remaining in range(total_time, 0, -1):
        if waiting_future.done():
            break
        try:
            await message.bot.edit_message_text(
                text=f"⏳ Осталось {remaining} секунд...",
                chat_id=message.chat.id,
                message_id=countdown_msg.message_id
            )
        except Exception:
            pass
        await asyncio.sleep(1)

async def game_loop(message: Message):
    user_id = message.from_user.id

    while user_id in sessions and sessions[user_id]["active"]:
        session = sessions.get(user_id)
        if not session:
            return

        if session["question_index"] >= len(survival_questions):
            await message.answer("🎉 Поздравляем! Вы прошли все уровни!")
            break

        current_level = session["question_index"] + 1
        current_question = survival_questions[session["question_index"]]
        energy = "🔋" * session["lives"]

        # Отправляем вопрос
        question_msg = await message.answer(
            f"🆙 Уровень {current_level}:\n"
            f"{current_question['question']}\n"
            f"⚡ Энергия: {energy}"
        )

        # Сообщение с таймером
        countdown_msg = await message.answer("⏳ Осталось 40 секунд...")

        session["messages_to_edit"] = {
            "question": question_msg.message_id,
            "countdown": countdown_msg.message_id
        }

        # Ожидаем ответ
        loop = asyncio.get_event_loop()
        session["waiting_future"] = loop.create_future()

        # Запускаем таймер
        timer_task = asyncio.create_task(
            countdown_timer(message, countdown_msg, 40, session["waiting_future"])
        )

        try:
            if user_id not in sessions or not sessions[user_id]["active"]:
                return
            user_answer = await asyncio.wait_for(session["waiting_future"], timeout=44)
        except asyncio.TimeoutError:
            user_answer = None

        timer_task.cancel()

        # Плавная пауза перед редактированием
        await asyncio.sleep(0.3)

        # Вместо удаления — редактируем
        try:
            await message.bot.edit_message_text(
                text="✅ Вопрос завершён.",
                chat_id=message.chat.id,
                message_id=session["messages_to_edit"]["question"]
            )
        except:
            pass

        try:
            await message.bot.edit_message_text(
                text="⏳ Время вышло." if user_answer is None else "⌛ Обработка ответа...",
                chat_id=message.chat.id,
                message_id=session["messages_to_edit"]["countdown"]
            )
        except:
            pass

        correct_answer = current_question["answer"].strip().lower()

        if user_answer is None:
            session["lives"] -= 1
            if session["lives"] <= 0:
                await message.answer("⏳ Время вышло, и у тебя не осталось энергии.")
                break
            else:
                await message.answer("⏳ Время вышло! Попробуй ещё раз.")
        else:
            if user_answer.strip().lower() == correct_answer:
                await message.answer(
                    f"✅ Уровень {current_level} пройден!\n\n"
                    f"{current_question['explanation']}"
                )
                session["score"] += 1
                session["question_index"] += 1
            else:
                session["lives"] -= 1
                if session["lives"] <= 0:
                    await message.answer("❌ Неверно! К сожалению, энергия закончилась.")
                    break
                else:
                    await message.answer("❌ Неверно! Попробуй ещё раз.")

    # ⛔️ Защита от отсутствующей сессии
    session = sessions.get(user_id)
    if not session:
        await message.answer("⚠️ Игра прервана. Пожалуйста, начните заново.")
        return

    # Завершение игры (результаты)
    elapsed_time = time.time() - session["start_time"]
    minutes, seconds = divmod(int(elapsed_time), 60)

    first_name = message.from_user.first_name or ""
    username = message.from_user.username or ""
    display_name = first_name or username or "Аноним"

    score = session["score"]
    time_spent = int(elapsed_time)

    existing_record = supabase.table("survival_results").select("id", "score", "time_spent").eq("user_id", user_id).execute()

    if existing_record.data:
        supabase.table("survival_results").update({
            "score": score,
            "time_spent": time_spent
        }).eq("user_id", user_id).execute()
    else:
        supabase.table("survival_results").insert({
            "user_id": user_id,
            "username": display_name,
            "score": score,
            "time_spent": time_spent
        }).execute()

    result = supabase.table("survival_results").select("user_id", "score").order("score", desc=True).execute()
    all_results = result.data
    total_players = len(all_results)
    position = next((i + 1 for i, res in enumerate(all_results) if res["user_id"] == user_id), "N/A")

    await message.answer(
        f"🏁 Игра завершена! 📊\n"
        f"✅ Пройденных уровней: {score}\n"
        f"⏱ Время игры: {minutes} мин {seconds} сек.\n"
        f"🏆 Ты занял *{position}-е место* из {total_players} участников!",
        reply_markup=restart_keyboard,
        parse_mode="Markdown"
    )

    sessions.pop(user_id, None)



@survival_router.message()
async def handle_answers(message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id)
    if session and session.get("waiting_future") and not session["waiting_future"].done():
        session["waiting_future"].set_result(message.text)

