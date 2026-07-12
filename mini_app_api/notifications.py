"""
Admin notifications — mirrors documents_bot's notify_admins() (client
uploaded a document, AI verdict, progress, links), but sends via a direct
Telegram Bot API call (stdlib urllib, same convention as bitrix.py) instead
of python-telegram-bot, and reads recipients from docbot.admins (Postgres)
instead of the bot's own admins.txt file, which mini_app_api has no access
to (different Render service, different disk).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from . import db

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


def _send_message(chat_id: int, text: str, timeout: int = 10) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Failed to notify admin {chat_id}: HTTP {e.code}: {body}")
    except Exception as e:
        logger.error(f"Failed to notify admin {chat_id}: {e}")


def notify_admins(conn, text: str) -> None:
    """Best-effort — a notification failure must never break the upload
    that triggered it, so every error here is swallowed after logging."""
    try:
        admin_ids = db.get_admin_ids(conn)
    except Exception as e:
        logger.error(f"Failed to load admin ids: {e}")
        return
    if not admin_ids:
        logger.warning("No admins in docbot.admins to notify")
        return
    for admin_id in admin_ids:
        _send_message(admin_id, text)


def notify_document_uploaded(conn, *, client: dict, doc_name: str, validation_status: str | None,
                              file_url: str | None, folder_url: str | None,
                              docs_ready: int, docs_total: int) -> None:
    if validation_status == "rejected":
        title = "❌ <b>AI відхилив документ</b>"
        status_emoji = "❌"
    elif validation_status == "uncertain":
        title = "⚠️ <b>Документ потребує перевірки</b>"
        status_emoji = "⚠️"
    else:
        title = "📄 <b>Клієнт завантажив документ</b>"
        status_emoji = "✅"

    status_text = validation_status or "не перевірено"
    lines = [
        title,
        "",
        f"👤 {client['full_name']}",
        f"📱 {client['phone']}",
        f"📑 {doc_name}",
        f"{status_emoji} <b>AI:</b> {status_text}",
        f"📊 <b>Прогрес:</b> {docs_ready}/{docs_total} документів",
    ]
    if file_url:
        lines += ["", f'📁 <a href="{file_url}">Переглянути документ</a>']
    if folder_url:
        lines.append(f'📂 <a href="{folder_url}">Папка клієнта</a>')

    notify_admins(conn, "\n".join(lines))
