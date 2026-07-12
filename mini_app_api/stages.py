"""
Maps the client's CRM records (funnel 0 deal + funnel 1 pre-court + funnel 2
court, see db.py) onto the 11-step roadmap shown in the Cabinet stepper.

Confirmed against live crm.dim_deal_stages (funnel_id column, added
2026-07-12) rather than guessed — every stage_id below is cross-checked
against the real stage name in that funnel. Two open questions from the
business were resolved directly with the user on 2026-07-12:
  - "Підпис договору" = funnel 0 deal reaching stage_id "WON" (deal closed
    successfully in the sales funnel, right before a funnel 1 deal gets
    created off the back of it).
  - "Процес попереднього слухання" (which duplicated C2:NEW) was merged
    into "Попереднє засідання" — there's no separate real stage for it.

  1 Консультація                        — no CRM match at all yet
  2 Надання даних                       — funnel 0 deal, not yet WON
  3 Підпис договору                     — funnel 0 deal, stage_id == WON
  4 Передсудовий період                 — any funnel 1 (C1:) deal
  5 Попереднє засідання                 — funnel 2, stage_id == C2:NEW (or
                                           C2:UC_D0NOU4 "Залишено без руху",
                                           which has no dedicated step)
  6 Провадження відкрите                — C2:UC_74W40L
  7 Розгляд вимог кредиторів            — C2:UC_X9CE8S
  8 Перегляд плану реструктуризації     — C2:UC_SOU3XU
  9 План реструктуризації погоджено     — C2:UC_Z9QW49
 10 План реструктуризації затверджено   — C2:UC_BCGRYT
 11 Ліквідація                          — C2:UC_YRXWP0 (or C2:WON "Успіх",
                                           the funnel's own terminal stage)

Note rows 7/8 above are in real stage-sort order (X9CE8S=40 before
SOU3XU=50) — the opposite order was in an earlier draft of this mapping.
"""
from __future__ import annotations

STEP_LABELS = [
    "Консультація",
    "Надання даних",
    "Підпис договору",
    "Передсудовий період",
    "Попереднє засідання",
    "Провадження відкрите",
    "Розгляд вимог кредиторів",
    "Перегляд плану реструктуризації",
    "План реструктуризації погоджено",
    "План реструктуризації затверджено",
    "Ліквідація",
]

_COURT_STAGE_STEP = {
    "C2:NEW": 5,  # Попереднє засідання
    "C2:UC_D0NOU4": 5,  # Залишено без руху — no dedicated step, grouped with the preceding one
    "C2:UC_74W40L": 6,  # Провадження відкрите
    "C2:UC_X9CE8S": 7,  # Розгляд вимог кредиторів
    "C2:UC_SOU3XU": 8,  # Перегляд плану реструктуризації
    "C2:UC_Z9QW49": 9,  # План реструктуризації погоджено
    "C2:UC_BCGRYT": 10,  # План реструктуризації затверджено
    "C2:UC_YRXWP0": 11,  # Ліквідація (Списання боргів)
    "C2:WON": 11,  # Успіх — treated as the same terminal step as Ліквідація
}


def compute_step(deal: dict | None, pre_court_deal: dict | None, court_deal: dict | None) -> int:
    """Returns 1-based step index into STEP_LABELS."""
    if court_deal:
        return _COURT_STAGE_STEP.get(court_deal["stage_id"], 5)

    if pre_court_deal:
        return 4

    if deal:
        return 3 if deal["stage_id"] == "WON" else 2

    return 1
