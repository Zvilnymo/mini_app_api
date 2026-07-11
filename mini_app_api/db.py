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


def create_client(conn, telegram_id: int, full_name: str, phone: str):
    with conn.cursor() as cur:
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
