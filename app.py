import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# =========================================================
# НАЛАШТУВАННЯ
# =========================================================

BOT_TOKEN = os.getenv("RANDOM_BOT_TOKEN")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
KYIV = ZoneInfo("Europe/Kyiv")

MAX_PHOTOS = 10
MIN_WINNERS = 1
MAX_WINNERS = 15

# Чернетки поки зберігаються в пам'яті.
# На наступному етапі перенесемо їх у PostgreSQL.
drafts = {}


# =========================================================
# TELEGRAM API
# =========================================================

def telegram_request(method, data=None):
    try:
        response = requests.post(
            f"{TELEGRAM_API}/{method}",
            json=data or {},
            timeout=25,
        )
        result = response.json()

        if not result.get("ok"):
            print("TELEGRAM API ERROR:", method, result)

        return result

    except Exception as e:
        print("TELEGRAM REQUEST ERROR:", method, repr(e))
        return {"ok": False, "error": str(e)}


def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    return telegram_request("sendMessage", payload)


def send_photo(chat_id, photo, caption=None, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "photo": photo,
        "parse_mode": "HTML",
    }

    if caption:
        payload["caption"] = caption

    if reply_markup:
        payload["reply_markup"] = reply_markup

    return telegram_request("sendPhoto", payload)


def send_media_group(chat_id, photos, caption=None):
    media = []

    for index, file_id in enumerate(photos):
        item = {
            "type": "photo",
            "media": file_id,
        }

        if index == 0 and caption:
            item["caption"] = caption
            item["parse_mode"] = "HTML"

        media.append(item)

    return telegram_request(
        "sendMediaGroup",
        {
            "chat_id": chat_id,
            "media": media,
        },
    )


def answer_callback(callback_id, text=None, show_alert=False):
    payload = {
        "callback_query_id": callback_id,
        "show_alert": show_alert,
    }

    if text:
        payload["text"] = text

    return telegram_request("answerCallbackQuery", payload)


# =========================================================
# ЗАГАЛЬНІ ПЕРЕВІРКИ
# =========================================================

def is_admin(user_id):
    if not ADMIN_TELEGRAM_ID:
        return False

    return str(user_id) == str(ADMIN_TELEGRAM_ID)


def is_private_chat(message):
    return message.get("chat", {}).get("type") == "private"


def private_chat_from_callback(callback):
    return callback.get("message", {}).get("chat", {}).get("type") == "private"


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


def cancel_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "❌ Скасувати",
                    "callback_data": "cancel_create",
                }
            ]
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
# ЧЕРНЕТКА РОЗІГРАШУ
# =========================================================

def new_draft(user_id):
    drafts[user_id] = {
        "step": "title",
        "title": "",
        "description": "",
        "photos": [],
        "winners": 1,
        "subscriptions": [],
        "boost_enabled": False,
        "boost_multiplier": 2,
        "end_mode": None,
        "end_value": None,
        "created_at": datetime.now(KYIV).isoformat(),
    }

    return drafts[user_id]


def get_draft(user_id):
    return drafts.get(user_id)


def delete_draft(user_id):
    drafts.pop(user_id, None)


# =========================================================
# КНОПКИ МАЙСТРА
# =========================================================

def photos_keyboard(photo_count):
    buttons = []

    if 1 <= photo_count <= MAX_PHOTOS:
        buttons.append(
            [
                {
                    "text": f"✅ Фото готові ({photo_count})",
                    "callback_data": "photos_done",
                }
            ]
        )

    buttons.append(
        [
            {
                "text": "❌ Скасувати",
                "callback_data": "cancel_create",
            }
        ]
    )

    return {"inline_keyboard": buttons}


def winners_keyboard():
    rows = []
    row = []

    for number in range(MIN_WINNERS, MAX_WINNERS + 1):
        row.append(
            {
                "text": str(number),
                "callback_data": f"winners:{number}",
            }
        )

        if len(row) == 5:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append(
        [
            {
                "text": "❌ Скасувати",
                "callback_data": "cancel_create",
            }
        ]
    )

    return {"inline_keyboard": rows}


def subscriptions_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "➕ Додати Telegram-підписку",
                    "callback_data": "subscription_add",
                }
            ],
            [
                {
                    "text": "⏭ Без обов'язкових підписок",
                    "callback_data": "subscriptions_skip",
                }
            ],
            [
                {
                    "text": "✅ Підписки готові",
                    "callback_data": "subscriptions_done",
                }
            ],
            [
                {
                    "text": "❌ Скасувати",
                    "callback_data": "cancel_create",
                }
            ],
        ]
    }


def boost_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🚀 Увімкнути буст ×2",
                    "callback_data": "boost:2",
                },
                {
                    "text": "🚀 Увімкнути буст ×3",
                    "callback_data": "boost:3",
                },
            ],
            [
                {
                    "text": "⏭ Без бусту",
                    "callback_data": "boost:0",
                }
            ],
            [
                {
                    "text": "❌ Скасувати",
                    "callback_data": "cancel_create",
                }
            ],
        ]
    }


def end_mode_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "⏰ За датою і часом",
                    "callback_data": "end_mode:time",
                }
            ],
            [
                {
                    "text": "👥 За кількістю учасників",
                    "callback_data": "end_mode:participants",
                }
            ],
            [
                {
                    "text": "❌ Скасувати",
                    "callback_data": "cancel_create",
                }
            ],
        ]
    }


def preview_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Запустити",
                    "callback_data": "giveaway_publish",
                }
            ],
            [
                {
                    "text": "✏️ Створити заново",
                    "callback_data": "giveaway_restart",
                },
                {
                    "text": "❌ Скасувати",
                    "callback_data": "cancel_create",
                },
            ],
        ]
    }


# =========================================================
# ФОРМАТУВАННЯ
# =========================================================

def subscriptions_text(subscriptions):
    if not subscriptions:
        return "немає"

    return "\n".join(f"• {item}" for item in subscriptions)


def build_preview(draft):
    boost_text = "немає"

    if draft["boost_enabled"]:
        boost_text = f"×{draft['boost_multiplier']} додаткових шансів"

    if draft["end_mode"] == "time":
        end_text = f"⏰ {draft['end_value']}"
    elif draft["end_mode"] == "participants":
        end_text = f"👥 після {draft['end_value']} учасників"
    else:
        end_text = "не задано"

    return (
        "🎁 <b>ПЕРЕВІРКА РОЗІГРАШУ</b>\n\n"
        f"🏷 <b>Назва:</b>\n{draft['title']}\n\n"
        f"📝 <b>Опис:</b>\n{draft['description']}\n\n"
        f"📸 <b>Фото:</b> {len(draft['photos'])}\n"
        f"🏆 <b>Переможців:</b> {draft['winners']}\n\n"
        f"📢 <b>Обов'язкові Telegram-підписки:</b>\n"
        f"{subscriptions_text(draft['subscriptions'])}\n\n"
        f"🚀 <b>Буст:</b> {boost_text}\n"
        f"🏁 <b>Завершення:</b> {end_text}\n\n"
        "⚠️ Перевірте дані перед запуском."
    )


def show_preview(chat_id, draft):
    text = build_preview(draft)

    if draft["photos"]:
        send_media_group(
            chat_id,
            draft["photos"],
            caption=text,
        )

        send_message(
            chat_id,
            "👇 <b>Що робимо з розіграшем?</b>",
            preview_keyboard(),
        )

    else:
        send_message(
            chat_id,
            text,
            preview_keyboard(),
        )


# =========================================================
# СТАРТ МАЙСТРА
# =========================================================

def start_giveaway_wizard(chat_id, user_id):
    new_draft(user_id)

    send_message(
        chat_id,
        "🎁 <b>СТВОРЕННЯ РОЗІГРАШУ</b>\n\n"
        "Крок 1 із 8\n\n"
        "🏷 Надішліть <b>назву розіграшу</b>.\n\n"
        "Наприклад:\n"
        "🪙 Розіграш пам'ятної монети НБУ",
        cancel_keyboard(),
    )


# =========================================================
# ОБРОБКА МАЙСТРА — ТЕКСТ І ФОТО
# =========================================================

def handle_draft_message(message, user_id, draft):
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    # -----------------------------------------------------
    # НАЗВА
    # -----------------------------------------------------

    if draft["step"] == "title":
        if not text:
            send_message(
                chat_id,
                "⚠️ Надішліть назву текстовим повідомленням.",
                cancel_keyboard(),
            )
            return

        if len(text) > 250:
            send_message(
                chat_id,
                "⚠️ Назва занадто довга. Максимум 250 символів.",
                cancel_keyboard(),
            )
            return

        draft["title"] = text
        draft["step"] = "description"

        send_message(
            chat_id,
            "✅ Назву збережено.\n\n"
            "Крок 2 із 8\n\n"
            "📝 Тепер надішліть <b>опис розіграшу</b>.\n\n"
            "Можна вказати приз, умови та іншу інформацію.",
            cancel_keyboard(),
        )
        return

    # -----------------------------------------------------
    # ОПИС
    # -----------------------------------------------------

    if draft["step"] == "description":
        if not text:
            send_message(
                chat_id,
                "⚠️ Надішліть опис текстовим повідомленням.",
                cancel_keyboard(),
            )
            return

        if len(text) > 3000:
            send_message(
                chat_id,
                "⚠️ Опис занадто довгий. Максимум 3000 символів.",
                cancel_keyboard(),
            )
            return

        draft["description"] = text
        draft["step"] = "photos"

        send_message(
            chat_id,
            "✅ Опис збережено.\n\n"
            "Крок 3 із 8\n\n"
            "📸 Надішліть від <b>1 до 10 фото</b>.\n\n"
            "Можете надсилати по одному або альбомом.\n"
            "Коли закінчите — натисніть <b>«✅ Фото готові»</b>.",
            photos_keyboard(0),
        )
        return

    # -----------------------------------------------------
    # ФОТО
    # -----------------------------------------------------

    if draft["step"] == "photos":
        photos = message.get("photo")

        if not photos:
            send_message(
                chat_id,
                "📸 Зараз очікую фото.\n\n"
                f"Отримано: {len(draft['photos'])}/{MAX_PHOTOS}",
                photos_keyboard(len(draft["photos"])),
            )
            return

        if len(draft["photos"]) >= MAX_PHOTOS:
            send_message(
                chat_id,
                "⚠️ Уже отримано максимальні 10 фото.",
                photos_keyboard(len(draft["photos"])),
            )
            return

        file_id = photos[-1]["file_id"]
        draft["photos"].append(file_id)

        send_message(
            chat_id,
            f"📸 Фото додано: <b>{len(draft['photos'])}/{MAX_PHOTOS}</b>\n\n"
            "Можете додати ще або натиснути «✅ Фото готові».",
            photos_keyboard(len(draft["photos"])),
        )
        return

    # -----------------------------------------------------
    # ДОДАВАННЯ TELEGRAM-ПІДПИСКИ
    # -----------------------------------------------------

    if draft["step"] == "subscription_input":
        if not text:
            send_message(
                chat_id,
                "⚠️ Надішліть @username Telegram-каналу або групи.",
                subscriptions_keyboard(),
            )
            return

        value = text.strip()

        if not value.startswith("@"):
            value = "@" + value.lstrip("/")

        if value not in draft["subscriptions"]:
            draft["subscriptions"].append(value)

        draft["step"] = "subscriptions"

        send_message(
            chat_id,
            "✅ Підписку додано.\n\n"
            f"📢 Зараз додано: <b>{len(draft['subscriptions'])}</b>\n\n"
            f"{subscriptions_text(draft['subscriptions'])}",
            subscriptions_keyboard(),
        )
        return

    # -----------------------------------------------------
    # ЗАВЕРШЕННЯ ЗА ЧАСОМ
    # -----------------------------------------------------

    if draft["step"] == "end_time":
        if not text:
            send_message(
                chat_id,
                "⚠️ Надішліть дату і час текстом.",
                cancel_keyboard(),
            )
            return

        try:
            parsed = datetime.strptime(text, "%d.%m.%Y %H:%M")
            parsed = parsed.replace(tzinfo=KYIV)

            if parsed <= datetime.now(KYIV):
                raise ValueError("past")

        except Exception:
            send_message(
                chat_id,
                "⚠️ Невірний формат або час уже минув.\n\n"
                "Введіть так:\n"
                "<code>05.09.2026 21:00</code>",
                cancel_keyboard(),
            )
            return

        draft["end_value"] = parsed.strftime("%d.%m.%Y %H:%M")
        draft["step"] = "preview"

        show_preview(chat_id, draft)
        return

    # -----------------------------------------------------
    # ЗАВЕРШЕННЯ ЗА КІЛЬКІСТЮ УЧАСНИКІВ
    # -----------------------------------------------------

    if draft["step"] == "end_participants":
        try:
            target = int(text)
        except Exception:
            target = 0

        if target < draft["winners"]:
            send_message(
                chat_id,
                "⚠️ Кількість учасників не може бути меншою "
                "за кількість переможців.\n\n"
                f"Мінімум: <b>{draft['winners']}</b>",
                cancel_keyboard(),
            )
            return

        if target > 100000:
            send_message(
                chat_id,
                "⚠️ Максимум 100000 учасників.",
                cancel_keyboard(),
            )
            return

        draft["end_value"] = target
        draft["step"] = "preview"

        show_preview(chat_id, draft)
        return

    send_message(
        chat_id,
        "⚠️ Використайте кнопки поточного кроку.",
    )


# =========================================================
# ОБРОБКА ПОВІДОМЛЕНЬ
# =========================================================

def handle_message(message):
    # У групах бот повністю мовчить.
    if not is_private_chat(message):
        return

    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    user_id = user.get("id")
    text = (message.get("text") or "").strip()

    # Загальні команди
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
        show_main_menu(chat_id, user_id)
        return

    if text == "/cancel":
        delete_draft(user_id)
        send_message(
            chat_id,
            "❌ Створення розіграшу скасовано.",
        )
        show_main_menu(chat_id, user_id)
        return

    # Створення розіграшів — тільки адміністратор
    if is_admin(user_id):
        draft = get_draft(user_id)

        if draft:
            handle_draft_message(message, user_id, draft)
            return

    # Учасникам довільний текст не створює спам —
    # просто один раз показуємо меню.
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

    # У групах не працюємо.
    if not private_chat_from_callback(callback):
        answer_callback(callback_id)
        return

    # -----------------------------------------------------
    # ГОЛОВНЕ МЕНЮ
    # -----------------------------------------------------

    if data == "create_giveaway":
        if not is_admin(user_id):
            answer_callback(
                callback_id,
                "⛔ Тільки адміністратор може створювати розіграші.",
                True,
            )
            return

        answer_callback(callback_id)
        start_giveaway_wizard(chat_id, user_id)
        return

    if data == "active_giveaways":
        answer_callback(callback_id)

        if not is_admin(user_id):
            return

        send_message(
            chat_id,
            "📋 <b>Активні розіграші</b>\n\n"
            "Поки активних розіграшів немає.\n\n"
            "Після підключення PostgreSQL тут буде повний список.",
        )
        return

    if data == "finished_giveaways":
        answer_callback(callback_id)

        if not is_admin(user_id):
            return

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
            "ℹ️ <b>RandomCoin Bot</b>\n\n"
            "🎁 Створення розіграшів\n"
            "📸 1–10 фото\n"
            "🏆 1–15 переможців\n"
            "📢 Обов'язкові Telegram-підписки\n"
            "🚀 Бусти учасників\n"
            "⏰ Завершення за часом\n"
            "👥 Завершення за кількістю учасників\n\n"
            "Усі адміністративні функції доступні тільки власнику.",
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
            "Коли буде запущено розіграш, бот покаже всі умови "
            "та кнопку участі.",
        )
        return

    # -----------------------------------------------------
    # СКАСУВАННЯ
    # -----------------------------------------------------

    if data == "cancel_create":
        answer_callback(callback_id)

        if not is_admin(user_id):
            return

        delete_draft(user_id)

        send_message(
            chat_id,
            "❌ Створення розіграшу скасовано.",
        )

        show_main_menu(chat_id, user_id)
        return

    # Далі тільки адміністратор
    if not is_admin(user_id):
        answer_callback(
            callback_id,
            "⛔ Недостатньо прав.",
            True,
        )
        return

    draft = get_draft(user_id)

    if not draft:
        answer_callback(
            callback_id,
            "⚠️ Чернетку не знайдено. Почніть створення заново.",
            True,
        )
        return

    # -----------------------------------------------------
    # ФОТО ГОТОВІ
    # -----------------------------------------------------

    if data == "photos_done":
        if len(draft["photos"]) < 1:
            answer_callback(
                callback_id,
                "⚠️ Додайте хоча б одне фото.",
                True,
            )
            return

        answer_callback(callback_id)
        draft["step"] = "winners"

        send_message(
            chat_id,
            "✅ Фото збережено.\n\n"
            "Крок 4 із 8\n\n"
            "🏆 Оберіть <b>кількість переможців</b> від 1 до 15:",
            winners_keyboard(),
        )
        return

    # -----------------------------------------------------
    # КІЛЬКІСТЬ ПЕРЕМОЖЦІВ
    # -----------------------------------------------------

    if data.startswith("winners:"):
        try:
            winners = int(data.split(":", 1)[1])
        except Exception:
            winners = 0

        if winners < MIN_WINNERS or winners > MAX_WINNERS:
            answer_callback(callback_id, "⚠️ Невірне значення.", True)
            return

        answer_callback(callback_id)

        draft["winners"] = winners
        draft["step"] = "subscriptions"

        send_message(
            chat_id,
            f"✅ Переможців: <b>{winners}</b>\n\n"
            "Крок 5 із 8\n\n"
            "📢 Чи потрібні <b>обов'язкові Telegram-підписки</b>?\n\n"
            "Бот зможе перевіряти підписку на канал або групу, "
            "де він має необхідний доступ.",
            subscriptions_keyboard(),
        )
        return

    # -----------------------------------------------------
    # ПІДПИСКИ
    # -----------------------------------------------------

    if data == "subscription_add":
        answer_callback(callback_id)
        draft["step"] = "subscription_input"

        send_message(
            chat_id,
            "📢 Надішліть <b>@username</b> Telegram-каналу або групи.\n\n"
            "Наприклад:\n"
            "<code>@East_Auction</code>\n\n"
            "⚠️ Для автоматичної перевірки бот повинен мати "
            "достатні права в цьому ресурсі.",
            cancel_keyboard(),
        )
        return

    if data == "subscriptions_skip":
        answer_callback(callback_id)

        draft["subscriptions"] = []
        draft["step"] = "boost"

        send_message(
            chat_id,
            "✅ Обов'язкові підписки вимкнено.\n\n"
            "Крок 6 із 8\n\n"
            "🚀 Чи використовувати <b>буст</b> — додаткові шанси "
            "для учасників?",
            boost_keyboard(),
        )
        return

    if data == "subscriptions_done":
        answer_callback(callback_id)

        draft["step"] = "boost"

        send_message(
            chat_id,
            "✅ Налаштування підписок завершено.\n\n"
            "Крок 6 із 8\n\n"
            "🚀 Чи використовувати <b>буст</b> — додаткові шанси "
            "для учасників?",
            boost_keyboard(),
        )
        return

    # -----------------------------------------------------
    # БУСТ
    # -----------------------------------------------------

    if data.startswith("boost:"):
        try:
            multiplier = int(data.split(":", 1)[1])
        except Exception:
            multiplier = 0

        answer_callback(callback_id)

        if multiplier in (2, 3):
            draft["boost_enabled"] = True
            draft["boost_multiplier"] = multiplier
        else:
            draft["boost_enabled"] = False
            draft["boost_multiplier"] = 1

        draft["step"] = "end_mode"

        boost_text = (
            f"увімкнено ×{draft['boost_multiplier']}"
            if draft["boost_enabled"]
            else "вимкнено"
        )

        send_message(
            chat_id,
            f"✅ Буст: <b>{boost_text}</b>\n\n"
            "Крок 7 із 8\n\n"
            "🏁 Як завершувати розіграш?",
            end_mode_keyboard(),
        )
        return

    # -----------------------------------------------------
    # СПОСІБ ЗАВЕРШЕННЯ
    # -----------------------------------------------------

    if data == "end_mode:time":
        answer_callback(callback_id)

        draft["end_mode"] = "time"
        draft["step"] = "end_time"

        send_message(
            chat_id,
            "⏰ <b>Завершення за датою і часом</b>\n\n"
            "Крок 8 із 8\n\n"
            "Надішліть дату і час у форматі:\n"
            "<code>05.09.2026 21:00</code>\n\n"
            "Часовий пояс: <b>Europe/Kyiv</b>.",
            cancel_keyboard(),
        )
        return

    if data == "end_mode:participants":
        answer_callback(callback_id)

        draft["end_mode"] = "participants"
        draft["step"] = "end_participants"

        send_message(
            chat_id,
            "👥 <b>Завершення за кількістю учасників</b>\n\n"
            "Крок 8 із 8\n\n"
            "Надішліть потрібну кількість учасників числом.\n\n"
            f"Мінімум: <b>{draft['winners']}</b>",
            cancel_keyboard(),
        )
        return

    # -----------------------------------------------------
    # ПЕРЕЗАПУСК ЧЕРНЕТКИ
    # -----------------------------------------------------

    if data == "giveaway_restart":
        answer_callback(callback_id)
        start_giveaway_wizard(chat_id, user_id)
        return

    # -----------------------------------------------------
    # ПУБЛІКАЦІЯ
    # -----------------------------------------------------

    if data == "giveaway_publish":
        answer_callback(callback_id)

        # Поки що це тестове завершення майстра.
        # На наступному етапі тут буде запис у PostgreSQL,
        # публікація розіграшу та реєстрація учасників.
        finished_draft = dict(draft)
        delete_draft(user_id)

        send_message(
            chat_id,
            "🎉 <b>РОЗІГРАШ ПІДГОТОВЛЕНО!</b>\n\n"
            "✅ Усі основні параметри зібрані.\n\n"
            f"🏷 {finished_draft['title']}\n"
            f"🏆 Переможців: {finished_draft['winners']}\n"
            f"📸 Фото: {len(finished_draft['photos'])}\n\n"
            "🚧 На наступному етапі підключимо PostgreSQL, "
            "справжню публікацію, кнопку участі та автоматичний "
            "вибір переможців.",
        )

        show_main_menu(chat_id, user_id)
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
