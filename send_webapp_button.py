import requests

# Замените на ваш токен бота и chat_id
bot_token = "7885960281:AAHnI0ZnQaVNiZ2uii3fMO8KTxon4Uh5X7Q"
chat_id = "732402669"

url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

reply_markup = {
    "inline_keyboard": [
        [
            {
                "text": "Открыть WebApp",
                "web_app": {
                    "url": "https://muhadjirun.netlify.app"
                }
            }
        ]
    ]
}

payload = {
    "chat_id": chat_id,
    "text": "Откройте мини-приложение",
    "reply_markup": reply_markup
}

response = requests.post(url, json=payload)

# Логируем статус и текст ответа для проверки
print(response.status_code, response.text)
