"""
Minimal live Bitrix24 REST client — only what mini_app_api needs to write
back to Bitrix (creating a complaint task). All read-heavy case/payment data
comes from the crm.* warehouse in db.py instead; this module exists solely
for the one write path that has no warehouse equivalent.

Uses stdlib urllib (no new dependency), matching documents_bot's own
_bitrix_post convention in telegram_bot.py.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta

BITRIX_WEBHOOK = os.getenv("BITRIX_WEBHOOK")


def _post(method: str, payload: dict, timeout: int = 15) -> dict:
    if not BITRIX_WEBHOOK:
        raise RuntimeError("BITRIX_WEBHOOK is not configured")
    url = BITRIX_WEBHOOK.rstrip("/") + "/" + method
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if "error" in result:
        raise RuntimeError(f"Bitrix24 {method} error: {result.get('error_description', result['error'])}")
    return result


def create_complaint_task(*, title: str, description: str, responsible_id: int, deal_id: int | None = None) -> int:
    fields = {
        "TITLE": title,
        "DESCRIPTION": description,
        "RESPONSIBLE_ID": responsible_id,
        "DEADLINE": (datetime.utcnow() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S"),
        "PRIORITY": "2",
    }
    if deal_id:
        fields["UF_CRM_TASK"] = [f"D_{deal_id}"]
    result = _post("tasks.task.add", {"fields": fields})
    return result["result"]["task"]["id"]
