"""
schema.py — типи даних агента підтримки.

Кордон контракту між шарами: вхід (повідомлення клієнта + що ми про нього
знаємо) → рішення триажу → результат (відповідь + куди ескалювати).

Все на stdlib (dataclasses + enum), як решта проєкту. Жодних зовнішніх залежностей.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


class Decision(str, enum.Enum):
    """Три виходи триажу — це і є Олегова «фільтрація мусору».

    ANSWER          — Аішка відповідає сама, юрист не потрібен.
    ANSWER_AND_FLAG — Аішка відповідає (заспокоює/дає крок), але паралельно
                      кладе картку спеціалісту: тон тривожний або тема на межі.
    ESCALATE        — Аішка НЕ відповідає по суті (тільки «передаю юристу»):
                      потрібне живе юридичне судження / дані справи / людина.
    """

    ANSWER = "answer"
    ANSWER_AND_FLAG = "answer_and_flag"
    ESCALATE = "escalate"


class Channel(str, enum.Enum):
    TELEGRAM = "telegram"
    VIBER = "viber"
    CABINET = "cabinet"   # вбудований чат у клієнтській апці (М6)


@dataclass
class IncomingMessage:
    """Одне вхідне повідомлення клієнта (після підписання договору, стадія «поддержка»)."""

    text: str
    client_id: str = "anon"          # непрозорий ключ (НЕ імʼя): звʼязок з CRM живе окремо
    channel: Channel = Channel.CABINET
    has_media: bool = False          # прикріплений файл/фото (виписка, скрін тощо)
    ts: Optional[str] = None         # ISO-час, опційно


@dataclass
class ClientContext:
    """Що агент знає про справу цього клієнта на момент відповіді.

    У проді наповнюється read-only з CRM/сховища (Bitrix, warehouse) — див. М6.
    Поки конектора нема — `known=False`, і будь-яке персональне питання про
    стан справи автоматично йде в ескалацію (агент не вигадує факти — правило CLAUDE #2).
    """

    known: bool = False
    stage: Optional[str] = None          # напр. "збір документів", "подано в суд", "провадження відкрито"
    lawyer_name: Optional[str] = None
    missing_documents: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)  # довільні відомі факти по справі


@dataclass
class Escalation:
    """Картка спеціалісту, коли агент пасує або страхується."""

    to: str          # роль: "юрист", "служба турботи", "психолог", "арбітражний"
    reason: str      # чому ескалюємо (людською мовою, для внутрішньої стрічки)
    priority: str = "normal"   # normal | high  (high — гнів/загроза/суїцид-ризик)


@dataclass
class AgentResult:
    """Що агент віддає нагору (в апку/бота)."""

    decision: Decision
    answer: Optional[str]                 # текст клієнту; None лише коли decision=ESCALATE без заглушки
    category: str = "other"               # тема (case_status, documents, ...)
    confidence: float = 0.0               # 0..1 впевненість, що відповідь коректна
    sources: list[str] = field(default_factory=list)   # id записів FAQ, на які спираємось
    escalation: Optional[Escalation] = None
    used_llm: bool = False                # відповідь згенерована LLM (True) чи шаблоном (False)
    signals: dict[str, Any] = field(default_factory=dict)  # діагностика триажу (для тестів/логів)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["decision"] = self.decision.value
        return d
