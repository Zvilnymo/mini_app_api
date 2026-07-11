"""
Maps the client's CRM records (funnel 0 deal + funnel 1 pre-court + funnel 2
court, see db.py) onto the 8-step stepper from the Figma/PDF design:

  1 Перша консультація   2 Надання даних        3 Погодження договору
  4 Оплата авансу        5 Початок робіт         6 Передсудовий період
  7 Судовий період       8 Післясудовий період

This is a first-pass mapping built from real stage names/order pulled from
crm.dim_deal_stages on 2026-07-11 (see db.py docstring) — not signed off by
the business yet. Treat step boundaries as adjustable, not fixed.
"""
from __future__ import annotations

STEP_LABELS = [
    "Перша консультація",
    "Надання даних",
    "Погодження договору",
    "Оплата авансу",
    "Початок робіт",
    "Передсудовий період",
    "Судовий період",
    "Післясудовий період",
]

_COURT_FINAL_STAGES = {"C2:UC_YRXWP0", "C2:WON"}
_PRE_COURT_LATE_STAGES = {"C1:UC_G2I1CF", "C1:UC_G3VNH9", "C1:UC_LRQ9FP", "C1:UC_SX3XC2", "C1:WON"}
_DEAL_CONTRACT_REVIEW_STAGES = {"UC_KCI6RH", "UC_93JK28", "UC_5LCQJK", "UC_ITJY47"}


def compute_step(deal: dict | None, pre_court_deal: dict | None, court_deal: dict | None) -> int:
    """Returns 1-based step index into STEP_LABELS."""
    if court_deal:
        return 8 if court_deal["stage_id"] in _COURT_FINAL_STAGES else 7

    if pre_court_deal:
        return 6 if pre_court_deal["stage_id"] in _PRE_COURT_LATE_STAGES else 5

    if deal:
        stage_id = deal["stage_id"]
        if stage_id == "WON":
            return 5
        if stage_id == "EXECUTING":
            return 4
        if stage_id in _DEAL_CONTRACT_REVIEW_STAGES:
            return 3
        return 2

    return 1
