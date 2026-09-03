import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# =========================================================
# НАЛАШТУВАННЯ
# =========================================================

BOT_TOKEN = os.getenv("RANDOM_BOT_TOKEN")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# =========================================================
# TELEGRAM
# =========================================================

def telegram_request(method, data=None):
    try:
        response = requests.post(
            f"{TELEGRAM_API}/{method}",
            json=data or {},
            timeout=20,
        )
        return response.json()
    except Exception as e:
        print("TELEGRAM ERROR:", repr(e))
        return None


def send_message(chat_id, text, reply_markup=None):
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }

    if reply_markup:
        data["reply_markup"] = reply_markup

    return telegram_request("sendMessage", data)


def answer_callback(callback_id, text=None, show_alert=False):
    data = {
        "callback_query_id": callback_id,
        "show_alert": show_alert,
    }

    if text:
        data["text"] = text

    return telegram_request("answerCallbackQuery", data)


# =========================================================
# ПЕРЕВІРКИ
# =========================================================

def is_admin(user_id):
    if not ADMIN_TELEGRAM_ID:
        return False

    return str(user_id) == str(ADMIN_TELEGRAM_ID)


def is_private_chat(message):
    chat = message.get("chat", {})
    return chat.get("type") == "private"


# =========================================================
# МЕНЮ
# =========================================================

def admin_menu():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🎁 Створити розіграш",
                    "callback_data": "create_giveaway",
                }
            ],
            [
                {
                    "text": "📋 Активні розіграші",
                    "callback_data": "active_giveaways",
                }
            ],
            [
                {
                    "text": "🏆 Завершені",
                    "callback_data": "finished_giveaways",
                }
            ],
            [
                {
                    "text": "ℹ️ Допомога",
                    "callback_data": "help",
                }
            ],
        ]
    }


def participant_menu():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🎁 Активні розіграші",
                    "callback_data": "participant_active",
                }
            ],
            [
                {
                    "text": "ℹ️ Допомога",
                    "callback_data": "participant_help",
                }
            ],
        ]
    }


def show_main_menu(chat_id, user_id):
    if is_admin(user_id):
        send_message(
            chat_id,
            "🎁 <b>RandomCoin Bot</b>\n\n"
            "Панель керування розіграшами.\n\n"
            "Оберіть потрібну дію 👇",
            admin_menu(),
        )
    else:
        send_message(
            chat_id,
            "🎁 <b>RandomCoin Bot</b>\n\n"
            "Тут ви можете брати участь у розіграшах.\n\n"
            "Оберіть дію 👇",
            participant_menu(),
        )


# =========================================================
# ОБРОБКА ПОВІДОМЛЕНЬ
# =========================================================

def handle_message(message):
    # У групах бот мовчить.
    if not is_private_chat(message):
        return

    chat_id = message["chat"]["id"]

    user = message.get("from", {})
    user_id = user.get("id")

    text = (message.get("text") or "").strip()

    if text in ("/start", "/menu"):
        show_main_menu(chat_id, user_id)
        return

    if text == "/ping":
        send_message(
            chat_id,
            "🏓 <b>Pong!</b>\n\n"
            "🎁 RandomCoin Bot працює нормально ✅",
        )
        return

    if text == "/help":
        send_message(
            chat_id,
            "ℹ️ <b>Допомога</b>\n\n"
            "🎁 RandomCoin Bot використовується для проведення "
            "та участі в розіграшах.\n\n"
            "Усі основні дії доступні через меню.",
        )
        return

    # Адміністративні команди
    if text == "/create":
        if not is_admin(user_id):
            send_message(
                chat_id,
                "⛔ Ця команда доступна тільки адміністратору.",
            )
            return

        send_message(
            chat_id,
            "🎁 <b>Створення розіграшу</b>\n\n"
            "Майстер створення розіграшу буде доданий наступним етапом.",
        )
        return

    # Якщо написали щось інше
    show_main_menu(chat_id, user_id)


# =========================================================
# CALLBACK-КНОПКИ
# =========================================================

def handle_callback(callback):
    callback_id = callback["id"]

    user = callback.get("from", {})
    user_id = user.get("id")

    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")

    data = callback.get("data", "")

    # На випадок натискання кнопки не в особистому чаті
    if message.get("chat", {}).get("type") != "private":
        answer_callback(callback_id)
        return

    if data == "create_giveaway":
        if not is_admin(user_id):
            answer_callback(
                callback_id,
                "⛔ Тільки адміністратор може створювати розіграші.",
                True,
            )
            return

        answer_callback(callback_id)

        send_message(
            chat_id,
            "🎁 <b>Створення нового розіграшу</b>\n\n"
            "Наступним кроком тут буде майстер створення:\n\n"
            "📝 Назва\n"
            "📄 Опис\n"
            "📸 1–10 фото\n"
            "🏆 1–15 переможців\n"
            "📢 Обов'язкові підписки\n"
            "🚀 Бусти\n"
            "⏰ Завершення за часом або кількістю учасників.",
        )
        return

    if data == "active_giveaways":
        if not is_admin(user_id):
            answer_callback(callback_id)
            return

        answer_callback(callback_id)

        send_message(
            chat_id,
            "📋 <b>Активні розіграші</b>\n\n"
            "Поки активних розіграшів немає.",
        )
        return

    if data == "finished_giveaways":
        if not is_admin(user_id):
            answer_callback(callback_id)
            return

        answer_callback(callback_id)

        send_message(
            chat_id,
            "🏆 <b>Завершені розіграші</b>\n\n"
            "Поки завершених розіграшів немає.",
        )
        return

    if data == "help":
        answer_callback(callback_id)

        send_message(
            chat_id,
            "ℹ️ <b>Панель адміністратора</b>\n\n"
            "🎁 Створення розіграшів\n"
            "📋 Перегляд активних\n"
            "🏆 Історія завершених\n"
            "👥 Контроль учасників\n"
            "📢 Перевірка підписок\n"
            "🚀 Система бустів",
        )
        return

    if data == "participant_active":
        answer_callback(callback_id)

        send_message(
            chat_id,
            "🎁 <b>Активні розіграші</b>\n\n"
            "Поки активних розіграшів немає.",
        )
        return

    if data == "participant_help":
        answer_callback(callback_id)

        send_message(
            chat_id,
            "ℹ️ <b>Як брати участь</b>\n\n"
            "Коли з'явиться активний розіграш, бот покаже кнопку "
            "участі та всі необхідні умови.",
        )
        return

    answer_callback(callback_id)


# =========================================================
# WEB
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return "RandomCoin bot is running ✅", 200


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(silent=True) or {}

    try:
        if update.get("callback_query"):
            handle_callback(update["callback_query"])

        elif update.get("message"):
            handle_message(update["message"])

    except Exception as e:
        print("WEBHOOK ERROR:", repr(e))

    return jsonify({"ok": True})


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(
        host="0.0.0.0",
        port=port,
    )
