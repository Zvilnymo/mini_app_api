"""
Complaint departments and routing — mirrors complaint_bot's flow (department
-> ПІБ співробітника -> текст скарги -> Bitrix task), same 5 departments,
but RESPONSIBLE_ID varies by department instead of one fixed ID, and two
people are always CC'd via AUDITORS regardless of department.
"""
from __future__ import annotations

# Always in copy (AUDITORS) on every complaint, confirmed via their Bitrix
# profile URLs (.../personal/user/<id>/):
DIRECTOR_BITRIX_ID = 1  # Олег Болотський — керівник компанії
ALWAYS_CC_IDS = [DIRECTOR_BITRIX_ID, 594]  # + Тетяна Ніконова

DEPARTMENTS = {
    "legal": {"name": "Юридичний відділ", "responsible_id": 601},  # Ростислав Паук
    "support": {"name": "Відділ піклування (Підтримка)", "responsible_id": 594},  # Тетяна Ніконова
    "collectors": {"name": "Служба антиколекторської підтримки", "responsible_id": 601},  # Ростислав Паук
    "pre_court": {"name": "Відділ досудового врегулювання боргів", "responsible_id": 601},  # Ростислав Паук
    "consulting": {"name": "Консультаційний відділ (Помічник Юриста)", "responsible_id": 30},  # Олександр Васильєв
}
