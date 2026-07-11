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
