import os
import json
import html
import secrets
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
import requests
from flask import Flask, jsonify, request


app = Flask(__name__)

# =========================================================
# RANDOMCOIN BOT
# =========================================================

APP_VERSION = "randomcoin-2026-09-03-clean-v1"

BOT_TOKEN = os.getenv("RANDOM_BOT_TOKEN")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

RANDOM_PUBLISH_CHAT_ID = os.getenv(
    "RANDOM_PUBLISH_CHAT_ID",
    "-1003918292894",
)
RANDOM_PUBLISH_THREAD_ID = os.getenv("RANDOM_PUBLISH_THREAD_ID")

BOT_USERNAME = os.getenv(
    "RANDOM_BOT_USERNAME",
    "RandomCoinUA_bot",
).lstrip("@")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
KYIV = ZoneInfo("Europe/Kyiv")

MAX_PHOTOS = 10
MIN_WINNERS = 1
MAX_WINNERS = 15

# Telegram-ресурси, підписку на які бот перевіряє автоматично.
# Для стабільної роботи RandomCoin Bot потрібно додати адміністратором
# у групу @EastAuction і канал @East_Auction.
REQUIRED_TELEGRAM = [
    {
        "chat_id": "@EastAuction",
        "title": "⚖️ Telegram-група «Східний Аукціон»",
        "url": "https://t.me/EastAuction",
    },
    {
        "chat_id": "@East_Auction",
        "title": "📢 Telegram-канал East Auction",
        "url": "https://t.me/East_Auction",
    },
]

# Ці умови бот показує як рекомендації.
# Telegram Bot API не дозволяє надійно перевірити, чи користувач
# «підписаний» на іншого бота. Facebook-група потребує окремої
# інтеграції Meta, якої зараз немає.
RECOMMENDED_LINKS = [
    {
        "title": "🤖 Запустити NumizmatCoin_bot",
        "url": "https://t.me/NumizmatCoin_bot",
    },
    {
        "title": "👥 Facebook-група «Східний Аукціон»",
        "url": "https://www.facebook.com/groups/1278662330184542",
    },
]

# Чернетки створення розіграшу зберігаються в RAM.
# Опубліковані розіграші та учасники зберігаються в PostgreSQL.
drafts = {}

_background_started = False
_background_lock = threading.Lock()


# =========================================================
# HELPERS
# =========================================================

def esc(value):
    return html.escape(str(value or ""))


def as_int(value, default=None):
    try:
        return int(value)
    except Exception:
        return default


def is_admin(user_id):
    return bool(
        ADMIN_TELEGRAM_ID
        and str(user_id) == str(ADMIN_TELEGRAM_ID)
    )


def is_private_message(message):
    return message.get("chat", {}).get("type") == "private"


def is_private_callback(callback):
    return (
        callback.get("message", {})
        .get("chat", {})
        .get("type")
        == "private"
    )


def thread_id():
    return as_int(RANDOM_PUBLISH_THREAD_ID)


# =========================================================
# DATABASE
# =========================================================

def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
    )


def init_db():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS randomcoin_giveaways (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    photos JSONB NOT NULL DEFAULT '[]'::jsonb,
                    winners_count INTEGER NOT NULL,
                    boost_multiplier INTEGER NOT NULL DEFAULT 1,
                    end_mode TEXT NOT NULL,
                    end_at TIMESTAMPTZ,
                    participant_target INTEGER,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_by BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at TIMESTAMPTZ,
                    public_chat_id BIGINT,
                    public_message_id BIGINT,
                    winners JSONB NOT NULL DEFAULT '[]'::jsonb
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS randomcoin_participants (
                    id BIGSERIAL PRIMARY KEY,
                    giveaway_id BIGINT NOT NULL
                        REFERENCES randomcoin_giveaways(id)
                        ON DELETE CASCADE,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    weight INTEGER NOT NULL DEFAULT 1,
                    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(giveaway_id, user_id)
                )
                """
            )

            # Безпечні міграції, якщо таблиці вже існують.
            cur.execute(
                """
                ALTER TABLE randomcoin_giveaways
                ADD COLUMN IF NOT EXISTS boost_multiplier INTEGER
                NOT NULL DEFAULT 1
                """
            )
            cur.execute(
                """
                ALTER TABLE randomcoin_giveaways
                ADD COLUMN IF NOT EXISTS public_chat_id BIGINT
                """
            )
            cur.execute(
                """
                ALTER TABLE randomcoin_giveaways
                ADD COLUMN IF NOT EXISTS public_message_id BIGINT
                """
            )
            cur.execute(
                """
                ALTER TABLE randomcoin_giveaways
                ADD COLUMN IF NOT EXISTS winners JSONB
                NOT NULL DEFAULT '[]'::jsonb
                """
            )
            cur.execute(
                """
                ALTER TABLE randomcoin_participants
                ADD COLUMN IF NOT EXISTS weight INTEGER
                NOT NULL DEFAULT 1
                """
            )

        conn.commit()


def create_giveaway(draft, admin_id):
    end_at = None
    participant_target = None

    if draft["end_mode"] == "time":
        end_at = datetime.strptime(
            draft["end_value"],
            "%d.%m.%Y %H:%M",
        ).replace(tzinfo=KYIV)
    else:
        participant_target = int(draft["end_value"])

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO randomcoin_giveaways (
                    title,
                    description,
                    photos,
                    winners_count,
                    boost_multiplier,
                    end_mode,
                    end_at,
                    participant_target,
                    status,
                    created_by
                )
                VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s,%s,'active',%s)
                RETURNING id
                """,
                (
                    draft["title"],
                    draft["description"],
                    json.dumps(draft["photos"]),
                    draft["winners"],
                    draft["boost_multiplier"]
                    if draft["boost_enabled"]
                    else 1,
                    draft["end_mode"],
                    end_at,
                    participant_target,
                    admin_id,
                ),
            )
            row = cur.fetchone()

        conn.commit()

    return int(row["id"])


def delete_giveaway(giveaway_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM randomcoin_giveaways WHERE id=%s",
                (giveaway_id,),
            )
        conn.commit()


def set_public_message(giveaway_id, chat_id, message_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE randomcoin_giveaways
                SET public_chat_id=%s,
                    public_message_id=%s
                WHERE id=%s
                """,
                (chat_id, message_id, giveaway_id),
            )
        conn.commit()


def get_giveaway(giveaway_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM randomcoin_giveaways WHERE id=%s",
                (giveaway_id,),
            )
            return cur.fetchone()


def active_giveaways(limit=20):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    g.*,
                    (
                        SELECT COUNT(*)
                        FROM randomcoin_participants p
                        WHERE p.giveaway_id=g.id
                    ) AS participants_count
                FROM randomcoin_giveaways g
                WHERE g.status='active'
                ORDER BY g.id DESC
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()


def finished_giveaways(limit=20):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    g.*,
                    (
                        SELECT COUNT(*)
                        FROM randomcoin_participants p
                        WHERE p.giveaway_id=g.id
                    ) AS participants_count
                FROM randomcoin_giveaways g
                WHERE g.status='finished'
                ORDER BY g.finished_at DESC NULLS LAST, g.id DESC
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()


def giveaway_participants(giveaway_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM randomcoin_participants
                WHERE giveaway_id=%s
                ORDER BY id
                """,
                (giveaway_id,),
            )
            return cur.fetchall()


def participant_count(giveaway_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS c
                FROM randomcoin_participants
                WHERE giveaway_id=%s
                """,
                (giveaway_id,),
            )
            row = cur.fetchone()

    return int(row["c"])


def register_participant(giveaway_id, user):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO randomcoin_participants (
                    giveaway_id,
                    user_id,
                    username,
                    first_name,
                    weight
                )
                VALUES (%s,%s,%s,%s,1)
                ON CONFLICT (giveaway_id, user_id)
                DO NOTHING
                RETURNING id
                """,
                (
                    giveaway_id,
                    user.get("id"),
                    user.get("username"),
                    user.get("first_name"),
                ),
            )
            row = cur.fetchone()

        conn.commit()

    return row is not None


def grant_boost(giveaway_id, user_id, multiplier):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE randomcoin_participants
                SET weight=%s
                WHERE giveaway_id=%s
                  AND user_id=%s
                RETURNING id
                """,
                (multiplier, giveaway_id, user_id),
            )
            row = cur.fetchone()

        conn.commit()

    return row is not None


def mark_finished(giveaway_id, winners):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE randomcoin_giveaways
                SET status='finished',
                    finished_at=NOW(),
                    winners=%s::jsonb
                WHERE id=%s
                  AND status='active'
                """,
                (
                    json.dumps(winners, ensure_ascii=False),
                    giveaway_id,
                ),
            )
        conn.commit()


# =========================================================
# TELEGRAM API
# =========================================================

def telegram_request(method, data=None):
    if not BOT_TOKEN:
        return {
            "ok": False,
            "description": "RANDOM_BOT_TOKEN is missing",
        }

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
        return {
            "ok": False,
            "description": str(e),
        }


def send_message(
    chat_id,
    text,
    reply_markup=None,
    message_thread_id=None,
):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    if message_thread_id:
        payload["message_thread_id"] = int(message_thread_id)

    return telegram_request("sendMessage", payload)


def send_photo(chat_id, photo, message_thread_id=None):
    payload = {
        "chat_id": chat_id,
        "photo": photo,
    }

    if message_thread_id:
        payload["message_thread_id"] = int(message_thread_id)

    return telegram_request("sendPhoto", payload)


def send_media_group(chat_id, photos, message_thread_id=None):
    media = [
        {
            "type": "photo",
            "media": file_id,
        }
        for file_id in photos
    ]

    payload = {
        "chat_id": chat_id,
        "media": media,
    }

    if message_thread_id:
        payload["message_thread_id"] = int(message_thread_id)

    return telegram_request("sendMediaGroup", payload)


def answer_callback(callback_id, text=None, show_alert=False):
    payload = {
        "callback_query_id": callback_id,
        "show_alert": show_alert,
    }

    if text:
        payload["text"] = text

    return telegram_request("answerCallbackQuery", payload)


def configure_commands():
    default_commands = [
        {"command": "start", "description": "Запустити бота"},
        {"command": "menu", "description": "Відкрити меню"},
        {"command": "giveaways", "description": "Активні розіграші"},
        {"command": "help", "description": "Допомога"},
        {"command": "ping", "description": "Перевірка роботи"},
    ]

    telegram_request(
        "setMyCommands",
        {
            "commands": default_commands,
        },
    )

    # Для приватного чату адміністратора додаємо службові команди.
    admin_id = as_int(ADMIN_TELEGRAM_ID)

    if admin_id:
        admin_commands = default_commands + [
            {"command": "create", "description": "Створити розіграш"},
            {"command": "active", "description": "Активні розіграші"},
            {"command": "finished", "description": "Завершені розіграші"},
            {"command": "participants", "description": "Учасники розіграшу"},
            {"command": "boost", "description": "Надати буст учаснику"},
            {"command": "finish", "description": "Завершити розіграш"},
            {"command": "cancel", "description": "Скасувати чернетку"},
            {"command": "status", "description": "Статус налаштувань"},
        ]

        telegram_request(
            "setMyCommands",
            {
                "commands": admin_commands,
                "scope": {
                    "type": "chat",
                    "chat_id": admin_id,
                },
            },
        )

    telegram_request(
        "setChatMenuButton",
        {
            "menu_button": {
                "type": "commands",
            }
        },
    )


# =========================================================
# SUBSCRIPTIONS
# =========================================================

def check_membership(chat_id, user_id):
    result = telegram_request(
        "getChatMember",
        {
            "chat_id": chat_id,
            "user_id": user_id,
        },
    )

    if not result.get("ok"):
        return False, "api_error"

    member = result.get("result", {})
    status = member.get("status")

    if status in ("creator", "administrator", "member"):
        return True, status

    if status == "restricted":
        return bool(member.get("is_member", False)), status

    return False, status or "unknown"


def check_required_subscriptions(user_id):
    missing = []
    errors = []

    for resource in REQUIRED_TELEGRAM:
        ok, status = check_membership(
            resource["chat_id"],
            user_id,
        )

        if ok:
            continue

        if status == "api_error":
            errors.append(resource)
        else:
            missing.append(resource)

    return missing, errors


# =========================================================
# MENUS
# =========================================================

def admin_inline_menu():
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


def participant_inline_menu():
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


def persistent_keyboard(user_id):
    if is_admin(user_id):
        keyboard = [
            ["🎁 Створити розіграш"],
            ["📋 Активні розіграші", "🏆 Завершені"],
            ["ℹ️ Допомога"],
        ]
    else:
        keyboard = [
            ["🎁 Активні розіграші"],
            ["ℹ️ Допомога"],
        ]

    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "is_persistent": True,
    }


def show_main_menu(chat_id, user_id):
    if is_admin(user_id):
        text = (
            "🎁 <b>RandomCoin Bot</b>\n\n"
            "Панель керування розіграшами.\n\n"
            "Оберіть потрібну дію 👇"
        )
        inline = admin_inline_menu()
    else:
        text = (
            "🎁 <b>RandomCoin Bot</b>\n\n"
            "Тут ви можете брати участь у розіграшах.\n\n"
            "Оберіть дію 👇"
        )
        inline = participant_inline_menu()

    send_message(
        chat_id,
        text,
        inline,
    )

    # Постійне нижнє меню в приватному чаті.
    send_message(
        chat_id,
        "⬇️ <b>Меню бота увімкнено.</b>",
        persistent_keyboard(user_id),
    )


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


def photos_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Фото завантажені",
                    "callback_data": "photos_done",
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


def boost_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🚀 Буст ×2",
                    "callback_data": "boost:2",
                },
                {
                    "text": "🚀 Буст ×3",
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
                    "text": "🔄 Створити заново",
                    "callback_data": "giveaway_restart",
                },
                {
                    "text": "❌ Скасувати",
                    "callback_data": "cancel_create",
                },
            ],
        ]
    }


def join_keyboard(giveaway_id):
    rows = []

    for resource in REQUIRED_TELEGRAM:
        rows.append(
            [
                {
                    "text": resource["title"],
                    "url": resource["url"],
                }
            ]
        )

    for resource in RECOMMENDED_LINKS:
        rows.append(
            [
                {
                    "text": resource["title"],
                    "url": resource["url"],
                }
            ]
        )

    rows.append(
        [
            {
                "text": "✅ Перевірити та взяти участь",
                "callback_data": f"join:{giveaway_id}",
            }
        ]
    )

    return {"inline_keyboard": rows}


# =========================================================
# DRAFTS
# =========================================================

def new_draft(user_id):
    drafts[user_id] = {
        "step": "title",
        "title": "",
        "description": "",
        "photos": [],
        "winners": 1,
        "boost_enabled": False,
        "boost_multiplier": 1,
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
# TEXT
# =========================================================

def draft_end_text(draft):
    if draft["end_mode"] == "time":
        return draft["end_value"]

    return f"після {draft['end_value']} учасників"


def db_end_text(giveaway):
    if giveaway["end_mode"] == "time":
        end_at = giveaway.get("end_at")

        if end_at:
            return end_at.astimezone(KYIV).strftime("%d.%m.%Y %H:%M")

        return "не задано"

    return f"після {giveaway['participant_target']} учасників"


def build_preview(draft):
    boost_text = (
        f"×{draft['boost_multiplier']}"
        if draft["boost_enabled"]
        else "немає"
    )

    return (
        "🎁 <b>ПЕРЕВІРКА РОЗІГРАШУ</b>\n\n"
        f"🏷 <b>Назва:</b>\n{esc(draft['title'])}\n\n"
        f"📝 <b>Опис:</b>\n{esc(draft['description'])}\n\n"
        f"📸 <b>Фото:</b> {len(draft['photos'])}\n"
        f"🏆 <b>Переможців:</b> {draft['winners']}\n"
        f"🚀 <b>Буст:</b> {boost_text}\n"
        f"🏁 <b>Завершення:</b> {esc(draft_end_text(draft))}\n\n"
        "📢 <b>Обов'язкові Telegram-підписки:</b>\n"
        "✅ Група «Східний Аукціон» — @EastAuction\n"
        "✅ Канал East Auction — @East_Auction\n\n"
        "💡 <b>Рекомендації:</b>\n"
        "🤖 запустити @NumizmatCoin_bot\n"
        "👥 вступити до Facebook-групи «Східний Аукціон»\n\n"
        "⚠️ Перевірте дані перед запуском."
    )


def build_public_text(giveaway_id, draft):
    boost_text = (
        f"×{draft['boost_multiplier']}"
        if draft["boost_enabled"]
        else "немає"
    )

    return (
        "🎁 <b>НОВИЙ РОЗІГРАШ</b> 🎁\n\n"
        f"🆔 <b>№{giveaway_id}</b>\n"
        f"🏷 <b>{esc(draft['title'])}</b>\n\n"
        f"📝 {esc(draft['description'])}\n\n"
        f"🏆 Переможців: <b>{draft['winners']}</b>\n"
        f"🚀 Буст: <b>{boost_text}</b>\n"
        f"🏁 Завершення: <b>{esc(draft_end_text(draft))}</b>\n\n"
        "📢 <b>Обов'язкові умови:</b>\n"
        "✅ учасник Telegram-групи «Східний Аукціон»\n"
        "✅ підписка на Telegram-канал East Auction\n\n"
        "💡 <b>Рекомендуємо:</b>\n"
        "🤖 запустити @NumizmatCoin_bot\n"
        "👥 вступити до Facebook-групи «Східний Аукціон»\n\n"
        "👇 Для участі натисніть кнопку нижче."
    )


def show_preview(chat_id, draft):
    # 1 фото — одне фото.
    # 2–10 фото — один Telegram-альбом.
    if len(draft["photos"]) == 1:
        send_photo(
            chat_id,
            draft["photos"][0],
        )
    else:
        send_media_group(
            chat_id,
            draft["photos"],
        )

    # Підтвердження тільки після того, як усі фото вже завантажені.
    send_message(
        chat_id,
        build_preview(draft),
        preview_keyboard(),
    )


# =========================================================
# CREATE WIZARD
# =========================================================

def start_wizard(chat_id, user_id):
    new_draft(user_id)

    send_message(
        chat_id,
        "🎁 <b>СТВОРЕННЯ РОЗІГРАШУ</b>\n\n"
        "Крок 1 із 6\n\n"
        "🏷 Надішліть <b>назву розіграшу</b>.",
        cancel_keyboard(),
    )


def handle_draft_message(message, user_id, draft):
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    if draft["step"] == "title":
        if not text:
            send_message(chat_id, "⚠️ Надішліть назву текстом.")
            return

        draft["title"] = text[:250]
        draft["step"] = "description"

        send_message(
            chat_id,
            "✅ Назву збережено.\n\n"
            "Крок 2 із 6\n\n"
            "📝 Надішліть <b>опис розіграшу</b>.",
            cancel_keyboard(),
        )
        return

    if draft["step"] == "description":
        if not text:
            send_message(chat_id, "⚠️ Надішліть опис текстом.")
            return

        draft["description"] = text[:3000]
        draft["step"] = "photos"

        send_message(
            chat_id,
            "✅ Опис збережено.\n\n"
            "Крок 3 із 6\n\n"
            "📸 Надішліть від <b>1 до 10 фото</b>.\n\n"
            "Можна надіслати всі фото одним альбомом або кількома "
            "повідомленнями.\n\n"
            "🔕 Бот не відповідає після кожного фото.\n"
            "Після завантаження <b>всіх</b> фото натисніть "
            "<b>«✅ Фото завантажені»</b>.",
            photos_keyboard(),
        )
        return

    if draft["step"] == "photos":
        photos = message.get("photo")

        if not photos:
            return

        if len(draft["photos"]) >= MAX_PHOTOS:
            return

        file_id = photos[-1]["file_id"]

        if file_id not in draft["photos"]:
            draft["photos"].append(file_id)

        # Навмисно не надсилаємо відповідь на кожне фото.
        return

    if draft["step"] == "end_time":
        if not text:
            return

        try:
            parsed = datetime.strptime(
                text,
                "%d.%m.%Y %H:%M",
            ).replace(tzinfo=KYIV)

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

        show_preview(
            chat_id,
            draft,
        )
        return

    if draft["step"] == "end_participants":
        target = as_int(text, 0)

        if target < draft["winners"]:
            send_message(
                chat_id,
                "⚠️ Кількість учасників не може бути меншою за "
                "кількість переможців.\n\n"
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

        show_preview(
            chat_id,
            draft,
        )
        return


# =========================================================
# PUBLISH TO GROUP
# =========================================================

def publish_to_group(giveaway_id, draft):
    group_chat_id = as_int(RANDOM_PUBLISH_CHAT_ID)

    if group_chat_id is None:
        raise RuntimeError("RANDOM_PUBLISH_CHAT_ID is invalid")

    current_thread_id = thread_id()

    # Спочатку весь комплект фото.
    if len(draft["photos"]) == 1:
        media_result = send_photo(
            group_chat_id,
            draft["photos"][0],
            message_thread_id=current_thread_id,
        )
    else:
        media_result = send_media_group(
            group_chat_id,
            draft["photos"],
            message_thread_id=current_thread_id,
        )

    if not media_result.get("ok"):
        raise RuntimeError(
            "Не вдалося опублікувати фото: "
            + str(media_result)
        )

    # Потім текст і кнопка, яка веде в приватний чат бота.
    deep_link = (
        f"https://t.me/{BOT_USERNAME}"
        f"?start=giveaway_{giveaway_id}"
    )

    markup = {
        "inline_keyboard": [
            [
                {
                    "text": "🎁 Взяти участь",
                    "url": deep_link,
                }
            ]
        ]
    }

    post_result = send_message(
        group_chat_id,
        build_public_text(giveaway_id, draft),
        markup,
        message_thread_id=current_thread_id,
    )

    if not post_result.get("ok"):
        raise RuntimeError(
            "Не вдалося опублікувати текст: "
            + str(post_result)
        )

    set_public_message(
        giveaway_id,
        group_chat_id,
        post_result["result"]["message_id"],
    )


# =========================================================
# PARTICIPATION
# =========================================================

def show_join_screen(chat_id, giveaway_id):
    giveaway = get_giveaway(giveaway_id)

    if not giveaway or giveaway["status"] != "active":
        send_message(
            chat_id,
            "⚠️ Цей розіграш завершено або не знайдено.",
        )
        return

    send_message(
        chat_id,
        "🎁 <b>УЧАСТЬ У РОЗІГРАШІ</b>\n\n"
        f"🏷 <b>{esc(giveaway['title'])}</b>\n\n"
        "📢 <b>Обов'язково:</b>\n"
        "✅ бути учасником Telegram-групи «Східний Аукціон»\n"
        "✅ бути підписаним на Telegram-канал East Auction\n\n"
        "💡 <b>Рекомендуємо:</b>\n"
        "🤖 запустити NumizmatCoin_bot\n"
        "👥 вступити до Facebook-групи «Східний Аукціон»\n\n"
        "Після виконання умов натисніть кнопку перевірки.",
        join_keyboard(giveaway_id),
    )


def attempt_join(chat_id, user, giveaway_id):
    giveaway = get_giveaway(giveaway_id)

    if not giveaway or giveaway["status"] != "active":
        send_message(
            chat_id,
            "⚠️ Розіграш уже недоступний.",
        )
        return

    missing, technical_errors = check_required_subscriptions(
        user["id"]
    )

    if technical_errors:
        names = "\n".join(
            f"• {esc(item['title'])}"
            for item in technical_errors
        )

        send_message(
            chat_id,
            "⚠️ <b>Не вдалося перевірити Telegram-підписки.</b>\n\n"
            "Переконайтесь, що RandomCoin Bot доданий адміністратором "
            "у ці ресурси:\n\n"
            f"{names}",
            join_keyboard(giveaway_id),
        )
        return

    if missing:
        names = "\n".join(
            f"❌ {esc(item['title'])}"
            for item in missing
        )

        send_message(
            chat_id,
            "⚠️ <b>Не всі обов'язкові умови виконані.</b>\n\n"
            f"{names}\n\n"
            "Підпишіться та натисніть перевірку ще раз.",
            join_keyboard(giveaway_id),
        )
        return

    created = register_participant(
        giveaway_id,
        user,
    )

    count = participant_count(
        giveaway_id
    )

    if created:
        send_message(
            chat_id,
            "🎉 <b>ВИ БЕРЕТЕ УЧАСТЬ!</b>\n\n"
            f"🎁 {esc(giveaway['title'])}\n"
            f"👥 Учасників зараз: <b>{count}</b>\n"
            "🎟 Ваш базовий шанс: <b>1</b>\n\n"
            "✅ Обов'язкові Telegram-підписки перевірено.",
        )
    else:
        send_message(
            chat_id,
            "✅ Ви вже зареєстровані в цьому розіграші.",
        )

    if (
        giveaway["end_mode"] == "participants"
        and giveaway["participant_target"]
        and count >= giveaway["participant_target"]
    ):
        finish_giveaway(giveaway_id)


# =========================================================
# BOOST
# =========================================================

def show_participants_to_admin(chat_id, giveaway_id):
    giveaway = get_giveaway(giveaway_id)

    if not giveaway:
        send_message(chat_id, "⚠️ Розіграш не знайдено.")
        return

    participants = giveaway_participants(giveaway_id)

    if not participants:
        send_message(
            chat_id,
            f"👥 У розіграші #{giveaway_id} учасників ще немає.",
        )
        return

    lines = [
        f"👥 <b>Учасники розіграшу #{giveaway_id}</b>",
        f"🎁 {esc(giveaway['title'])}",
        "",
    ]

    for index, item in enumerate(participants, start=1):
        username = (
            f"@{esc(item['username'])}"
            if item.get("username")
            else esc(item.get("first_name") or "Учасник")
        )

        lines.append(
            f"{index}. {username} — ID <code>{item['user_id']}</code> "
            f"— шансів: <b>{item['weight']}</b>"
        )

    multiplier = int(giveaway.get("boost_multiplier") or 1)

    if multiplier > 1:
        lines.extend(
            [
                "",
                f"🚀 Буст цього розіграшу: ×{multiplier}",
                "Щоб надати буст:",
                f"<code>/boost {giveaway_id} USER_ID</code>",
            ]
        )

    send_message(
        chat_id,
        "\n".join(lines),
    )


def admin_grant_boost(chat_id, giveaway_id, target_user_id):
    giveaway = get_giveaway(giveaway_id)

    if not giveaway or giveaway["status"] != "active":
        send_message(
            chat_id,
            "⚠️ Активний розіграш не знайдено.",
        )
        return

    multiplier = int(giveaway.get("boost_multiplier") or 1)

    if multiplier <= 1:
        send_message(
            chat_id,
            "ℹ️ У цьому розіграші буст вимкнений.",
        )
        return

    updated = grant_boost(
        giveaway_id,
        target_user_id,
        multiplier,
    )

    if not updated:
        send_message(
            chat_id,
            "⚠️ Учасника з таким Telegram ID у цьому розіграші немає.",
        )
        return

    send_message(
        chat_id,
        f"🚀 Буст ×{multiplier} надано учаснику "
        f"<code>{target_user_id}</code> у розіграші #{giveaway_id}.",
    )

    send_message(
        target_user_id,
        f"🚀 <b>Вам надано буст ×{multiplier}!</b>\n\n"
        f"🎁 {esc(giveaway['title'])}\n"
        f"Тепер у жеребкуванні у вас <b>{multiplier}</b> шанси.",
    )


# =========================================================
# WINNERS
# =========================================================

def weighted_unique_winners(participants, winners_count):
    pool = [dict(item) for item in participants]
    winners = []

    need = min(
        int(winners_count),
        len(pool),
    )

    while pool and len(winners) < need:
        total_weight = sum(
            max(1, int(item.get("weight") or 1))
            for item in pool
        )

        pick = secrets.randbelow(total_weight) + 1

        running = 0
        selected_index = 0

        for index, item in enumerate(pool):
            running += max(
                1,
                int(item.get("weight") or 1),
            )

            if pick <= running:
                selected_index = index
                break

        winners.append(
            pool.pop(selected_index)
        )

    return winners


def winner_html(participant):
    username = participant.get("username")

    if username:
        return f"@{esc(username)}"

    user_id = participant["user_id"]
    first_name = esc(
        participant.get("first_name")
        or "Учасник"
    )

    return (
        f'<a href="tg://user?id={user_id}">'
        f"{first_name}</a>"
    )


def finish_giveaway(giveaway_id):
    giveaway = get_giveaway(giveaway_id)

    if not giveaway or giveaway["status"] != "active":
        return False

    participants = giveaway_participants(giveaway_id)

    winners = (
        weighted_unique_winners(
            participants,
            giveaway["winners_count"],
        )
        if participants
        else []
    )

    winners_json = [
        {
            "user_id": item["user_id"],
            "username": item.get("username"),
            "first_name": item.get("first_name"),
            "weight": int(item.get("weight") or 1),
        }
        for item in winners
    ]

    mark_finished(
        giveaway_id,
        winners_json,
    )

    if winners:
        winners_text = "\n".join(
            f"🏆 {index}. {winner_html(item)}"
            for index, item in enumerate(
                winners,
                start=1,
            )
        )
    else:
        winners_text = "😔 Немає зареєстрованих учасників."

    final_text = (
        "🎊🎊🎊 <b>РОЗІГРАШ ЗАВЕРШЕНО!</b> 🎊🎊🎊\n\n"
        f"🎁 <b>{esc(giveaway['title'])}</b>\n\n"
        f"👥 Учасників: <b>{len(participants)}</b>\n"
        f"🏆 Переможців: <b>{len(winners)}</b>\n\n"
        "🥳 <b>НАШІ ПЕРЕМОЖЦІ:</b>\n"
        f"{winners_text}\n\n"
        "✨ Вітаємо переможців!\n"
        "🤝 Дякуємо всім за участь!\n\n"
        "🎁 До зустрічі в наступних розіграшах!"
    )

    group_chat_id = as_int(RANDOM_PUBLISH_CHAT_ID)

    if group_chat_id:
        send_message(
            group_chat_id,
            final_text,
            message_thread_id=thread_id(),
        )

    admin_id = as_int(ADMIN_TELEGRAM_ID)

    if admin_id:
        send_message(
            admin_id,
            final_text,
        )

    return True


# =========================================================
# AUTO FINISH
# =========================================================

def finish_due_giveaways():
    now = datetime.now(KYIV)

    for giveaway in active_giveaways(limit=100):
        if (
            giveaway["end_mode"] == "time"
            and giveaway.get("end_at")
            and giveaway["end_at"].astimezone(KYIV) <= now
        ):
            finish_giveaway(
                giveaway["id"]
            )


def background_loop():
    while True:
        try:
            finish_due_giveaways()
        except Exception as e:
            print("BACKGROUND ERROR:", repr(e))

        time.sleep(30)


def ensure_started():
    global _background_started

    if _background_started:
        return

    with _background_lock:
        if _background_started:
            return

        init_db()
        configure_commands()

        thread = threading.Thread(
            target=background_loop,
            daemon=True,
        )
        thread.start()

        _background_started = True

        print("RANDOMCOIN STARTED:", APP_VERSION)


# =========================================================
# LISTS
# =========================================================

def show_active(chat_id, for_admin=False):
    rows = active_giveaways()

    if not rows:
        send_message(
            chat_id,
            "🎁 Активних розіграшів поки немає.",
        )
        return

    for giveaway in rows[:20]:
        text = (
            f"🎁 <b>#{giveaway['id']} {esc(giveaway['title'])}</b>\n\n"
            f"👥 Учасників: <b>{giveaway['participants_count']}</b>\n"
            f"🏆 Переможців: <b>{giveaway['winners_count']}</b>\n"
            f"🏁 Завершення: <b>{esc(db_end_text(giveaway))}</b>"
        )

        if for_admin:
            markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": "👥 Учасники",
                            "callback_data": f"admin_participants:{giveaway['id']}",
                        },
                        {
                            "text": "🏁 Завершити",
                            "callback_data": f"admin_finish:{giveaway['id']}",
                        },
                    ]
                ]
            }
            send_message(
                chat_id,
                text,
                markup,
            )
        else:
            deep_link = (
                f"https://t.me/{BOT_USERNAME}"
                f"?start=giveaway_{giveaway['id']}"
            )
            send_message(
                chat_id,
                text,
                {
                    "inline_keyboard": [
                        [
                            {
                                "text": "🎁 Взяти участь",
                                "url": deep_link,
                            }
                        ]
                    ]
                },
            )


def show_finished(chat_id):
    rows = finished_giveaways()

    if not rows:
        send_message(
            chat_id,
            "🏆 Завершених розіграшів поки немає.",
        )
        return

    for giveaway in rows[:20]:
        send_message(
            chat_id,
            f"🏆 <b>#{giveaway['id']} {esc(giveaway['title'])}</b>\n"
            f"👥 Учасників: {giveaway['participants_count']}\n"
            "✅ Завершено",
        )


# =========================================================
# STATUS
# =========================================================

def show_status(chat_id):
    group_id = as_int(RANDOM_PUBLISH_CHAT_ID)
    database_ok = False
    database_error = None

    try:
        init_db()
        database_ok = True
    except Exception as e:
        database_error = str(e)

    lines = [
        "🛠 <b>СТАТУС RANDOMCOIN BOT</b>",
        "",
        f"🔧 Версія: <code>{APP_VERSION}</code>",
        f"🤖 Username: @{esc(BOT_USERNAME)}",
        f"👤 Admin ID: <code>{esc(ADMIN_TELEGRAM_ID)}</code>",
        f"💾 PostgreSQL: {'✅' if database_ok else '❌'}",
        f"👥 Група для публікації: <code>{esc(RANDOM_PUBLISH_CHAT_ID)}</code>",
    ]

    if database_error:
        lines.append(f"⚠️ DB: {esc(database_error)}")

    # Перевіряємо, чи сам бот бачить потрібні Telegram-ресурси.
    me = telegram_request("getMe")
    bot_id = None

    if me.get("ok"):
        bot_id = me["result"]["id"]

    if bot_id:
        lines.append("")
        lines.append("📢 <b>Права у ресурсах:</b>")

        for resource in REQUIRED_TELEGRAM:
            ok, status = check_membership(
                resource["chat_id"],
                bot_id,
            )
            lines.append(
                f"{'✅' if ok else '❌'} {esc(resource['title'])} "
                f"— {esc(status)}"
            )

    send_message(
        chat_id,
        "\n".join(lines),
    )


# =========================================================
# MESSAGE HANDLER
# =========================================================

def handle_message(message):
    # Бот у групах мовчить.
    if not is_private_message(message):
        return

    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    user_id = user.get("id")
    text = (message.get("text") or "").strip()

    # Deep-link з кнопки «Взяти участь».
    if text.startswith("/start giveaway_"):
        giveaway_id = as_int(
            text.split("giveaway_", 1)[1].split()[0],
            0,
        )

        if giveaway_id:
            show_join_screen(
                chat_id,
                giveaway_id,
            )
            return

    if text in ("/start", "/menu"):
        show_main_menu(
            chat_id,
            user_id,
        )
        return

    if text == "/ping":
        send_message(
            chat_id,
            "🏓 <b>Pong!</b>\n\n"
            "🎁 RandomCoin Bot працює нормально ✅\n"
            f"🔧 <code>{APP_VERSION}</code>",
        )
        return

    if text in ("/giveaways", "🎁 Активні розіграші"):
        show_active(
            chat_id,
            for_admin=is_admin(user_id),
        )
        return

    if text in ("/help", "ℹ️ Допомога"):
        send_message(
            chat_id,
            "ℹ️ <b>RandomCoin Bot</b>\n\n"
            "🎁 Створення та участь у розіграшах\n"
            "📸 1–10 фото\n"
            "🏆 1–15 переможців\n"
            "📢 Перевірка Telegram-підписок\n"
            "🚀 Адмін-керований буст ×2 / ×3\n"
            "⏰ Завершення за часом\n"
            "👥 Завершення за кількістю учасників\n"
            "🏆 Випадковий вибір переможців",
        )
        return

    # Далі службові дії доступні тільки власнику.
    if text == "/create":
        if is_admin(user_id):
            start_wizard(chat_id, user_id)
        else:
            send_message(chat_id, "⛔ Недостатньо прав.")
        return

    if text == "/active":
        if is_admin(user_id):
            show_active(chat_id, for_admin=True)
        else:
            send_message(chat_id, "⛔ Недостатньо прав.")
        return

    if text == "/finished":
        if is_admin(user_id):
            show_finished(chat_id)
        else:
            send_message(chat_id, "⛔ Недостатньо прав.")
        return

    if text == "/status":
        if is_admin(user_id):
            show_status(chat_id)
        else:
            send_message(chat_id, "⛔ Недостатньо прав.")
        return

    if text.startswith("/participants"):
        if not is_admin(user_id):
            send_message(chat_id, "⛔ Недостатньо прав.")
            return

        parts = text.split()

        if len(parts) != 2 or not parts[1].isdigit():
            send_message(
                chat_id,
                "Використання:\n"
                "<code>/participants GIVEAWAY_ID</code>",
            )
            return

        show_participants_to_admin(
            chat_id,
            int(parts[1]),
        )
        return

    if text.startswith("/boost"):
        if not is_admin(user_id):
            send_message(chat_id, "⛔ Недостатньо прав.")
            return

        parts = text.split()

        if (
            len(parts) != 3
            or not parts[1].isdigit()
            or not parts[2].isdigit()
        ):
            send_message(
                chat_id,
                "Використання:\n"
                "<code>/boost GIVEAWAY_ID USER_ID</code>",
            )
            return

        admin_grant_boost(
            chat_id,
            int(parts[1]),
            int(parts[2]),
        )
        return

    if text.startswith("/finish"):
        if not is_admin(user_id):
            send_message(chat_id, "⛔ Недостатньо прав.")
            return

        parts = text.split()

        if len(parts) != 2 or not parts[1].isdigit():
            send_message(
                chat_id,
                "Використання:\n"
                "<code>/finish GIVEAWAY_ID</code>",
            )
            return

        if finish_giveaway(int(parts[1])):
            send_message(
                chat_id,
                "✅ Розіграш завершено.",
            )
        else:
            send_message(
                chat_id,
                "⚠️ Активний розіграш не знайдено.",
            )
        return

    if text == "/cancel":
        if is_admin(user_id):
            delete_draft(user_id)
            send_message(
                chat_id,
                "❌ Створення розіграшу скасовано.",
            )
            show_main_menu(chat_id, user_id)
        return

    if text == "🎁 Створити розіграш":
        if not is_admin(user_id):
            send_message(
                chat_id,
                "⛔ Ця функція доступна тільки адміністратору.",
            )
            return

        start_wizard(
            chat_id,
            user_id,
        )
        return

    if (
        text == "📋 Активні розіграші"
        and is_admin(user_id)
    ):
        show_active(
            chat_id,
            for_admin=True,
        )
        return

    if (
        text == "🏆 Завершені"
        and is_admin(user_id)
    ):
        show_finished(chat_id)
        return

    if is_admin(user_id):
        draft = get_draft(user_id)

        if draft:
            handle_draft_message(
                message,
                user_id,
                draft,
            )
            return

    show_main_menu(
        chat_id,
        user_id,
    )


# =========================================================
# CALLBACK HANDLER
# =========================================================

def handle_callback(callback):
    callback_id = callback["id"]
    user = callback.get("from", {})
    user_id = user.get("id")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    data = callback.get("data", "")

    # У групах callback-дії не використовуємо.
    if not is_private_callback(callback):
        answer_callback(callback_id)
        return

    if data.startswith("join:"):
        answer_callback(callback_id)

        giveaway_id = as_int(
            data.split(":", 1)[1],
            0,
        )

        if giveaway_id:
            attempt_join(
                chat_id,
                user,
                giveaway_id,
            )
        return

    if data.startswith("admin_participants:"):
        answer_callback(callback_id)

        if not is_admin(user_id):
            return

        giveaway_id = as_int(
            data.split(":", 1)[1],
            0,
        )

        show_participants_to_admin(
            chat_id,
            giveaway_id,
        )
        return

    if data.startswith("admin_finish:"):
        if not is_admin(user_id):
            answer_callback(
                callback_id,
                "⛔ Недостатньо прав.",
                True,
            )
            return

        giveaway_id = as_int(
            data.split(":", 1)[1],
            0,
        )

        answer_callback(callback_id)

        if finish_giveaway(giveaway_id):
            send_message(
                chat_id,
                "✅ Розіграш завершено вручну.",
            )
        else:
            send_message(
                chat_id,
                "⚠️ Активний розіграш не знайдено.",
            )
        return

    if data == "create_giveaway":
        if not is_admin(user_id):
            answer_callback(
                callback_id,
                "⛔ Тільки адміністратор.",
                True,
            )
            return

        answer_callback(callback_id)
        start_wizard(chat_id, user_id)
        return

    if data == "active_giveaways":
        answer_callback(callback_id)

        if is_admin(user_id):
            show_active(
                chat_id,
                for_admin=True,
            )
        return

    if data == "finished_giveaways":
        answer_callback(callback_id)

        if is_admin(user_id):
            show_finished(chat_id)
        return

    if data == "participant_active":
        answer_callback(callback_id)

        show_active(
            chat_id,
            for_admin=False,
        )
        return

    if data in ("help", "participant_help"):
        answer_callback(callback_id)

        send_message(
            chat_id,
            "ℹ️ <b>RandomCoin Bot</b>\n\n"
            "🎁 Розіграші\n"
            "📢 Перевірка Telegram-підписок\n"
            "🚀 Бусти\n"
            "🏆 Автоматичний вибір переможців",
        )
        return

    if data == "cancel_create":
        answer_callback(callback_id)

        if is_admin(user_id):
            delete_draft(user_id)
            send_message(
                chat_id,
                "❌ Створення розіграшу скасовано.",
            )
            show_main_menu(chat_id, user_id)
        return

    # Далі тільки адміністратор.
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
            "⚠️ Чернетку не знайдено. Почніть заново.",
            True,
        )
        return

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
            f"✅ Отримано фото: <b>{len(draft['photos'])}</b>\n\n"
            "Крок 4 із 6\n\n"
            "🏆 Оберіть кількість переможців від 1 до 15:",
            winners_keyboard(),
        )
        return

    if data.startswith("winners:"):
        winners = as_int(
            data.split(":", 1)[1],
            0,
        )

        if not (
            MIN_WINNERS
            <= winners
            <= MAX_WINNERS
        ):
            answer_callback(
                callback_id,
                "⚠️ Невірна кількість.",
                True,
            )
            return

        answer_callback(callback_id)

        draft["winners"] = winners
        draft["step"] = "boost"

        send_message(
            chat_id,
            f"✅ Переможців: <b>{winners}</b>\n\n"
            "Крок 5 із 6\n\n"
            "🚀 Чи використовувати буст додаткових шансів?\n\n"
            "Буст зможеш надати конкретному учаснику сам.",
            boost_keyboard(),
        )
        return

    if data.startswith("boost:"):
        multiplier = as_int(
            data.split(":", 1)[1],
            0,
        )

        answer_callback(callback_id)

        if multiplier in (2, 3):
            draft["boost_enabled"] = True
            draft["boost_multiplier"] = multiplier
        else:
            draft["boost_enabled"] = False
            draft["boost_multiplier"] = 1

        draft["step"] = "end_mode"

        send_message(
            chat_id,
            "✅ Буст налаштовано.\n\n"
            "Крок 6 із 6\n\n"
            "🏁 Як завершувати розіграш?",
            end_mode_keyboard(),
        )
        return

    if data == "end_mode:time":
        answer_callback(callback_id)

        draft["end_mode"] = "time"
        draft["step"] = "end_time"

        send_message(
            chat_id,
            "⏰ Надішліть дату і час у форматі:\n"
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
            "👥 Надішліть потрібну кількість учасників числом.\n\n"
            f"Мінімум: <b>{draft['winners']}</b>",
            cancel_keyboard(),
        )
        return

    if data == "giveaway_restart":
        answer_callback(callback_id)
        start_wizard(chat_id, user_id)
        return

    if data == "giveaway_publish":
        answer_callback(callback_id)

        giveaway_id = None

        try:
            giveaway_id = create_giveaway(
                draft,
                user_id,
            )

            publish_to_group(
                giveaway_id,
                draft,
            )

        except Exception as e:
            print(
                "PUBLISH GIVEAWAY ERROR:",
                repr(e),
            )

            if giveaway_id:
                try:
                    delete_giveaway(giveaway_id)
                except Exception as cleanup_error:
                    print(
                        "PUBLISH CLEANUP ERROR:",
                        repr(cleanup_error),
                    )

            send_message(
                chat_id,
                "❌ <b>Не вдалося запустити розіграш.</b>\n\n"
                "Перевірте:\n"
                "• DATABASE_URL\n"
                "• RANDOM_PUBLISH_CHAT_ID\n"
                "• чи RandomCoin Bot доданий адміністратором у групу\n"
                "• чи має він право надсилати повідомлення та фото\n\n"
                f"🔧 <code>{APP_VERSION}</code>",
            )
            return

        launched = dict(draft)
        delete_draft(user_id)

        send_message(
            chat_id,
            "🎉 <b>РОЗІГРАШ ЗАПУЩЕНО!</b>\n\n"
            f"🆔 №{giveaway_id}\n"
            f"🏷 {esc(launched['title'])}\n"
            f"📸 Фото: {len(launched['photos'])}\n"
            f"🏆 Переможців: {launched['winners']}\n\n"
            "✅ Збережено в PostgreSQL.\n"
            "✅ Опубліковано в Telegram-групі.\n"
            "✅ Кнопка «🎁 Взяти участь» активна.\n\n"
            f"🔧 <code>{APP_VERSION}</code>",
        )

        show_main_menu(
            chat_id,
            user_id,
        )
        return

    answer_callback(callback_id)


# =========================================================
# WEB
# =========================================================

@app.route("/", methods=["GET"])
def home():
    try:
        ensure_started()

        return jsonify(
            {
                "ok": True,
                "version": APP_VERSION,
                "database": bool(DATABASE_URL),
                "publish_chat": RANDOM_PUBLISH_CHAT_ID,
                "bot_username": BOT_USERNAME,
            }
        )

    except Exception as e:
        return jsonify(
            {
                "ok": False,
                "version": APP_VERSION,
                "error": str(e),
            }
        ), 500


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    try:
        ensure_started()
    except Exception as e:
        print(
            "STARTUP ERROR:",
            repr(e),
        )

    update = (
        request.get_json(
            silent=True
        )
        or {}
    )

    try:
        if update.get("callback_query"):
            handle_callback(
                update["callback_query"]
            )

        elif update.get("message"):
            handle_message(
                update["message"]
            )

    except Exception as e:
        print(
            "WEBHOOK ERROR:",
            repr(e),
        )

    return jsonify(
        {
            "ok": True,
            "version": APP_VERSION,
        }
    )


# =========================================================
# START
# =========================================================

try:
    configure_commands()

    if DATABASE_URL:
        ensure_started()

except Exception as e:
    print(
        "STARTUP ERROR:",
        repr(e),
    )


if __name__ == "__main__":
    port = int(
        os.environ.get(
            "PORT",
            10000,
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
