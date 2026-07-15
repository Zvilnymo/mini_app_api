"""
Postgres access for mini_app_api.

Same database as documents_bot (docbot.* schema) and the etl_zv warehouse
(crm.* schema) — confirmed by public.v_support_client_activity joining both
schemas in one query, and by running these queries live against ZV_DB.
One DATABASE_URL is enough for everything here.

crm.* schema shape (verified live on 2026-07-11):
  crm.dim_contacts(id, full_name, phone, etl_loaded_at) is the canonical
  contact record. crm.fact_deals / crm.fact_pre_court_deals /
  crm.fact_court_deals / crm.fact_invoices all carry a direct contact_id
  FK to it — no need to route through fact_leads (which has no contact_id
  or name columns at all).

  Three funnels correspond to three fact tables:
    fact_deals            -> funnel_id=0 (no stage prefix)   "contract signing"
    fact_pre_court_deals   -> funnel_id=1 (C1: prefix)         "document collection / pre-court"
    fact_court_deals       -> funnel_id=2 (C2: prefix)         "court process"
  Human-readable stage names come from crm.dim_deal_stages.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import psycopg2
import psycopg2.extras

_LOCAL_ENV_CANDIDATES = [
    Path(__file__).resolve().parents[2] / ".env",  # work_zvilnymo/.env (local dev fallback)
]


def _local_dsn_kwargs() -> dict:
    for path in _LOCAL_ENV_CANDIDATES:
        if path.exists():
            cfg = json.loads(path.read_text(encoding="utf-8"))["databases"]["ZV_DB"]
            return dict(
                host=cfg["host"],
                port=cfg["port"],
                dbname=cfg["database"],
                user=cfg["user"],
                password=cfg["password"],
            )
    raise RuntimeError(
        "DATABASE_URL is not set and no local .env fallback was found "
        f"(looked at: {[str(p) for p in _LOCAL_ENV_CANDIDATES]})"
    )


def get_connection():
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    return psycopg2.connect(cursor_factory=psycopg2.extras.RealDictCursor, **_local_dsn_kwargs())


def normalize_phone(phone: str) -> str:
    return re.sub(r"[^0-9]", "", phone or "")


# ---------------------------------------------------------------------------
# docbot.clients / docbot.documents (same tables documents_bot writes to)
# ---------------------------------------------------------------------------

def get_client_by_telegram_id(conn, telegram_id: int):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM docbot.clients WHERE telegram_id = %s", (telegram_id,))
        return cur.fetchone()


def get_client_by_phone(conn, phone: str):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM docbot.clients WHERE phone = %s", (phone,))
        return cur.fetchone()


def get_client_by_id(conn, client_id: int):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM docbot.clients WHERE id = %s", (client_id,))
        return cur.fetchone()


class PhoneAlreadyLinked(Exception):
    """Raised when the phone being registered already belongs to a different telegram_id."""


def create_client(conn, telegram_id: int, full_name: str, phone: str):
    phone = normalize_phone(phone)
    with conn.cursor() as cur:
        # Telegram verifies the phone via requestContact, but that only
        # proves the *current* device owns the number right now — it doesn't
        # prove this is the same person who registered it earlier. Rebinding
        # automatically would let anyone with access to that phone (family
        # member, new SIM owner) hijack another client's documents/case, so
        # a phone already claimed by a different telegram_id is refused, not
        # silently reassigned (see docbot.clients' UNIQUE(phone), which this
        # used to bypass by storing the raw, unnormalized phone string).
        cur.execute(
            "SELECT telegram_id FROM docbot.clients WHERE regexp_replace(phone, '[^0-9]', '', 'g') = %s",
            (phone,),
        )
        existing = cur.fetchone()
        if existing and existing["telegram_id"] != telegram_id:
            raise PhoneAlreadyLinked(phone)

        cur.execute(
            """
            INSERT INTO docbot.clients (telegram_id, full_name, phone)
            VALUES (%s, %s, %s)
            ON CONFLICT (telegram_id) DO UPDATE
            SET full_name = EXCLUDED.full_name,
                phone = EXCLUDED.phone,
                last_activity = CURRENT_TIMESTAMP
            RETURNING *
            """,
            (telegram_id, full_name, phone),
        )
        conn.commit()
        return cur.fetchone()


def update_client_screening(conn, client_id: int, *, has_gambling_crypto: bool, is_fraud_victim: bool,
                             has_sold_property: bool, income_over_30k: bool):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE docbot.clients
            SET has_gambling_crypto = %s, is_fraud_victim = %s,
                has_sold_property = %s, income_over_30k = %s
            WHERE id = %s
            RETURNING *
            """,
            (has_gambling_crypto, is_fraud_victim, has_sold_property, income_over_30k, client_id),
        )
        conn.commit()
        return cur.fetchone()


def is_screening_complete(client: dict) -> bool:
    return all(
        client.get(key) is not None
        for key in ("has_gambling_crypto", "is_fraud_victim", "has_sold_property", "income_over_30k")
    )


# ---------------------------------------------------------------------------
# docbot.declarations — the "Декларація" questionnaire (17 free-text fields),
# same table documents_bot's own /declaration flow reads and writes.
# ---------------------------------------------------------------------------

# Whitelist of real docbot.declarations text columns — used to build UPDATE
# statements safely (column names can't be parameterized, so only ever
# accept keys from this fixed set, never anything from the request).
DECLARATION_FIELD_KEYS = (
    "email_password",
    "living_address_2022_2025",
    "registration_change",
    "property_alienation_self",
    "property_alienation_family",
    "family_vehicles",
    "corporate_rights",
    "crypto_foreign_credits",
    "specific_bank_credits",
    "online_betting",
    "bank_installments",
    "creditor_address",
    "housing_owner",
    "marriage_transactions",
    "alienation_documents",
    "vehicle_power_of_attorney",
    "alimony_info",
)


def get_or_create_declaration(conn, client_id: int, attempt: int = 1):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM docbot.declarations WHERE client_id = %s AND attempt = %s",
            (client_id, attempt),
        )
        row = cur.fetchone()
        if row:
            return row
        cur.execute(
            "INSERT INTO docbot.declarations (client_id, attempt) VALUES (%s, %s) RETURNING *",
            (client_id, attempt),
        )
        conn.commit()
        return cur.fetchone()


def save_declaration_answers(conn, client_id: int, answers: dict, attempt: int = 1):
    fields = {k: v for k, v in answers.items() if k in DECLARATION_FIELD_KEYS}
    if not fields:
        return get_or_create_declaration(conn, client_id, attempt)
    get_or_create_declaration(conn, client_id, attempt)  # ensure the row exists first
    set_clause = ", ".join(f"{key} = %s" for key in fields)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE docbot.declarations SET {set_clause} WHERE client_id = %s AND attempt = %s RETURNING *",
            (*fields.values(), client_id, attempt),
        )
        conn.commit()
        return cur.fetchone()


def complete_declaration(conn, client_id: int, attempt: int = 1):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE docbot.declarations SET status = 'completed', completed_at = CURRENT_TIMESTAMP "
            "WHERE client_id = %s AND attempt = %s",
            (client_id, attempt),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# docbot.admins — Telegram IDs to notify on client activity (document
# uploads, etc). Replaces documents_bot's admins.txt file: that file lives
# on the bot service's own disk, unreachable from mini_app_api, and is
# wiped on redeploy if no persistent disk is mounted there. Postgres is
# already shared between both services and isn't ephemeral, so it's the
# more reliable home for this list going forward.
#
# `scope` distinguishes which admin panel someone registered for
# ('documents' vs 'conferences', see main.py's two admin_secret_code deep
# links) — PK is (telegram_id, scope) so one person can hold both.
# ---------------------------------------------------------------------------

def get_admin_ids(conn, scope: str = "documents") -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT telegram_id FROM docbot.admins WHERE scope = %s ORDER BY added_at", (scope,))
        return [row["telegram_id"] for row in cur.fetchall()]


def register_admin(conn, telegram_id: int, full_name: str | None = None, scope: str = "documents"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docbot.admins (telegram_id, full_name, scope) VALUES (%s, %s, %s)
            ON CONFLICT (telegram_id, scope) DO NOTHING
            RETURNING *
            """,
            (telegram_id, full_name, scope),
        )
        conn.commit()
        return cur.fetchone()


def is_admin(conn, telegram_id: int, scope: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM docbot.admins WHERE telegram_id = %s AND scope = %s", (telegram_id, scope))
        return cur.fetchone() is not None


def get_documents_by_client(conn, client_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM docbot.documents WHERE client_id = %s ORDER BY uploaded_at DESC",
            (client_id,),
        )
        return cur.fetchall()


def add_document(conn, client_id: int, document_type: str, file_name: str,
                  drive_file_id: str, drive_file_url: str, file_size: int,
                  validation_status: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docbot.documents
                (client_id, document_type, file_name, drive_file_id, drive_file_url,
                 file_size, validation_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (client_id, document_type, file_name, drive_file_id, drive_file_url,
             file_size, validation_status),
        )
        conn.commit()
        return cur.fetchone()


def get_latest_document(conn, client_id: int, document_type: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM docbot.documents
            WHERE client_id = %s AND document_type = %s
            ORDER BY uploaded_at DESC
            LIMIT 1
            """,
            (client_id, document_type),
        )
        return cur.fetchone()


def update_document_file(conn, document_id: int, *, drive_file_id: str,
                          drive_file_url: str, file_size: int):
    """Used for text-type documents (ecpass/emailpass) whose Disk file is
    overwritten in place — updates the same docbot.documents row rather than
    inserting a new one, since the underlying Disk file ID doesn't change
    (disk.file.uploadversion keeps it stable)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE docbot.documents
            SET drive_file_id = %s, drive_file_url = %s, file_size = %s,
                uploaded_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING *
            """,
            (drive_file_id, drive_file_url, file_size, document_id),
        )
        conn.commit()
        return cur.fetchone()


# ---------------------------------------------------------------------------
# crm.* warehouse (filled by etl_zv from Bitrix24) — read-only
# ---------------------------------------------------------------------------

# Invoice stage_id -> paid/unpaid, mirrors q_client_invoices.py
PAID_INVOICE_STAGES = ("DT31_1:UC_WW75SB", "DT31_1:P")
DEBTOR_INVOICE_STAGE = "DT31_1:UC_OH8Y4S"


def get_contact_by_phone(conn, phone_norm: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, full_name, phone
            FROM crm.dim_contacts
            WHERE regexp_replace(phone, '[^0-9]', '', 'g') = %s
            LIMIT 1
            """,
            (phone_norm,),
        )
        return cur.fetchone()


def get_lead_debt(conn, phone_norm: str):
    """Debt amount as originally declared at the lead stage (crm.fact_leads),
    before any deal/case record exists — used on Home as the client's
    starting debt figure, since fact_deals.total_debt only exists once a
    deal has actually been created."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT total_debt
            FROM crm.fact_leads
            WHERE regexp_replace(phone, '[^0-9]', '', 'g') = %s AND total_debt IS NOT NULL
            ORDER BY date_create DESC
            LIMIT 1
            """,
            (phone_norm,),
        )
        row = cur.fetchone()
        return float(row["total_debt"]) if row else None


def get_deal(conn, contact_id: int):
    """funnel 0 — contract signing."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, stage_id, opportunity, date_create, close_date, manager_id, total_debt
            FROM crm.fact_deals
            WHERE contact_id = %s
            ORDER BY date_create DESC
            LIMIT 1
            """,
            (contact_id,),
        )
        return cur.fetchone()


def get_pre_court_deal(conn, contact_id: int):
    """funnel 1 (C1:*) — document collection / pre-court."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, stage_id, court_filing_date, date_create, close_date,
                   manager_id, total_debt, creditors_count, banks_count
            FROM crm.fact_pre_court_deals
            WHERE contact_id = %s
            ORDER BY date_create DESC
            LIMIT 1
            """,
            (contact_id,),
        )
        return cur.fetchone()


def get_court_deal(conn, contact_id: int):
    """funnel 2 (C2:*) — court process."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, stage_id, court_filing_date, date_create, close_date, debt_to_write_off,
                   manager_id, total_debt, creditors_count, banks_count
            FROM crm.fact_court_deals
            WHERE contact_id = %s
            ORDER BY date_create DESC
            LIMIT 1
            """,
            (contact_id,),
        )
        return cur.fetchone()


def get_stage_name(conn, stage_id: str) -> str | None:
    if not stage_id:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM crm.dim_deal_stages WHERE stage_id = %s", (stage_id,))
        row = cur.fetchone()
        return row["name"] if row else None


def get_invoices(conn, contact_id: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, amount, stage_id, stage_name, payment_date, invoice_date
            FROM crm.fact_invoices
            WHERE contact_id = %s
            ORDER BY invoice_date
            """,
            (contact_id,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# docbot.invoice_receipts — tracks "client submitted a receipt, awaiting
# manager review" per invoice. crm.fact_invoices itself is a read-only ETL
# mirror of Bitrix (see payments.py) — this local table is what lets the
# Cabinet show a "На перевірці" state that survives reloads, until the
# invoice's own stage_id actually flips to paid on Bitrix's side.
# ---------------------------------------------------------------------------

def mark_receipt_submitted(conn, invoice_id: int, client_id: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docbot.invoice_receipts (invoice_id, client_id) VALUES (%s, %s)
            ON CONFLICT (invoice_id) DO UPDATE SET submitted_at = CURRENT_TIMESTAMP
            """,
            (invoice_id, client_id),
        )
        conn.commit()


def get_pending_receipt_invoice_ids(conn, client_id: int) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT invoice_id FROM docbot.invoice_receipts WHERE client_id = %s", (client_id,))
        return {row["invoice_id"] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# docbot.events / event_rsvp / event_attendance / event_feedback — "Зустрічі"
# (conferences), ported from conf_bot's self-contained Postgres schema (no
# Bitrix involvement there either). Unlike conf_bot's automatic bulk-invite
# eligibility engine (per-type dedup, daily RSVP limits, type-4-needs-type-1
# prerequisites), an admin here always picks recipients explicitly — same
# client-visible behavior (invite -> RSVP -> reminder -> feedback), simpler
# admin-side implementation.
# ---------------------------------------------------------------------------

def list_event_types(conn, active_only: bool = True):
    with conn.cursor() as cur:
        if active_only:
            cur.execute("SELECT * FROM docbot.event_types WHERE active ORDER BY type_code")
        else:
            cur.execute("SELECT * FROM docbot.event_types ORDER BY type_code")
        return cur.fetchall()


def get_client_checklist(conn, client_id: int):
    """The required conference types + whether this client has attended one
    of each — the client-facing progress checklist, same idea as
    documents.checklist_for_client but for event_types.required instead of
    a file upload."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.type_code, t.title, t.description,
                   EXISTS (
                       SELECT 1 FROM docbot.events e
                       JOIN docbot.event_attendance a ON a.event_id = e.event_id
                       WHERE e.type_code = t.type_code AND a.client_id = %s AND a.attended
                   ) AS completed
            FROM docbot.event_types t
            WHERE t.required
            ORDER BY t.type_code
            """,
            (client_id,),
        )
        return cur.fetchall()


def get_client_events(conn, client_id: int):
    """Every event this client has been invited to, most recent first,
    each row carrying that client's own rsvp/attendance/feedback state."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.event_id, e.title, e.description, e.start_at, e.duration_min,
                   e.format, e.link, e.person_name, e.person_role,
                   r.rsvp, r.rsvp_at,
                   a.attended,
                   f.stars AS feedback_stars, f.comment AS feedback_comment
            FROM docbot.event_rsvp r
            JOIN docbot.events e ON e.event_id = r.event_id
            LEFT JOIN docbot.event_attendance a ON a.event_id = r.event_id AND a.client_id = r.client_id
            LEFT JOIN docbot.event_feedback f ON f.event_id = r.event_id AND f.client_id = r.client_id
            WHERE r.client_id = %s
            ORDER BY e.start_at DESC
            """,
            (client_id,),
        )
        return cur.fetchall()


def get_client_event(conn, event_id: int, client_id: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.*, r.rsvp FROM docbot.events e
            JOIN docbot.event_rsvp r ON r.event_id = e.event_id
            WHERE e.event_id = %s AND r.client_id = %s
            """,
            (event_id, client_id),
        )
        return cur.fetchone()


def submit_rsvp(conn, event_id: int, client_id: int, rsvp: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE docbot.event_rsvp SET rsvp = %s, rsvp_at = CURRENT_TIMESTAMP
            WHERE event_id = %s AND client_id = %s
            RETURNING *
            """,
            (rsvp, event_id, client_id),
        )
        conn.commit()
        return cur.fetchone()


def submit_feedback(conn, event_id: int, client_id: int, stars: int, comment: str | None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docbot.event_feedback (event_id, client_id, stars, comment)
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (event_id, client_id, stars, comment),
        )
        conn.commit()
        return cur.fetchone()


def create_event(conn, *, type_code: int | None, title: str, description: str | None, start_at,
                  duration_min: int, format: str, link: str | None, person_name: str | None,
                  person_role: str | None, created_by: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docbot.events
                (type_code, title, description, start_at, duration_min, format, link,
                 person_name, person_role, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (type_code, title, description, start_at, duration_min, format, link,
             person_name, person_role, created_by),
        )
        conn.commit()
        return cur.fetchone()


def update_event_field(conn, event_id: int, field: str, value):
    # Whitelisted, mirrors conf_bot's editable-field set — never build this
    # from unvalidated request input.
    if field not in ("title", "description", "start_at", "duration_min", "format", "link",
                      "person_name", "person_role"):
        raise ValueError(f"non-editable event field: {field}")
    with conn.cursor() as cur:
        cur.execute(f"UPDATE docbot.events SET {field} = %s WHERE event_id = %s RETURNING *", (value, event_id))
        conn.commit()
        return cur.fetchone()


def get_event(conn, event_id: int):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM docbot.events WHERE event_id = %s", (event_id,))
        return cur.fetchone()


def list_events(conn, upcoming: bool = True, limit: int = 100):
    with conn.cursor() as cur:
        if upcoming:
            cur.execute(
                "SELECT * FROM docbot.events WHERE start_at >= now() ORDER BY start_at LIMIT %s", (limit,)
            )
        else:
            cur.execute(
                "SELECT * FROM docbot.events WHERE start_at < now() ORDER BY start_at DESC LIMIT %s", (limit,)
            )
        return cur.fetchall()


def get_event_rsvp_rows(conn, event_id: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.client_id, r.rsvp, r.rsvp_at, c.full_name, c.phone, c.telegram_id
            FROM docbot.event_rsvp r
            JOIN docbot.clients c ON c.id = r.client_id
            WHERE r.event_id = %s
            ORDER BY r.invited_at
            """,
            (event_id,),
        )
        return cur.fetchall()


def get_going_clients(conn, event_id: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id AS client_id, c.telegram_id, c.full_name
            FROM docbot.event_rsvp r
            JOIN docbot.clients c ON c.id = r.client_id
            WHERE r.event_id = %s AND r.rsvp = 'going'
            """,
            (event_id,),
        )
        return cur.fetchall()


def delete_event(conn, event_id: int):
    # Matches conf_bot's own behavior: cancel = hard delete, no status flag.
    # RSVP/attendance/feedback rows cascade via FK ON DELETE CASCADE.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM docbot.events WHERE event_id = %s", (event_id,))
        conn.commit()


def search_clients(conn, query: str, limit: int = 20):
    """For the admin's "invite" picker — match by name or phone digits."""
    digits = re.sub(r"[^0-9]", "", query)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, full_name, phone, telegram_id FROM docbot.clients
            WHERE full_name ILIKE %s OR (%s <> '' AND regexp_replace(phone, '[^0-9]', '', 'g') LIKE %s)
            ORDER BY full_name
            LIMIT %s
            """,
            (f"%{query}%", digits, f"%{digits}%", limit),
        )
        return cur.fetchall()


def invite_clients(conn, event_id: int, client_ids: list[int]):
    with conn.cursor() as cur:
        for client_id in client_ids:
            cur.execute(
                """
                INSERT INTO docbot.event_rsvp (event_id, client_id) VALUES (%s, %s)
                ON CONFLICT (event_id, client_id) DO NOTHING
                """,
                (event_id, client_id),
            )
        conn.commit()


def mark_attendance(conn, event_id: int, client_id: int, attended: bool):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO docbot.event_attendance (event_id, client_id, attended, marked_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (event_id, client_id) DO UPDATE
            SET attended = EXCLUDED.attended, marked_at = EXCLUDED.marked_at
            """,
            (event_id, client_id, attended),
        )
        conn.commit()


def get_events_needing_reminder(conn, window: str):
    """window: '24h' or '60m' — events starting soon whose 'going' clients
    haven't been reminded yet at this window."""
    col = "reminded_24h" if window == "24h" else "reminded_60m"
    interval = "24 hours" if window == "24h" else "60 minutes"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT r.event_id, r.client_id, c.telegram_id, e.title, e.start_at, e.link
            FROM docbot.event_rsvp r
            JOIN docbot.events e ON e.event_id = r.event_id
            JOIN docbot.clients c ON c.id = r.client_id
            WHERE r.rsvp = 'going' AND NOT r.{col}
              AND e.start_at BETWEEN now() AND now() + interval '{interval}'
            """,
        )
        return cur.fetchall()


def mark_reminded(conn, event_id: int, client_id: int, window: str):
    col = "reminded_24h" if window == "24h" else "reminded_60m"
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE docbot.event_rsvp SET {col} = TRUE WHERE event_id = %s AND client_id = %s",
            (event_id, client_id),
        )
        conn.commit()
