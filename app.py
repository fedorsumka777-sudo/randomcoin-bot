import os
import json
import time
import psycopg
from psycopg.rows import dict_row
import threading
import calendar
import hmac
import io
import tempfile
import uuid
import hashlib
import base64
import re
from difflib import SequenceMatcher
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont
from flask import Flask, request, jsonify


app = Flask(__name__)

# =========================================================
# НАЛАШТУВАННЯ
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TEST_BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "NumizmatCoin_bot").lstrip("@")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

TELEGRAM_CHANNEL_URL = "https://t.me/East_Auction"
TELEGRAM_GROUP_URL = "https://t.me/EastAuction"
FACEBOOK_URL = "https://www.facebook.com/groups/1278662330184542"

# Ресурси спільноти для адмін-статистики.
COMMUNITY_GROUP_CHAT = os.getenv("COMMUNITY_GROUP_CHAT", "@EastAuction")
COMMUNITY_CHANNEL_CHAT = os.getenv("COMMUNITY_CHANNEL_CHAT", "@East_Auction")
COMMUNITY_GROUP_USERNAME = os.getenv("COMMUNITY_GROUP_USERNAME", "EastAuction").lstrip("@").casefold()

# Куди публікувати лоти. Якщо не задано — для тесту публікуємо в чат продавця.
PUBLISH_CHAT_ID = os.getenv("PUBLISH_CHAT_ID")
NUMIZMATIKA_THREAD_ID = os.getenv("NUMIZMATIKA_THREAD_ID")
BONISTIKA_THREAD_ID = os.getenv("BONISTIKA_THREAD_ID")
REVIEWS_THREAD_ID = os.getenv("REVIEWS_THREAD_ID")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://numizmat-auction-bot-2.onrender.com").rstrip("/")

# Постійна база Render PostgreSQL.
DATABASE_URL = os.getenv("DATABASE_URL")
KYIV = ZoneInfo("Europe/Kyiv")
APP_VERSION = "numizmatcoin-2026-09-03-admin-panel-v5"
MAX_PHOTOS = 10

UA_MONTHS = {
    1: "Січень",
    2: "Лютий",
    3: "Березень",
    4: "Квітень",
    5: "Травень",
    6: "Червень",
    7: "Липень",
    8: "Серпень",
    9: "Вересень",
    10: "Жовтень",
    11: "Листопад",
    12: "Грудень",
}


# Захист від дублювання запуску фонового циклу в одному процесі.
_background_started = False
_background_lock = threading.Lock()


# =========================================================
# БАЗА ДАНИХ — POSTGRESQL
# =========================================================

class DBConnection:
    """
    Невеликий сумісний шар, щоб решта коду могла й надалі
    використовувати SQL-плейсхолдери '?'.
    Для PostgreSQL вони автоматично перетворюються на '%s'.
    """

    def __init__(self):
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not configured. "
                "Add Render Internal Database URL to Environment."
            )
        self.conn = psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row,
            connect_timeout=15,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
        return False

    def execute(self, sql, params=None):
        cleaned = sql.strip()

        # SQLite використовував BEGIN IMMEDIATE.
        # У PostgreSQL транзакція починається автоматично.
        if cleaned.upper() == "BEGIN IMMEDIATE":
            return self.conn.execute("SELECT 1")

        pg_sql = sql.replace("?", "%s")
        return self.conn.execute(pg_sql, params or ())


def db():
    return DBConnection()


def init_db():
    schema_statements = [
        """
        CREATE TABLE IF NOT EXISTS drafts (
            user_id BIGINT PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            state TEXT NOT NULL,
            data_json TEXT NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS lots (
            id BIGSERIAL PRIMARY KEY,
            seller_id BIGINT NOT NULL,
            seller_chat_id BIGINT NOT NULL,
            seller_name TEXT,
            section TEXT NOT NULL,
            category TEXT NOT NULL,
            subcategory TEXT,
            category_key TEXT,
            subcategory_key TEXT,
            sale_type TEXT NOT NULL,
            title TEXT NOT NULL,
            material TEXT NOT NULL,
            fixed_price BIGINT,
            start_price BIGINT,
            current_price BIGINT,
            bid_step BIGINT,
            blitz_price BIGINT,
            reserve_price BIGINT,
            end_ts DOUBLE PRECISION NOT NULL,
            anti_sniper INTEGER NOT NULL DEFAULT 0,
            phone TEXT NOT NULL,
            card_last4 TEXT NOT NULL,
            extra_info TEXT,
            status TEXT NOT NULL,
            leader_id BIGINT,
            leader_name TEXT,
            winner_id BIGINT,
            winner_name TEXT,
            published_chat_id BIGINT,
            published_message_id BIGINT,
            published_content_message_id BIGINT,
            published_thread_id BIGINT,
            created_at DOUBLE PRECISION NOT NULL,
            published_at DOUBLE PRECISION,
            finished_at DOUBLE PRECISION
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS lot_photos (
            id BIGSERIAL PRIMARY KEY,
            lot_id BIGINT NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
            file_id TEXT NOT NULL,
            position INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS proxy_bids (
            lot_id BIGINT NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            user_name TEXT,
            max_amount BIGINT NOT NULL,
            priority_ts DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(lot_id, user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS scam_reports (
            id BIGSERIAL PRIMARY KEY,
            reporter_id BIGINT NOT NULL,
            reporter_name TEXT,
            subject_text TEXT NOT NULL,
            subject_normalized TEXT,
            card_hash TEXT,
            card_last4 TEXT,
            description TEXT NOT NULL,
            associates TEXT,
            evidence TEXT,
            source TEXT NOT NULL DEFAULT 'user_report',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at DOUBLE PRECISION NOT NULL,
            moderated_at DOUBLE PRECISION,
            moderated_by BIGINT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS scam_identifiers (
            id BIGSERIAL PRIMARY KEY,
            report_id BIGINT NOT NULL REFERENCES scam_reports(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            value_display TEXT,
            value_normalized TEXT,
            value_hash TEXT,
            value_last4 TEXT,
            created_at DOUBLE PRECISION NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_scam_identifiers_report ON scam_identifiers(report_id)",
        "CREATE INDEX IF NOT EXISTS idx_scam_identifiers_kind_norm ON scam_identifiers(kind, value_normalized)",
        "CREATE INDEX IF NOT EXISTS idx_scam_identifiers_kind_hash ON scam_identifiers(kind, value_hash)",
        """
        CREATE TABLE IF NOT EXISTS scam_card_fragments (
            id BIGSERIAL PRIMARY KEY,
            report_id BIGINT NOT NULL REFERENCES scam_reports(id) ON DELETE CASCADE,
            fragment_len INTEGER NOT NULL,
            fragment_hash TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            UNIQUE(report_id, fragment_len, fragment_hash)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_scam_card_fragments_lookup ON scam_card_fragments(fragment_len, fragment_hash)",
        "CREATE INDEX IF NOT EXISTS idx_scam_card_fragments_report ON scam_card_fragments(report_id)",
        """
        CREATE TABLE IF NOT EXISTS lot_subscriptions (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            section TEXT NOT NULL,
            category_key TEXT,
            subcategory_key TEXT,
            created_at DOUBLE PRECISION NOT NULL,
            UNIQUE(user_id, section, category_key, subcategory_key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_lot_subscriptions_user ON lot_subscriptions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_lot_subscriptions_match ON lot_subscriptions(section, category_key, subcategory_key)",
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id BIGSERIAL PRIMARY KEY,
            reviewer_id BIGINT NOT NULL,
            reviewer_name TEXT,
            target_username TEXT NOT NULL,
            target_display TEXT,
            rating INTEGER NOT NULL CHECK(rating IN (-1, 1)),
            comment TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bid_history (
            id BIGSERIAL PRIMARY KEY,
            lot_id BIGINT NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            user_name TEXT,
            requested_max BIGINT NOT NULL,
            resulting_price BIGINT NOT NULL,
            resulting_leader_id BIGINT,
            created_at DOUBLE PRECISION NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            first_seen DOUBLE PRECISION NOT NULL,
            last_seen DOUBLE PRECISION NOT NULL,
            use_count BIGINT NOT NULL DEFAULT 1
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bot_activity (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT,
            username TEXT,
            display_name TEXT,
            action TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS lot_favorites (
            user_id BIGINT NOT NULL,
            lot_id BIGINT NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
            created_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(user_id, lot_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS guarantee_requests (
            id BIGSERIAL PRIMARY KEY,
            lot_id BIGINT NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
            buyer_id BIGINT NOT NULL,
            buyer_name TEXT,
            seller_id BIGINT NOT NULL,
            seller_name TEXT,
            amount BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notification_flags (
            user_id BIGINT NOT NULL,
            lot_id BIGINT NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(user_id, lot_id, kind)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS community_activity (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            chat_username TEXT,
            chat_title TEXT,
            user_id BIGINT,
            username TEXT,
            display_name TEXT,
            event_type TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_scam_subject ON scam_reports(subject_normalized)",
        "CREATE INDEX IF NOT EXISTS idx_scam_card_hash ON scam_reports(card_hash)",
        "CREATE INDEX IF NOT EXISTS idx_scam_status ON scam_reports(status)",
        "CREATE INDEX IF NOT EXISTS idx_reviews_target ON reviews(target_username)",
        "CREATE INDEX IF NOT EXISTS idx_lots_status_end ON lots(status, end_ts)",
        "CREATE INDEX IF NOT EXISTS idx_lots_category ON lots(category, subcategory)",
        "CREATE INDEX IF NOT EXISTS idx_bot_activity_created ON bot_activity(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_bot_activity_user ON bot_activity(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_favorites_lot ON lot_favorites(lot_id)",
        "CREATE INDEX IF NOT EXISTS idx_guarantee_status ON guarantee_requests(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_guarantee_buyer ON guarantee_requests(buyer_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_guarantee_seller ON guarantee_requests(seller_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_community_activity_chat_time ON community_activity(chat_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_community_activity_username_time ON community_activity(chat_username, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_community_activity_event_time ON community_activity(event_type, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_community_activity_user_time ON community_activity(user_id, created_at)",
        "ALTER TABLE lots ADD COLUMN IF NOT EXISTS reserve_reached_notified BOOLEAN NOT NULL DEFAULT FALSE",
        # Безпечні міграції для баз, створених старішими версіями.
        "ALTER TABLE lots ADD COLUMN IF NOT EXISTS published_content_message_id BIGINT",
        "ALTER TABLE lots ADD COLUMN IF NOT EXISTS subcategory TEXT",
        "ALTER TABLE lots ADD COLUMN IF NOT EXISTS category_key TEXT",
        "ALTER TABLE lots ADD COLUMN IF NOT EXISTS subcategory_key TEXT",
        "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS target_display TEXT",
        "ALTER TABLE scam_reports ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'user_report'",
        "ALTER TABLE scam_identifiers ADD COLUMN IF NOT EXISTS value_last6 TEXT",
        "ALTER TABLE scam_identifiers ADD COLUMN IF NOT EXISTS value_last8 TEXT",
        "CREATE INDEX IF NOT EXISTS idx_scam_identifiers_card_last4 ON scam_identifiers(kind, value_last4)",
        "CREATE INDEX IF NOT EXISTS idx_scam_identifiers_card_last6 ON scam_identifiers(kind, value_last6)",
        "CREATE INDEX IF NOT EXISTS idx_scam_identifiers_card_last8 ON scam_identifiers(kind, value_last8)",
    ]

    with db() as conn:
        for statement in schema_statements:
            conn.execute(statement)


def database_health():
    try:
        with db() as conn:
            row = conn.execute(
                """
                SELECT
                    current_database() AS database_name,
                    current_user AS database_user,
                    version() AS version
                """
            ).fetchone()
        return {
            "ok": True,
            "backend": "postgresql",
            "database": row["database_name"],
            "user": row["database_user"],
        }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "postgresql",
            "error": str(exc),
        }


def get_draft(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "user_id": row["user_id"],
        "chat_id": row["chat_id"],
        "state": row["state"],
        "data": json.loads(row["data_json"]),
    }


def save_draft(user_id, chat_id, state, data):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO drafts(user_id, chat_id, state, data_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                state=excluded.state,
                data_json=excluded.data_json,
                updated_at=excluded.updated_at
            """,
            (user_id, chat_id, state, json.dumps(data, ensure_ascii=False), time.time()),
        )


def delete_draft(user_id):
    with db() as conn:
        conn.execute("DELETE FROM drafts WHERE user_id = ?", (user_id,))


# =========================================================
# TELEGRAM API
# =========================================================

def telegram_api(method, data=None):
    if not BOT_TOKEN:
        return {"ok": False, "description": "TEST_BOT_TOKEN is not configured"}
    try:
        response = requests.post(
            f"{TELEGRAM_API}/{method}",
            json=data or {},
            timeout=25,
        )
        return response.json()
    except Exception as e:
        print("TELEGRAM API ERROR:", method, repr(e))
        return {"ok": False, "description": repr(e)}


def send_message(chat_id, text, reply_markup=None, message_thread_id=None):
    data = {"chat_id": chat_id, "text": text}
    if reply_markup:
        data["reply_markup"] = reply_markup
    if message_thread_id:
        data["message_thread_id"] = int(message_thread_id)
    return telegram_api("sendMessage", data)


def edit_message(chat_id, message_id, text, reply_markup=None):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if reply_markup is not None:
        data["reply_markup"] = reply_markup
    return telegram_api("editMessageText", data)


def edit_caption(chat_id, message_id, caption, reply_markup=None):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": caption,
    }
    if reply_markup is not None:
        data["reply_markup"] = reply_markup
    return telegram_api("editMessageCaption", data)


def delete_message(chat_id, message_id):
    return telegram_api(
        "deleteMessage",
        {"chat_id": chat_id, "message_id": message_id},
    )


def delete_later(chat_id, message_id, delay=12):
    def _worker():
        time.sleep(delay)
        try:
            delete_message(chat_id, message_id)
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()


def register_bot_commands():
    private_commands = [
        {"command": "start", "description": "Запустити бота"},
        {"command": "bot_menu", "description": "Головне меню бота"},
        {"command": "help", "description": "Список команд"},
        {"command": "ping", "description": "Перевірка роботи бота"},
        {"command": "rules", "description": "Правила групи"},
        {"command": "lot", "description": "Виставити лот"},
        {"command": "search_lot", "description": "Пошук лотів"},
        {"command": "my_lots", "description": "Мої лоти"},
        {"command": "favorites", "description": "Обрані лоти"},
        {"command": "profile", "description": "Моя репутація"},
        {"command": "threadid", "description": "ID поточної теми"},
    ]

    group_commands = [
        {"command": "bot_menu", "description": "Головне меню бота"},
        {"command": "help", "description": "Список команд"},
        {"command": "ping", "description": "Перевірка роботи бота"},
        {"command": "rules", "description": "Правила групи"},
        {"command": "threadid", "description": "ID поточної теми"},
    ]

    telegram_api(
        "setMyCommands",
        {
            "commands": private_commands,
            "scope": {"type": "all_private_chats"},
        },
    )

    telegram_api(
        "setMyCommands",
        {
            "commands": group_commands,
            "scope": {"type": "all_group_chats"},
        },
    )


def answer_callback(callback_id, text=None, show_alert=False):
    data = {"callback_query_id": callback_id, "show_alert": show_alert}
    if text:
        data["text"] = text
    return telegram_api("answerCallbackQuery", data)


def send_media_group(chat_id, file_ids, caption=None, message_thread_id=None):
    media = []
    for index, file_id in enumerate(file_ids):
        item = {"type": "photo", "media": file_id}
        if index == 0 and caption:
            item["caption"] = caption
        media.append(item)
    data = {"chat_id": chat_id, "media": media}
    if message_thread_id:
        data["message_thread_id"] = int(message_thread_id)
    return telegram_api("sendMediaGroup", data)


def send_photo(chat_id, file_id, caption=None, message_thread_id=None, reply_markup=None):
    data = {"chat_id": chat_id, "photo": file_id}
    if caption:
        data["caption"] = caption
    if message_thread_id:
        data["message_thread_id"] = int(message_thread_id)
    if reply_markup:
        data["reply_markup"] = reply_markup
    return telegram_api("sendPhoto", data)


# =========================================================
# ДОПОМІЖНІ ФУНКЦІЇ
# =========================================================

def user_display_name(user):
    username = user.get("username")
    if username:
        return f"@{username}"
    full = " ".join(
        p for p in [user.get("first_name", ""), user.get("last_name", "")] if p
    ).strip()
    return full or f"ID {user.get('id')}"


def parse_positive_int(text, allow_zero=False):
    cleaned = text.replace(" ", "").replace("грн", "").strip()
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    if allow_zero:
        return value if value >= 0 else None
    return value if value > 0 else None


def parse_end_datetime(text):
    try:
        dt = datetime.strptime(text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=KYIV)
        if dt.timestamp() <= time.time():
            return None
        return dt.timestamp()
    except ValueError:
        return None


def format_dt(ts):
    return datetime.fromtimestamp(ts, KYIV).strftime("%d.%m.%Y %H:%M")


def section_title(section):
    return "🪙 Нумізматика" if section == "numizmatika" else "💵 Боністика"


def target_thread(section):
    if section == "numizmatika" and NUMIZMATIKA_THREAD_ID:
        return int(NUMIZMATIKA_THREAD_ID)
    if section == "bonistika" and BONISTIKA_THREAD_ID:
        return int(BONISTIKA_THREAD_ID)
    return None


def get_publish_chat_id(fallback_chat_id):
    if PUBLISH_CHAT_ID:
        return int(PUBLISH_CHAT_ID)
    return fallback_chat_id


def track_user_activity(user, action="update"):
    """Зберігає користувача і журнал використання бота."""
    if not user or not user.get("id"):
        return

    user_id = int(user["id"])
    username = user.get("username")
    display_name = user_display_name(user)
    now = time.time()

    try:
        with db() as conn:
            conn.execute(
                """
                INSERT INTO bot_users(
                    user_id, username, display_name,
                    first_seen, last_seen, use_count
                ) VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    display_name=excluded.display_name,
                    last_seen=excluded.last_seen,
                    use_count=bot_users.use_count + 1
                """,
                (user_id, username, display_name, now, now),
            )
            conn.execute(
                """
                INSERT INTO bot_activity(
                    user_id, username, display_name, action, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, username, display_name, str(action)[:300], now),
            )
    except Exception as exc:
        print("ACTIVITY ERROR:", repr(exc))


def track_community_message(message):
    """
    Накопичує статистику групи з моменту встановлення цієї версії:
    звичайні повідомлення, вступи та виходи.
    """
    chat = message.get("chat", {})
    chat_type = chat.get("type")
    if chat_type not in ("group", "supergroup"):
        return

    chat_id = chat.get("id")
    if chat_id is None:
        return

    chat_username = (chat.get("username") or "").lstrip("@").casefold() or None
    chat_title = chat.get("title")
    now = time.time()
    actor = message.get("from") or {}

    events = []

    new_members = message.get("new_chat_members") or []
    for member in new_members:
        events.append((
            member.get("id"),
            member.get("username"),
            user_display_name(member),
            "join",
        ))

    left_member = message.get("left_chat_member")
    if left_member:
        events.append((
            left_member.get("id"),
            left_member.get("username"),
            user_display_name(left_member),
            "leave",
        ))

    # Службове повідомлення про вступ/вихід не рахуємо ще й як звичайне.
    if not new_members and not left_member:
        events.append((
            actor.get("id"),
            actor.get("username"),
            user_display_name(actor) if actor else None,
            "message",
        ))

    try:
        with db() as conn:
            for uid, username, display_name, event_type in events:
                conn.execute(
                    """
                    INSERT INTO community_activity(
                        chat_id, chat_username, chat_title,
                        user_id, username, display_name,
                        event_type, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(chat_id),
                        chat_username,
                        chat_title,
                        uid,
                        username,
                        display_name,
                        event_type,
                        now,
                    ),
                )
    except Exception as exc:
        print("COMMUNITY ACTIVITY ERROR:", repr(exc))


def telegram_chat_member_count(chat_ref):
    result = telegram_api(
        "getChatMemberCount",
        {"chat_id": chat_ref},
    )
    if result and result.get("ok"):
        try:
            return int(result.get("result"))
        except Exception:
            return None
    return None


def telegram_bot_status_in_chat(chat_ref):
    me = telegram_api("getMe")
    if not me or not me.get("ok"):
        return "невідомо"

    bot_id = me["result"]["id"]
    result = telegram_api(
        "getChatMember",
        {
            "chat_id": chat_ref,
            "user_id": bot_id,
        },
    )
    if not result or not result.get("ok"):
        return "немає доступу"

    status = (result.get("result") or {}).get("status") or "невідомо"
    labels = {
        "creator": "власник",
        "administrator": "адміністратор ✅",
        "member": "учасник",
        "restricted": "обмежений",
        "left": "не учасник",
        "kicked": "заблокований",
    }
    return labels.get(status, status)


def _community_group_where():
    """
    Події групи фільтруємо за PUBLISH_CHAT_ID, якщо це числовий ID.
    Якщо ні — за username EastAuction.
    """
    try:
        if PUBLISH_CHAT_ID:
            return "chat_id=?", (int(PUBLISH_CHAT_ID),)
    except Exception:
        pass

    return "LOWER(COALESCE(chat_username,''))=?", (COMMUNITY_GROUP_USERNAME,)


def community_period_stats(since_ts):
    where, params = _community_group_where()
    with db() as conn:
        row = conn.execute(
            f"""
            SELECT
                SUM(CASE WHEN event_type='message' THEN 1 ELSE 0 END) AS messages,
                SUM(CASE WHEN event_type='join' THEN 1 ELSE 0 END) AS joins,
                SUM(CASE WHEN event_type='leave' THEN 1 ELSE 0 END) AS leaves,
                COUNT(DISTINCT CASE
                    WHEN event_type='message' AND user_id IS NOT NULL
                    THEN user_id
                END) AS active_users
            FROM community_activity
            WHERE {where}
              AND created_at>=?
            """,
            tuple(params) + (since_ts,),
        ).fetchone()

    return {
        "messages": int(row["messages"] or 0),
        "joins": int(row["joins"] or 0),
        "leaves": int(row["leaves"] or 0),
        "active_users": int(row["active_users"] or 0),
    }


def community_tracking_since():
    where, params = _community_group_where()
    with db() as conn:
        row = conn.execute(
            f"""
            SELECT MIN(created_at) AS first_ts
            FROM community_activity
            WHERE {where}
            """,
            tuple(params),
        ).fetchone()
    return row["first_ts"] if row else None


def group_statistics_text():
    now = time.time()
    day = community_period_stats(now - 86400)
    week = community_period_stats(now - 7 * 86400)

    group_members = telegram_chat_member_count(COMMUNITY_GROUP_CHAT)
    channel_members = telegram_chat_member_count(COMMUNITY_CHANNEL_CHAT)

    group_status = telegram_bot_status_in_chat(COMMUNITY_GROUP_CHAT)
    channel_status = telegram_bot_status_in_chat(COMMUNITY_CHANNEL_CHAT)

    tracking_from = community_tracking_since()
    tracking_text = (
        format_dt(tracking_from)
        if tracking_from
        else "ще немає накопичених подій"
    )

    def shown(value):
        return str(value) if value is not None else "недоступно"

    return (
        "👥 СТАТИСТИКА СПІЛЬНОТИ\n\n"
        "📌 ПОТОЧНИЙ СТАН\n"
        f"👥 Учасників Telegram-групи: {shown(group_members)}\n"
        f"📢 Підписників Telegram-каналу: {shown(channel_members)}\n"
        f"🤖 Бот у групі: {group_status}\n"
        f"🤖 Бот у каналі: {channel_status}\n\n"
        "🕒 ЗА ОСТАННІ 24 ГОДИНИ\n"
        f"🆕 Вступило до групи: {day['joins']}\n"
        f"🚪 Вийшло з групи: {day['leaves']}\n"
        f"💬 Повідомлень у групі: {day['messages']}\n"
        f"🔥 Активних авторів: {day['active_users']}\n\n"
        "📅 ЗА ОСТАННІ 7 ДНІВ\n"
        f"🆕 Вступило до групи: {week['joins']}\n"
        f"🚪 Вийшло з групи: {week['leaves']}\n"
        f"💬 Повідомлень у групі: {week['messages']}\n"
        f"🔥 Активних авторів: {week['active_users']}\n\n"
        f"📊 Накопичення детальної статистики з: {tracking_text}\n"
        "ℹ️ Telegram не віддає боту історичні вступи, виходи та активність "
        "за минулі періоди, тому ці показники накопичуються з моменту "
        "встановлення цієї версії."
    )


def bot_statistics_text():
    now = time.time()
    day = now - 86400
    week = now - 7 * 86400

    with db() as conn:
        users = conn.execute(
            "SELECT COUNT(*) AS c FROM bot_users"
        ).fetchone()["c"]
        new_day = conn.execute(
            "SELECT COUNT(*) AS c FROM bot_users WHERE first_seen>=?",
            (day,),
        ).fetchone()["c"]
        new_week = conn.execute(
            "SELECT COUNT(*) AS c FROM bot_users WHERE first_seen>=?",
            (week,),
        ).fetchone()["c"]

        active_day = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id) AS c
            FROM bot_activity
            WHERE created_at>=? AND user_id IS NOT NULL
            """,
            (day,),
        ).fetchone()["c"]
        active_week = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id) AS c
            FROM bot_activity
            WHERE created_at>=? AND user_id IS NOT NULL
            """,
            (week,),
        ).fetchone()["c"]

        active_lots = conn.execute(
            "SELECT COUNT(*) AS c FROM lots WHERE status='active'"
        ).fetchone()["c"]
        lots_day = conn.execute(
            "SELECT COUNT(*) AS c FROM lots WHERE created_at>=?",
            (day,),
        ).fetchone()["c"]

        bids_total = conn.execute(
            "SELECT COUNT(*) AS c FROM bid_history"
        ).fetchone()["c"]
        bids_day = conn.execute(
            "SELECT COUNT(*) AS c FROM bid_history WHERE created_at>=?",
            (day,),
        ).fetchone()["c"]

        sold = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM lots
            WHERE status IN ('sold','blitz_sold')
            """
        ).fetchone()["c"]

        scam_approved = conn.execute(
            "SELECT COUNT(*) AS c FROM scam_reports WHERE status='approved'"
        ).fetchone()["c"]
        scam_pending = conn.execute(
            "SELECT COUNT(*) AS c FROM scam_reports WHERE status='pending'"
        ).fetchone()["c"]

        guarantees = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM guarantee_requests
            WHERE status='pending'
            """
        ).fetchone()["c"]

        favorites = conn.execute(
            "SELECT COUNT(*) AS c FROM lot_favorites"
        ).fetchone()["c"]

        reviews = conn.execute(
            "SELECT COUNT(*) AS c FROM reviews"
        ).fetchone()["c"]

    return (
        "🤖 СТАТИСТИКА NUMIZMATCOIN BOT\n\n"
        "👥 КОРИСТУВАЧІ\n"
        f"👤 Усього користувачів: {int(users or 0)}\n"
        f"🆕 Нових за 24 год: {int(new_day or 0)}\n"
        f"📅 Нових за 7 днів: {int(new_week or 0)}\n"
        f"🔥 Активних за 24 год: {int(active_day or 0)}\n"
        f"🔥 Активних за 7 днів: {int(active_week or 0)}\n\n"
        "⚖️ АУКЦІОНИ\n"
        f"🟢 Активних лотів: {int(active_lots or 0)}\n"
        f"🆕 Створено лотів за 24 год: {int(lots_day or 0)}\n"
        f"📈 Ставок усього: {int(bids_total or 0)}\n"
        f"📈 Ставок за 24 год: {int(bids_day or 0)}\n"
        f"✅ Проданих лотів: {int(sold or 0)}\n\n"
        "🛡 БЕЗПЕКА ТА СЕРВІСИ\n"
        f"🤡 Підтверджених записів шахраїв: {int(scam_approved or 0)}\n"
        f"🟡 Скарг на модерації: {int(scam_pending or 0)}\n"
        f"🛡 Заявок Гарант: {int(guarantees or 0)}\n"
        f"❤️ Лотів зараз в обраному: {int(favorites or 0)}\n"
        f"⭐ Відгуків у базі: {int(reviews or 0)}"
    )


def mark_notification_once(user_id, lot_id, kind):
    try:
        with db() as conn:
            cur = conn.execute(
                """
                INSERT INTO notification_flags(user_id, lot_id, kind, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, lot_id, kind) DO NOTHING
                """,
                (int(user_id), int(lot_id), kind, time.time()),
            )
            return cur.rowcount == 1
    except Exception as exc:
        print("NOTIFICATION FLAG ERROR:", repr(exc))
        return False


def favorite_exists(user_id, lot_id):
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM lot_favorites WHERE user_id=? AND lot_id=?",
            (user_id, lot_id),
        ).fetchone()
    return bool(row)


def toggle_favorite(user_id, lot_id):
    with db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM lot_favorites WHERE user_id=? AND lot_id=?",
            (user_id, lot_id),
        ).fetchone()
        if exists:
            conn.execute(
                "DELETE FROM lot_favorites WHERE user_id=? AND lot_id=?",
                (user_id, lot_id),
            )
            return False
        conn.execute(
            """
            INSERT INTO lot_favorites(user_id, lot_id, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, lot_id) DO NOTHING
            """,
            (user_id, lot_id, time.time()),
        )
        return True


def favorite_lots(user_id, active_only=False, limit=30):
    where = "AND l.status='active'" if active_only else ""
    with db() as conn:
        return conn.execute(
            f"""
            SELECT l.*
            FROM lot_favorites f
            JOIN lots l ON l.id=f.lot_id
            WHERE f.user_id=? {where}
            ORDER BY
                CASE WHEN l.status='active' THEN 0 ELSE 1 END,
                l.end_ts ASC,
                l.id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()


def my_lots_rows(user_id, status_group="all", limit=30):
    clauses = ["seller_id=?"]
    params = [user_id]

    groups = {
        "active": ("active",),
        "pending": ("pending_approval",),
        "sold": ("sold", "blitz_sold"),
        "finished": ("finished", "reserve_not_met"),
        "rejected": ("rejected",),
    }

    statuses = groups.get(status_group)
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)

    params.append(limit)
    with db() as conn:
        return conn.execute(
            f"""
            SELECT *
            FROM lots
            WHERE {' AND '.join(clauses)}
            ORDER BY id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()


def lot_bid_count(lot_id):
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM bid_history WHERE lot_id=?",
            (lot_id,),
        ).fetchone()
    return int(row["c"] or 0)


def lot_bid_summary(lot_id, limit=20):
    lot = get_lot(lot_id)
    if not lot:
        return "⚠️ Лот не знайдено."

    with db() as conn:
        rows = conn.execute(
            """
            SELECT user_id, user_name, requested_max, resulting_price, created_at
            FROM bid_history
            WHERE lot_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (lot_id, limit),
        ).fetchall()

    lines = [
        f"📈 СТАВКИ ЛОТА №{lot_id}",
        f"🏷 {lot['title']}",
        "",
    ]
    if not rows:
        lines.append("Ставок поки немає.")
    else:
        for idx, row in enumerate(rows, 1):
            lines.append(
                f"{idx}. {row['user_name'] or row['user_id']} — "
                f"{row['resulting_price']} грн "
                f"({format_dt(row['created_at'])})"
            )
    return "\n".join(lines)[:3900]


def seller_finish_own_lot(user_id, lot_id):
    lot = get_lot(lot_id)
    if not lot or int(lot["seller_id"]) != int(user_id):
        return False, "Лот не знайдено або він не ваш."
    if lot["status"] != "active":
        return False, "Цей лот уже не активний."
    if lot["sale_type"] == "auction" and lot.get("leader_id"):
        return False, "Аукціон зі ставками не можна достроково завершити продавцем."

    with db() as conn:
        cur = conn.execute(
            """
            UPDATE lots
            SET status='finished', finished_at=?
            WHERE id=? AND seller_id=? AND status='active'
            """,
            (time.time(), lot_id, user_id),
        )
    if cur.rowcount != 1:
        return False, "Не вдалося завершити лот."
    refresh_public_lot(lot_id)
    return True, "Лот завершено."


def copy_lot_to_draft(user_id, chat_id, lot_id):
    lot = get_lot(lot_id)
    if not lot or int(lot["seller_id"]) != int(user_id):
        return False

    photos = get_lot_photos(lot_id)
    data = {
        "section": lot["section"],
        "category": lot["category"],
        "subcategory": lot.get("subcategory"),
        "category_key": lot.get("category_key"),
        "subcategory_key": lot.get("subcategory_key"),
        "sale_type": lot["sale_type"],
        "title": lot["title"],
        "material": lot["material"],
        "fixed_price": lot.get("fixed_price"),
        "start_price": lot.get("start_price"),
        "bid_step": lot.get("bid_step"),
        "blitz_price": lot.get("blitz_price"),
        "reserve_price": lot.get("reserve_price"),
        "anti_sniper": lot.get("anti_sniper") or 0,
        "phone": lot["phone"],
        "card_last4": lot["card_last4"],
        "extra_info": lot["extra_info"],
        "photos": [p["file_id"] for p in photos],
        "copied_from": lot_id,
    }
    save_draft(user_id, chat_id, "end_picker", data)
    ask_end(chat_id, user_id, data)
    return True


def scam_exact_links_for_user(user_id):
    """Лише точні прив'язки username/імені до підтверджених scam-записів."""
    with db() as conn:
        user = conn.execute(
            "SELECT * FROM bot_users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not user:
        return 0

    keys = set()
    if user.get("username"):
        keys.add("@" + user["username"].casefold())
        keys.add(user["username"].casefold())
    if user.get("display_name"):
        key = scam_latin_key(user["display_name"])
        if key:
            keys.add(key)

    if not keys:
        return 0

    count = 0
    seen = set()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT i.report_id, i.value_normalized
            FROM scam_identifiers i
            JOIN scam_reports s ON s.id=i.report_id
            WHERE s.status='approved'
              AND i.kind IN ('name','alias','username','profile')
            """
        ).fetchall()
    for row in rows:
        value = (row["value_normalized"] or "").casefold()
        if value in keys and row["report_id"] not in seen:
            seen.add(row["report_id"])
            count += 1
    return count


def reputation_stats(user_id):
    with db() as conn:
        user = conn.execute(
            "SELECT * FROM bot_users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        sold = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM lots
            WHERE seller_id=? AND status IN ('sold','blitz_sold')
            """,
            (user_id,),
        ).fetchone()["c"]
        purchases = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM lots
            WHERE winner_id=? AND status IN ('sold','blitz_sold')
            """,
            (user_id,),
        ).fetchone()["c"]

    reviews_pos = reviews_neg = 0
    if user and user.get("username"):
        target = normalize_identity("@" + user["username"])
        if target:
            reviews_pos, reviews_neg, _, _ = review_stats(target)

    return {
        "user": user,
        "sold": int(sold or 0),
        "purchases": int(purchases or 0),
        "deals": int(sold or 0) + int(purchases or 0),
        "complaints": scam_exact_links_for_user(user_id),
        "reviews_pos": int(reviews_pos),
        "reviews_neg": int(reviews_neg),
    }


def reputation_text(user_id):
    stats = reputation_stats(user_id)
    user = stats["user"]
    if user:
        name = user.get("display_name") or f"ID {user_id}"
        since = format_dt(user["first_seen"])
    else:
        name = f"ID {user_id}"
        since = "даних поки немає"

    return (
        "⭐ РЕПУТАЦІЯ КОРИСТУВАЧА\n\n"
        f"👤 {name}\n"
        f"🤝 Успішних угод: {stats['deals']}\n"
        f"📦 Проданих лотів: {stats['sold']}\n"
        f"🛒 Покупок: {stats['purchases']}\n"
        f"👍 Позитивних відгуків: {stats['reviews_pos']}\n"
        f"👎 Негативних відгуків: {stats['reviews_neg']}\n"
        f"🚨 Точних збігів у підтвердженій базі скарг: {stats['complaints']}\n"
        f"📅 У боті з: {since}\n\n"
        "ℹ️ Скарги рахуються лише за точними прив'язаними ідентифікаторами; "
        "збіг ПІБ сам по собі не є доказом."
    )


def create_guarantee_request(lot_id, buyer):
    lot = get_lot(lot_id)
    if not lot or lot["status"] != "active":
        return None, "Лот неактивний або не знайдений."
    buyer_id = int(buyer.get("id"))
    if int(lot["seller_id"]) == buyer_id:
        return None, "Не можна створити Гарант для власного лота."

    amount = lot["fixed_price"] if lot["sale_type"] == "fixed" else lot["current_price"]
    now = time.time()
    with db() as conn:
        existing = conn.execute(
            """
            SELECT id
            FROM guarantee_requests
            WHERE lot_id=? AND buyer_id=? AND status='pending'
            ORDER BY id DESC LIMIT 1
            """,
            (lot_id, buyer_id),
        ).fetchone()
        if existing:
            return int(existing["id"]), "Заявка вже створена."

        cur = conn.execute(
            """
            INSERT INTO guarantee_requests(
                lot_id, buyer_id, buyer_name,
                seller_id, seller_name, amount,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            RETURNING id
            """,
            (
                lot_id, buyer_id, user_display_name(buyer),
                lot["seller_id"], lot["seller_name"], amount,
                now, now,
            ),
        )
        request_id = int(cur.fetchone()["id"])

    return request_id, "Заявку створено."


def guarantee_admin_menu(request_id):
    return {
        "inline_keyboard": [[
            {"text": "✅ Прийняти", "callback_data": f"garok:{request_id}"},
            {"text": "❌ Відхилити", "callback_data": f"garno:{request_id}"},
        ]]
    }


def notify_admin_guarantee(request_id):
    if not ADMIN_TELEGRAM_ID:
        return
    with db() as conn:
        row = conn.execute(
            """
            SELECT g.*, l.title
            FROM guarantee_requests g
            JOIN lots l ON l.id=g.lot_id
            WHERE g.id=?
            """,
            (request_id,),
        ).fetchone()
    if not row:
        return
    send_message(
        int(ADMIN_TELEGRAM_ID),
        "🛡 НОВА ЗАЯВКА «ГАРАНТ»\n\n"
        f"🆔 Заявка №{row['id']}\n"
        f"⚖️ Лот №{row['lot_id']}: {row['title']}\n"
        f"🛒 Покупець: {row['buyer_name']} (ID {row['buyer_id']})\n"
        f"👤 Продавець: {row['seller_name']} (ID {row['seller_id']})\n"
        f"💰 Сума: {row['amount']} грн\n"
        "🟡 Статус: очікує рішення",
        guarantee_admin_menu(request_id),
    )


def admin_panel_text():
    return (
        "📊 АДМІН-ПАНЕЛЬ NUMIZMATCOIN\n\n"
        "Оберіть потрібний розділ 👇\n\n"
        "👥 Статистика групи — Telegram-група та канал, "
        "вступи/виходи й активність.\n"
        "🤖 Статистика бота — користувачі, лоти, ставки, "
        "шахраї, Гарант, обране та відгуки."
    )


def recent_activity_text(limit=20):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM bot_activity
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    lines = ["🧾 ОСТАННЯ АКТИВНІСТЬ", ""]
    if not rows:
        lines.append("Журнал поки порожній.")
        return "\n".join(lines)

    for row in rows:
        who = row["display_name"] or row["username"] or row["user_id"]
        lines.append(
            f"• {format_dt(row['created_at'])} — {who}\n"
            f"  ↳ {row['action']}"
        )
    return "\n".join(lines)[:3900]


def my_lots_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "🟢 Активні", "callback_data": "mylots:active"},
                {"text": "🟡 На перевірці", "callback_data": "mylots:pending"},
            ],
            [
                {"text": "✅ Продані", "callback_data": "mylots:sold"},
                {"text": "🏁 Завершені", "callback_data": "mylots:finished"},
            ],
            [
                {"text": "❌ Відхилені", "callback_data": "mylots:rejected"},
                {"text": "📚 Усі", "callback_data": "mylots:all"},
            ],
            [{"text": "⬅️ Головне меню", "callback_data": "info:home"}],
        ]
    }


def my_lot_actions(lot):
    rows = []
    if lot["status"] == "active":
        rows.append([
            {"text": "📈 Ставки", "callback_data": f"mylotbids:{lot['id']}"},
            {"text": "🏁 Завершити", "callback_data": f"mylotfinish:{lot['id']}"},
        ])
    rows.append([
        {"text": "🔁 Повторити / скопіювати", "callback_data": f"mylotcopy:{lot['id']}"},
    ])
    return {"inline_keyboard": rows}


def show_my_lots(chat_id, user_id, group="all"):
    rows = my_lots_rows(user_id, group)
    if not rows:
        send_message(
            chat_id,
            "👤 МОЇ ЛОТИ\n\nУ цьому розділі лотів немає.",
            my_lots_menu(),
        )
        return

    send_message(chat_id, f"👤 МОЇ ЛОТИ — {group.upper()}", my_lots_menu())
    for lot in rows:
        price = lot["fixed_price"] if lot["sale_type"] == "fixed" else lot["current_price"]
        send_message(
            chat_id,
            f"⚖️ Лот №{lot['id']}\n"
            f"🏷 {lot['title']}\n"
            f"📌 Статус: {lot['status']}\n"
            f"💰 Ціна: {price or '—'} грн\n"
            f"⏰ {format_dt(lot['end_ts'])}",
            my_lot_actions(lot),
        )


def show_favorites(chat_id, user_id):
    rows = favorite_lots(user_id)
    if not rows:
        send_message(
            chat_id,
            "❤️ ОБРАНІ ЛОТИ\n\nВи ще не додали жодного лота в обране."
        )
        return

    send_message(
        chat_id,
        "❤️ ОБРАНІ ЛОТИ\n\n"
        "Бот нагадає про активний обраний лот приблизно за 30 хвилин до завершення."
    )
    show_search_results(chat_id, rows)


def admin_panel_menu():
    return {
        "inline_keyboard": [
            [{"text": "👥 Статистика групи", "callback_data": "admin:group_stats"}],
            [{"text": "🤖 Статистика бота", "callback_data": "admin:bot_stats"}],
            [{"text": "⬅️ Головне меню", "callback_data": "info:home"}],
        ]
    }


def admin_group_stats_menu():
    return {
        "inline_keyboard": [
            [{"text": "🔄 Оновити", "callback_data": "admin:group_stats"}],
            [{"text": "⬅️ Адмін-панель", "callback_data": "menu_admin_panel"}],
        ]
    }


def admin_bot_stats_menu():
    return {
        "inline_keyboard": [
            [{"text": "🧾 Остання активність", "callback_data": "admin:activity"}],
            [{"text": "🛡 Заявки Гарант", "callback_data": "admin:guarantees"}],
            [{"text": "🔄 Оновити", "callback_data": "admin:bot_stats"}],
            [{"text": "⬅️ Адмін-панель", "callback_data": "menu_admin_panel"}],
        ]
    }


def pending_guarantees_text(limit=20):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT g.*, l.title
            FROM guarantee_requests g
            JOIN lots l ON l.id=g.lot_id
            WHERE g.status='pending'
            ORDER BY g.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    lines = ["🛡 ЗАЯВКИ ГАРАНТ", ""]
    if not rows:
        lines.append("Активних заявок немає.")
    else:
        for r in rows:
            lines.append(
                f"№{r['id']} | Лот №{r['lot_id']} | {r['amount']} грн\n"
                f"🛒 {r['buyer_name']} → 👤 {r['seller_name']}"
            )
    return "\n".join(lines)[:3900]


# =========================================================
# МЕНЮ
# =========================================================

def main_menu(user_id=None):
    rows = [
            [{"text": "⚖️ Виставити лот ✅", "callback_data": "menu_create_lot"}],
            [
                {"text": "🔎 Пошук лотів", "callback_data": "menu_search_lot"},
                {"text": "👤 Мої лоти", "callback_data": "menu_my_lots"},
            ],
            [
                {"text": "❤️ Обране", "callback_data": "menu_favorites"},
                {"text": "⭐ Моя репутація", "callback_data": "menu_profile"},
            ],
            [{"text": "🔔 Підписки на нові лоти", "callback_data": "menu_subscriptions"}],
            [
                {"text": "⚖️ Telegram канал East Auction ↗️", "url": TELEGRAM_CHANNEL_URL},
                {"text": "📖 Каталоги", "callback_data": "menu_catalogs"},
            ],
            [
                {"text": "🔱 Telegram група «Східний Аукціон» ↗️", "url": TELEGRAM_GROUP_URL},
                {"text": "🧠 Корисні посилання", "callback_data": "menu_links"},
            ],
            [{"text": "🗓️ Спільнота у Facebook ↗️", "url": FACEBOOK_URL}],
            [{"text": "🖼 Водяний знак на фото", "callback_data": "menu_watermark"}],
            [{"text": "🤡 Перевірка шахраїв", "callback_data": "menu_scammers"}],
            [{"text": "⭐ Відгуки та рейтинг", "callback_data": "menu_reviews"}],
    ]

    # Кнопку адмін-панелі бачить ТІЛЬКИ власник бота.
    if (
        ADMIN_TELEGRAM_ID
        and user_id is not None
        and str(user_id) == str(ADMIN_TELEGRAM_ID)
    ):
        rows.append([
            {"text": "📊 Адмін-панель", "callback_data": "menu_admin_panel"}
        ])

    return {"inline_keyboard": rows}


def catalogs_menu():
    return {
        "inline_keyboard": [
            [{
                "text": "🪙 Каталог монет СССР — Тіліжинський",
                "url": "https://drive.google.com/file/d/1gjT7mkSEBKc7OZMVY4NEjr-2iA1tIU99/view?usp=sharing",
            }],
            [{
                "text": "🇺🇦 Стандартні монети України 1992–2014 — ІТК8",
                "url": "https://drive.google.com/file/d/1lZ4gHmDXYbZ8K-jV2BR9tjGtOt6GXqTy/view?usp=sharing",
            }],
            [{
                "text": "🇺🇦 Каталог цінних монет України",
                "url": "https://drive.google.com/file/d/1B3_KHjOYsPwFqXSlBRRKjCLDP7mjKH2w/view?usp=drive_link",
            }],
            [{
                "text": "☦️ Хрести, підвіски й накладки Київської Русі X–XIII ст.",
                "url": "https://drive.google.com/file/d/1sn_aJDhrT1t9r-DPmY1sib_1L79n4XYF/view?usp=sharing",
            }],
            [{
                "text": "🔱 НБУ — архів випущених монет 1995–2024",
                "url": "https://docs.google.com/spreadsheets/d/13PDcAOvyflSRcTr8bDcSHI4WPNz2C5IE/edit?usp=sharing&ouid=108382732968174164879&rtpof=true&sd=true",
            }],
            [{"text": "↩️ Назад до меню", "callback_data": "info:home"}],
        ]
    }


def useful_links_menu():
    return {
        "inline_keyboard": [
            [{"text": "🏦 Національний Банк України", "url": "https://coins.bank.gov.ua/"}],
            [{"text": "🆕 newauction — сайт аукціонів", "url": "https://newauction.org/ru"}],
            [{"text": "🇺🇦🪙 UA_Coin", "url": "https://www.ua-coins.info/ru"}],
            [{
                "text": "💵 Боністика — сайт",
                "url": "http://www.banknote.ws/COLLECTION/countries/EUR/RUS/RUS-SPEC1/RUS-UKR.htm#UKRAINIAN%20SOCIALIST%20SOVIET%20REPUBLIC",
            }],
            [{"text": "📈 Курс золота / долара", "url": "https://ru.tradingview.com/symbols/XAUUSD/"}],
            [{"text": "✌️ Violity — сайт аукціонів", "url": "https://violity.com/ua"}],
            [{"text": "📌 Колекціонування — блог", "url": "https://collecting.samoosvita.in.ua/"}],
            [{"text": "🫗 Накласти водяний знак", "url": "https://www.iloveimg.com/ru/watermark-image"}],
            [{"text": "↩️ Назад до меню", "callback_data": "info:home"}],
        ]
    }


def scammers_menu(user_id=None):
    rows = [
        [{"text": "🔎 Універсальний пошук", "callback_data": "scam:any"}],
        [
            {"text": "💳 За карткою", "callback_data": "scam:card"},
            {"text": "📱 За телефоном", "callback_data": "scam:phone"},
        ],
        [{"text": "👤 За ПІБ / nickname / username", "callback_data": "scam:nick"}],
        [{"text": "➕ Подати інформацію про шахрая", "callback_data": "scam:report"}],
        [{"text": "⬅️ Головне меню", "callback_data": "scam:home"}],
    ]
    return {"inline_keyboard": rows}


def scam_sources_menu(user_id=None):
    return {
        "inline_keyboard": [
            [{"text": "🛡 Кіберполіція STOP FRAUD ↗️", "url": "https://cyberpolice.gov.ua/stopfraud/"}],
            [{"text": "🔎 StopFraud / MITI ↗️", "url": "https://stopfraud.miti.page/"}],
            [{"text": "🔄 Нова перевірка", "callback_data": "menu_scammers"}],
            [{"text": "⬅️ Головне меню", "callback_data": "scam:home"}],
        ]
    }


def reviews_menu():
    return {
        "inline_keyboard": [
            [{"text": "✍️ Залишити відгук", "callback_data": "review:add"}],
            [
                {"text": "🏆 ТОП-20", "callback_data": "review:top20"},
                {"text": "🚨 АНТИРЕЙТИНГ-20", "callback_data": "review:anti20"},
            ],
            [{"text": "⬅️ Головне меню", "callback_data": "review:home"}],
        ]
    }


def review_vote_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "👍 Позитивний", "callback_data": "reviewvote:1"},
                {"text": "👎 Негативний", "callback_data": "reviewvote:-1"},
            ],
            [{"text": "❌ Скасувати", "callback_data": "review:cancel"}],
        ]
    }


def normalize_identity(value):
    value = " ".join((value or "").strip().split())
    if value.startswith("https://t.me/"):
        value = value.rstrip("/").rsplit("/", 1)[-1]
    if value.startswith("@"):
        value = value[1:].strip()
    if len(value) < 2 or len(value) > 150:
        return None
    return value.casefold()


def clean_identity_display(value):
    value = " ".join((value or "").strip().split())
    if value.startswith("https://t.me/"):
        value = value.rstrip("/").rsplit("/", 1)[-1]
    if not value or len(value) > 150:
        return None
    return value


def display_identity(key, display=None):
    raw = clean_identity_display(display or key)
    if not raw:
        return "—"
    # @ зберігаємо лише якщо користувач сам його ввів або це схоже на username.
    if raw.startswith("@"):
        return raw
    if " " not in raw and raw.replace("_", "").isalnum():
        return raw
    return raw


# Backward-compatible aliases used elsewhere in older code.
def normalize_username(value):
    return normalize_identity(value)


def display_username(value):
    return display_identity(value)


def review_stats(target_username):
    key = normalize_identity(target_username)
    if not key:
        return 0, 0, 0, 0
    with db() as conn:
        rows = conn.execute(
            "SELECT rating FROM reviews WHERE target_username = ? ORDER BY id DESC",
            (key,),
        ).fetchall()
    positive = sum(1 for r in rows if r["rating"] == 1)
    negative = sum(1 for r in rows if r["rating"] == -1)
    total = positive + negative
    score = positive - negative
    return positive, negative, total, score


def review_rating_text(target_username, target_display=None):
    positive, negative, total, score = review_stats(target_username)
    shown = display_identity(target_username, target_display)
    if total == 0:
        return (
            "⭐ РЕЙТИНГ КОРИСТУВАЧА\n\n"
            f"👤 {shown}\n\n"
            "Відгуків поки немає."
        )
    percent = round((positive / total) * 100)
    return (
        "⭐ РЕЙТИНГ КОРИСТУВАЧА\n\n"
        f"👤 {shown}\n\n"
        f"👍 Позитивних: {positive}\n"
        f"👎 Негативних: {negative}\n"
        f"📝 Усього відгуків: {total}\n"
        f"📊 Позитивний рейтинг: {percent}%\n"
        f"⚖️ Баланс: {score:+d}"
    )


def review_top20_text(reverse=False):
    with db() as conn:
        if reverse:
            rows = conn.execute(
                """
                SELECT
                    target_username,
                    MAX(COALESCE(target_display, target_username)) AS target_display,
                    SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS positive,
                    SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END) AS negative,
                    COUNT(*) AS total,
                    SUM(rating) AS score
                FROM reviews
                GROUP BY target_username
                HAVING COUNT(*) >= 1
                ORDER BY
                    negative DESC,
                    score ASC,
                    total DESC,
                    target_username ASC
                LIMIT 20
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT
                    target_username,
                    MAX(COALESCE(target_display, target_username)) AS target_display,
                    SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS positive,
                    SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END) AS negative,
                    COUNT(*) AS total,
                    SUM(rating) AS score
                FROM reviews
                GROUP BY target_username
                HAVING COUNT(*) >= 1
                ORDER BY
                    score DESC,
                    positive DESC,
                    total DESC,
                    target_username ASC
                LIMIT 20
                """
            ).fetchall()

    title = "🚨 АНТИРЕЙТИНГ-20" if reverse else "🏆 ТОП-20 РЕЙТИНГУ"
    if not rows:
        return f"{title}\n\nВідгуків поки немає."

    lines = [title, ""]
    for idx, row in enumerate(rows, 1):
        positive = int(row["positive"] or 0)
        negative = int(row["negative"] or 0)
        total = int(row["total"] or 0)
        percent = round((positive / total) * 100) if total else 0
        name = display_identity(row["target_username"], row["target_display"])
        lines.append(
            f"{idx}. {name}\n"
            f"   📊 {percent}%  |  👍 {positive}  👎 {negative}  |  📝 {total}"
        )

    lines.append("")
    lines.append("ℹ️ Рейтинг формується лише з відгуків, залишених через цього бота.")
    return "\n".join(lines)


def publish_review_to_topic(review_id):
    if not PUBLISH_CHAT_ID or not REVIEWS_THREAD_ID:
        return False

    with db() as conn:
        review = conn.execute(
            "SELECT * FROM reviews WHERE id = ?",
            (review_id,),
        ).fetchone()
    if not review:
        return False

    sign = "👍 ПОЗИТИВНИЙ ВІДГУК" if review["rating"] == 1 else "👎 НЕГАТИВНИЙ ВІДГУК"
    positive, negative, total, score = review_stats(review["target_username"])
    percent = round((positive / total) * 100) if total else 0
    shown = display_identity(review["target_username"], review.get("target_display"))

    body = (
        f"⭐ ВІДГУК №{review['id']}\n\n"
        f"{sign}\n"
        f"👤 Про користувача: {shown}\n"
        f"✍️ Автор: {review['reviewer_name'] or review['reviewer_id']}\n\n"
        f"💬 {review['comment']}\n\n"
        f"📊 Поточний рейтинг: {percent}% позитивних\n"
        f"👍 {positive}   👎 {negative}"
    )

    result = send_message(
        int(PUBLISH_CHAT_ID),
        body,
        message_thread_id=int(REVIEWS_THREAD_ID),
    )
    return bool(result and result.get("ok"))


def normalize_scam_subject(value):
    value = " ".join((value or "").strip().split())
    if not value:
        return None
    return value.casefold()


_SCAM_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d",
    "е": "e", "є": "ye", "ё": "yo", "ж": "zh", "з": "z", "и": "i",
    "і": "i", "ї": "yi", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def scam_latin_key(value):
    """Єдиний ключ для кирилиці/латини: Вася Алергуш -> vasya alergush."""
    value = normalize_scam_subject(value)
    if not value:
        return ""

    out = []
    for ch in value:
        out.append(_SCAM_TRANSLIT.get(ch, ch))

    latin = "".join(out)

    # Уніфікуємо часті варіанти транслітерації.
    latin = latin.replace("kh", "h")
    latin = latin.replace("yi", "i")
    latin = latin.replace("ye", "e")
    latin = latin.replace("yo", "o")
    latin = latin.replace("yu", "u")
    latin = re.sub(r"[^a-z0-9@._ -]+", " ", latin)
    return " ".join(latin.split())


_SCAM_NICKNAME_EQUIV = {
    "саня": ["саша", "александр", "олександр"],
    "саша": ["саня", "александр", "олександр"],
    "sanya": ["sasha", "alexandr", "aleksandr", "oleksandr"],
    "sasha": ["sanya", "alexandr", "aleksandr", "oleksandr"],
    "вася": ["василь", "василий"],
    "vasya": ["vasyl", "vasiliy", "vasilii"],
    "женя": ["евгений", "євген", "євгеній"],
    "zhenya": ["evgeniy", "yevhen", "yevhenii"],
}


def scam_query_variants(value):
    """
    Варіанти пошуку:
    - оригінал;
    - латинізований варіант;
    - поширені скорочені імена (Саня/Саша тощо).
    """
    base = normalize_scam_subject(value) or ""
    latin = scam_latin_key(value)

    variants = {base, latin}

    for token in base.split():
        for alias in _SCAM_NICKNAME_EQUIV.get(token, []):
            variants.add(base.replace(token, alias))

    for token in latin.split():
        for alias in _SCAM_NICKNAME_EQUIV.get(token, []):
            variants.add(latin.replace(token, alias))

    return [v for v in variants if v]


def scam_subject_score(query_value, subject_value):
    """
    Точний і консервативний пошук ПІБ/nickname.
    Менше число = кращий збіг:
    0 exact, 1 whole-word/prefix, 2 substring, 3 fuzzy.
    """
    q_variants = scam_query_variants(query_value)
    subject_norm = normalize_scam_subject(subject_value) or ""
    subject_latin = scam_latin_key(subject_value)
    subject_variants = {subject_norm, subject_latin}

    best = None

    for q in q_variants:
        for s in subject_variants:
            if not q or not s:
                continue

            if q == s:
                score = (0, 0.0)
            elif len(q) >= 3 and (s.startswith(q + " ") or f" {q} " in f" {s} "):
                score = (1, abs(len(s) - len(q)))
            elif len(q) >= 4 and q in s:
                score = (2, abs(len(s) - len(q)))
            else:
                # Fuzzy навмисно суворий: короткі або слабко схожі імена
                # не повинні створювати хибне звинувачення.
                ratio = SequenceMatcher(None, q, s).ratio()
                if len(q) >= 5 and ratio >= 0.84:
                    score = (3, -ratio)
                else:
                    continue

            if best is None or score < best:
                best = score

    return best


def normalize_card_digits(value):
    """
    Normalize a bank-card value safely.
    Accepts spaces/dashes and Excel/CSV values ending in .0.
    """
    raw = str(value or "").strip()

    # Excel/CSV artifact: 1111222233334440.0 -> 1111222233334440
    if re.fullmatch(r"[\d\s-]+\.0", raw):
        raw = raw[:-2]

    digits = "".join(ch for ch in raw if ch.isdigit())
    if 12 <= len(digits) <= 19:
        return digits
    return None


def card_lookup_hash(digits):
    # Залишаємо той самий ключ, щоб не зламати пошук уже збережених карток.
    # Якщо BOT_TOKEN колись змінюватиметься, перед ротацією треба окремо
    # перенести/перехешувати карткові індекси.
    key = (BOT_TOKEN or "numizmat-bot").encode("utf-8")
    return hmac.new(key, digits.encode("utf-8"), hashlib.sha256).hexdigest()


def card_fragment_hash(fragment):
    """HMAC для фрагмента картки. Сам фрагмент у БД не зберігається."""
    return card_lookup_hash(fragment)


def iter_card_fragments(digits, min_len=4, max_len=11):
    """
    Усі безперервні фрагменти картки довжиною 4..11 цифр.
    Це дає пошук по будь-якій частині картки, не зберігаючи її відкрито.
    """
    if not digits:
        return
    upper = min(max_len, len(digits) - 1)
    for length in range(min_len, upper + 1):
        for start in range(0, len(digits) - length + 1):
            yield digits[start:start + length]


def add_scam_card_fragments(conn, report_id, digits):
    seen = set()
    now = time.time()
    for fragment in iter_card_fragments(digits):
        key = (len(fragment), card_fragment_hash(fragment))
        if key in seen:
            continue
        seen.add(key)
        conn.execute(
            """
            INSERT INTO scam_card_fragments(report_id, fragment_len, fragment_hash, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(report_id, fragment_len, fragment_hash) DO NOTHING
            """,
            (report_id, len(fragment), key[1], now),
        )


def card_mask(digits):
    if not digits:
        return "—"
    return f"**** **** **** {digits[-4:]}"


def split_multi_values(value):
    """Розбиває кілька значень по новому рядку, ; або |. Коми не чіпаємо."""
    raw = str(value or "").strip()
    if not raw:
        return []
    parts = re.split(r"[\n;|]+", raw)
    result = []
    seen = set()
    for item in parts:
        item = " ".join(item.strip().split())
        if not item:
            continue
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def normalize_phone(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "3" + digits
    if 10 <= len(digits) <= 15:
        return digits
    return None


def extract_phone_numbers(value):
    raw = str(value or "")
    candidates = re.findall(r"(?:\+?\d[\s()\-]*){10,15}", raw)
    result = []
    for candidate in candidates:
        normalized = normalize_phone(candidate)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def normalize_profile(value):
    value = " ".join(str(value or "").strip().split())
    if not value:
        return None
    lower = value.casefold().rstrip("/")
    for prefix in (
        "https://t.me/",
        "http://t.me/",
        "t.me/",
        "https://telegram.me/",
        "http://telegram.me/",
        "telegram.me/",
    ):
        if lower.startswith(prefix):
            lower = lower[len(prefix):].split("?", 1)[0].strip("/")
            return "@" + lower.lstrip("@")
    if lower.startswith("@"):
        return "@" + lower.lstrip("@")
    if "facebook.com/" in lower:
        lower = lower.split("?", 1)[0].rstrip("/")
        return lower
    return lower


def normalize_card_list(value):
    result = []
    for candidate in re.findall(r"(?:\d[\s\-]*){12,19}", str(value or "")):
        digits = "".join(ch for ch in candidate if ch.isdigit())
        if 12 <= len(digits) <= 19 and digits not in result:
            result.append(digits)
    return result


def add_scam_identifier(
    conn,
    report_id,
    kind,
    value_display=None,
    value_normalized=None,
    value_hash=None,
    value_last4=None,
    value_last6=None,
    value_last8=None,
):
    if not any((value_display, value_normalized, value_hash)):
        return
    exists = conn.execute(
        """
        SELECT id
        FROM scam_identifiers
        WHERE report_id=?
          AND kind=?
          AND COALESCE(value_normalized,'')=COALESCE(?, '')
          AND COALESCE(value_hash,'')=COALESCE(?, '')
        LIMIT 1
        """,
        (report_id, kind, value_normalized, value_hash),
    ).fetchone()
    if exists:
        return
    conn.execute(
        """
        INSERT INTO scam_identifiers(
            report_id, kind, value_display, value_normalized,
            value_hash, value_last4, value_last6, value_last8, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report_id,
            kind,
            value_display,
            value_normalized,
            value_hash,
            value_last4,
            value_last6,
            value_last8,
            time.time(),
        ),
    )


def scam_match_label(kind):
    labels = {
        "name": "ПІБ",
        "alias": "варіант імені",
        "associate": "пов'язане ім'я",
        "username": "username",
        "profile": "профіль",
        "phone": "телефон",
        "card": "картка",
    }
    return labels.get(kind, kind)


def scam_collect_identifier_rows(report_ids):
    if not report_ids:
        return {}
    placeholders = ",".join("?" for _ in report_ids)
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM scam_identifiers
            WHERE report_id IN ({placeholders})
            ORDER BY id
            """,
            tuple(report_ids),
        ).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["report_id"], []).append(row)
    return grouped


def _name_tokens(value):
    return [t for t in scam_latin_key(value).split() if t]



def search_reason_from_score(score):
    """Людський опис якості збігу для результатів пошуку."""
    if not score:
        return "збіг"
    level = score[0] if isinstance(score, (tuple, list)) else score
    return {
        0: "точний збіг",
        1: "збіг за повним словом / початком",
        2: "частковий збіг",
        3: "схоже написання",
    }.get(level, "збіг")

def scam_report_preview(data):
    cards = data.get("card_digits_list") or []
    phones = data.get("phones") or []
    aliases = data.get("aliases") or []
    profiles = data.get("profiles") or []

    cards_text = ", ".join(card_mask(x) for x in cards) if cards else "не вказано"
    phones_text = ", ".join("+" + x for x in phones) if phones else "не вказано"
    aliases_text = "; ".join(aliases) if aliases else "не вказано"
    profiles_text = "; ".join(profiles) if profiles else "не вказано"

    return (
        "📢 ПЕРЕВІРКА ПОДАНИХ ДАНИХ\n"
        f"👤 Основне ПІБ / nickname: {data.get('subject_text', '—')}\n"
        f"🔄 Інші імена / написання: {aliases_text}\n"
        f"📱 Телефон(и): {phones_text}\n"
        f"💳 Картка(и): {cards_text}\n"
        f"🌐 Профілі / username: {profiles_text}\n"
        f"👥 Спільники / пов'язані особи: {data.get('associates', 'не вказано')}\n"
        f"🤡 Опис: {data.get('description', '—')}\n"
        f"📌 Докази: {data.get('evidence', 'не вказано')}\n\n"
        "⚠️ Перевірте написання і цифри. Після подачі запис піде адміністратору на модерацію."
    )


def scam_report_preview_menu():
    return {
        "inline_keyboard": [
            [{"text": "✅ Подати інформацію", "callback_data": "scamreport:submit"}],
            [{"text": "❌ Скасувати", "callback_data": "scamreport:cancel"}],
        ]
    }


def scam_admin_menu(report_id):
    return {
        "inline_keyboard": [[
            {"text": "✅ Перевірено", "callback_data": f"scamapprove:{report_id}"},
            {"text": "❌ Відхилити", "callback_data": f"scamreject:{report_id}"},
        ]]
    }


def scam_report_for_admin(report_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM scam_reports WHERE id=?",
            (report_id,),
        ).fetchone()
        identifiers = conn.execute(
            "SELECT * FROM scam_identifiers WHERE report_id=? ORDER BY id",
            (report_id,),
        ).fetchall()

    if not row:
        return None

    aliases = [x["value_display"] for x in identifiers if x["kind"] == "alias" and x["value_display"]]
    phones = [x["value_display"] for x in identifiers if x["kind"] == "phone" and x["value_display"]]
    cards = [
        f"**** **** **** {x['value_last4']}"
        for x in identifiers
        if x["kind"] == "card" and x["value_last4"]
    ]
    profiles = [
        x["value_display"]
        for x in identifiers
        if x["kind"] in ("username", "profile") and x["value_display"]
    ]

    return (
        f"🛡 НОВА ІНФОРМАЦІЯ НА МОДЕРАЦІЮ №{row['id']}\n"
        f"👤 ПІБ / nickname: {row['subject_text']}\n"
        f"🔄 Інші імена: {'; '.join(aliases) if aliases else 'не вказано'}\n"
        f"📱 Телефон(и): {'; '.join(phones) if phones else 'не вказано'}\n"
        f"💳 Картка(и): {'; '.join(cards) if cards else ('**** **** **** ' + str(row['card_last4']) if row['card_last4'] else 'не вказано')}\n"
        f"🌐 Профілі: {'; '.join(profiles) if profiles else 'не вказано'}\n"
        f"👥 Спільники: {row['associates'] or 'не вказано'}\n"
        f"🤡 Опис: {row['description']}\n"
        f"📌 Докази: {row['evidence'] or 'не вказано'}\n"
        f"✍️ Подав: {row['reporter_name'] or row['reporter_id']}\n"
        "🟡 Статус: повідомлення на перевірці"
    )


def insert_scam_report(user_id, reporter_name, data, status="pending", source="user_report"):
    cards = data.get("card_digits_list") or []
    first_card = cards[0] if cards else None

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO scam_reports(
                reporter_id, reporter_name,
                subject_text, subject_normalized,
                card_hash, card_last4,
                description, associates, evidence,
                source, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                user_id,
                reporter_name,
                data["subject_text"],
                normalize_scam_subject(data["subject_text"]),
                card_lookup_hash(first_card) if first_card else None,
                first_card[-4:] if first_card else None,
                data["description"],
                data.get("associates"),
                data.get("evidence"),
                source,
                status,
                time.time(),
            ),
        )
        report_id = cur.fetchone()["id"]

        add_scam_identifier(
            conn, report_id, "name",
            value_display=data["subject_text"],
            value_normalized=scam_latin_key(data["subject_text"]),
        )

        for alias in data.get("aliases") or []:
            add_scam_identifier(
                conn, report_id, "alias",
                value_display=alias,
                value_normalized=scam_latin_key(alias),
            )

        for phone in data.get("phones") or []:
            normalized = normalize_phone(phone)
            add_scam_identifier(
                conn, report_id, "phone",
                value_display=("+" + normalized) if normalized else phone,
                value_normalized=normalized,
            )

        for digits in cards:
            add_scam_identifier(
                conn, report_id, "card",
                value_display=card_mask(digits),
                value_hash=card_lookup_hash(digits),
                value_last4=digits[-4:],
                value_last6=digits[-6:] if len(digits) >= 6 else None,
                value_last8=digits[-8:] if len(digits) >= 8 else None,
            )
            add_scam_card_fragments(conn, report_id, digits)

        for profile in data.get("profiles") or []:
            normalized = normalize_profile(profile)
            kind = "username" if normalized and normalized.startswith("@") else "profile"
            add_scam_identifier(
                conn, report_id, kind,
                value_display=profile,
                value_normalized=normalized,
            )

        associates = data.get("associates")
        if associates and associates != "не вказано":
            for associate in split_multi_values(associates):
                add_scam_identifier(
                    conn, report_id, "associate",
                    value_display=associate,
                    value_normalized=scam_latin_key(associate),
                )

        return report_id


def duplicate_scam_report_id(reporter_id, data):
    """
    Блокуємо очевидний повтор тієї самої заявки тим самим автором.
    Додаткова скарга від іншої людини не блокується.
    """
    subject = normalize_scam_subject(data.get("subject_text")) or ""
    cards = data.get("card_digits_list") or []
    phones = [normalize_phone(x) for x in (data.get("phones") or [])]
    profiles = [normalize_profile(x) for x in (data.get("profiles") or [])]

    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM scam_reports
            WHERE reporter_id=?
              AND status IN ('pending','approved')
              AND subject_normalized=?
            ORDER BY id DESC
            LIMIT 50
            """,
            (reporter_id, subject),
        ).fetchall()

        for row in rows:
            identifiers = conn.execute(
                "SELECT * FROM scam_identifiers WHERE report_id=?",
                (row["id"],),
            ).fetchall()

            existing_hashes = {
                x["value_hash"] for x in identifiers
                if x["kind"] == "card" and x["value_hash"]
            }
            if any(card_lookup_hash(c) in existing_hashes for c in cards):
                return row["id"]

            existing_phones = {
                x["value_normalized"] for x in identifiers
                if x["kind"] == "phone" and x["value_normalized"]
            }
            if any(p and p in existing_phones for p in phones):
                return row["id"]

            existing_profiles = {
                x["value_normalized"] for x in identifiers
                if x["kind"] in ("username","profile") and x["value_normalized"]
            }
            if any(p and p in existing_profiles for p in profiles):
                return row["id"]

    return None


def notify_admin_about_scam_report(report_id):
    if not ADMIN_TELEGRAM_ID:
        return False
    body = scam_report_for_admin(report_id)
    if not body:
        return False
    result = send_message(
        int(ADMIN_TELEGRAM_ID),
        body,
        scam_admin_menu(report_id),
    )
    return bool(result and result.get("ok"))


def _active_scam_rows():
    """
    Публічний пошук працює ТІЛЬКИ по підтверджених адміністратором записах.
    Pending-заявки не повинні звинувачувати людину до модерації.
    """
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM scam_reports
            WHERE status = 'approved'
            ORDER BY created_at DESC
            LIMIT 10000
            """
        ).fetchall()


def scam_db_results_by_card(value):
    """
    Суперточний пошук картки:
    - 12–19 цифр: тільки точний збіг ПОВНОГО номера;
    - 4–11 цифр: точний збіг цього фрагмента в БУДЬ-ЯКОМУ місці картки;
    - відкритий повний номер або фрагмент у нових індексах не зберігається.
    """
    raw = str(value or "").strip()
    if re.fullmatch(r"[\d\s-]+\.0", raw):
        raw = raw[:-2]

    # У режимі картки не дозволяємо довільний текст: лише цифри/пробіли/дефіси.
    if not re.fullmatch(r"[\d\s-]+", raw):
        return []

    q = "".join(ch for ch in raw if ch.isdigit())
    if not (4 <= len(q) <= 19):
        return []

    full = 12 <= len(q) <= 19
    ranked = []
    seen = set()

    with db() as conn:
        if full:
            qhash = card_lookup_hash(q)
            rows = conn.execute(
                """
                SELECT DISTINCT s.*
                FROM scam_reports s
                LEFT JOIN scam_identifiers i
                  ON i.report_id=s.id AND i.kind='card'
                WHERE s.status='approved'
                  AND (s.card_hash=? OR i.value_hash=?)
                ORDER BY s.created_at DESC
                LIMIT 100
                """,
                (qhash, qhash),
            ).fetchall()

            for row in rows:
                if row["id"] not in seen:
                    ranked.append((0, -float(row["created_at"]), row, "картка", "точний збіг повного номера"))
                    seen.add(row["id"])
        else:
            frag_hash = card_fragment_hash(q)
            rows = conn.execute(
                """
                SELECT DISTINCT s.*
                FROM scam_reports s
                JOIN scam_card_fragments f ON f.report_id=s.id
                WHERE s.status='approved'
                  AND f.fragment_len=?
                  AND f.fragment_hash=?
                ORDER BY s.created_at DESC
                LIMIT 100
                """,
                (len(q), frag_hash),
            ).fetchall()

            for row in rows:
                if row["id"] not in seen:
                    ranked.append((1, -float(row["created_at"]), row, "фрагмент картки", f"точний збіг {len(q)} цифр"))
                    seen.add(row["id"])

            # Сумісність зі старою схемою: для старих записів інколи доступні
            # лише останні 4/6/8 цифр. Це НЕ замінює новий довільний fragment-index.
            legacy_rows = _active_scam_rows()
            identifiers = scam_collect_identifier_rows([r["id"] for r in legacy_rows])
            for row in legacy_rows:
                if row["id"] in seen:
                    continue

                matched = False
                if len(q) == 4 and row["card_last4"] == q:
                    matched = True

                if not matched:
                    for item in identifiers.get(row["id"], []):
                        if item["kind"] != "card":
                            continue
                        if len(q) == 4 and item["value_last4"] == q:
                            matched = True
                        elif len(q) <= 6 and item.get("value_last6") and q in item["value_last6"]:
                            matched = True
                        elif len(q) <= 8 and item.get("value_last8") and q in item["value_last8"]:
                            matched = True
                        if matched:
                            break

                if matched:
                    ranked.append((2, -float(row["created_at"]), row, "картка старого формату", "збіг у доступному фрагменті"))
                    seen.add(row["id"])

    # Legacy imported data: повні картки могли залишитися у текстових полях.
    # Для 12–19 цифр тут теж вимагаємо ТІЛЬКИ повну рівність, не substring.
    for row in _active_scam_rows():
        if row["id"] in seen:
            continue
        hay = " ".join(
            str(x or "") for x in
            (row["subject_text"], row["description"], row["associates"], row["evidence"])
        )
        compact = re.sub(r"(?<=\d)\.0(?!\d)", "", hay)
        for match in re.finditer(r"(?<!\d)(?:\d[\s-]*){12,19}(?!\d)", compact):
            card = "".join(ch for ch in match.group(0) if ch.isdigit())
            if (full and card == q) or ((not full) and q in card):
                reason = "точний збіг повного номера" if full else f"точний збіг фрагмента {len(q)} цифр"
                ranked.append((3, -float(row["created_at"]), row, "картка у старому записі", reason))
                seen.add(row["id"])
                break

    ranked.sort(key=lambda x: (x[0], x[1]))
    return [(x[2], x[3], x[4]) for x in ranked[:100]]

def scam_db_results_by_phone(value):
    phone = normalize_phone(value)
    if not phone:
        return []

    with db() as conn:
        exact = conn.execute(
            """
            SELECT DISTINCT s.*
            FROM scam_reports s
            JOIN scam_identifiers i ON i.report_id=s.id
            WHERE s.status='approved'
              AND i.kind='phone'
              AND i.value_normalized=?
            ORDER BY
                CASE WHEN s.status='approved' THEN 0 ELSE 1 END,
                s.created_at DESC
            LIMIT 100
            """,
            (phone,),
        ).fetchall()

    result = []
    seen = set()
    for row in exact:
        result.append((row, "телефон", "точний збіг"))
        seen.add(row["id"])

    # Сумісність зі старими записами, де телефон міг бути записаний
    # у ПІБ/описі/спільниках до появи окремого поля.
    for row in _active_scam_rows():
        if row["id"] in seen:
            continue
        haystack = " ".join(
            str(x or "")
            for x in (
                row["subject_text"],
                row["description"],
                row["associates"],
                row["evidence"],
            )
        )
        if phone in extract_phone_numbers(haystack):
            result.append((row, "телефон у старому записі", "точний збіг"))
            seen.add(row["id"])

    return result


def scam_db_results_by_profile(value):
    normalized = normalize_profile(value)
    if not normalized:
        return []

    with db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT s.*, i.kind AS matched_kind
            FROM scam_reports s
            JOIN scam_identifiers i ON i.report_id=s.id
            WHERE s.status='approved'
              AND i.kind IN ('username','profile')
              AND i.value_normalized=?
            ORDER BY
                CASE WHEN s.status='approved' THEN 0 ELSE 1 END,
                s.created_at DESC
            LIMIT 100
            """,
            (normalized,),
        ).fetchall()

    return [
        (row, "username / профіль", "точний збіг")
        for row in rows
    ]


def scam_db_results_by_subject(value):
    if not normalize_scam_subject(value):
        return []

    rows = _active_scam_rows()
    identifiers = scam_collect_identifier_rows([row["id"] for row in rows])

    ranked = []
    for row in rows:
        candidates = [
            ("ПІБ / nickname", row["subject_text"]),
        ]

        if row["associates"]:
            candidates.append(("спільники / інші імена", row["associates"]))

        for item in identifiers.get(row["id"], []):
            if item["kind"] in ("name", "alias", "associate", "username", "profile"):
                candidates.append(
                    (
                        scam_match_label(item["kind"]),
                        item["value_display"] or item["value_normalized"] or "",
                    )
                )

        best_score = None
        best_field = None
        for field_name, field_value in candidates:
            score = scam_subject_score(value, field_value)
            if score is not None and (best_score is None or score < best_score):
                best_score = score
                best_field = field_name

        if best_score is None:
            continue

        status_rank = 0 if row["status"] == "approved" else 1
        ranked.append(
            (
                best_score,
                status_rank,
                -float(row["created_at"]),
                row,
                best_field or "ПІБ",
            )
        )

    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return [
        (
            item[3],
            item[4],
            search_reason_from_score(item[0]),
        )
        for item in ranked[:100]
    ]


def detect_scam_query_kind(value):
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())

    # Якщо повідомлення складається тільки з цифр/пробілів/дефісів:
    # 4–8 цифр трактуємо як фрагмент картки;
    # 9–11 — як телефон;
    # 12–19 — як повну картку.
    if re.fullmatch(r"[\d\s-]+(?:\.0)?", raw):
        if 4 <= len(digits) <= 8 or 12 <= len(digits) <= 19:
            return "card"
        if 9 <= len(digits) <= 11 and normalize_phone(raw):
            return "phone"

    if normalize_phone(raw) and 10 <= len(digits) <= 12:
        return "phone"

    if raw.startswith("@") or "t.me/" in raw.casefold() or "facebook.com/" in raw.casefold():
        return "profile"

    return "name"


def scam_search_matches(kind, value):
    if kind == "card":
        raw = str(value or "").strip()
        if re.fullmatch(r"[\d\s-]+\.0", raw):
            raw = raw[:-2]
        if not re.fullmatch(r"[\d\s-]+", raw):
            return []
        digits = "".join(ch for ch in raw if ch.isdigit())
        return scam_db_results_by_card(digits) if 4 <= len(digits) <= 19 else []

    if kind == "phone":
        return scam_db_results_by_phone(value)

    if kind == "profile":
        return scam_db_results_by_profile(value)

    if kind in ("nick", "name"):
        return scam_db_results_by_subject(value)

    if kind == "any":
        detected = detect_scam_query_kind(value)
        return scam_search_matches(detected, value)

    return []


def scam_db_result_text(kind, value):
    if kind == "card":
        raw = str(value or "").strip()
        if re.fullmatch(r"[\d\s-]+\.0", raw):
            raw = raw[:-2]
        if not re.fullmatch(r"[\d\s-]+", raw):
            return None
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not (4 <= len(digits) <= 19):
            return None
        subject = card_mask(digits) if len(digits) >= 12 else f"фрагмент ••••{digits}"
    elif kind == "phone":
        phone = normalize_phone(value)
        if not phone:
            return None
        subject = "+" + phone
    else:
        subject = str(value or "").strip()
        if len(subject) < 2:
            return None

    matches = scam_search_matches(kind, value)

    lines = [
        "🤡 ПЕРЕВІРКА У НАШІЙ БАЗІ",
        f"🔎 Запит: {subject}",
    ]

    if not matches:
        lines.extend([
            "",
            "🟢 Точних або достатньо близьких збігів у нашій базі не знайдено.",
        ])
    else:
        lines.extend([
            "",
            f"🔴 Знайдено підтверджених збігів: {len(matches)}",
            "",
        ])

        # Показуємо всі релевантні варіанти, доки Telegram дозволяє довжину.
        for index, (row, field_name, reason) in enumerate(matches, start=1):
            status_icon = "🔴"
            status_text = "підтверджено адміністратором"
            card_info = (
                f" | 💳 ****{row['card_last4']}"
                if row["card_last4"]
                else ""
            )

            source_label = row.get("source") or "невідоме джерело"
            created_label = format_dt(row["created_at"]) if row.get("created_at") else "—"
            item = (
                f"{index}. {status_icon} {row['subject_text']}{card_info}\n"
                f"   ↳ {reason}; поле: {field_name}; {status_text}\n"
                f"   📅 Додано: {created_label} | 📌 Джерело: {source_label}"
            )

            # Для найкращих кількох збігів даємо короткий опис.
            if index <= 5 and row["description"]:
                description = " ".join(row["description"].split())
                if len(description) > 160:
                    description = description[:157] + "..."
                item += f"\n   🤡 {description}"

            # Не обрізаємо посередині запису. Якщо повідомлення стане завеликим,
            # завершимо списком і повідомимо, скільки ще є збігів.
            projected = "\n".join(lines + [item])
            if len(projected) > 3600:
                remaining = len(matches) - index + 1
                lines.append(f"… ще збігів: {remaining}. Уточніть запит, щоб звузити список.")
                break

            lines.append(item)

    lines.extend([
        "",
        "⚠️ Це інформація з внутрішньої бази повідомлень і модерації. "
        "Збіг імені сам по собі не доводить особу — звіряйте картку, телефон, профіль та докази.",
    ])
    return "\n".join(lines)[:3900]


def scam_check_text(kind, value):
    local_text = scam_db_result_text(kind, value)
    if not local_text:
        return None
    return (
        local_text
        + "\n\n🌐 Додатково можна вручну перевірити відкриті джерела "
          "кнопками нижче."
    )


# =========================================================
# ВОДЯНІ ЗНАКИ
# =========================================================

def watermark_text_menu():
    return {
        "inline_keyboard": [
            [{"text": "⚖️ Східний Аукціон", "callback_data": "wmtext:default"}],
            [{"text": "✍️ Свій текст", "callback_data": "wmtext:custom"}],
            [{"text": "❌ Скасувати", "callback_data": "wm:cancel"}],
        ]
    }


def watermark_position_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "🎯 Центр", "callback_data": "wmpos:center"},
                {"text": "⬆️ Зверху", "callback_data": "wmpos:top"},
                {"text": "⬇️ Знизу", "callback_data": "wmpos:bottom"},
            ],
            [
                {"text": "⬅️ Зліва", "callback_data": "wmpos:left"},
                {"text": "➡️ Справа", "callback_data": "wmpos:right"},
            ],
            [
                {"text": "↖️ Верх-ліво", "callback_data": "wmpos:top_left"},
                {"text": "↗️ Верх-право", "callback_data": "wmpos:top_right"},
            ],
            [
                {"text": "↙️ Низ-ліво", "callback_data": "wmpos:bottom_left"},
                {"text": "↘️ Низ-право", "callback_data": "wmpos:bottom_right"},
            ],
            [{"text": "📐 По діагоналі", "callback_data": "wmpos:diagonal"}],
            [{"text": "🔳 По всьому фото", "callback_data": "wmpos:tile"}],
            [{"text": "❌ Скасувати", "callback_data": "wm:cancel"}],
        ]
    }


def watermark_opacity_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "🌫 Легкий 20%", "callback_data": "wmopacity:20"},
                {"text": "◻️ Середній 35%", "callback_data": "wmopacity:35"},
            ],
            [{"text": "◼️ Виразний 50%", "callback_data": "wmopacity:50"}],
            [{"text": "❌ Скасувати", "callback_data": "wm:cancel"}],
        ]
    }


def watermark_photos_menu(count):
    return {
        "inline_keyboard": [
            [{"text": f"✅ Обробити фото ({count})", "callback_data": "wm:process"}],
            [{"text": "❌ Скасувати", "callback_data": "wm:cancel"}],
        ]
    }


def find_watermark_font(size):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def text_size(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=1)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def add_watermark_to_image(image, text_value, position, opacity):
    base = image.convert("RGBA")
    width, height = base.size
    font_size = max(18, min(width, height) // 18)
    font = find_watermark_font(font_size)
    alpha = max(15, min(90, int(255 * opacity / 100)))

    overlay = Image.new("RGBA", base.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)
    tw, th = text_size(draw, text_value, font)
    margin = max(12, min(width, height) // 40)

    def draw_at(x, y):
        draw.text(
            (x, y),
            text_value,
            font=font,
            fill=(255, 255, 255, alpha),
            stroke_width=max(1, font_size // 30),
            stroke_fill=(0, 0, 0, max(20, alpha // 2)),
        )

    positions = {
        "center": ((width - tw) // 2, (height - th) // 2),
        "top": ((width - tw) // 2, margin),
        "bottom": ((width - tw) // 2, height - th - margin),
        "left": (margin, (height - th) // 2),
        "right": (width - tw - margin, (height - th) // 2),
        "top_left": (margin, margin),
        "top_right": (width - tw - margin, margin),
        "bottom_left": (margin, height - th - margin),
        "bottom_right": (width - tw - margin, height - th - margin),
    }

    if position in positions:
        x, y = positions[position]
        draw_at(max(margin, x), max(margin, y))
    elif position == "diagonal":
        strip_w = max(width, height) * 2
        strip_h = max(th * 3, font_size * 3)
        strip = Image.new("RGBA", (strip_w, strip_h), (255, 255, 255, 0))
        sdraw = ImageDraw.Draw(strip)
        sw, sh = text_size(sdraw, text_value, font)
        sdraw.text(
            ((strip_w - sw) // 2, (strip_h - sh) // 2),
            text_value,
            font=font,
            fill=(255, 255, 255, alpha),
            stroke_width=max(1, font_size // 30),
            stroke_fill=(0, 0, 0, max(20, alpha // 2)),
        )
        rotated = strip.rotate(28, expand=True, resample=Image.Resampling.BICUBIC)
        overlay.alpha_composite(
            rotated,
            ((width - rotated.width) // 2, (height - rotated.height) // 2),
        )
    else:  # tile
        step_x = max(tw + margin * 4, width // 2)
        step_y = max(th + margin * 5, height // 4)
        y = margin
        row = 0
        while y < height:
            x = margin - (step_x // 3 if row % 2 else 0)
            while x < width:
                draw_at(x, y)
                x += step_x
            y += step_y
            row += 1

    result = Image.alpha_composite(base, overlay).convert("RGB")
    return result


def get_telegram_file_bytes(file_id):
    info = telegram_api("getFile", {"file_id": file_id})
    if not info.get("ok"):
        raise RuntimeError(info.get("description", "Telegram getFile error"))
    file_path = info["result"]["file_path"]
    response = requests.get(
        f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}",
        timeout=40,
    )
    response.raise_for_status()
    return response.content


def send_photo_bytes(chat_id, photo_bytes, filename="watermark.jpg", caption=None):
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    files = {"photo": (filename, photo_bytes, "image/jpeg")}
    response = requests.post(
        f"{TELEGRAM_API}/sendPhoto",
        data=data,
        files=files,
        timeout=60,
    )
    return response.json()


def send_media_group_bytes(chat_id, items):
    media = []
    files = {}
    for idx, photo_bytes in enumerate(items):
        key = f"file{idx}"
        media.append({"type": "photo", "media": f"attach://{key}"})
        files[key] = (f"watermark_{idx+1}.jpg", photo_bytes, "image/jpeg")

    response = requests.post(
        f"{TELEGRAM_API}/sendMediaGroup",
        data={"chat_id": str(chat_id), "media": json.dumps(media, ensure_ascii=False)},
        files=files,
        timeout=120,
    )
    return response.json()


def process_watermark_batch(chat_id, user_id, data):
    file_ids = data.get("photos", [])
    if not file_ids:
        send_message(chat_id, "❌ Фото для обробки не знайдені.")
        return

    processed = []
    try:
        for file_id in file_ids:
            raw = get_telegram_file_bytes(file_id)
            with Image.open(io.BytesIO(raw)) as img:
                result = add_watermark_to_image(
                    img,
                    data["watermark_text"],
                    data["position"],
                    int(data["opacity"]),
                )
                out = io.BytesIO()
                result.save(out, format="JPEG", quality=92, optimize=True)
                processed.append(out.getvalue())

        if len(processed) == 1:
            send_photo_bytes(
                chat_id,
                processed[0],
                caption="✅ Водяний знак нанесено.",
            )
        else:
            send_media_group_bytes(chat_id, processed)
            send_message(chat_id, f"✅ Готово. Оброблено фото: {len(processed)}")
    except Exception as exc:
        print("WATERMARK ERROR:", repr(exc))
        send_message(chat_id, "❌ Не вдалося обробити фото. Спробуйте ще раз.")
    finally:
        delete_draft(user_id)
        show_main_menu(chat_id)


def lot_section_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "🪙 Нумізматика", "callback_data": "sec:num"},
                {"text": "💵 Боністика", "callback_data": "sec:bon"},
            ],
            [{"text": "❌ Скасувати лот", "callback_data": "lot:cancel"}],
        ]
    }


NUM_SECTIONS = [
    ("n_precious", "🥇 Золоті, платинові та паладієві монети"),
    ("n_rus", "🏰 Київська Русь, Литовська Русь та удільний період"),
    ("n_empire", "👑 Монети Російської імперії"),
    ("n_poland", "🇵🇱 Монети Польщі"),
    ("n_ussr", "☭ Монети РРФСР та СРСР"),
    ("n_ukraine", "🇺🇦 Монети України"),
    ("n_asia", "🌏 Монети Азії"),
    ("n_africa", "🌍 Монети Африки"),
    ("n_antique", "🏛️ Античні монети"),
    ("n_east", "🕌 Монети країн Сходу до 1918 р."),
    ("n_medieval", "🏰 Середньовічна Європа"),
    ("n_germany", "🇩🇪 Монети Німеччини"),
    ("n_austria", "🇦🇹 Австрія та Австро-Угорщина"),
    ("n_usa", "🇺🇸 Монети США"),
    ("n_silver", "🥈 Срібні монети світу"),
    ("n_europe", "🇪🇺 Монети Європи"),
    ("n_world", "🌎 Америка, Австралія та Океанія"),
    ("n_tokens", "🪙 Жетони та токени"),
    ("n_sets", "📦 Монети оптом / колекції"),
    ("n_docs", "📜 Документи та сертифікати монет"),
]

BON_SECTIONS = [
    ("b_empire", "👑 Бони Імператорської Росії"),
    ("b_civil", "⚔️ Бони громадянської війни 1917–1923"),
    ("b_ussr", "☭ Бони РРФСР та СРСР"),
    ("b_russia", "🇷🇺 Білети Банку Росії"),
    ("b_ua_centers", "🏛️ Українські емісійні центри до розпаду СРСР"),
    ("b_ukraine", "🇺🇦 Бони України"),
    ("b_europe", "🇪🇺 Бони Європи"),
    ("b_usa", "🇺🇸 Бони США"),
    ("b_asia", "🌏 Бони Азії"),
    ("b_africa", "🌍 Бони Африки"),
    ("b_world", "🌎 Америка, Австралія та Океанія"),
    ("b_sets", "📦 Колекції та добірки бон"),
]

SUBSECTIONS = {
    "n_precious": [
        ("np_gold", "🥇 Золоті монети"),
        ("np_platinum", "⚪ Платинові монети"),
        ("np_palladium", "🔘 Паладієві монети"),
        ("np_bimetal", "🟡 Біметал із дорогоцінних металів"),
    ],
    "n_rus": [
        ("nr_kyiv", "🏰 Київська Русь"),
        ("nr_lith", "⚔️ Литовська Русь"),
        ("nr_udil", "👑 Удільні князівства"),
        ("nr_prague", "🪙 Празькі гроші та наслідування"),
    ],
    "n_empire": [
        ("ne_pre1700", "👑 Допетровський період"),
        ("ne_peter", "⚓ Петро I"),
        ("ne_1700", "🦅 XVIII століття"),
        ("ne_1800", "🦅 XIX століття"),
        ("ne_1900", "🦅 1900–1917"),
        ("ne_comm", "🎖️ Пам'ятні та ювілейні"),
    ],
    "n_poland": [
        ("npl_kingdom", "👑 Королівство Польське"),
        ("npl_commonwealth", "⚜️ Річ Посполита"),
        ("npl_1918", "🇵🇱 Польща 1918–1939"),
        ("npl_modern", "🇵🇱 Польща після 1945"),
    ],
    "n_ussr": [
        ("ns_rsfsr", "☭ Монети РРФСР"),
        ("ns_regular", "🪙 Обігові монети СРСР"),
        ("ns_trials", "🧪 Пробні монети СРСР"),
        ("ns_comm", "🎖️ Ювілейні та пам'ятні СРСР"),
        ("ns_sets", "📦 Річні набори та тематичні добірки"),
        ("ns_errors", "⚠️ Браки та різновиди"),
    ],
    "n_ukraine": [
        ("nu_regular", "🪙 Обігові монети України"),
        ("nu_comm", "🎖️ Пам'ятні та ювілейні НБУ"),
        ("nu_precious", "🥇 Монети України з дорогоцінних металів"),
        ("nu_trials", "🧪 Пробні монети та зразки"),
        ("nu_errors", "⚠️ Браки, різновиди та помилки"),
        ("nu_sets", "📦 Набори та ролики"),
    ],
    "n_asia": [
        ("na_china", "🇨🇳 Китай"),
        ("na_japan", "🇯🇵 Японія"),
        ("na_korea", "🇰🇷 Корея"),
        ("na_india", "🇮🇳 Індія"),
        ("na_seasia", "🌏 Південно-Східна Азія"),
        ("na_other", "🪙 Інші країни Азії"),
    ],
    "n_africa": [
        ("naf_north", "🌍 Північна Африка"),
        ("naf_south", "🌍 Південна Африка"),
        ("naf_central", "🌍 Центральна та Західна Африка"),
        ("naf_other", "🪙 Інші країни Африки"),
    ],
    "n_antique": [
        ("nant_blacksea", "🌊 Північне Причорномор'я"),
        ("nant_bospor", "🏺 Боспорське царство та Крим"),
        ("nant_greece", "🏛️ Стародавня Греція"),
        ("nant_rome", "🦅 Стародавній Рим"),
        ("nant_byz", "👑 Візантія"),
        ("nant_other", "🏺 Інша антика"),
    ],
    "n_east": [
        ("no_ottoman", "☪️ Османська імперія"),
        ("no_persia", "🕌 Персія"),
        ("no_khan", "🐎 Ханства та емірати"),
        ("no_islamic", "☪️ Мусульманські династії"),
        ("no_other", "🪙 Інший Схід до 1918 р."),
    ],
    "n_medieval": [
        ("nm_britain", "🏰 Британські острови"),
        ("nm_france", "⚜️ Франція"),
        ("nm_italy", "🏛️ Італійські держави"),
        ("nm_iberia", "🏰 Іспанія та Португалія"),
        ("nm_balkans", "🏰 Балкани"),
        ("nm_other", "🪙 Інша середньовічна Європа"),
    ],
    "n_germany": [
        ("ng_empire", "🇩🇪 Німецька імперія 1871–1918"),
        ("ng_weimar", "🇩🇪 Німеччина 1919–1933"),
        ("ng_1933", "🇩🇪 Німеччина 1933–1945"),
        ("ng_gdr", "☭ НДР"),
        ("ng_frg", "🇩🇪 ФРН"),
        ("ng_states", "🏰 Німецькі держави до 1871"),
    ],
    "n_austria": [
        ("nat_habsburg", "👑 Габсбурзькі землі"),
        ("nat_empire", "🇦🇹 Австрійська імперія"),
        ("nat_ah", "🦅 Австро-Угорщина"),
        ("nat_modern", "🇦🇹 Австрія XX–XXI ст."),
    ],
    "n_usa": [
        ("nus_regular", "🇺🇸 Обігові монети США"),
        ("nus_silver", "🥈 Срібні монети США"),
        ("nus_gold", "🥇 Золоті монети США"),
        ("nus_comm", "🎖️ Пам'ятні монети США"),
        ("nus_errors", "⚠️ Браки та різновиди США"),
    ],
    "n_silver": [
        ("nsi_europe", "🥈 Срібло Європи"),
        ("nsi_america", "🥈 Срібло Америки"),
        ("nsi_asia", "🥈 Срібло Азії"),
        ("nsi_other", "🥈 Інше срібло світу"),
    ],
    "n_europe": [
        ("neu_pre1918", "🇪🇺 Європа до 1918"),
        ("neu_1919", "🇪🇺 Європа 1919–1970"),
        ("neu_1971", "🇪🇺 Європа 1971–2001"),
        ("neu_euro", "💶 Євро та монети після 2002"),
        ("neu_other", "🪙 Інша Європа"),
    ],
    "n_world": [
        ("nw_namerica", "🌎 Канада та Північна Америка"),
        ("nw_samerica", "🌎 Південна Америка"),
        ("nw_australia", "🇦🇺 Австралія"),
        ("nw_oceania", "🌊 Океанія"),
        ("nw_other", "🪙 Інші території"),
    ],
    "n_tokens": [
        ("nt_trade", "🪙 Торгові та платіжні жетони"),
        ("nt_transport", "🚋 Транспортні жетони"),
        ("nt_casino", "🎰 Ігрові жетони"),
        ("nt_medals", "🏅 Медалі та пам'ятні жетони"),
        ("nt_other", "🪙 Інші жетони і токени"),
    ],
    "n_sets": [
        ("nset_country", "📦 Колекції за країнами"),
        ("nset_period", "📦 Колекції за періодами"),
        ("nset_rolls", "🧰 Ролики та мішки"),
        ("nset_bulk", "⚖️ Монети оптом"),
        ("nset_other", "📦 Інші набори"),
    ],
    "n_docs": [
        ("nd_cert", "📜 Сертифікати до монет"),
        ("nd_boxes", "🎁 Футляри та пакування"),
        ("nd_docs", "📄 Документи НБУ та монетних дворів"),
        ("nd_other", "📚 Інша супровідна документація"),
    ],

    "b_empire": [
        ("be_state", "👑 Державні кредитні білети"),
        ("be_notes", "💵 Банкноти та розмінні знаки"),
        ("be_local", "🏛️ Місцеві випуски"),
        ("be_other", "📜 Інші бони імперії"),
    ],
    "b_civil": [
        ("bc_white", "⚔️ Білі уряди та армії"),
        ("bc_red", "☭ Радянські місцеві випуски"),
        ("bc_city", "🏙️ Міські та регіональні випуски"),
        ("bc_private", "🏭 Приватні та кооперативні випуски"),
    ],
    "b_ussr": [
        ("bu_rsfsr", "☭ РРФСР"),
        ("bu_1922", "💵 СРСР 1922–1960"),
        ("bu_1961", "💵 СРСР 1961–1991"),
        ("bu_checks", "🎟️ Чеки, сертифікати та замінники грошей"),
        ("bu_errors", "⚠️ Браки, зразки та різновиди"),
    ],
    "b_russia": [
        ("br_1992", "🇷🇺 Росія 1992–1999"),
        ("br_2000", "🇷🇺 Росія 2000–сьогодні"),
        ("br_samples", "🧪 Зразки та тестові банкноти"),
    ],
    "b_ua_centers": [
        ("buc_unr", "🇺🇦 УНР та Українська Держава"),
        ("buc_zunr", "🏛️ ЗУНР"),
        ("buc_local", "🏙️ Місцеві українські випуски"),
        ("buc_other", "📜 Інші українські емісійні центри"),
    ],
    "b_ukraine": [
        ("buk_karb", "🇺🇦 Купоно-карбованці"),
        ("buk_hryvnia", "💵 Гривні"),
        ("buk_comm", "🎖️ Пам'ятні банкноти"),
        ("buk_samples", "🧪 Зразки, тестові та нерозрізані аркуші"),
        ("buk_errors", "⚠️ Браки та різновиди"),
    ],
    "b_europe": [
        ("bev_west", "🇪🇺 Західна Європа"),
        ("bev_east", "🇪🇺 Східна Європа"),
        ("bev_balkans", "🇪🇺 Балкани"),
        ("bev_notgeld", "🎟️ Нотгельди"),
        ("bev_other", "💶 Інша Європа"),
    ],
    "b_usa": [
        ("bus_federal", "🇺🇸 Федеральні резервні банкноти"),
        ("bus_old", "🦅 США до 1928"),
        ("bus_silver", "🥈 Silver Certificates"),
        ("bus_gold", "🥇 Gold Certificates"),
        ("bus_other", "💵 Інші бони США"),
    ],
    "b_asia": [
        ("bas_china", "🇨🇳 Китай"),
        ("bas_japan", "🇯🇵 Японія"),
        ("bas_india", "🇮🇳 Індія"),
        ("bas_seasia", "🌏 Південно-Східна Азія"),
        ("bas_other", "💵 Інші країни Азії"),
    ],
    "b_africa": [
        ("baf_north", "🌍 Північна Африка"),
        ("baf_south", "🌍 Південна Африка"),
        ("baf_central", "🌍 Центральна та Західна Африка"),
        ("baf_other", "💵 Інші країни Африки"),
    ],
    "b_world": [
        ("bw_canada", "🇨🇦 Канада"),
        ("bw_latin", "🌎 Латинська Америка"),
        ("bw_australia", "🇦🇺 Австралія"),
        ("bw_oceania", "🌊 Океанія"),
        ("bw_other", "💵 Інші території"),
    ],
    "b_sets": [
        ("bset_country", "📦 Колекції за країнами"),
        ("bset_period", "📦 Колекції за періодами"),
        ("bset_theme", "📦 Тематичні добірки"),
        ("bset_other", "📦 Інші набори бон"),
    ],
}

TOP_SECTION_MAP = dict(NUM_SECTIONS + BON_SECTIONS)
SUBSECTION_MAP = {
    sub_key: sub_label
    for values in SUBSECTIONS.values()
    for sub_key, sub_label in values
}


def top_category_menu(section):
    source = NUM_SECTIONS if section == "numizmatika" else BON_SECTIONS
    rows = []
    for i in range(0, len(source), 2):
        row = []
        for key, label in source[i:i + 2]:
            row.append({"text": label, "callback_data": f"cat:{key}"})
        rows.append(row)
    rows.append([
        {"text": "⬅️ Назад", "callback_data": "cat:back"},
        {"text": "❌ Скасувати", "callback_data": "lot:cancel"},
    ])
    return {"inline_keyboard": rows}


def subsection_menu(category_key):
    rows = []
    for i in range(0, len(SUBSECTIONS.get(category_key, [])), 2):
        row = []
        for key, label in SUBSECTIONS[category_key][i:i + 2]:
            row.append({"text": label, "callback_data": f"sub:{key}"})
        rows.append(row)
    rows.append([
        {"text": "⬅️ Назад до розділів", "callback_data": "sub:back"},
        {"text": "❌ Скасувати", "callback_data": "lot:cancel"},
    ])
    return {"inline_keyboard": rows}


def search_main_menu():
    return {
        "inline_keyboard": [
            [{"text": "🗂 Пошук за розділом / підрозділом", "callback_data": "search:category"}],
            [{"text": "🔎 Пошук за назвою / ключовими словами", "callback_data": "search:keyword"}],
            [{"text": "🟢 Аукціони, які тривають", "callback_data": "search:active_auctions"}],
            [{"text": "1️⃣ Аукціони зі стартом 1 грн", "callback_data": "search:one_uah"}],
            [{"text": "⬅️ Головне меню", "callback_data": "search:home"}],
        ]
    }


def search_section_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "🪙 Нумізматика", "callback_data": "searchsec:num"},
                {"text": "💵 Боністика", "callback_data": "searchsec:bon"},
            ],
            [{"text": "⬅️ Назад", "callback_data": "search:back"}],
        ]
    }


def search_top_category_menu(section):
    source = NUM_SECTIONS if section == "numizmatika" else BON_SECTIONS
    rows = []
    for i in range(0, len(source), 2):
        row = []
        for key, label in source[i:i + 2]:
            row.append({"text": label, "callback_data": f"searchcat:{key}"})
        rows.append(row)
    rows.append([{"text": "⬅️ Назад", "callback_data": "search:category"}])
    return {"inline_keyboard": rows}


def search_subsection_menu(category_key):
    rows = [[{
        "text": "📚 Усі лоти цього розділу",
        "callback_data": f"searchall:{category_key}",
    }]]
    source = SUBSECTIONS.get(category_key, [])
    for i in range(0, len(source), 2):
        row = []
        for key, label in source[i:i + 2]:
            row.append({"text": label, "callback_data": f"searchsub:{key}"})
        rows.append(row)
    section = "num" if category_key.startswith("n_") else "bon"
    rows.append([{"text": "⬅️ Назад до розділів", "callback_data": f"searchsec:{section}"}])
    return {"inline_keyboard": rows}


def subscriptions_main_menu():
    return {
        "inline_keyboard": [
            [{"text": "🪙 Нумізматика", "callback_data": "subscrsec:num"}],
            [{"text": "💵 Боністика", "callback_data": "subscrsec:bon"}],
            [{"text": "📋 Мої підписки", "callback_data": "subscr:list"}],
            [{"text": "🧹 Видалити всі підписки", "callback_data": "subscr:clear"}],
            [{"text": "⬅️ Головне меню", "callback_data": "subscr:home"}],
        ]
    }


def subscription_exists(user_id, section, category_key=None, subcategory_key=None):
    with db() as conn:
        row = conn.execute(
            """
            SELECT id FROM lot_subscriptions
            WHERE user_id=?
              AND section=?
              AND category_key IS NOT DISTINCT FROM ?
              AND subcategory_key IS NOT DISTINCT FROM ?
            LIMIT 1
            """,
            (user_id, section, category_key, subcategory_key),
        ).fetchone()
    return bool(row)


def toggle_subscription(user_id, section, category_key=None, subcategory_key=None):
    with db() as conn:
        row = conn.execute(
            """
            SELECT id FROM lot_subscriptions
            WHERE user_id=?
              AND section=?
              AND category_key IS NOT DISTINCT FROM ?
              AND subcategory_key IS NOT DISTINCT FROM ?
            LIMIT 1
            """,
            (user_id, section, category_key, subcategory_key),
        ).fetchone()

        if row:
            conn.execute("DELETE FROM lot_subscriptions WHERE id=?", (row["id"],))
            return False

        conn.execute(
            """
            INSERT INTO lot_subscriptions(
                user_id, section, category_key, subcategory_key, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, section, category_key, subcategory_key, time.time()),
        )
        return True


def subscriptions_section_menu(user_id, section):
    source = NUM_SECTIONS if section == "numizmatika" else BON_SECTIONS
    title = "🪙 Вся Нумізматика" if section == "numizmatika" else "💵 Вся Боністика"
    whole_on = subscription_exists(user_id, section, None, None)

    rows = [[{
        "text": ("✅ " if whole_on else "🔔 ") + title,
        "callback_data": f"subscrall:{section}",
    }]]

    for key, label in source:
        # Позначка ✅, якщо є підписка на весь цей розділ.
        whole_category = subscription_exists(user_id, section, key, None)
        rows.append([{
            "text": ("✅ " if whole_category else "📂 ") + label,
            "callback_data": f"subscrcat:{section}:{key}",
        }])

    rows.append([{"text": "⬅️ Назад", "callback_data": "subscr:back"}])
    return {"inline_keyboard": rows}


def subscriptions_subsection_menu(user_id, section, category_key):
    category_label = TOP_SECTION_MAP.get(category_key, "Розділ")
    whole_on = subscription_exists(user_id, section, category_key, None)

    rows = [[{
        "text": ("✅ " if whole_on else "🔔 ") + "Увесь цей розділ",
        "callback_data": f"subscrwholecat:{section}:{category_key}",
    }]]

    for sub_key, sub_label in SUBSECTIONS.get(category_key, []):
        on = subscription_exists(user_id, section, category_key, sub_key)
        rows.append([{
            "text": ("✅ " if on else "▫️ ") + sub_label,
            "callback_data": f"subscrsub:{section}:{category_key}:{sub_key}",
        }])

    rows.append([{
        "text": "⬅️ До розділів",
        "callback_data": f"subscrsec:{'num' if section == 'numizmatika' else 'bon'}",
    }])

    return {
        "text": f"🔔 {category_label}",
        "reply_markup": {"inline_keyboard": rows},
    }


def subscription_label(row):
    section = row["section"]
    category_key = row["category_key"]
    subcategory_key = row["subcategory_key"]

    if category_key is None:
        return "🪙 Вся Нумізматика" if section == "numizmatika" else "💵 Вся Боністика"

    category = TOP_SECTION_MAP.get(category_key, category_key)
    if subcategory_key is None:
        return f"📂 {category}"

    subcategory = SUBSECTION_MAP.get(subcategory_key, subcategory_key)
    return f"▫️ {category} → {subcategory}"


def my_subscriptions_text(user_id):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT section, category_key, subcategory_key
            FROM lot_subscriptions
            WHERE user_id=?
            ORDER BY section, category_key NULLS FIRST, subcategory_key NULLS FIRST
            """,
            (user_id,),
        ).fetchall()

    if not rows:
        return (
            "📋 МОЇ ПІДПИСКИ\n\n"
            "У вас поки немає підписок на нові лоти."
        )

    lines = ["📋 МОЇ ПІДПИСКИ", ""]
    for idx, row in enumerate(rows, 1):
        lines.append(f"{idx}. {subscription_label(row)}")

    lines.extend([
        "",
        "🔔 Коли новий лот із вибраної категорії буде опубліковано, "
        "бот надішле вам приватне повідомлення."
    ])
    return "\n".join(lines)


def matching_subscription_user_ids(lot):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT user_id
            FROM lot_subscriptions
            WHERE section=?
              AND (
                    (category_key IS NULL AND subcategory_key IS NULL)
                 OR (category_key=? AND subcategory_key IS NULL)
                 OR (category_key=? AND subcategory_key=?)
              )
            """,
            (
                lot["section"],
                lot["category_key"],
                lot["category_key"],
                lot["subcategory_key"],
            ),
        ).fetchall()
    return [int(r["user_id"]) for r in rows]


def notify_lot_subscribers(lot_id):
    lot = get_lot(lot_id)
    if not lot or lot["status"] != "active":
        return

    subscribers = matching_subscription_user_ids(lot)
    if not subscribers:
        return

    url = lot_public_url(lot)
    markup = None
    if url:
        markup = {
            "inline_keyboard": [[
                {"text": "➡️ Відкрити лот", "url": url}
            ]]
        }

    if lot["sale_type"] == "auction":
        price_line = f"💰 Поточна ціна: {lot['current_price']} грн"
        sale_line = "🔨 Аукціон"
    else:
        price_line = f"💰 Ціна: {lot['fixed_price']} грн"
        sale_line = "💵 Фіксована ціна"

    text_msg = (
        "🔔 НОВИЙ ЛОТ ЗА ВАШОЮ ПІДПИСКОЮ\n\n"
        f"🏷 {lot['title']}\n"
        f"🗂 {lot['category']}\n"
        f"📂 {lot['subcategory'] or '—'}\n"
        f"{sale_line}\n"
        f"{price_line}\n"
        f"⏰ Завершення: {format_dt(lot['end_ts'])}"
    )

    photos = get_lot_photos(lot_id)
    for user_id in subscribers:
        # Продавцю його власний лот не дублюємо.
        if user_id == int(lot["seller_id"]):
            continue
        try:
            if photos:
                send_photo(
                    user_id,
                    photos[0],
                    caption=text_msg,
                    reply_markup=markup,
                )
            else:
                send_message(user_id, text_msg, markup)
            time.sleep(0.05)
        except Exception:
            # Якщо користувач заблокував бота — просто пропускаємо.
            pass


def sale_type_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "🔨 Аукціон", "callback_data": "type:auction"},
                {"text": "💰 Фіксована ціна", "callback_data": "type:fixed"},
            ],
            [
                {"text": "⬅️ Назад", "callback_data": "type:back"},
                {"text": "❌ Скасувати", "callback_data": "lot:cancel"},
            ],
        ]
    }


def yes_no_menu(prefix, yes_text="✅ Так", no_text="❌ Ні"):
    return {
        "inline_keyboard": [[
            {"text": yes_text, "callback_data": f"{prefix}:yes"},
            {"text": no_text, "callback_data": f"{prefix}:no"},
        ], [{"text": "❌ Скасувати лот", "callback_data": "lot:cancel"}]]
    }


def anti_sniper_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "Без антиснайпера", "callback_data": "anti:0"},
                {"text": "+5 хв", "callback_data": "anti:5"},
            ],
            [
                {"text": "+10 хв", "callback_data": "anti:10"},
                {"text": "+15 хв", "callback_data": "anti:15"},
            ],
            [{"text": "❌ Скасувати лот", "callback_data": "lot:cancel"}],
        ]
    }


def photos_done_menu(count):
    rows = []
    if count >= 1:
        rows.append([{"text": f"✅ Фото готові ({count})", "callback_data": "photos:done"}])
    rows.append([{"text": "❌ Скасувати лот", "callback_data": "lot:cancel"}])
    return {"inline_keyboard": rows}


def preview_menu():
    return {
        "inline_keyboard": [
            [{"text": "✅ Опублікувати", "callback_data": "preview:publish"}],
            [
                {"text": "🔄 Створити заново", "callback_data": "preview:restart"},
                {"text": "❌ Скасувати", "callback_data": "lot:cancel"},
            ],
        ]
    }


def admin_approval_menu(lot_id):
    return {
        "inline_keyboard": [[
            {"text": "✅ Погодити", "callback_data": f"admok:{lot_id}"},
            {"text": "❌ Відхилити", "callback_data": f"admno:{lot_id}"},
        ]]
    }


def auction_buttons(lot):
    if lot["status"] != "active":
        return {"inline_keyboard": []}

    lot_id = lot["id"]
    step = lot["bid_step"]
    autobid_url = f"https://t.me/{BOT_USERNAME}?start=autobid_{lot_id}"

    rows = [[
        {"text": f"📈 +{step} грн", "callback_data": f"bid:{lot_id}"},
        {"text": "🤖 Автоставка ↗️", "url": autobid_url},
    ]]
    if lot["blitz_price"]:
        rows.append([{
            "text": f"⚡ Бліц {lot['blitz_price']} грн",
            "callback_data": f"blitzlot:{lot_id}",
        }])

    rows.append([
        {"text": "❤️ Обране", "callback_data": f"favlot:{lot_id}"},
        {"text": "⭐ Репутація продавця", "callback_data": f"reputation:{lot['seller_id']}"},
    ])
    rows.append([{
        "text": "🛡 Використати Гарант",
        "callback_data": f"guarantee:{lot_id}",
    }])
    return {"inline_keyboard": rows}


# =========================================================
# ТЕКСТИ ЛОТІВ
# =========================================================

def main_menu_text():
    return (
        "🤖 КОМАНДИ ДЛЯ БОТА\n\n"
        "😎 /bot_menu - запуск меню БОТа\n"
        "📋 /help — список команд\n"
        "📣 /ping — перевірка роботи бота\n"
        "📜 /rules — правила групи\n"
        "⚖️ /lot - виставити лот у групу ✅\n"
        "🔎 /search_lot - пошук лотів\n"
        "🧵 /threadid - ID поточної теми\n\n"
        "⚠️ Доступні команди можуть змінюватися\n\n"
        "⬇️ Нижче меню БОТа ⬇️"
    )


def build_preview(data):
    common = (
        f"🏷 Назва:\n{data['title']}\n\n"
        f"🧱 Матеріал:\n{data['material']}\n\n"
    )
    if data["sale_type"] == "auction":
        blitz = f"{data['blitz_price']} грн" if data.get("blitz_price") else "немає"
        reserve = "🔒 Присутня резервна ціна" if data.get("reserve_price") else "немає"
        return (
            "📢 ПЕРЕВІРКА АУКЦІОННОГО ЛОТУ\n\n"
            "Перевірте надані вами дані перед публікацією⬇️\n\n"
            + common
            + f"💰 Стартова ціна:\n{data['start_price']} грн\n\n"
            + f"📈 Крок ставки:\n{data['bid_step']} грн\n\n"
            + f"⚡ Бліц-ціна:\n{blitz}\n\n"
            + f"🔒 Резерв:\n{reserve}\n\n"
            + f"⏰ Дата та час завершення:\n{format_dt(data['end_ts'])}\n\n"
            + f"⏱ Антиснайпер:\n+{data.get('anti_sniper', 0)} хв\n\n"
            + f"📞 Телефон для зв'язку:\n{data['phone']}\n\n"
            + f"💳 Оплата:\nкартка **** {data['card_last4']}\n\n"
            + f"ℹ️ Додаткова інформація:\n{data['extra_info']}\n\n"
            + f"🗂 Розділ:\n{data['category']}\n\n" + f"📂 Підрозділ:\n{data.get('subcategory', '—')}\n\n"
            + f"📸 Фото:\nотримано ✅ ({len(data.get('photos', []))})\n\n"
            + "⚠️ Перевірте всі дані перед публікацією."
        )
    return (
        "📢 ПЕРЕВІРКА ЛОТУ\n\n"
        "Перевірте надані вами дані для публікації⬇️\n\n"
        + common
        + f"💰 Фіксована ціна:\n{data['fixed_price']} грн\n\n"
        + f"📞 Телефон для зв'язку:\n{data['phone']}\n\n"
        + f"💳 Оплата: картка **** {data['card_last4']}\n\n"
        + f"⏰ Дата та час завершення:\n{format_dt(data['end_ts'])}\n\n"
        + f"ℹ️ Додаткова інформація:\n{data['extra_info']}\n\n"
        + f"🗂 Розділ:\n{data['category']}\n\n" + f"📂 Підрозділ:\n{data.get('subcategory', '—')}\n\n"
        + f"📸 Фото: отримано ✅ ({len(data.get('photos', []))})\n\n"
        + "⚠️ Перевірте дані перед публікацією."
    )


def build_public_lot(lot):
    if lot["sale_type"] == "fixed":
        status_text = "✅ АКТИВНИЙ ЛОТ" if lot["status"] == "active" else "🏁 ЛОТ ЗАВЕРШЕНО"
        return (
            f"{status_text}\n\n"
            f"🏷 {lot['title']}\n\n"
            f"🧱 Матеріал: {lot['material']}\n"
            f"💰 Фіксована ціна: {lot['fixed_price']} грн\n"
            f"⏰ Завершення: {format_dt(lot['end_ts'])}\n"
            f"🗂 Розділ: {lot['category']}\n" + f"📂 Підрозділ: {lot['subcategory'] or '—'}\n\n"
            f"👤 Продавець: {lot['seller_name']}\n"
            f"📞 Телефон: {lot['phone']}\n\n"
            f"ℹ️ {lot['extra_info']}\n\n"
            f"💳 Оплата: картка **** {lot['card_last4']}\n\n"
            f"🆔 Лот №{lot['id']}"
        )

    blitz = f"{lot['blitz_price']} грн" if lot["blitz_price"] else "немає"
    reserve = "🔒 Присутня резервна ціна" if lot["reserve_price"] else "🔓 Без резерву"
    if lot["status"] == "active":
        status_text = "🟢 Аукціон триває"
    else:
        status_text = "🔴 Аукціон завершено"

    if lot["leader_name"]:
        leader = f"{lot['leader_name']} — {lot['current_price']} грн"
    else:
        leader = "ставок ще немає"
    return (
        f"{status_text}\n\n"
        f"🏷 {lot['title']}\n\n"
        f"🧱 Матеріал: {lot['material']}\n"
        f"💰 Стартова ціна: {lot['start_price']} грн\n"
        f"💵 Поточна ціна: {lot['current_price']} грн\n"
        f"📈 Крок ставки: {lot['bid_step']} грн\n"
        f"⚡ Бліц-ціна: {blitz}\n"
        f"{reserve}\n"
        f"⏰ Завершення: {format_dt(lot['end_ts'])}\n"
        f"⏱ Антиснайпер: +{lot['anti_sniper']} хв\n\n"
        f"👑 Лідер: {leader}\n\n"
        f"👤 Продавець: {lot['seller_name']}\n"
        f"📞 Телефон: {lot['phone']}\n\n"
        f"ℹ️ {lot['extra_info']}\n"
        f"🗂 Розділ: {lot['category']}\n" + f"📂 Підрозділ: {lot['subcategory'] or '—'}\n\n"
        f"🆔 Лот №{lot['id']}"
    )


def build_admin_preview(lot, photos_count):
    reserve = f"{lot['reserve_price']} грн" if lot["reserve_price"] else "немає"
    return (
        "🛡 ПОГОДЖЕННЯ ЛОТА З РЕЗЕРВОМ\n\n"
        f"🆔 Лот №{lot['id']}\n"
        f"👤 Продавець: {lot['seller_name']}\n"
        f"🗂 {lot['category']}\n" + f"📂 {lot['subcategory'] or '—'}\n"
        f"🏷 {lot['title']}\n"
        f"💰 Старт: {lot['start_price']} грн\n"
        f"📈 Крок: {lot['bid_step']} грн\n"
        f"🔒 РЕЗЕРВ: {reserve}\n"
        f"⚡ Бліц: {lot['blitz_price'] or 'немає'}\n"
        f"⏰ Завершення: {format_dt(lot['end_ts'])}\n"
        f"📸 Фото: {photos_count}\n\n"
        "Сума резерву бачить тільки адміністратор."
    )


# =========================================================
# КОМАНДИ / СТАРТ СТВОРЕННЯ
# =========================================================

def show_main_menu(chat_id, user_id=None):
    # У приватному чаті chat_id == user_id, тому старі виклики
    # show_main_menu(chat_id) теж коректно приховують/показують адмін-кнопку.
    viewer_id = user_id if user_id is not None else chat_id
    return send_message(chat_id, main_menu_text(), main_menu(viewer_id))


def command_lot(chat_id, user_id):
    delete_draft(user_id)
    save_draft(user_id, chat_id, "choose_section", {})
    send_message(
        chat_id,
        "⚖️ СТВОРЕННЯ ЛОТА\n\n"
        "Оберіть розділ, у який бажаєте виставити лот 👇",
        lot_section_menu(),
    )


def cancel_lot(chat_id, user_id):
    delete_draft(user_id)
    send_message(chat_id, "❌ Створення лота скасовано.\n\nПовертаю в головне меню 👇")
    show_main_menu(chat_id)


def start_autobid(chat_id, user_id, lot_id):
    with db() as conn:
        lot = conn.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
    if not lot or lot["status"] != "active" or lot["sale_type"] != "auction":
        send_message(chat_id, "❌ Цей аукціон уже недоступний для автоставки.")
        return
    if lot["seller_id"] == user_id:
        send_message(chat_id, "❌ Продавець не може робити ставки на власний лот.")
        return
    minimum = lot["current_price"] + lot["bid_step"]
    save_draft(user_id, chat_id, "autobid_amount", {"lot_id": lot_id})
    send_message(
        chat_id,
        f"🤖 АВТОСТАВКА — лот №{lot_id}\n\n"
        f"Поточна ціна: {lot['current_price']} грн\n"
        f"Крок: {lot['bid_step']} грн\n\n"
        f"Введіть вашу максимальну суму.\n"
        f"Мінімум зараз: {minimum} грн\n\n"
        "⚠️ Інші учасники не бачитимуть вашу максимальну суму.",
    )


def private_start_url(payload):
    return f"https://t.me/{BOT_USERNAME}?start={payload}"


def temporary_private_redirect(message, text, payload):
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    thread_id = message.get("message_thread_id")

    markup = {
        "inline_keyboard": [[
            {"text": "➡️ Відкрити бота", "url": private_start_url(payload)}
        ]]
    }

    # Прибираємо саму команду з групи, якщо бот має права адміністратора.
    if message_id:
        try:
            delete_message(chat_id, message_id)
        except Exception:
            pass

    result = send_message(
        chat_id,
        text,
        markup,
        message_thread_id=thread_id,
    )
    if result.get("ok"):
        sent_id = result.get("result", {}).get("message_id")
        if sent_id:
            delete_later(chat_id, sent_id, 12)


def command_threadid(message):
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type", "private")
    thread_id = message.get("message_thread_id")

    if chat_type not in ("group", "supergroup"):
        send_message(
            chat_id,
            "🧵 /threadid потрібно запускати всередині теми Telegram-групи."
        )
        return

    if not thread_id:
        send_message(
            chat_id,
            "🧵 Це повідомлення не знаходиться в окремій темі або Telegram не передав message_thread_id."
        )
        return

    send_message(
        chat_id,
        f"🧵 ID цієї теми: {thread_id}",
        message_thread_id=thread_id,
    )


def command_search_lot(chat_id, user_id):
    delete_draft(user_id)
    send_message(
        chat_id,
        "🔎 ПОШУК ЛОТІВ\n\n"
        "Оберіть спосіб пошуку 👇",
        search_main_menu(),
    )


def lot_search_rows():
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM lots
            WHERE published_chat_id IS NOT NULL
            ORDER BY
                CASE WHEN status='active' THEN 0 ELSE 1 END,
                created_at DESC
            """
        ).fetchall()


def search_lots_by_category(category):
    return [
        row for row in lot_search_rows()
        if row["category"] == category
    ][:20]


def search_lots_by_subcategory(subcategory):
    return [
        row for row in lot_search_rows()
        if row["subcategory"] == subcategory
    ][:20]


def search_active_auctions():
    now = time.time()
    return [
        row for row in lot_search_rows()
        if row["sale_type"] == "auction"
        and row["status"] == "active"
        and row["end_ts"] > now
    ][:20]


def search_one_uah_auctions():
    now = time.time()
    return [
        row for row in lot_search_rows()
        if row["sale_type"] == "auction"
        and row["status"] == "active"
        and row["end_ts"] > now
        and int(row["start_price"] or 0) == 1
    ][:20]


def parse_price_range(value):
    raw = " ".join((value or "").strip().casefold().split())
    nums = [int(x) for x in re.findall(r"\d+", raw)]
    if not nums:
        return None

    if ("до" in raw or raw.startswith("<")) and len(nums) >= 1:
        return 0, nums[0]
    if ("від" in raw or raw.startswith(">")) and len(nums) >= 1:
        return nums[0], None
    if len(nums) >= 2:
        low, high = sorted(nums[:2])
        return low, high
    return 0, nums[0]


def search_lots_by_price_range(low, high=None):
    rows = lot_search_rows()
    result = []
    for lot in rows:
        price = (
            lot["fixed_price"]
            if lot["sale_type"] == "fixed"
            else lot["current_price"]
        )
        if price is None:
            continue
        if low is not None and price < low:
            continue
        if high is not None and price > high:
            continue
        result.append(lot)
    return result[:50]


def search_lots_by_keywords(query):
    words = [w.casefold() for w in query.split() if w.strip()]
    if not words:
        return []

    results = []
    for row in lot_search_rows():
        haystack = " ".join([
            str(row["title"] or ""),
            str(row["material"] or ""),
            str(row["extra_info"] or ""),
            str(row["category"] or ""),
            str(row["subcategory"] or ""),
            str(row["seller_name"] or ""),
        ]).casefold()

        if all(word in haystack for word in words):
            results.append(row)
        if len(results) >= 20:
            break
    return results


def lot_public_url(lot):
    msg_id = lot["published_content_message_id"] or lot["published_message_id"]
    if not msg_id:
        return None
    return f"{TELEGRAM_GROUP_URL.rstrip('/')}/{msg_id}"


def search_result_text(lot):
    if lot["status"] == "active":
        status = "🟢 Активний"
    else:
        status = "🔴 Завершений"

    if lot["sale_type"] == "auction":
        price = f"{lot['current_price']} грн"
        sale = "🔨 Аукціон"
    else:
        price = f"{lot['fixed_price']} грн"
        sale = "💰 Фіксована ціна"

    return (
        f"{status}\n"
        f"🆔 Лот №{lot['id']}\n"
        f"🏷 {lot['title']}\n"
        f"🗂 {lot['category']}\n"
        f"📂 {lot['subcategory'] or '—'}\n"
        f"{sale}\n"
        f"💵 Ціна: {price}\n"
        f"⏰ Завершення: {format_dt(lot['end_ts'])}\n"
        f"👤 Продавець: {lot['seller_name']}"
    )


def show_search_results(chat_id, rows):
    if not rows:
        send_message(
            chat_id,
            "🔎 За вашим запитом лотів не знайдено.",
            search_main_menu(),
        )
        return

    send_message(
        chat_id,
        f"🔎 Знайдено лотів: {len(rows)}\n"
        "Показую максимум 20 результатів.",
    )

    for lot in rows:
        url = lot_public_url(lot)
        markup = None
        if url:
            markup = {
                "inline_keyboard": [[
                    {"text": "➡️ Відкрити лот у групі", "url": url}
                ]]
            }

        photos = get_lot_photos(lot["id"])
        if photos:
            send_photo(
                chat_id,
                photos[0],
                caption=search_result_text(lot),
                reply_markup=markup,
            )
        else:
            send_message(chat_id, search_result_text(lot), markup)

    send_message(
        chat_id,
        "🔎 Виконати інший пошук?",
        search_main_menu(),
    )


# =========================================================
# СТВОРЕННЯ ЛОТА — КРОКИ
# =========================================================

def ask_title(chat_id, user_id, data):
    save_draft(user_id, chat_id, "title", data)
    send_message(chat_id, "🏷 Введіть назву лота:")


def ask_material(chat_id, user_id, data):
    save_draft(user_id, chat_id, "material", data)
    send_message(
        chat_id,
        "🧱 Вкажіть матеріал лоту:\n\n"
        "Наприклад:\n"
        "🥈 срібло\n"
        "🥇 золото\n"
        "🟤 мідь\n"
        "⚙️ нікель\n"
        "📄 папір\n\n"
        "✍️ Введіть матеріал одним повідомленням 👇",
    )


def ask_blitz_choice(chat_id, user_id, data):
    save_draft(user_id, chat_id, "blitz_choice", data)
    send_message(chat_id, "⚡ Додати бліц-ціну?", yes_no_menu("blitz"))


def ask_reserve_choice(chat_id, user_id, data):
    save_draft(user_id, chat_id, "reserve_choice", data)
    send_message(
        chat_id,
        "🔒 Додати приховану резервну ціну?\n\n"
        "У публічному лоті буде видно лише «🔒 Присутня резервна ціна».\n"
        "Сама сума учасникам не показується.",
        yes_no_menu("reserve"),
    )


def end_date_calendar(year=None, month=None):
    now = datetime.now(KYIV)
    year = year or now.year
    month = month or now.month

    rows = []
    rows.append([
        {"text": "Пн", "callback_data": "noop"},
        {"text": "Вт", "callback_data": "noop"},
        {"text": "Ср", "callback_data": "noop"},
        {"text": "Чт", "callback_data": "noop"},
        {"text": "Пт", "callback_data": "noop"},
        {"text": "Сб", "callback_data": "noop"},
        {"text": "Нд", "callback_data": "noop"},
    ])

    cal = calendar.Calendar(firstweekday=0)
    today = now.date()

    for week in cal.monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append({"text": " ", "callback_data": "noop"})
                continue

            date_obj = datetime(year, month, day, tzinfo=KYIV).date()
            if date_obj < today:
                row.append({"text": "·", "callback_data": "noop"})
            else:
                row.append({
                    "text": str(day),
                    "callback_data": f"enddate:{year:04d}-{month:02d}-{day:02d}"
                })
        rows.append(row)

    prev_year, prev_month = year, month - 1
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1

    next_year, next_month = year, month + 1
    if next_month == 13:
        next_month = 1
        next_year += 1

    nav = []
    if (year, month) > (today.year, today.month):
        nav.append({
            "text": "⬅️",
            "callback_data": f"endcal:{prev_year:04d}-{prev_month:02d}"
        })

    nav.append({
        "text": f"{UA_MONTHS[month]} {year}",
        "callback_data": "noop"
    })
    nav.append({
        "text": "➡️",
        "callback_data": f"endcal:{next_year:04d}-{next_month:02d}"
    })
    rows.append(nav)
    rows.append([{"text": "❌ Скасувати лот", "callback_data": "lot:cancel"}])

    return {"inline_keyboard": rows}


def end_hour_menu():
    rows = []
    for start in range(0, 24, 4):
        rows.append([
            {"text": f"{hour:02d}:__", "callback_data": f"endhour:{hour:02d}"}
            for hour in range(start, start + 4)
        ])
    rows.append([{"text": "⬅️ Назад до дати", "callback_data": "endtime:backdate"}])
    rows.append([{"text": "❌ Скасувати лот", "callback_data": "lot:cancel"}])
    return {"inline_keyboard": rows}


def end_minute_menu(hour):
    minutes = list(range(0, 60, 5))
    rows = []
    for start in range(0, len(minutes), 4):
        rows.append([
            {
                "text": f"{hour:02d}:{minute:02d}",
                "callback_data": f"endminute:{minute:02d}"
            }
            for minute in minutes[start:start + 4]
        ])
    rows.append([{"text": "⬅️ Назад до години", "callback_data": "endtime:backhour"}])
    rows.append([{"text": "❌ Скасувати лот", "callback_data": "lot:cancel"}])
    return {"inline_keyboard": rows}


def _date_picker_token(user_id, chat_id, ttl_seconds=1800):
    expires = int(time.time()) + ttl_seconds
    payload = f"{user_id}:{chat_id}:{expires}"
    secret = (BOT_TOKEN or "date-picker").encode("utf-8")
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _verify_date_picker_token(token):
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        user_id, chat_id, expires, sig = raw.split(":", 3)
        payload = f"{user_id}:{chat_id}:{expires}"
        secret = (BOT_TOKEN or "date-picker").encode("utf-8")
        expected = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if int(expires) < int(time.time()):
            return None
        return int(user_id), int(chat_id)
    except Exception:
        return None


def date_picker_menu(user_id, chat_id):
    token = _date_picker_token(user_id, chat_id)
    return {
        "inline_keyboard": [
            [{
                "text": "📅 Обрати дату та час",
                "web_app": {"url": f"{PUBLIC_BASE_URL}/date-picker?token={token}"},
            }],
            [{"text": "❌ Скасувати лот", "callback_data": "lot:cancel"}],
        ]
    }


def ask_end(chat_id, user_id, data):
    data.pop("end_date", None)
    data.pop("end_hour", None)
    save_draft(user_id, chat_id, "end_picker", data)
    send_message(
        chat_id,
        "⏰ Дата та час завершення\n\n"
        "Натисніть кнопку нижче та оберіть дату і точний час у календарі 👇",
        date_picker_menu(user_id, chat_id),
    )


def ask_phone(chat_id, user_id, data):
    save_draft(user_id, chat_id, "phone", data)
    send_message(chat_id, "📞 Введіть телефон для зв'язку:")


def ask_card(chat_id, user_id, data):
    save_draft(user_id, chat_id, "card_last4", data)
    send_message(
        chat_id,
        "💳 Введіть ТІЛЬКИ останні 4 цифри картки.\n\n"
        "Наприклад: 4444",
    )


def ask_extra(chat_id, user_id, data):
    save_draft(user_id, chat_id, "extra_info", data)
    send_message(
        chat_id,
        "ℹ️ Додаткова інформація про лот\n\n"
        "Вкажіть за бажанням додаткові відомості:\n"
        "🔹 стан;\n"
        "🔹 тираж;\n"
        "🔹 комплектність;\n"
        "🔹 особливості або іншу важливу інформацію.\n\n"
        "✍️ Введіть додаткову інформацію одним повідомленням 👇",
    )


def ask_photos(chat_id, user_id, data):
    data.setdefault("photos", [])
    data.pop("photo_status_message_id", None)
    save_draft(user_id, chat_id, "photos", data)
    send_message(
        chat_id,
        f"📸 Надішліть від 1 до {MAX_PHOTOS} фото.\n\n"
        "Можна надіслати фото по одному або альбомом.\n"
        "Після завантаження фото з'явиться одна кнопка «✅ Фото готові»."
    )


def show_preview(chat_id, user_id, data):
    save_draft(user_id, chat_id, "preview", data)
    send_message(chat_id, build_preview(data), preview_menu())


# =========================================================
# ЗБЕРЕЖЕННЯ ТА ПУБЛІКАЦІЯ ЛОТА
# =========================================================

def create_lot_from_draft(user_id, chat_id, user, data, status):
    seller_name = user_display_name(user)
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO lots(
                seller_id, seller_chat_id, seller_name, section, category,
                subcategory, category_key, subcategory_key,
                sale_type, title, material, fixed_price, start_price,
                current_price, bid_step, blitz_price, reserve_price, end_ts,
                anti_sniper, phone, card_last4, extra_info, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                user_id, chat_id, seller_name, data["section"], data["category"],
                data.get("subcategory"), data.get("category_key"), data.get("subcategory_key"),
                data["sale_type"], data["title"], data["material"],
                data.get("fixed_price"), data.get("start_price"),
                data.get("start_price"), data.get("bid_step"),
                data.get("blitz_price"), data.get("reserve_price"), data["end_ts"],
                data.get("anti_sniper", 0), data["phone"], data["card_last4"],
                data["extra_info"], status, time.time(),
            ),
        )
        lot_id = cur.fetchone()["id"]
        for pos, file_id in enumerate(data.get("photos", []), start=1):
            conn.execute(
                "INSERT INTO lot_photos(lot_id, file_id, position) VALUES (?, ?, ?)",
                (lot_id, file_id, pos),
            )
    return lot_id


def get_lot(lot_id):
    with db() as conn:
        return conn.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()


def get_lot_photos(lot_id):
    with db() as conn:
        rows = conn.execute(
            "SELECT file_id FROM lot_photos WHERE lot_id = ? ORDER BY position", (lot_id,)
        ).fetchall()
    return [r["file_id"] for r in rows]


def publish_lot(lot_id):
    lot = get_lot(lot_id)
    if not lot:
        return False, "Лот не знайдено"
    if lot["status"] not in ("ready", "pending_approval"):
        return False, "Лот уже опрацьовано"

    chat_id = get_publish_chat_id(lot["seller_chat_id"])
    thread_id = target_thread(lot["section"])
    photos = get_lot_photos(lot_id)

    with db() as conn:
        conn.execute(
            "UPDATE lots SET status='active', published_at=? WHERE id=?",
            (time.time(), lot_id),
        )
    lot = get_lot(lot_id)
    caption = build_public_lot(lot)
    markup = auction_buttons(lot) if lot["sale_type"] == "auction" else None

    result = None
    control_message_id = None

    if len(photos) == 1:
        # Один фотопост: фото + повний опис + кнопки аукціону в одному повідомленні.
        data = {
            "chat_id": chat_id,
            "photo": photos[0],
            "caption": caption,
        }
        if thread_id:
            data["message_thread_id"] = int(thread_id)
        if markup:
            data["reply_markup"] = markup
        result = telegram_api("sendPhoto", data)

    elif len(photos) > 1:
        # Telegram не дозволяє прикріпити inline-кнопки до media group.
        # Тому всі фото й опис ідуть ОДНИМ альбомом; для аукціону нижче
        # створюється лише окремий компактний блок керування ставками.
        result = send_media_group(
            chat_id,
            photos[:MAX_PHOTOS],
            caption=caption,
            message_thread_id=thread_id,
        )
        if result.get("ok") and markup:
            controls = send_message(
                chat_id,
                f"🟢 Аукціон триває\n🎯 Ставки — лот №{lot_id}",
                markup,
                thread_id,
            )
            if controls.get("ok"):
                control_message_id = controls.get("result", {}).get("message_id")

    else:
        result = send_message(chat_id, caption, markup, thread_id)

    if not result or not result.get("ok"):
        with db() as conn:
            conn.execute("UPDATE lots SET status='ready' WHERE id=?", (lot_id,))
        return False, (result or {}).get("description", "Помилка Telegram")

    raw_result = result.get("result", {})
    if isinstance(raw_result, list):
        main_message_id = raw_result[0].get("message_id") if raw_result else None
    else:
        main_message_id = raw_result.get("message_id")

    published_message_id = control_message_id or main_message_id

    with db() as conn:
        conn.execute(
            """
            UPDATE lots
            SET published_chat_id=?, published_message_id=?,
                published_content_message_id=?, published_thread_id=?
            WHERE id=?
            """,
            (chat_id, published_message_id, main_message_id, thread_id, lot_id),
        )

    # Сповіщення підписникам запускаємо у фоні, щоб публікація лота не гальмувала.
    threading.Thread(
        target=notify_lot_subscribers,
        args=(lot_id,),
        daemon=True,
    ).start()

    return True, None


def refresh_public_lot(lot_id):
    lot = get_lot(lot_id)
    if not lot or not lot["published_chat_id"]:
        return

    active = lot["status"] == "active" and lot["sale_type"] == "auction"
    markup = auction_buttons(lot) if active else {"inline_keyboard": []}
    photos = get_lot_photos(lot_id)
    chat_id = lot["published_chat_id"]
    control_id = lot["published_message_id"]
    content_id = lot["published_content_message_id"] or control_id

    if photos:
        if content_id:
            edit_caption(
                chat_id,
                content_id,
                build_public_lot(lot),
                markup if len(photos) == 1 else None,
            )
    else:
        if content_id:
            edit_message(
                chat_id,
                content_id,
                build_public_lot(lot),
                markup,
            )

    if len(photos) > 1 and lot["sale_type"] == "auction" and control_id:
        if active:
            control_text = (
                f"🟢 Аукціон триває\n"
                f"🎯 Ставки — лот №{lot_id}\n"
                f"💰 Поточна ціна: {lot['current_price']} грн\n"
                f"👑 Лідер: {(lot['leader_name'] + ' — ' + str(lot['current_price']) + ' грн') if lot['leader_name'] else 'ставок ще немає'}"
            )
        else:
            control_text = (
                f"🔴 Аукціон завершено\n"
                f"🎯 Лот №{lot_id}\n"
                f"💰 Фінальна ціна: {lot['current_price']} грн\n"
                f"👑 Переможець: {lot['winner_name'] or lot['leader_name'] or 'немає'}"
            )
        edit_message(chat_id, control_id, control_text, markup)


def submit_preview(chat_id, user_id, user, data):
    has_reserve = data["sale_type"] == "auction" and data.get("reserve_price")
    status = "pending_approval" if has_reserve else "ready"
    lot_id = create_lot_from_draft(user_id, chat_id, user, data, status)
    delete_draft(user_id)

    if has_reserve:
        if not ADMIN_TELEGRAM_ID:
            send_message(
                chat_id,
                f"🛡 Лот №{lot_id} збережено, але для погодження потрібно додати "
                "ADMIN_TELEGRAM_ID у Environment на Render.",
            )
            return
        lot = get_lot(lot_id)
        photos = get_lot_photos(lot_id)
        admin_id = int(ADMIN_TELEGRAM_ID)
        if photos:
            if len(photos) == 1:
                send_photo(admin_id, photos[0])
            else:
                send_media_group(admin_id, photos)
        send_message(admin_id, build_admin_preview(lot, len(photos)), admin_approval_menu(lot_id))
        send_message(
            chat_id,
            f"🛡 Лот №{lot_id} має резервну ціну і відправлений адміністратору на погодження.\n"
            "Після погодження він буде опублікований автоматично.",
        )
        show_main_menu(chat_id)
        return

    ok, error = publish_lot(lot_id)
    if ok:
        send_message(chat_id, f"✅ Лот №{lot_id} успішно опубліковано!")
    else:
        send_message(chat_id, f"❌ Не вдалося опублікувати лот №{lot_id}: {error}")
    show_main_menu(chat_id)


# =========================================================
# СТАВКИ / АВТОСТАВКИ / БЛІЦ
# =========================================================

def place_proxy_bid(lot_id, user, requested_max):
    user_id = user.get("id")
    name = user_display_name(user)
    now = time.time()

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        lot = conn.execute("SELECT * FROM lots WHERE id=? FOR UPDATE", (lot_id,)).fetchone()
        if not lot or lot["status"] != "active" or lot["sale_type"] != "auction":
            return False, "Аукціон уже завершено або недоступний.", None
        if lot["end_ts"] <= now:
            return False, "Час аукціону вже завершився.", None
        if lot["seller_id"] == user_id:
            return False, "Продавець не може ставити на власний лот.", None

        minimum = lot["current_price"] + lot["bid_step"]
        if requested_max < minimum:
            return False, f"Мінімальна ставка зараз: {minimum} грн.", None

        existing = conn.execute(
            "SELECT * FROM proxy_bids WHERE lot_id=? AND user_id=?", (lot_id, user_id)
        ).fetchone()
        if existing and requested_max <= existing["max_amount"]:
            return False, f"Ваша поточна автоставка вже {existing['max_amount']} грн.", None

        # Коли користувач підвищує максимум — момент нового максимуму стає його пріоритетом.
        conn.execute(
            """
            INSERT INTO proxy_bids(lot_id, user_id, user_name, max_amount, priority_ts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(lot_id, user_id) DO UPDATE SET
                user_name=excluded.user_name,
                max_amount=excluded.max_amount,
                priority_ts=excluded.priority_ts
            """,
            (lot_id, user_id, name, requested_max, now),
        )

        bidders = conn.execute(
            """
            SELECT * FROM proxy_bids
            WHERE lot_id=?
            ORDER BY max_amount DESC, priority_ts ASC
            """,
            (lot_id,),
        ).fetchall()

        winner = bidders[0]
        if len(bidders) == 1:
            calculated = min(winner["max_amount"], lot["start_price"] + lot["bid_step"])
        else:
            second = bidders[1]
            calculated = min(winner["max_amount"], second["max_amount"] + lot["bid_step"])

        new_price = max(lot["current_price"], calculated)
        old_leader_id = lot["leader_id"]
        old_leader_name = lot["leader_name"]
        extended = False
        new_end = lot["end_ts"]
        anti = lot["anti_sniper"] or 0
        if anti > 0 and (lot["end_ts"] - now) <= anti * 60:
            # Класичний багаторазовий антиснайпер:
            # кожна нова ставка в останні N хвилин знову дає повні N хвилин.
            new_end = now + anti * 60
            extended = True

        conn.execute(
            """
            UPDATE lots
            SET current_price=?, leader_id=?, leader_name=?, end_ts=?
            WHERE id=?
            """,
            (new_price, winner["user_id"], winner["user_name"], new_end, lot_id),
        )
        conn.execute(
            """
            INSERT INTO bid_history(
                lot_id, user_id, user_name, requested_max,
                resulting_price, resulting_leader_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (lot_id, user_id, name, requested_max, new_price, winner["user_id"], now),
        )

    refreshed = get_lot(lot_id)
    result = {
        "price": refreshed["current_price"],
        "leader_id": refreshed["leader_id"],
        "leader_name": refreshed["leader_name"],
        "old_leader_id": old_leader_id,
        "old_leader_name": old_leader_name,
        "extended": extended,
        "end_ts": refreshed["end_ts"],
    }
    return True, "Ставку прийнято.", result


def notify_seller_new_bid(lot_id, bidder):
    lot = get_lot(lot_id)
    if not lot:
        return

    send_message(
        lot["seller_chat_id"],
        "📈 НОВА СТАВКА НА ВАШ ЛОТ\n\n"
        f"🆔 Лот №{lot_id}: {lot['title']}\n"
        f"👤 Учасник: {user_display_name(bidder)}\n"
        f"💵 Поточна ціна: {lot['current_price']} грн\n"
        f"⏰ Завершення: {format_dt(lot['end_ts'])}",
    )

    if (
        lot.get("reserve_price")
        and lot["current_price"] >= lot["reserve_price"]
        and not lot.get("reserve_reached_notified")
    ):
        with db() as conn:
            cur = conn.execute(
                """
                UPDATE lots
                SET reserve_reached_notified=TRUE
                WHERE id=? AND reserve_reached_notified=FALSE
                """,
                (lot_id,),
            )
        if cur.rowcount == 1:
            send_message(
                lot["seller_chat_id"],
                f"🔓 РЕЗЕРВ ДОСЯГНУТО\n\n"
                f"🆔 Лот №{lot_id}: {lot['title']}\n"
                f"💵 Поточна ціна: {lot['current_price']} грн\n"
                "✅ Якщо торги завершаться зараз, резерв виконано.",
            )


def notify_outbid(lot_id, result):
    old_id = result.get("old_leader_id")
    new_id = result.get("leader_id")
    if old_id and new_id and old_id != new_id:
        lot = get_lot(lot_id)
        send_message(
            old_id,
            f"🔔 Вашу ставку перебито!\n\n"
            f"🆔 Лот №{lot_id}: {lot['title']}\n"
            f"💵 Поточна ціна: {lot['current_price']} грн\n\n"
            "Можете повернутися до лота та зробити нову ставку.",
        )


def handle_step_bid(callback_id, lot_id, user):
    lot = get_lot(lot_id)
    if not lot:
        answer_callback(callback_id, "Лот не знайдено.", True)
        return
    requested = lot["current_price"] + lot["bid_step"]
    ok, msg, result = place_proxy_bid(lot_id, user, requested)
    if not ok:
        answer_callback(callback_id, msg, True)
        return
    refresh_public_lot(lot_id)
    notify_outbid(lot_id, result)
    notify_seller_new_bid(lot_id, user)
    extra = "\n⏱ Час продовжено антиснайпером." if result["extended"] else ""
    if result["leader_id"] == user.get("id"):
        answer_callback(callback_id, f"✅ Ви лідер. Ціна: {result['price']} грн.{extra}", True)
    else:
        answer_callback(callback_id, f"🤖 Вас автоматично перебила автоставка. Ціна: {result['price']} грн.{extra}", True)


def handle_blitz(callback_id, lot_id, user):
    user_id = user.get("id")
    name = user_display_name(user)
    now = time.time()

    current = get_lot(lot_id)
    if current and current["status"] == "active" and current["end_ts"] <= now:
        finish_due_lots()
        answer_callback(callback_id, "🔴 Аукціон уже завершено.", True)
        return

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        lot = conn.execute("SELECT * FROM lots WHERE id=? FOR UPDATE", (lot_id,)).fetchone()
        if not lot or lot["status"] != "active" or not lot["blitz_price"]:
            answer_callback(callback_id, "Бліц уже недоступний.", True)
            return
        if lot["seller_id"] == user_id:
            answer_callback(callback_id, "Продавець не може купити власний лот.", True)
            return
        conn.execute(
            """
            UPDATE lots
            SET status='blitz_sold', current_price=?, leader_id=?, leader_name=?,
                winner_id=?, winner_name=?, finished_at=?
            WHERE id=? AND status='active'
            """,
            (lot["blitz_price"], user_id, name, user_id, name, now, lot_id),
        )
    refresh_public_lot(lot_id)
    lot = get_lot(lot_id)
    answer_callback(callback_id, f"⚡ Бліц! Лот ваш за {lot['blitz_price']} грн.", True)
    send_message(
        lot["seller_chat_id"],
        f"🏁 ТОРГИ ЗАВЕРШЕНО — БЛІЦ\n\n"
        f"🆔 Лот №{lot_id}\n"
        f"🏷 {lot['title']}\n"
        f"✅ Лот продано за {lot['blitz_price']} грн.\n"
        f"🏆 Переможець: {name}\n\n"
        "Зв'яжіться з переможцем для завершення угоди.",
    )
    send_message(
        user_id,
        f"🏆 ВІТАЄМО! ВИ ПЕРЕМОЖЕЦЬ\n\n"
        f"🆔 Лот №{lot_id}\n"
        f"🏷 {lot['title']}\n"
        f"💰 Ціна: {lot['blitz_price']} грн.\n\n"
        f"👤 Продавець: {lot['seller_name']}\n"
        f"📞 Телефон продавця: {lot['phone']}\n"
        f"💳 Оплата: картка **** {lot['card_last4']}",
    )


# =========================================================
# ЗАВЕРШЕННЯ ЛОТІВ
# =========================================================

def finish_due_lots():
    now = time.time()
    with db() as conn:
        due = conn.execute(
            "SELECT id FROM lots WHERE status='active' AND end_ts<=?", (now,)
        ).fetchall()

    for row in due:
        lot_id = row["id"]
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            lot = conn.execute("SELECT * FROM lots WHERE id=? FOR UPDATE", (lot_id,)).fetchone()
            if not lot or lot["status"] != "active" or lot["end_ts"] > time.time():
                continue

            if lot["sale_type"] == "fixed":
                new_status = "finished"
                winner_id = None
                winner_name = None
            elif not lot["leader_id"]:
                new_status = "finished"
                winner_id = None
                winner_name = None
            elif lot["reserve_price"] and lot["current_price"] < lot["reserve_price"]:
                new_status = "reserve_not_met"
                winner_id = None
                winner_name = None
            else:
                new_status = "sold"
                winner_id = lot["leader_id"]
                winner_name = lot["leader_name"]

            cur = conn.execute(
                """
                UPDATE lots
                SET status=?, winner_id=?, winner_name=?, finished_at=?
                WHERE id=? AND status='active'
                """,
                (new_status, winner_id, winner_name, time.time(), lot_id),
            )
            if cur.rowcount != 1:
                continue

        refresh_public_lot(lot_id)
        lot = get_lot(lot_id)
        if lot["status"] == "sold":
            send_message(
                lot["seller_chat_id"],
                f"🏁 ТОРГИ ЗАВЕРШЕНО\n\n"
                f"🆔 Лот №{lot_id}\n"
                f"🏷 {lot['title']}\n"
                f"✅ Лот продано за {lot['current_price']} грн.\n"
                f"🏆 Переможець: {lot['winner_name']}\n\n"
                "Зв'яжіться з переможцем для завершення угоди.",
            )
            if lot["winner_id"]:
                send_message(
                    lot["winner_id"],
                    f"🏆 ВІТАЄМО! ВИ ПЕРЕМОЖЕЦЬ АУКЦІОНУ\n\n"
                    f"🆔 Лот №{lot_id}\n"
                    f"🏷 {lot['title']}\n"
                    f"💰 Фінальна ціна: {lot['current_price']} грн.\n\n"
                    f"👤 Продавець: {lot['seller_name']}\n"
                    f"📞 Телефон продавця: {lot['phone']}\n"
                    f"💳 Оплата: картка **** {lot['card_last4']}",
                )
        elif lot["status"] == "reserve_not_met":
            send_message(
                lot["seller_chat_id"],
                f"🏁 Аукціон №{lot_id} завершено.\n"
                f"🔒 Резервну ціну не досягнуто.\n"
                f"💵 Максимальна ставка: {lot['current_price']} грн.\n"
                "Лот не продано.",
            )
            if lot["leader_id"]:
                send_message(
                    lot["leader_id"],
                    f"🏁 Аукціон №{lot_id} завершено.\n"
                    "🔒 Резервну ціну не досягнуто, тому лот не продано.",
                )
        else:
            send_message(
                lot["seller_chat_id"],
                f"🏁 ТОРГИ ЗАВЕРШЕНО\n\n"
                f"🆔 Лот №{lot_id}\n"
                f"🏷 {lot['title']}\n"
                "Ставок-переможців немає. Лот завершено без продажу.",
            )


def send_due_reminders():
    now = time.time()
    lower = now + 25 * 60
    upper = now + 35 * 60

    with db() as conn:
        lots_rows = conn.execute(
            """
            SELECT *
            FROM lots
            WHERE status='active'
              AND end_ts BETWEEN ? AND ?
            ORDER BY end_ts
            """,
            (lower, upper),
        ).fetchall()

    for lot in lots_rows:
        recipients = set()

        if lot.get("leader_id"):
            recipients.add(int(lot["leader_id"]))

        with db() as conn:
            favs = conn.execute(
                "SELECT user_id FROM lot_favorites WHERE lot_id=?",
                (lot["id"],),
            ).fetchall()
        recipients.update(int(r["user_id"]) for r in favs)

        for user_id in recipients:
            if mark_notification_once(user_id, lot["id"], "30min"):
                send_message(
                    user_id,
                    "⏰ ДО ЗАВЕРШЕННЯ ЛОТА БЛИЗЬКО 30 ХВИЛИН\n\n"
                    f"🆔 Лот №{lot['id']}: {lot['title']}\n"
                    f"💵 Поточна ціна: {lot['current_price'] or lot['fixed_price']} грн\n"
                    f"🏁 Завершення: {format_dt(lot['end_ts'])}",
                )


def background_finisher():
    while True:
        try:
            finish_due_lots()
            send_due_reminders()
        except Exception as e:
            print("FINISHER ERROR:", repr(e))
        time.sleep(20)


def ensure_background_started():
    global _background_started
    with _background_lock:
        if _background_started:
            return
        _background_started = True
        threading.Thread(target=background_finisher, daemon=True).start()


# =========================================================
# CALLBACK КНОПКИ
# =========================================================

def handle_callback(callback):
    callback_id = callback.get("id")
    data = callback.get("data", "")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    user = callback.get("from", {})
    user_id = user.get("id")
    track_user_activity(user, f"callback:{data}")

    if data == "menu_my_lots":
        answer_callback(callback_id)
        show_my_lots(chat_id, user_id, "all")
        return

    if data.startswith("mylots:"):
        answer_callback(callback_id)
        group = data.split(":", 1)[1]
        show_my_lots(chat_id, user_id, group)
        return

    if data.startswith("mylotbids:"):
        lot_id = int(data.split(":", 1)[1])
        lot = get_lot(lot_id)
        if not lot or int(lot["seller_id"]) != int(user_id):
            answer_callback(callback_id, "Це не ваш лот.", True)
            return
        answer_callback(callback_id)
        send_message(chat_id, lot_bid_summary(lot_id))
        return

    if data.startswith("mylotfinish:"):
        lot_id = int(data.split(":", 1)[1])
        ok, msg = seller_finish_own_lot(user_id, lot_id)
        answer_callback(callback_id, msg, not ok)
        if ok:
            send_message(chat_id, f"🏁 Лот №{lot_id} завершено.")
        return

    if data.startswith("mylotcopy:"):
        lot_id = int(data.split(":", 1)[1])
        answer_callback(callback_id)
        if not copy_lot_to_draft(user_id, chat_id, lot_id):
            send_message(chat_id, "❌ Не вдалося скопіювати лот.")
        else:
            send_message(
                chat_id,
                f"🔁 Дані лота №{lot_id} скопійовано. "
                "Оберіть нову дату та час завершення.",
            )
        return

    if data == "menu_favorites":
        answer_callback(callback_id)
        show_favorites(chat_id, user_id)
        return

    if data.startswith("favlot:"):
        lot_id = int(data.split(":", 1)[1])
        enabled = toggle_favorite(user_id, lot_id)
        answer_callback(
            callback_id,
            "❤️ Додано в обране" if enabled else "💔 Видалено з обраного",
            True,
        )
        return

    if data == "menu_profile":
        answer_callback(callback_id)
        send_message(chat_id, reputation_text(user_id))
        return

    if data.startswith("reputation:"):
        target_id = int(data.split(":", 1)[1])
        answer_callback(callback_id)
        send_message(chat_id, reputation_text(target_id))
        return

    if data.startswith("guarantee:"):
        lot_id = int(data.split(":", 1)[1])
        request_id, msg = create_guarantee_request(lot_id, user)
        if not request_id:
            answer_callback(callback_id, msg, True)
            return
        answer_callback(callback_id, msg, True)
        notify_admin_guarantee(request_id)
        send_message(
            chat_id,
            f"🛡 Заявка Гарант №{request_id} прийнята.\\n"
            "Адміністратор отримає її на розгляд.",
        )
        return

    if data.startswith("garok:") or data.startswith("garno:"):
        if not ADMIN_TELEGRAM_ID or str(user_id) != str(ADMIN_TELEGRAM_ID):
            answer_callback(callback_id, "Немає доступу.", True)
            return
        request_id = int(data.split(":", 1)[1])
        new_status = "accepted" if data.startswith("garok:") else "rejected"
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM guarantee_requests WHERE id=?",
                (request_id,),
            ).fetchone()
            if not row or row["status"] != "pending":
                answer_callback(callback_id, "Заявку вже опрацьовано.", True)
                return
            conn.execute(
                """
                UPDATE guarantee_requests
                SET status=?, updated_at=?
                WHERE id=?
                """,
                (new_status, time.time(), request_id),
            )
        answer_callback(
            callback_id,
            "✅ Заявку прийнято" if new_status == "accepted" else "❌ Заявку відхилено",
        )
        buyer_text = (
            f"✅ Заявку Гарант №{request_id} прийнято адміністратором."
            if new_status == "accepted"
            else f"❌ Заявку Гарант №{request_id} відхилено адміністратором."
        )
        send_message(row["buyer_id"], buyer_text)
        send_message(
            row["seller_id"],
            f"🛡 По вашому лоту №{row['lot_id']} "
            + (
                f"прийнято заявку Гарант №{request_id}."
                if new_status == "accepted"
                else f"заявку Гарант №{request_id} відхилено."
            ),
        )
        return

    if data == "menu_admin_panel":
        if not ADMIN_TELEGRAM_ID or str(user_id) != str(ADMIN_TELEGRAM_ID):
            answer_callback(callback_id, "Доступ лише адміністратору.", True)
            return
        answer_callback(callback_id)
        send_message(chat_id, admin_panel_text(), admin_panel_menu())
        return

    if data == "admin:group_stats":
        if not ADMIN_TELEGRAM_ID or str(user_id) != str(ADMIN_TELEGRAM_ID):
            answer_callback(callback_id, "Немає доступу.", True)
            return
        answer_callback(callback_id)
        send_message(
            chat_id,
            group_statistics_text(),
            admin_group_stats_menu(),
        )
        return

    if data == "admin:bot_stats":
        if not ADMIN_TELEGRAM_ID or str(user_id) != str(ADMIN_TELEGRAM_ID):
            answer_callback(callback_id, "Немає доступу.", True)
            return
        answer_callback(callback_id)
        send_message(
            chat_id,
            bot_statistics_text(),
            admin_bot_stats_menu(),
        )
        return

    if data == "admin:activity":
        if not ADMIN_TELEGRAM_ID or str(user_id) != str(ADMIN_TELEGRAM_ID):
            answer_callback(callback_id, "Немає доступу.", True)
            return
        answer_callback(callback_id)
        send_message(chat_id, recent_activity_text(), admin_bot_stats_menu())
        return

    if data == "admin:guarantees":
        if not ADMIN_TELEGRAM_ID or str(user_id) != str(ADMIN_TELEGRAM_ID):
            answer_callback(callback_id, "Немає доступу.", True)
            return
        answer_callback(callback_id)
        send_message(chat_id, pending_guarantees_text(), admin_bot_stats_menu())
        return

    if data == "search:price":
        answer_callback(callback_id)
        save_draft(user_id, chat_id, "search_price", {})
        send_message(
            chat_id,
            "💰 ПОШУК ЗА ЦІНОЮ\\n\\n"
            "Введіть діапазон, наприклад:\\n"
            "• 100-500\\n"
            "• до 1000\\n"
            "• від 500",
        )
        return

    if data == "menu_search_lot":
        answer_callback(callback_id)
        command_search_lot(chat_id, user_id)
        return

    if data == "search:home":
        answer_callback(callback_id)
        delete_draft(user_id)
        show_main_menu(chat_id)
        return

    if data == "search:back":
        answer_callback(callback_id)
        command_search_lot(chat_id, user_id)
        return

    if data == "search:category":
        answer_callback(callback_id)
        send_message(
            chat_id,
            "🗂 ПОШУК ЗА РОЗДІЛОМ / ПІДРОЗДІЛОМ\n\n"
            "Оберіть розділ 👇",
            search_section_menu(),
        )
        return

    if data == "search:active_auctions":
        answer_callback(callback_id)
        show_search_results(chat_id, search_active_auctions())
        return

    if data == "search:one_uah":
        answer_callback(callback_id)
        show_search_results(chat_id, search_one_uah_auctions())
        return

    if data == "search:keyword":
        answer_callback(callback_id)
        save_draft(user_id, chat_id, "search_keyword", {})
        send_message(
            chat_id,
            "🔎 ПОШУК ЗА НАЗВОЮ / КЛЮЧОВИМИ СЛОВАМИ\n\n"
            "Введіть назву або одне чи кілька ключових слів.\n\n"
            "Наприклад:\n"
            "• микола2\n"
            "• олександр3\n"
            "• білон\n"
            "• дореформа",
        )
        return

    if data == "searchsec:num":
        answer_callback(callback_id)
        send_message(
            chat_id,
            "🪙 НУМІЗМАТИКА\n\nОберіть категорію 👇",
            search_top_category_menu("numizmatika"),
        )
        return

    if data == "searchsec:bon":
        answer_callback(callback_id)
        send_message(
            chat_id,
            "💵 БОНІСТИКА\n\nОберіть категорію 👇",
            search_top_category_menu("bonistika"),
        )
        return

    if data.startswith("searchcat:"):
        key = data.split(":", 1)[1]
        category = TOP_SECTION_MAP.get(key)
        if not category:
            answer_callback(callback_id, "Розділ не знайдено.", True)
            return
        answer_callback(callback_id)
        send_message(
            chat_id,
            f"🗂 {category}\n\nОберіть підрозділ 👇",
            search_subsection_menu(key),
        )
        return

    if data.startswith("searchall:"):
        key = data.split(":", 1)[1]
        category = TOP_SECTION_MAP.get(key)
        if not category:
            answer_callback(callback_id, "Розділ не знайдено.", True)
            return
        answer_callback(callback_id)
        show_search_results(chat_id, search_lots_by_category(category))
        return

    if data.startswith("searchsub:"):
        sub_key = data.split(":", 1)[1]
        subcategory = SUBSECTION_MAP.get(sub_key)
        if not subcategory:
            answer_callback(callback_id, "Підрозділ не знайдено.", True)
            return
        answer_callback(callback_id)
        show_search_results(chat_id, search_lots_by_subcategory(subcategory))
        return

    if data == "menu_create_lot":
        answer_callback(callback_id)
        command_lot(chat_id, user_id)
        return

    if data == "lot:cancel":
        answer_callback(callback_id)
        cancel_lot(chat_id, user_id)
        return

    if data == "sec:num":
        answer_callback(callback_id)
        draft = get_draft(user_id) or {"data": {}}
        d = draft["data"]
        d["section"] = "numizmatika"
        save_draft(user_id, chat_id, "choose_category", d)
        send_message(chat_id, "🪙 НУМІЗМАТИКА\n\nОберіть розділ монет 👇", top_category_menu("numizmatika"))
        return

    if data == "sec:bon":
        answer_callback(callback_id)
        draft = get_draft(user_id) or {"data": {}}
        d = draft["data"]
        d["section"] = "bonistika"
        save_draft(user_id, chat_id, "choose_category", d)
        send_message(chat_id, "💵 БОНІСТИКА\n\nОберіть розділ банкнот 👇", top_category_menu("bonistika"))
        return

    if data == "cat:back":
        answer_callback(callback_id)
        save_draft(user_id, chat_id, "choose_section", {})
        send_message(chat_id, "⚖️ СТВОРЕННЯ ЛОТА\n\nОберіть розділ 👇", lot_section_menu())
        return

    if data.startswith("cat:"):
        key = data.split(":", 1)[1]
        label = TOP_SECTION_MAP.get(key)
        if not label:
            answer_callback(callback_id, "Розділ не знайдено.", True)
            return
        draft = get_draft(user_id)
        if not draft:
            answer_callback(callback_id, "Почніть створення лота заново.", True)
            return
        d = draft["data"]
        d["category_key"] = key
        d["category"] = label
        d.pop("subcategory_key", None)
        d.pop("subcategory", None)
        save_draft(user_id, chat_id, "choose_subcategory", d)
        answer_callback(callback_id)
        send_message(
            chat_id,
            f"✅ Розділ:\n{label}\n\nОберіть підрозділ 👇",
            subsection_menu(key),
        )
        return

    if data == "sub:back":
        draft = get_draft(user_id)
        answer_callback(callback_id)
        if not draft or not draft["data"].get("section"):
            command_lot(chat_id, user_id)
            return
        d = draft["data"]
        d.pop("category_key", None)
        d.pop("category", None)
        d.pop("subcategory_key", None)
        d.pop("subcategory", None)
        save_draft(user_id, chat_id, "choose_category", d)
        send_message(chat_id, "🗂 Оберіть розділ 👇", top_category_menu(d["section"]))
        return

    if data.startswith("sub:"):
        sub_key = data.split(":", 1)[1]
        sub_label = SUBSECTION_MAP.get(sub_key)
        draft = get_draft(user_id)
        if not sub_label or not draft:
            answer_callback(callback_id, "Підрозділ не знайдено.", True)
            return
        d = draft["data"]
        parent_key = d.get("category_key")
        if not parent_key or sub_key not in dict(SUBSECTIONS.get(parent_key, [])):
            answer_callback(callback_id, "Підрозділ не належить вибраному розділу.", True)
            return
        d["subcategory_key"] = sub_key
        d["subcategory"] = sub_label
        save_draft(user_id, chat_id, "choose_type", d)
        answer_callback(callback_id)
        send_message(
            chat_id,
            f"✅ Розділ:\n{d['category']}\n\n"
            f"✅ Підрозділ:\n{sub_label}\n\n"
            "Оберіть тип продажу 👇",
            sale_type_menu(),
        )
        return

    if data == "type:back":
        draft = get_draft(user_id)
        answer_callback(callback_id)
        if not draft or not draft["data"].get("category_key"):
            command_lot(chat_id, user_id)
            return
        d = draft["data"]
        d.pop("subcategory_key", None)
        d.pop("subcategory", None)
        save_draft(user_id, chat_id, "choose_subcategory", d)
        send_message(
            chat_id,
            f"🗂 Розділ:\n{d['category']}\n\nОберіть підрозділ 👇",
            subsection_menu(d["category_key"]),
        )
        return

    if data in ("type:auction", "type:fixed"):
        draft = get_draft(user_id)
        if not draft:
            answer_callback(callback_id, "Почніть створення лота заново.", True)
            return
        d = draft["data"]
        d["sale_type"] = "auction" if data == "type:auction" else "fixed"
        answer_callback(callback_id)
        ask_title(chat_id, user_id, d)
        return

    if data.startswith("blitz:"):
        draft = get_draft(user_id)
        if not draft:
            answer_callback(callback_id, "Сесію створення втрачено.", True)
            return
        d = draft["data"]
        choice = data.split(":", 1)[1]
        answer_callback(callback_id)
        if choice == "yes":
            save_draft(user_id, chat_id, "blitz_price", d)
            send_message(chat_id, "⚡ Введіть бліц-ціну в гривнях:")
        else:
            d["blitz_price"] = None
            ask_reserve_choice(chat_id, user_id, d)
        return

    if data.startswith("reserve:"):
        draft = get_draft(user_id)
        if not draft:
            answer_callback(callback_id, "Сесію створення втрачено.", True)
            return
        d = draft["data"]
        choice = data.split(":", 1)[1]
        answer_callback(callback_id)
        if choice == "yes":
            save_draft(user_id, chat_id, "reserve_price", d)
            send_message(chat_id, "🔒 Введіть приховану резервну ціну в гривнях:")
        else:
            d["reserve_price"] = None
            ask_end(chat_id, user_id, d)
        return

    if data == "noop":
        answer_callback(callback_id)
        return

    if data.startswith("endcal:"):
        draft = get_draft(user_id)
        if not draft:
            answer_callback(callback_id, "Сесію створення втрачено.", True)
            return
        try:
            ym = data.split(":", 1)[1]
            year, month = [int(x) for x in ym.split("-")]
        except Exception:
            answer_callback(callback_id, "Невірна дата.", True)
            return
        answer_callback(callback_id)
        send_message(
            chat_id,
            "⏰ Оберіть дату завершення лота 👇",
            end_date_calendar(year, month),
        )
        return

    if data.startswith("enddate:"):
        draft = get_draft(user_id)
        if not draft:
            answer_callback(callback_id, "Сесію створення втрачено.", True)
            return
        date_text = data.split(":", 1)[1]
        try:
            chosen_date = datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError:
            answer_callback(callback_id, "Невірна дата.", True)
            return

        if chosen_date < datetime.now(KYIV).date():
            answer_callback(callback_id, "Ця дата вже минула.", True)
            return

        d = draft["data"]
        d["end_date"] = date_text
        save_draft(user_id, chat_id, "end_hour", d)
        answer_callback(callback_id)
        send_message(
            chat_id,
            f"📅 Дата: {chosen_date.strftime('%d.%m.%Y')}\n\n"
            "Тепер оберіть годину завершення 👇",
            end_hour_menu(),
        )
        return

    if data == "endtime:backdate":
        draft = get_draft(user_id)
        if not draft:
            answer_callback(callback_id, "Сесію створення втрачено.", True)
            return
        answer_callback(callback_id)
        ask_end(chat_id, user_id, draft["data"])
        return

    if data.startswith("endhour:"):
        draft = get_draft(user_id)
        if not draft:
            answer_callback(callback_id, "Сесію створення втрачено.", True)
            return
        try:
            hour = int(data.split(":", 1)[1])
        except ValueError:
            answer_callback(callback_id, "Невірна година.", True)
            return
        if hour < 0 or hour > 23:
            answer_callback(callback_id, "Невірна година.", True)
            return
        d = draft["data"]
        d["end_hour"] = hour
        save_draft(user_id, chat_id, "end_minute", d)
        answer_callback(callback_id)
        send_message(
            chat_id,
            f"🕐 Година: {hour:02d}\n\n"
            "Оберіть точний час завершення 👇",
            end_minute_menu(hour),
        )
        return

    if data == "endtime:backhour":
        draft = get_draft(user_id)
        if not draft:
            answer_callback(callback_id, "Сесію створення втрачено.", True)
            return
        d = draft["data"]
        answer_callback(callback_id)
        send_message(chat_id, "🕐 Оберіть годину завершення 👇", end_hour_menu())
        return

    if data.startswith("endminute:"):
        draft = get_draft(user_id)
        if not draft:
            answer_callback(callback_id, "Сесію створення втрачено.", True)
            return
        d = draft["data"]
        if not d.get("end_date") or "end_hour" not in d:
            answer_callback(callback_id, "Спочатку оберіть дату та годину.", True)
            ask_end(chat_id, user_id, d)
            return

        try:
            minute = int(data.split(":", 1)[1])
            year, month, day = [int(x) for x in d["end_date"].split("-")]
            dt = datetime(
                year, month, day,
                int(d["end_hour"]), minute,
                tzinfo=KYIV
            )
        except Exception:
            answer_callback(callback_id, "Не вдалося сформувати дату.", True)
            ask_end(chat_id, user_id, d)
            return

        if dt.timestamp() <= time.time():
            answer_callback(callback_id, "Цей час уже минув. Оберіть майбутній.", True)
            ask_end(chat_id, user_id, d)
            return

        d["end_ts"] = dt.timestamp()
        d.pop("end_date", None)
        d.pop("end_hour", None)
        answer_callback(callback_id)

        if d["sale_type"] == "auction":
            save_draft(user_id, chat_id, "anti_sniper", d)
            send_message(
                chat_id,
                f"✅ Завершення: {format_dt(d['end_ts'])}\n\n"
                "⏱ Оберіть антиснайпер:",
                anti_sniper_menu(),
            )
        else:
            d["anti_sniper"] = 0
            send_message(chat_id, f"✅ Завершення: {format_dt(d['end_ts'])}")
            ask_phone(chat_id, user_id, d)
        return

    if data.startswith("anti:"):
        draft = get_draft(user_id)
        if not draft:
            answer_callback(callback_id, "Сесію створення втрачено.", True)
            return
        minutes = int(data.split(":", 1)[1])
        d = draft["data"]
        d["anti_sniper"] = minutes
        answer_callback(callback_id)
        ask_phone(chat_id, user_id, d)
        return

    if data == "photos:done":
        draft = get_draft(user_id)
        if not draft:
            answer_callback(callback_id, "Сесію створення втрачено.", True)
            return
        d = draft["data"]
        if not d.get("photos"):
            answer_callback(callback_id, "Потрібно додати хоча б 1 фото.", True)
            return
        answer_callback(callback_id)
        show_preview(chat_id, user_id, d)
        return

    if data == "preview:restart":
        answer_callback(callback_id)
        command_lot(chat_id, user_id)
        return

    if data == "preview:publish":
        draft = get_draft(user_id)
        if not draft or draft["state"] != "preview":
            answer_callback(callback_id, "Дані лота не знайдено.", True)
            return
        answer_callback(callback_id, "Опрацьовую...")
        submit_preview(chat_id, user_id, user, draft["data"])
        return

    if data.startswith("admok:") or data.startswith("admno:"):
        if not ADMIN_TELEGRAM_ID or str(user_id) != str(ADMIN_TELEGRAM_ID):
            answer_callback(callback_id, "Немає доступу.", True)
            return
        action, lot_id_text = data.split(":", 1)
        lot_id = int(lot_id_text)
        lot = get_lot(lot_id)
        if not lot or lot["status"] != "pending_approval":
            answer_callback(callback_id, "Лот уже опрацьовано.", True)
            return
        if action == "admno":
            with db() as conn:
                conn.execute("UPDATE lots SET status='rejected' WHERE id=?", (lot_id,))
            answer_callback(callback_id, "Лот відхилено.")
            send_message(lot["seller_chat_id"], f"❌ Лот №{lot_id} з резервом не погоджено адміністратором.")
            return
        answer_callback(callback_id, "Погоджено. Публікую...")
        ok, error = publish_lot(lot_id)
        if ok:
            send_message(lot["seller_chat_id"], f"✅ Лот №{lot_id} погоджено та опубліковано!")
        else:
            send_message(lot["seller_chat_id"], f"❌ Помилка публікації лота №{lot_id}: {error}")
        return

    if data.startswith("bid:"):
        lot_id = int(data.split(":", 1)[1])
        handle_step_bid(callback_id, lot_id, user)
        return

    if data.startswith("blitz:") and data.split(":", 1)[1].isdigit():
        # Цей блок фактично не використовується через конфлікт з blitz:yes/no вище,
        # тому бліц лота обробляємо окремим префіксом нижче у нових кнопках.
        pass

    if data.startswith("blitzlot:"):
        lot_id = int(data.split(":", 1)[1])
        handle_blitz(callback_id, lot_id, user)
        return

    if data == "menu_subscriptions":
        answer_callback(callback_id)
        delete_draft(user_id)
        send_message(
            chat_id,
            "🔔 ПІДПИСКИ НА НОВІ ЛОТИ\n\n"
            "Оберіть Нумізматику або Боністику.\n"
            "Далі можна підписатися на весь розділ або на конкретні підрозділи 👇",
            subscriptions_main_menu(),
        )
        return

    if data == "subscr:home":
        answer_callback(callback_id)
        show_main_menu(chat_id)
        return

    if data == "subscr:back":
        answer_callback(callback_id)
        send_message(
            chat_id,
            "🔔 ПІДПИСКИ НА НОВІ ЛОТИ\n\nОберіть напрям 👇",
            subscriptions_main_menu(),
        )
        return

    if data == "subscr:list":
        answer_callback(callback_id)
        send_message(
            chat_id,
            my_subscriptions_text(user_id),
            subscriptions_main_menu(),
        )
        return

    if data == "subscr:clear":
        answer_callback(callback_id)
        with db() as conn:
            conn.execute("DELETE FROM lot_subscriptions WHERE user_id=?", (user_id,))
        send_message(
            chat_id,
            "🧹 Усі ваші підписки на нові лоти видалено.",
            subscriptions_main_menu(),
        )
        return

    if data.startswith("subscrsec:"):
        sec = data.split(":", 1)[1]
        section = "numizmatika" if sec == "num" else "bonistika"
        answer_callback(callback_id)
        title = "🪙 НУМІЗМАТИКА" if section == "numizmatika" else "💵 БОНІСТИКА"
        send_message(
            chat_id,
            f"🔔 {title}\n\n"
            "✅ — підписка вже активна.\n"
            "Натисніть розділ, щоб обрати весь розділ або конкретний підрозділ 👇",
            subscriptions_section_menu(user_id, section),
        )
        return

    if data.startswith("subscrall:"):
        section = data.split(":", 1)[1]
        enabled = toggle_subscription(user_id, section, None, None)
        answer_callback(
            callback_id,
            "✅ Підписку увімкнено" if enabled else "❌ Підписку вимкнено",
        )
        sec = "num" if section == "numizmatika" else "bon"
        send_message(
            chat_id,
            "🔔 Підписки оновлено.",
            subscriptions_section_menu(user_id, section),
        )
        return

    if data.startswith("subscrcat:"):
        _, section, category_key = data.split(":", 2)
        answer_callback(callback_id)
        menu = subscriptions_subsection_menu(user_id, section, category_key)
        send_message(chat_id, menu["text"], menu["reply_markup"])
        return

    if data.startswith("subscrwholecat:"):
        _, section, category_key = data.split(":", 2)
        enabled = toggle_subscription(user_id, section, category_key, None)
        answer_callback(
            callback_id,
            "✅ Підписку увімкнено" if enabled else "❌ Підписку вимкнено",
        )
        menu = subscriptions_subsection_menu(user_id, section, category_key)
        send_message(chat_id, menu["text"], menu["reply_markup"])
        return

    if data.startswith("subscrsub:"):
        _, section, category_key, subcategory_key = data.split(":", 3)
        enabled = toggle_subscription(
            user_id,
            section,
            category_key,
            subcategory_key,
        )
        answer_callback(
            callback_id,
            "✅ Підписку увімкнено" if enabled else "❌ Підписку вимкнено",
        )
        menu = subscriptions_subsection_menu(user_id, section, category_key)
        send_message(chat_id, menu["text"], menu["reply_markup"])
        return
    if data == "menu_catalogs":
        answer_callback(callback_id)
        send_message(
            chat_id,
            "📖 КАТАЛОГИ\n\nОберіть потрібний каталог 👇",
            catalogs_menu(),
        )
        return
    if data == "menu_links":
        answer_callback(callback_id)
        send_message(
            chat_id,
            "🧠 КОРИСНІ ПОСИЛАННЯ\n\nОберіть потрібний ресурс 👇",
            useful_links_menu(),
        )
        return
    if data == "info:home":
        answer_callback(callback_id)
        show_main_menu(chat_id)
        return
    if data == "menu_scammers":
        answer_callback(callback_id)
        delete_draft(user_id)
        send_message(
            chat_id,
            "🤡 ПЕРЕВІРКА ШАХРАЇВ\n\n"
            "Оберіть спосіб перевірки 👇",
            scammers_menu(user_id),
        )
        return

    if data == "scam:home":
        answer_callback(callback_id)
        delete_draft(user_id)
        show_main_menu(chat_id)
        return

    if data == "scam:any":
        answer_callback(callback_id)
        save_draft(user_id, chat_id, "scam_any", {})
        send_message(
            chat_id,
            "🔎 УНІВЕРСАЛЬНИЙ ПОШУК\n\n"
            "Надішліть одним повідомленням будь-який один ідентифікатор:\n"
            "• ПІБ / ім'я / nickname\n"
            "• @username або посилання на профіль\n"
            "• номер телефону\n"
            "• номер банківської картки\n\n"
            "Бот сам визначить тип запиту та покаже всі релевантні збіги.",
        )
        return

    if data == "scam:phone":
        answer_callback(callback_id)
        save_draft(user_id, chat_id, "scam_phone", {})
        send_message(
            chat_id,
            "📱 ПЕРЕВІРКА ЗА ТЕЛЕФОНОМ\n\n"
            "Введіть номер телефону, наприклад: +380XXXXXXXXX або 0XXXXXXXXX.",
        )
        return

    if data == "scam:card":
        answer_callback(callback_id)
        save_draft(user_id, chat_id, "scam_card", {})
        send_message(
            chat_id,
            "💳 ПЕРЕВІРКА ЗА НОМЕРОМ КАРТКИ\n\n"
            "Введіть:\n"
            "• повний номер картки — 12–19 цифр; або\n"
            "• будь-який відомий фрагмент — 4–11 цифр.\n\n"
            "Можна з пробілами або дефісами.\n"
            "🔐 Повні номери й пошукові фрагменти відкритим текстом у базі не зберігаються.",
        )
        return

    if data == "scam:nick":
        answer_callback(callback_id)
        save_draft(user_id, chat_id, "scam_nick", {})
        send_message(
            chat_id,
            "👤 ПЕРЕВІРКА ЗА ІМЕНЕМ / NICKNAME / USERNAME\n\n"
            "Можна вводити повне ПІБ, частину імені, nickname, username "
            "або варіант кирилицею/латиницею. Бот покаже всі релевантні збіги.",
        )
        return

    if data == "scam:report":
        answer_callback(callback_id)
        save_draft(user_id, chat_id, "scam_report_subject", {})
        send_message(
            chat_id,
            "➕ ПОДАТИ ІНФОРМАЦІЮ ПРО ШАХРАЯ\n\n"
            "Крок 1/8 — 👤 ОСНОВНЕ ІМ'Я\n"
            "Введіть ПІБ або основний nickname так, як він відомий вам.\n"
            "Наприклад: Іван Петренко або Vasya Alergush.",
        )
        return

    if data == "scamreport:cancel":
        answer_callback(callback_id)
        delete_draft(user_id)
        send_message(chat_id, "❌ Подання інформації скасовано.", scammers_menu(user_id))
        return

    if data == "scamreport:submit":
        draft = get_draft(user_id)
        if not draft or draft["state"] != "scam_report_preview":
            answer_callback(callback_id, "Почніть подання інформації заново.", True)
            return

        d = draft["data"]
        duplicate_id = duplicate_scam_report_id(user_id, d)
        if duplicate_id:
            answer_callback(
                callback_id,
                f"Схожа ваша заявка вже є: №{duplicate_id}",
                True,
            )
            return

        report_id = insert_scam_report(user_id, user_display_name(user), d)
        delete_draft(user_id)
        answer_callback(callback_id)

        # Адміну відправляємо на модерацію у фоні, щоб користувач не чекав.
        threading.Thread(
            target=notify_admin_about_scam_report,
            args=(report_id,),
            daemon=True,
        ).start()

        send_message(
            chat_id,
            f"✅ Інформацію прийнято.\n\n"
            f"🆔 Звернення №{report_id}\n"
            "🟡 Статус: на перевірці адміністратора.\n\n"
            "Після підтвердження вона братиме участь у перевірці нашої бази.",
            scammers_menu(user_id),
        )
        return

    if data.startswith("scamapprove:") or data.startswith("scamreject:"):
        if not ADMIN_TELEGRAM_ID or user_id != int(ADMIN_TELEGRAM_ID):
            answer_callback(callback_id, "Недостатньо прав.", True)
            return

        report_id = int(data.split(":", 1)[1])
        new_status = "approved" if data.startswith("scamapprove:") else "rejected"

        with db() as conn:
            report = conn.execute(
                "SELECT * FROM scam_reports WHERE id=?",
                (report_id,),
            ).fetchone()
            if not report:
                answer_callback(callback_id, "Звернення не знайдено.", True)
                return
            conn.execute(
                """
                UPDATE scam_reports
                SET status=?, moderated_at=?, moderated_by=?
                WHERE id=?
                """,
                (new_status, time.time(), user_id, report_id),
            )

        answer_callback(
            callback_id,
            "✅ Підтверджено" if new_status == "approved" else "❌ Відхилено",
        )

        try:
            send_message(
                report["reporter_id"],
                (
                    f"🛡 Звернення №{report_id}\n\n"
                    + (
                        "✅ Інформацію перевірено та додано до підтвердженої бази."
                        if new_status == "approved"
                        else "❌ Інформацію не підтверджено адміністратором."
                    )
                ),
            )
        except Exception:
            pass
        return

    if data == "menu_reviews":
        answer_callback(callback_id)
        delete_draft(user_id)
        send_message(
            chat_id,
            "⭐ ВІДГУКИ ТА РЕЙТИНГ\n\n"
            "Тут можна залишити відгук про користувача або перевірити його рейтинг 👇",
            reviews_menu(),
        )
        return

    if data == "review:home":
        answer_callback(callback_id)
        delete_draft(user_id)
        show_main_menu(chat_id)
        return

    if data == "review:cancel":
        answer_callback(callback_id)
        delete_draft(user_id)
        send_message(chat_id, "❌ Дію скасовано.", reviews_menu())
        return

    if data == "review:add":
        answer_callback(callback_id)
        save_draft(user_id, chat_id, "review_target", {})
        send_message(
            chat_id,
            "✍️ ЗАЛИШИТИ ВІДГУК\n\n"
            "Введіть ПІБ, nickname або username користувача одним повідомленням.\n\n"
            "Можна, наприклад:\n"
            "• Іван Петренко\n"
            "• Fedor\n"
            "• @username\n"
            "• Facebook nickname",
        )
        return

    if data == "review:top20":
        answer_callback(callback_id)
        delete_draft(user_id)
        send_message(chat_id, review_top20_text(False), reviews_menu())
        return

    if data == "review:anti20":
        answer_callback(callback_id)
        delete_draft(user_id)
        send_message(chat_id, review_top20_text(True), reviews_menu())
        return

    if data.startswith("reviewvote:"):
        draft = get_draft(user_id)
        if not draft or draft["state"] != "review_vote":
            answer_callback(callback_id, "Почніть відгук заново.", True)
            return
        rating = int(data.split(":", 1)[1])
        d = draft["data"]
        d["rating"] = rating
        save_draft(user_id, chat_id, "review_comment", d)
        answer_callback(callback_id)
        send_message(
            chat_id,
            "💬 Напишіть текст відгуку одним повідомленням 👇",
        )
        return

    if data == "menu_watermark":
        answer_callback(callback_id)
        delete_draft(user_id)
        save_draft(user_id, chat_id, "watermark_choose_text", {})
        send_message(
            chat_id,
            "🖼 ВОДЯНИЙ ЗНАК НА ФОТО\n\n"
            "Оберіть текст водяного знака 👇",
            watermark_text_menu(),
        )
        return

    if data == "wm:cancel":
        answer_callback(callback_id)
        delete_draft(user_id)
        send_message(chat_id, "❌ Обробку фото скасовано.")
        show_main_menu(chat_id)
        return

    if data == "wmtext:default":
        draft = get_draft(user_id)
        d = draft["data"] if draft else {}
        d["watermark_text"] = "⚖ Східний Аукціон"
        save_draft(user_id, chat_id, "watermark_position", d)
        answer_callback(callback_id)
        send_message(chat_id, "📍 Оберіть розміщення водяного знака 👇", watermark_position_menu())
        return

    if data == "wmtext:custom":
        draft = get_draft(user_id)
        d = draft["data"] if draft else {}
        save_draft(user_id, chat_id, "watermark_custom_text", d)
        answer_callback(callback_id)
        send_message(
            chat_id,
            "✍️ Введіть свій текст водяного знака одним повідомленням.\n"
            "Максимум 80 символів.",
        )
        return

    if data.startswith("wmpos:"):
        draft = get_draft(user_id)
        if not draft:
            answer_callback(callback_id, "Почніть заново.", True)
            return
        d = draft["data"]
        d["position"] = data.split(":", 1)[1]
        save_draft(user_id, chat_id, "watermark_opacity", d)
        answer_callback(callback_id)
        send_message(chat_id, "🌫 Оберіть прозорість водяного знака 👇", watermark_opacity_menu())
        return

    if data.startswith("wmopacity:"):
        draft = get_draft(user_id)
        if not draft:
            answer_callback(callback_id, "Почніть заново.", True)
            return
        d = draft["data"]
        d["opacity"] = int(data.split(":", 1)[1])
        d["photos"] = []
        d.pop("photo_status_message_id", None)
        save_draft(user_id, chat_id, "watermark_photos", d)
        answer_callback(callback_id)
        send_message(
            chat_id,
            "📸 Надішліть від 1 до 10 фото.\n\n"
            "Коли фото завантажаться, під останнім фото буде одна кнопка «✅ Обробити фото».",
        )
        return

    if data == "wm:process":
        draft = get_draft(user_id)
        if not draft or draft["state"] != "watermark_photos":
            answer_callback(callback_id, "Немає фото для обробки.", True)
            return
        d = draft["data"]
        if not d.get("photos"):
            answer_callback(callback_id, "Додайте хоча б одне фото.", True)
            return
        answer_callback(callback_id, "⏳ Обробляю фото…")
        send_message(chat_id, "⏳ Накладаю водяний знак. Зачекайте кілька секунд…")
        threading.Thread(
            target=process_watermark_batch,
            args=(chat_id, user_id, d.copy()),
            daemon=True,
        ).start()
        return

    answer_callback(callback_id, "Функція поки недоступна.")


# =========================================================
# ОБРОБКА ТЕКСТУ / ФОТО ПІД ЧАС СТВОРЕННЯ
# =========================================================

def handle_draft_message(message, user, draft):
    chat_id = message.get("chat", {}).get("id")
    user_id = user.get("id")
    text = (message.get("text") or "").strip()
    state = draft["state"]
    d = draft["data"]

    if state == "watermark_custom_text":
        custom = " ".join((text or "").strip().split())
        if len(custom) < 1 or len(custom) > 80:
            send_message(chat_id, "❌ Введіть текст від 1 до 80 символів.")
            return True
        d["watermark_text"] = custom
        save_draft(user_id, chat_id, "watermark_position", d)
        send_message(chat_id, "📍 Оберіть розміщення водяного знака 👇", watermark_position_menu())
        return True

    if state == "watermark_photos":
        photos = message.get("photo") or []
        if not photos:
            send_message(
                chat_id,
                "📸 Зараз очікую фото. Надішліть фото або натисніть «✅ Обробити фото».",
                watermark_photos_menu(len(d.get("photos", []))),
            )
            return True

        file_id = photos[-1].get("file_id")
        d.setdefault("photos", [])
        if len(d["photos"]) >= 10:
            send_message(chat_id, "⚠️ Максимум 10 фото.", watermark_photos_menu(len(d["photos"])))
            return True
        if file_id and file_id not in d["photos"]:
            d["photos"].append(file_id)

        old_status_id = d.get("photo_status_message_id")
        if old_status_id:
            try:
                delete_message(chat_id, old_status_id)
            except Exception:
                pass

        status_text = (
            f"📸 Фото готові ({len(d['photos'])})\n\n"
            "Можна додати ще фото — максимум 10.\n"
            "Коли завершили, натисніть кнопку нижче."
        )
        result = send_message(
            chat_id,
            status_text,
            watermark_photos_menu(len(d["photos"])),
        )
        if result.get("ok"):
            d["photo_status_message_id"] = result.get("result", {}).get("message_id")
        save_draft(user_id, chat_id, "watermark_photos", d)
        return True

    if state == "scam_report_subject":
        subject = " ".join((text or "").split())
        if len(subject) < 2 or len(subject) > 180:
            send_message(chat_id, "❌ Введіть коректне ПІБ або nickname (2–180 символів).")
            return True

        d = {"subject_text": subject}
        save_draft(user_id, chat_id, "scam_report_aliases", d)
        send_message(
            chat_id,
            "Крок 2/8 — 🔄 ІНШІ ІМЕНА / ВАРІАНТИ НАПИСАННЯ\n"
            "Вкажіть відомі псевдоніми, інше написання ПІБ, латиницю/кирилицю.\n"
            "Кожен варіант — з нового рядка або через «;».\n"
            "Приклад:\nВася Алергуш\nVasya Alergush\n\n"
            "Якщо невідомо — напишіть: немає",
        )
        return True

    if state == "scam_report_aliases":
        raw = (text or "").strip()
        if raw.casefold() in ("немає", "нема", "невідомо", "-", "нет"):
            aliases = []
        else:
            aliases = split_multi_values(raw)[:20]
            if any(len(x) > 180 for x in aliases):
                send_message(chat_id, "❌ Один із варіантів імені задовгий. Максимум 180 символів.")
                return True

        d["aliases"] = aliases
        save_draft(user_id, chat_id, "scam_report_phones", d)
        send_message(
            chat_id,
            "Крок 3/8 — 📱 ТЕЛЕФОН(И)\n"
            "Надішліть один або кілька номерів.\n"
            "Кожен номер — з нового рядка або через «;».\n"
            "Можна у форматі 0XXXXXXXXX або +380XXXXXXXXX.\n\n"
            "Якщо невідомо — напишіть: немає",
        )
        return True

    if state == "scam_report_phones":
        raw = (text or "").strip()
        if raw.casefold() in ("немає", "нема", "невідомо", "-", "нет"):
            phones = []
        else:
            phones = []
            invalid = []
            for item in split_multi_values(raw):
                phone = normalize_phone(item)
                if phone:
                    if phone not in phones:
                        phones.append(phone)
                else:
                    invalid.append(item)
            if invalid:
                send_message(
                    chat_id,
                    "❌ Не вдалося розпізнати номер(и):\n"
                    + "\n".join(invalid[:5])
                    + "\n\nВведіть телефон у форматі 0XXXXXXXXX або +380XXXXXXXXX.",
                )
                return True

        d["phones"] = phones[:20]
        save_draft(user_id, chat_id, "scam_report_cards", d)
        send_message(
            chat_id,
            "Крок 4/8 — 💳 БАНКІВСЬКА КАРТКА / КАРТКИ\n"
            "Надішліть один або кілька повних номерів карток.\n"
            "Кожна картка — з нового рядка або через «;».\n"
            "Допускаються пробіли та дефіси.\n\n"
            "Якщо невідомо — напишіть: немає\n"
            "🔐 Повний номер у базі відкритим текстом не зберігається.",
        )
        return True

    if state == "scam_report_cards":
        raw = (text or "").strip()
        if raw.casefold() in ("немає", "нема", "невідомо", "-", "нет"):
            cards = []
        else:
            provided = split_multi_values(raw)
            cards = []
            invalid = []
            for item in provided:
                digits = normalize_card_digits(item)
                if digits:
                    if digits not in cards:
                        cards.append(digits)
                else:
                    invalid.append(item)

            if invalid:
                send_message(
                    chat_id,
                    "❌ Є некоректні номери карток:\n"
                    + "\n".join(invalid[:5])
                    + "\n\nКожна картка повинна містити 12–19 цифр."
                )
                return True
            if not cards:
                send_message(chat_id, "❌ Не знайдено коректного номера картки (12–19 цифр).")
                return True
            if len(cards) > 20:
                send_message(chat_id, "❌ За один запис можна додати максимум 20 карток.")
                return True

        d["card_digits_list"] = cards
        # сумісність зі старими частинами коду/чернеток
        d["card_digits"] = cards[0] if cards else None
        save_draft(user_id, chat_id, "scam_report_profiles", d)
        send_message(
            chat_id,
            "Крок 5/8 — 🌐 ПРОФІЛІ / USERNAME\n"
            "Додайте @username, t.me-посилання, Facebook-профіль або інші сторінки.\n"
            "Кожен профіль — з нового рядка або через «;».\n\n"
            "Якщо невідомо — напишіть: немає",
        )
        return True

    if state == "scam_report_profiles":
        raw = (text or "").strip()
        if raw.casefold() in ("немає", "нема", "невідомо", "-", "нет"):
            profiles = []
        else:
            profiles = split_multi_values(raw)[:20]

        d["profiles"] = profiles
        save_draft(user_id, chat_id, "scam_report_associates", d)
        send_message(
            chat_id,
            "Крок 6/8 — 👥 СПІЛЬНИКИ / ПОВ'ЯЗАНІ ОСОБИ\n"
            "Вкажіть ПІБ, nickname або інші акаунти пов'язаних осіб.\n"
            "Кожну особу краще писати з нового рядка або через «;».\n\n"
            "Якщо немає — напишіть: немає",
        )
        return True

    if state == "scam_report_associates":
        associates = (text or "").strip()
        if associates.casefold() in ("немає", "нема", "невідомо", "-", "нет"):
            associates = "не вказано"
        d["associates"] = associates[:2000]

        save_draft(user_id, chat_id, "scam_report_description", d)
        send_message(
            chat_id,
            "Крок 7/8 — 🤡 ОПИС СИТУАЦІЇ\n"
            "Коротко й конкретно опишіть, що сталося: що продавали/купували, "
            "яка сума, що саме не виконано, важливі обставини.\n"
            "Мінімум 10 символів.",
        )
        return True

    if state == "scam_report_description":
        description = (text or "").strip()
        if len(description) < 10:
            send_message(chat_id, "❌ Опишіть ситуацію детальніше — мінімум 10 символів.")
            return True
        if len(description) > 2500:
            send_message(chat_id, "❌ Опис задовгий. Максимум 2500 символів.")
            return True

        d["description"] = description
        save_draft(user_id, chat_id, "scam_report_evidence", d)
        send_message(
            chat_id,
            "Крок 8/8 — 📌 ДОКАЗИ\n"
            "Додайте посилання на пост, профіль, номер звернення, опис наявних скриншотів "
            "або інше джерело, яке адміністратор зможе перевірити.\n\n"
            "⚠️ Докази ОБОВ'ЯЗКОВІ. Без них запис у базу не подається.",
        )
        return True

    if state == "scam_report_evidence":
        evidence = (text or "").strip()
        if (
            len(evidence) < 5
            or evidence.casefold() in ("немає", "нема", "невідомо", "-", "нет")
        ):
            send_message(
                chat_id,
                "❌ Для подання інформації потрібні докази. "
                "Додайте посилання, джерело або опис матеріалів для перевірки.",
            )
            return True

        d["evidence"] = evidence[:2000]
        save_draft(user_id, chat_id, "scam_report_preview", d)
        send_message(
            chat_id,
            scam_report_preview(d),
            scam_report_preview_menu(),
        )
        return True

    if state == "search_price":
        parsed = parse_price_range(text)
        if not parsed:
            send_message(
                chat_id,
                "❌ Не вдалося розпізнати ціну. Приклад: 100-500, до 1000 або від 500.",
            )
            return True
        low, high = parsed
        delete_draft(user_id)
        show_search_results(
            chat_id,
            search_lots_by_price_range(low, high),
        )
        return True

    if state == "scam_any":
        if not text or len(text.strip()) < 2:
            send_message(chat_id, "❌ Введіть дані для перевірки.")
            return True
        result = scam_check_text("any", text)
        if not result:
            send_message(chat_id, "❌ Не вдалося розпізнати запит.")
            return True
        delete_draft(user_id)
        send_message(chat_id, result, scam_sources_menu())
        return True

    if state == "scam_phone":
        if not text:
            send_message(chat_id, "❌ Введіть номер телефону.")
            return True
        result = scam_check_text("phone", text)
        if not result:
            send_message(chat_id, "❌ Некоректний номер телефону.")
            return True
        delete_draft(user_id)
        send_message(chat_id, result, scam_sources_menu())
        return True

    if state == "scam_card":
        if not text:
            send_message(chat_id, "❌ Введіть номер картки.")
            return True
        result = scam_check_text("card", text)
        if not result:
            send_message(
                chat_id,
                "❌ Некоректний запит. Введіть 4–11 цифр фрагмента або 12–19 цифр повного номера картки."
            )
            return True
        delete_draft(user_id)
        send_message(chat_id, result, scam_sources_menu())
        return True

    if state == "scam_nick":
        if not text:
            send_message(chat_id, "❌ Введіть ПІБ, ім’я, nickname або username.")
            return True
        result = scam_check_text("nick", text)
        if not result:
            send_message(chat_id, "❌ Некоректний nickname / username.")
            return True
        delete_draft(user_id)
        send_message(chat_id, result, scam_sources_menu())
        return True

    if state == "review_target":
        target_display = clean_identity_display(text)
        target = normalize_identity(text)
        if not target or not target_display:
            send_message(chat_id, "❌ Введіть ПІБ, nickname або username.")
            return True
        save_draft(
            user_id,
            chat_id,
            "review_vote",
            {"target_username": target, "target_display": target_display},
        )
        send_message(
            chat_id,
            f"👤 Відгук про: {target_display}\n\nОберіть оцінку 👇",
            review_vote_menu(),
        )
        return True

    if state == "review_comment":
        comment = (text or "").strip()
        if len(comment) < 3:
            send_message(chat_id, "❌ Відгук занадто короткий. Напишіть хоча б кілька слів.")
            return True
        if len(comment) > 1500:
            send_message(chat_id, "❌ Відгук задовгий. Максимум 1500 символів.")
            return True

        target = d.get("target_username")
        rating = int(d.get("rating") or 0)
        if not target or rating not in (-1, 1):
            delete_draft(user_id)
            send_message(chat_id, "❌ Дані відгуку втрачено. Почніть заново.", reviews_menu())
            return True

        reviewer_name = user_display_name(user)
        with db() as conn:
            cur = conn.execute(
                """
                INSERT INTO reviews(
                    reviewer_id, reviewer_name, target_username, target_display,
                    rating, comment, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (user_id, reviewer_name, target, d.get("target_display"), rating, comment, time.time()),
            )
            review_id = cur.fetchone()["id"]

        delete_draft(user_id)

        # Відповідаємо користувачу одразу. Публікація в тему йде у фоні,
        # щоб Telegram/Render не створювали відчуття зависання.
        send_message(
            chat_id,
            "✅ Відгук збережено.\n\n"
            f"{review_rating_text(target, d.get('target_display'))}",
            reviews_menu(),
        )

        threading.Thread(
            target=publish_review_to_topic,
            args=(review_id,),
            daemon=True,
        ).start()
        return True

        delete_draft(user_id)
        send_message(chat_id, review_rating_text(target), reviews_menu())
        return True

    if state == "search_keyword":
        if not text:
            send_message(chat_id, "❌ Введіть назву або ключове слово.")
            return True
        delete_draft(user_id)
        rows = search_lots_by_keywords(text)
        show_search_results(chat_id, rows)
        return True

    if state == "autobid_amount":
        amount = parse_positive_int(text)
        if not amount:
            send_message(chat_id, "❌ Введіть суму цифрами. Наприклад: 1500")
            return True
        lot_id = int(d["lot_id"])
        ok, msg, result = place_proxy_bid(lot_id, user, amount)
        if not ok:
            send_message(chat_id, f"❌ {msg}")
            return True
        delete_draft(user_id)
        refresh_public_lot(lot_id)
        notify_outbid(lot_id, result)
        extra = f"\n⏱ Нове завершення: {format_dt(result['end_ts'])}" if result["extended"] else ""
        if result["leader_id"] == user_id:
            send_message(
                chat_id,
                f"✅ Автоставку до {amount} грн встановлено.\n"
                f"👑 Ви зараз лідер.\n"
                f"💵 Поточна ціна: {result['price']} грн.{extra}\n\n"
                "Ваш максимальний ліміт прихований від інших учасників.",
            )
        else:
            send_message(
                chat_id,
                f"✅ Автоставку до {amount} грн прийнято, але інший учасник має вищий/раніший ліміт.\n"
                f"💵 Поточна ціна: {result['price']} грн.{extra}",
            )
        return True

    if state == "title":
        if not text:
            send_message(chat_id, "❌ Назва не може бути порожньою.")
            return True
        d["title"] = text[:300]
        ask_material(chat_id, user_id, d)
        return True

    if state == "material":
        if not text:
            send_message(chat_id, "❌ Вкажіть матеріал.")
            return True
        d["material"] = text[:150]
        if d["sale_type"] == "auction":
            save_draft(user_id, chat_id, "start_price", d)
            send_message(chat_id, "💰 Введіть стартову ціну в гривнях:")
        else:
            save_draft(user_id, chat_id, "fixed_price", d)
            send_message(chat_id, "💰 Введіть фіксовану ціну в гривнях:")
        return True

    if state == "start_price":
        value = parse_positive_int(text)
        if not value:
            send_message(chat_id, "❌ Стартова ціна має бути цілим числом більше 0.")
            return True
        d["start_price"] = value
        save_draft(user_id, chat_id, "bid_step", d)
        send_message(chat_id, "📈 Введіть крок ставки в гривнях:")
        return True

    if state == "bid_step":
        value = parse_positive_int(text)
        if not value:
            send_message(chat_id, "❌ Крок ставки має бути цілим числом більше 0.")
            return True
        d["bid_step"] = value
        ask_blitz_choice(chat_id, user_id, d)
        return True

    if state == "blitz_price":
        value = parse_positive_int(text)
        minimum = d["start_price"] + d["bid_step"]
        if not value or value <= minimum:
            send_message(chat_id, f"❌ Бліц-ціна має бути більшою за {minimum} грн.")
            return True
        d["blitz_price"] = value
        ask_reserve_choice(chat_id, user_id, d)
        return True

    if state == "reserve_price":
        value = parse_positive_int(text)
        if not value or value <= d["start_price"]:
            send_message(chat_id, f"❌ Резерв має бути більшим за стартову ціну {d['start_price']} грн.")
            return True
        if d.get("blitz_price") and value > d["blitz_price"]:
            send_message(chat_id, "❌ Резервна ціна не може бути більшою за бліц-ціну.")
            return True
        d["reserve_price"] = value
        ask_end(chat_id, user_id, d)
        return True

    if state == "fixed_price":
        value = parse_positive_int(text)
        if not value:
            send_message(chat_id, "❌ Фіксована ціна має бути цілим числом більше 0.")
            return True
        d["fixed_price"] = value
        ask_end(chat_id, user_id, d)
        return True

    if state in ("end_picker", "end_date", "end_hour", "end_minute", "end_datetime"):
        send_message(
            chat_id,
            "⏰ Дату та час завершення потрібно обрати кнопками нижче.",
        )
        ask_end(chat_id, user_id, d)
        return True

    if state == "phone":
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) < 6 or len(digits) > 15:
            send_message(chat_id, "❌ Введіть коректний номер телефону (6–15 цифр).")
            return True
        d["phone"] = text[:40]
        ask_card(chat_id, user_id, d)
        return True

    if state == "card_last4":
        if len(text) != 4 or not text.isdigit():
            send_message(chat_id, "❌ Потрібно ввести рівно 4 цифри. Наприклад: 7777")
            return True
        d["card_last4"] = text
        ask_extra(chat_id, user_id, d)
        return True

    if state == "extra_info":
        if not text:
            send_message(chat_id, "❌ Введіть короткий опис або стан лота.")
            return True
        d["extra_info"] = text[:1500]
        ask_photos(chat_id, user_id, d)
        return True

    if state == "photos":
        photos = message.get("photo") or []
        if not photos:
            send_message(chat_id, "📸 Зараз очікую фото. Надішліть фото або натисніть «✅ Фото готові».", photos_done_menu(len(d.get("photos", []))))
            return True
        file_id = photos[-1].get("file_id")
        d.setdefault("photos", [])
        if len(d["photos"]) >= MAX_PHOTOS:
            send_message(chat_id, f"⚠️ Максимум {MAX_PHOTOS} фото. Натисніть «✅ Фото готові».", photos_done_menu(len(d["photos"])))
            return True
        if file_id not in d["photos"]:
            d["photos"].append(file_id)
        save_draft(user_id, chat_id, "photos", d)

        # Статус завжди має бути ОДИН і стояти ПІСЛЯ останнього завантаженого фото.
        old_status_id = d.get("photo_status_message_id")
        if old_status_id:
            try:
                delete_message(chat_id, old_status_id)
            except Exception:
                pass

        status_text = (
            f"📸 Фото готові ({len(d['photos'])})\n\n"
            f"Можна додати ще фото — максимум {MAX_PHOTOS}.\n"
            "Коли завершили, натисніть кнопку нижче."
        )
        result = send_message(
            chat_id,
            status_text,
            photos_done_menu(len(d["photos"])),
        )
        if result.get("ok"):
            d["photo_status_message_id"] = result.get("result", {}).get("message_id")
            save_draft(user_id, chat_id, "photos", d)
        return True

    return False


# =========================================================
# WEBHOOK
# =========================================================

@app.route("/telegram-webhook", methods=["POST"])
@app.route("/telegram-webhook-v2", methods=["POST"])
def telegram_webhook_v2():
    ensure_background_started()
    update = request.get_json(silent=True) or {}

    callback = update.get("callback_query")
    if callback:
        try:
            handle_callback(callback)
        except Exception as e:
            print("CALLBACK ERROR:", repr(e))
            try:
                answer_callback(callback.get("id"), "Сталася помилка. Спробуйте ще раз.", True)
            except Exception:
                pass
        return jsonify({"ok": True})

    message = update.get("message")
    if not message:
        return jsonify({"ok": True})

    chat_id = message.get("chat", {}).get("id")
    user = message.get("from", {})
    user_id = user.get("id")
    text = (message.get("text") or "").strip()

    chat_type = message.get("chat", {}).get("type", "private")

    # Статистику бота і статистику групи ведемо ОКРЕМО.
    if chat_type == "private":
        track_user_activity(user, text[:120] if text else "message")
    elif chat_type in ("group", "supergroup"):
        track_community_message(message)
        # Команда в групі — це вже свідома взаємодія з ботом.
        if text.startswith("/"):
            track_user_activity(
                user,
                f"group_command:{text[:100]}",
            )

    # /start підтримує deep-link для автоставки, створення і пошуку лотів.
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        payload = parts[1] if len(parts) == 2 else ""

        if payload.startswith("autobid_"):
            try:
                lot_id = int(payload.split("_", 1)[1])
                start_autobid(chat_id, user_id, lot_id)
            except ValueError:
                show_main_menu(chat_id, user_id)
        elif payload == "create_lot":
            command_lot(chat_id, user_id)
        elif payload == "search_lot":
            command_search_lot(chat_id, user_id)
        else:
            show_main_menu(chat_id, user_id)
        return jsonify({"ok": True})

    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

    if command == "/bot_menu":
        delete_draft(user_id)
        show_main_menu(chat_id, user_id)
    elif command == "/help":
        send_message(
            chat_id,
            "📋 СПИСОК КОМАНД\n\n"
            "😎 /bot_menu — головне меню\n"
            "📋 /help — список команд\n"
            "📣 /ping — перевірка роботи бота\n"
            "📜 /rules — правила групи\n"
            "⚖️ /lot — виставити лот\n"
            "🔎 /search_lot — пошук лотів\n"
            "👤 /my_lots — мої лоти\n"
            "❤️ /favorites — обрані лоти\n"
            "⭐ /profile — моя репутація\n"
            "🧵 /threadid — ID поточної теми",
        )
    elif command == "/ping":
        send_message(chat_id, "🏓 Pong!\n\n✅ Бот працює нормально.")
    elif command == "/rules":
        send_message(chat_id, "📜 ПРАВИЛА СХІДНОГО АУКЦІОНУ\n\n🚧 Розділ правил готується.")
    elif command == "/threadid":
        command_threadid(message)
    elif command == "/lot":
        if chat_type in ("group", "supergroup"):
            temporary_private_redirect(
                message,
                "⚖️ Виставлення лота відбувається тільки в особистому чаті з ботом.",
                "create_lot",
            )
        else:
            command_lot(chat_id, user_id)
    elif command == "/search_lot":
        if chat_type in ("group", "supergroup"):
            temporary_private_redirect(
                message,
                "🔎 /search_lot працює тільки в особистому чаті з ботом.",
                "search_lot",
            )
        else:
            command_search_lot(chat_id, user_id)
    elif command == "/my_lots":
        if chat_type in ("group", "supergroup"):
            temporary_private_redirect(
                message,
                "👤 /my_lots працює тільки в особистому чаті з ботом.",
                "",
            )
        else:
            show_my_lots(chat_id, user_id, "all")
    elif command == "/favorites":
        if chat_type in ("group", "supergroup"):
            temporary_private_redirect(
                message,
                "❤️ /favorites працює тільки в особистому чаті з ботом.",
                "",
            )
        else:
            show_favorites(chat_id, user_id)
    elif command == "/profile":
        if chat_type in ("group", "supergroup"):
            temporary_private_redirect(
                message,
                "⭐ /profile працює тільки в особистому чаті з ботом.",
                "",
            )
        else:
            send_message(chat_id, reputation_text(user_id))
    else:
        draft = get_draft(user_id)
        if draft:
            handle_draft_message(message, user, draft)

    # Додаткова страховка: на кожному апдейті перевіряємо прострочені лоти.
    try:
        finish_due_lots()
    except Exception as e:
        print("FINISH CHECK ERROR:", repr(e))

    return jsonify({"ok": True})


# =========================================================
# ГОЛОВНА СТОРІНКА / HEALTH
# =========================================================

@app.route("/date-picker", methods=["GET", "POST"])
def date_picker_webapp():
    token = request.args.get("token", "")
    verified = _verify_date_picker_token(token)
    if not verified:
        return (
            "<meta charset='utf-8'><div style='font-family:sans-serif;padding:24px'>"
            "❌ Посилання недійсне або застаріло. Поверніться в бот і відкрийте календар ще раз."
            "</div>",
            400,
        )

    user_id, chat_id = verified
    if request.method == "POST":
        raw = (request.form.get("end_datetime") or "").strip()
        try:
            dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M").replace(tzinfo=KYIV)
        except ValueError:
            dt = None

        if not dt or dt.timestamp() <= time.time():
            return (
                "<meta charset='utf-8'><div style='font-family:sans-serif;padding:24px'>"
                "❌ Оберіть майбутню дату та час."
                "<br><br><a href='javascript:history.back()'>⬅️ Назад</a></div>",
                400,
            )

        draft = get_draft(user_id)
        if not draft or draft["state"] != "end_picker":
            return (
                "<meta charset='utf-8'><div style='font-family:sans-serif;padding:24px'>"
                "❌ Сесію створення лота втрачено. Поверніться в бот."
                "</div>",
                400,
            )

        d = draft["data"]
        d["end_ts"] = dt.timestamp()
        if d["sale_type"] == "auction":
            save_draft(user_id, chat_id, "anti_sniper", d)
            send_message(
                chat_id,
                f"✅ Завершення: {format_dt(d['end_ts'])}\n\n⏱ Оберіть антиснайпер:",
                anti_sniper_menu(),
            )
        else:
            d["anti_sniper"] = 0
            save_draft(user_id, chat_id, "phone", d)
            send_message(chat_id, f"✅ Завершення: {format_dt(d['end_ts'])}")
            ask_phone(chat_id, user_id, d)

        return """
        <!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
        <title>Дата збережена</title></head>
        <body style='font-family:Arial,sans-serif;padding:28px;text-align:center'>
        <h2>✅ Дату та час збережено</h2><p>Поверніться в Telegram — бот уже надіслав наступний крок.</p>
        <button onclick='if(window.Telegram&&Telegram.WebApp){Telegram.WebApp.close()}else{window.close()}' style='font-size:18px;padding:12px 22px'>Повернутися в Telegram</button>
        <script src='https://telegram.org/js/telegram-web-app.js'></script>
        </body></html>
        """

    min_dt = datetime.now(KYIV).strftime("%Y-%m-%dT%H:%M")
    return f"""
    <!doctype html>
    <html lang='uk'>
    <head>
      <meta charset='utf-8'>
      <meta name='viewport' content='width=device-width,initial-scale=1,maximum-scale=1'>
      <title>Дата завершення лота</title>
      <script src='https://telegram.org/js/telegram-web-app.js'></script>
      <style>
        body {{ font-family: Arial, sans-serif; margin:0; padding:24px; background:#18222d; color:white; }}
        .card {{ max-width:520px; margin:0 auto; }}
        h2 {{ margin-top:0; }}
        input {{ width:100%; box-sizing:border-box; font-size:20px; padding:16px; border-radius:12px; border:1px solid #74808d; background:#fff; color:#111; }}
        button {{ width:100%; margin-top:18px; font-size:18px; padding:14px; border:0; border-radius:24px; background:#4f9be8; color:white; font-weight:600; }}
        p {{ color:#c7d1dc; line-height:1.4; }}
      </style>
    </head>
    <body>
      <div class='card'>
        <h2>⏰ Завершення лота</h2>
        <p>Оберіть дату та точний час. Поле відкриє календар і вибір часу на вашому пристрої.</p>
        <form method='post' action='/date-picker?token={token}'>
          <input type='datetime-local' name='end_datetime' min='{min_dt}' required>
          <button type='submit'>✅ Зберегти дату та час</button>
        </form>
      </div>
    </body></html>
    """


@app.route("/db-health")
def db_health():
    status = database_health()
    return jsonify(status), (200 if status["ok"] else 500)


@app.route("/")
def home():
    ensure_background_started()
    db_status = database_health()
    return jsonify({
        "ok": True,
        "service": "numizmat-bot-v3",
        "version": "4.4-card-partial-search",
        "database": db_status,
        "webhook_paths": ["/telegram-webhook", "/telegram-webhook-v2"],
    })


@app.route("/health")
def health():
    ensure_background_started()
    return jsonify({"status": "healthy", "version": "4.4-card-partial-search"})


@app.route("/close-expired", methods=["GET", "POST"])
def close_expired_endpoint():
    """
    Compatibility endpoint for the existing Render scheduled call.
    Safely checks and closes due lots.
    """
    ensure_background_started()
    try:
        finish_due_lots()
        return jsonify({"ok": True, "message": "expired lots checked"})
    except Exception as e:
        print("CLOSE EXPIRED ERROR:", repr(e))
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================================================
# ЗАПУСК
# =========================================================

init_db()
register_bot_commands()
ensure_background_started()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 10000)),
    )
