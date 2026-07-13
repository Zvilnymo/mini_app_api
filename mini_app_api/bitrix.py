"""
Minimal live Bitrix24 REST client — only what mini_app_api needs to write
back to Bitrix (creating a complaint task). All read-heavy case/payment data
comes from the crm.* warehouse in db.py instead; this module exists solely
for the one write path that has no warehouse equivalent.

Uses stdlib urllib (no new dependency), matching documents_bot's own
_bitrix_post convention in telegram_bot.py.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta

def _resolve_webhook() -> str:
    # Same fallback documents_bot's telegram_bot.py uses: a single
    # BITRIX_WEBHOOK env var takes priority if set, otherwise build it from
    # the three B24_* parts (how it's actually configured on Render here).
    direct = os.getenv("BITRIX_WEBHOOK")
    if direct:
        return direct
    domain = os.getenv("B24_DOMAIN", "")
    user_id = os.getenv("B24_USER_ID", "")
    token = os.getenv("B24_TOKEN_DEALS", "")
    if domain and user_id and token:
        return f"https://{domain}/rest/{user_id}/{token}/"
    return ""


BITRIX_WEBHOOK = _resolve_webhook()


def _resolve_task_webhook() -> str:
    # tasks.task.add needs its own webhook (BITRIX_WEBHOOK_TASK, added on
    # Render) — the deals webhook's token isn't scoped for task creation.
    # Same B24_DOMAIN/B24_USER_ID as the deals webhook, just a different
    # token. Falls back to the deals webhook if this one isn't configured.
    token = os.getenv("BITRIX_WEBHOOK_TASK")
    if not token:
        return BITRIX_WEBHOOK
    domain = os.getenv("B24_DOMAIN", "")
    user_id = os.getenv("B24_USER_ID", "")
    if domain and user_id:
        return f"https://{domain}/rest/{user_id}/{token}/"
    return BITRIX_WEBHOOK


BITRIX_WEBHOOK_TASK = _resolve_task_webhook()


def _post(method: str, payload: dict, timeout: int = 15, webhook: str = "") -> dict:
    webhook = webhook or BITRIX_WEBHOOK
    if not webhook:
        raise RuntimeError("BITRIX_WEBHOOK is not configured")
    url = webhook.rstrip("/") + "/" + method
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Bitrix puts the actual error/error_description in the response
        # body even on 4xx/5xx — urlopen only gives us the bare status
        # unless we read the body off the error object ourselves.
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            detail = parsed.get("error_description", parsed.get("error", body))
        except json.JSONDecodeError:
            detail = body
        raise RuntimeError(f"Bitrix24 {method} HTTP {e.code}: {detail}") from e
    if "error" in result:
        raise RuntimeError(f"Bitrix24 {method} error: {result.get('error_description', result['error'])}")
    return result


def create_complaint_task(*, title: str, description: str, responsible_id: int, deal_id: int | None = None,
                           auditors: list[int] | None = None) -> int:
    fields = {
        "TITLE": title,
        "DESCRIPTION": description,
        "RESPONSIBLE_ID": responsible_id,
        "DEADLINE": (datetime.utcnow() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S"),
        "PRIORITY": "2",
    }
    if deal_id:
        fields["UF_CRM_TASK"] = [f"D_{deal_id}"]
    if auditors:
        # "В копію" — AUDITORS get notified and can see the task, without
        # being the one it's assigned to (RESPONSIBLE_ID stays the
        # department head).
        fields["AUDITORS"] = auditors
    result = _post("tasks.task.add", {"fields": fields}, webhook=BITRIX_WEBHOOK_TASK)
    return result["result"]["task"]["id"]


# Рахунки (invoices) are a Bitrix24 Smart Process, entityTypeId=31 — same
# entity etl_zv's crm.item.list pulls into crm.fact_invoices (see db.py's
# PAID_INVOICE_STAGES / get_invoices), confirmed by the "DT31_1:..." stage
# ID prefix. No existing write path touches invoices at all — documents_bot's
# old receipt flow only ever saved to Google Drive + a Telegram ping, never
# Bitrix — so this is new.
INVOICE_ENTITY_TYPE_ID = 31

# Custom "file" UF field added directly on the Рахунки (invoices) Smart
# Process item — the receipt goes here in addition to Bitrix Disk, so it's
# visible right on the invoice card itself, not just linked from a comment.
INVOICE_RECEIPT_FIELD = "ufCrmSmartInvoiceReceipt"


def add_invoice_comment(invoice_id: int, comment: str) -> int:
    result = _post(
        "crm.timeline.comment.add",
        {"fields": {"ENTITY_TYPE_ID": INVOICE_ENTITY_TYPE_ID, "ENTITY_ID": invoice_id, "COMMENT": comment}},
    )
    return result["result"]


def set_invoice_receipt_file(invoice_id: int, filename: str, content: bytes) -> None:
    encoded = base64.b64encode(content).decode("ascii")
    _post(
        "crm.item.update",
        {
            "entityTypeId": INVOICE_ENTITY_TYPE_ID,
            "id": invoice_id,
            "fields": {INVOICE_RECEIPT_FIELD: [filename, encoded]},
        },
    )
