"""
support_agent — «мозок» AI-чату застосунку: 3 скіли + детермінований триаж.

Джерело: модуль `zvilnymo-support/` проєкту «Звільнимо» (ТЗ Олега 23.07.2026 —
юрист + психолог + гуморист, проактивний тон, «фільтр мусору»). Файли
`skills.py`, `triage.py`, `schema.py`, `faq.json` — ВЕНДОРНІ КОПІЇ, тримаємо їх
синхронними з оригіналом вручну (як `prompts.py`/`ai_document_validator.py` тут
уже синхронять з documents_bot). Уся склейка з цим застосунком — у `bridge.py`,
щоб вендорні файли лишались чистими й оновлювались копіюванням.

Чистий stdlib — жодних нових залежностей у requirements.txt.
"""
from .bridge import (
    CATEGORIES,
    CATEGORY_CASE_STATUS,
    CATEGORY_COMPLAINT,
    CATEGORY_DISTRESS,
    CATEGORY_EMOTIONAL,
    CATEGORY_FAQ,
    CATEGORY_OFF_TOPIC,
    CATEGORY_UNCERTAIN,
    build_system_prompt,
    escalation_window_minutes,
    is_closed_reply,
    is_pure_courtesy,
    offline_reply,
    pretriage,
    proactive_fallback,
    promises_handoff,
)

__all__ = [
    "is_pure_courtesy",
    "promises_handoff",
    "CATEGORIES",
    "CATEGORY_CASE_STATUS",
    "CATEGORY_COMPLAINT",
    "CATEGORY_DISTRESS",
    "CATEGORY_EMOTIONAL",
    "CATEGORY_FAQ",
    "CATEGORY_OFF_TOPIC",
    "CATEGORY_UNCERTAIN",
    "build_system_prompt",
    "escalation_window_minutes",
    "is_closed_reply",
    "offline_reply",
    "pretriage",
    "proactive_fallback",
]
