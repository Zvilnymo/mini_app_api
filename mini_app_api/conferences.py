"""
Conference/meeting business logic — ported from conf_bot (aiogram bot,
self-contained Postgres, no Bitrix). Client-facing RSVP/feedback happens
inside the Mini App itself (see main.py's /api/conferences/*), not via
Telegram inline-keyboard callbacks like the original bot — mini_app_api has
no bot polling/webhook process to receive those. Telegram is only used for
a plain notification ping ("open the app"), matching notify_document_uploaded's
existing pattern.
"""
from __future__ import annotations

from datetime import datetime

from . import db, notifications


def _fmt_dt(start_at: datetime) -> str:
    return start_at.strftime("%d.%m.%Y о %H:%M")


def notify_invite(client_telegram_id: int, title: str) -> None:
    notifications.notify_client(
        client_telegram_id,
        f"📅 <b>Запрошення на зустріч</b>\n\n{title}\n\nВідкрийте застосунок, щоб підтвердити участь.",
    )


def notify_event_update(conn, event_id: int, what: str) -> None:
    for row in db.get_going_clients(conn, event_id):
        if row["telegram_id"]:
            notifications.notify_client(
                row["telegram_id"],
                f"🛠 <b>Зміни у зустрічі</b>\n\n{what}\n\nПеревірте деталі в застосунку.",
            )


def notify_event_cancel(conn, event_id: int, title: str) -> None:
    for row in db.get_going_clients(conn, event_id):
        if row["telegram_id"]:
            notifications.notify_client(
                row["telegram_id"],
                f"❌ <b>Зустріч скасовано</b>\n\n«{title}» більше не відбудеться. "
                f"Ми повідомимо про нову дату найближчим часом.",
            )


def notify_admins_new_rsvp(conn, *, client_name: str, event_title: str, rsvp: str) -> None:
    verb = "підтвердив участь у" if rsvp == "going" else "відхилив запрошення на"
    notifications.notify_admins(
        conn,
        f"📅 <b>Клієнт {verb} зустрічі</b>\n\n👤 {client_name}\n🗓 {event_title}",
        scope="conferences",
    )


def send_invites(conn, event: dict, client_ids: list[int]) -> None:
    db.invite_clients(conn, event["event_id"], client_ids)
    for client_id in client_ids:
        client = db.get_client_by_id(conn, client_id)
        if client and client.get("telegram_id"):
            notify_invite(client["telegram_id"], event["title"])


def send_reminders(conn) -> None:
    """Called periodically (see main.py's scheduler) — pings 'going' clients
    24h and again ~1h before their meeting starts, once each."""
    for window in ("24h", "60m"):
        for row in db.get_events_needing_reminder(conn, window):
            if row["telegram_id"]:
                when = "завтра" if window == "24h" else "менш ніж за годину"
                link_line = f"\n🔗 {row['link']}" if row["link"] else ""
                notifications.notify_client(
                    row["telegram_id"],
                    f"🔔 <b>Нагадування</b>\n\nЗустріч «{row['title']}» відбудеться {when}, "
                    f"о {_fmt_dt(row['start_at'])}.{link_line}",
                )
            db.mark_reminded(conn, row["event_id"], row["client_id"], window)
