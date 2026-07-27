"""
bridge.py — склейка вендорного агента з конвеєром чату застосунку.

Що робить:
1. `build_system_prompt()` — системний промпт із трьох скілів (юрист/психолог/
   гуморист) + правила проактивного тону, ЗБЕРІГАЮЧИ доменні уточнення, які вже
   були вистраждані в цьому застосунку (плутанина «АК vs вартість наших послуг»,
   короткі уточнення продовжують попередню тему, звертання на «Ви» тощо).
2. `pretriage()` — ДЕТЕРМІНОВАНИЙ запобіжник ПЕРЕД зверненням до LLM. Спрацьовує
   тільки на двох критичних класах, де не можна залежати від моделі:
     • розпач/криза → готова відповідь з контактом штатного психолога;
     • гнів/претензія/загроза → бот не сперечається, віддає живій людині.
   Усе інше повертає None — відповідає LLM за скілами.
3. `is_closed_reply()` / `proactive_fallback()` — анти-«закрита відповідь».
   Пряма вимога Олега: «так», «добре», «я уточню і повернусь» — заборонені.
4. `offline_reply()` — деградація без LLM: замість заглушки віддаємо проактивну
   заготовку з `faq.json`, щоб клієнт не лишався ні з чим.

Категорії лишаються сумісними з наявними (`case_status`, `faq`, `off_topic`,
`emotional`, `uncertain`) — колонка `docbot.chat_messages.category` вільна від
CHECK-обмежень (перевірено на живій БД 26.07), тож два нові значення
(`distress`, `complaint`) безпечні й потрібні для правильної ескалації.
"""
from __future__ import annotations

import json
import os
import re

from . import skills
from .schema import ClientContext, Decision, IncomingMessage
from .triage import triage as _triage

# ── Категорії ─────────────────────────────────────────────────────────────
CATEGORY_CASE_STATUS = "case_status"
CATEGORY_FAQ = "faq"
CATEGORY_OFF_TOPIC = "off_topic"
CATEGORY_EMOTIONAL = "emotional"
CATEGORY_UNCERTAIN = "uncertain"
# Нові: розпач і претензія. Раніше «це розвод, поверніть гроші» не ескалювалось
# узагалі (падало в faq/case_status) — тепер це окремий клас із передачею людині.
CATEGORY_DISTRESS = "distress"
CATEGORY_COMPLAINT = "complaint"

CATEGORIES = (
    CATEGORY_CASE_STATUS, CATEGORY_FAQ, CATEGORY_OFF_TOPIC, CATEGORY_EMOTIONAL,
    CATEGORY_UNCERTAIN, CATEGORY_DISTRESS, CATEGORY_COMPLAINT,
)

_FAQ_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faq.json")


def _faq_entry(entry_id: str) -> dict:
    try:
        with open(_FAQ_PATH, encoding="utf-8") as f:
            for e in json.load(f).get("entries", []):
                if e.get("id") == entry_id:
                    return e
    except (OSError, ValueError):
        pass
    return {}


# ── 1. Системний промпт ───────────────────────────────────────────────────
# Доменні уточнення, вистраждані в цьому застосунку — НЕ ВТРАЧАЄМО їх при
# заміні мозку (саме тут жила головна плутанина клієнтів).
_APP_DOMAIN_NOTES = """\
[ДОМЕННІ УТОЧНЕННЯ ЦЬОГО ЗАСТОСУНКУ]
• Звертайся до клієнта на «Ви» (завжди з великої: Ви/Вам/Вас), за іменем із
  даних справи. Ім'я — у привітанні й там, де звучить природно, не в кожному реченні.
• ЧАСТА ПЛУТАНИНА: вартість НАШИХ юридичних послуг (орієнтовно 40 000 грн,
  залежить від суми боргу) і оплата арбітражного керуючого (АК) — це ДВІ РІЗНІ
  речі. АК — окрема виплата за законом (5 прожиткових мінімумів за кожен місяць
  роботи, авансом за 3 місяці; частина АК погоджується на знижку до 50% і
  розстрочку). Якщо клієнт щойно питав про АК і далі пише «а скільки коштує» —
  відповідай саме про АК, а не про загальну вартість послуг компанії.
• Коротке повідомлення («а скільки це коштує?», «а коли?», «у кого?», «не
  зрозумів») — це ПРОДОВЖЕННЯ попередньої теми або прохання уточнити ТВОЮ
  попередню відповідь, а не нове питання. Не підміняй тему і не повторюй ту саму
  фразу дослівно — поясни конкретніше.
• Для «case_status» і «faq» використовуй ОБИДВА джерела разом, якщо доречно:
  наприклад «скільки триватиме МОЯ справа» — назви поточний етап із даних справи
  І типовий строк із бази знань.
• Факти бери ТІЛЬКИ з наданих даних справи та бази знань. Жодних сум, строків,
  назв документів, яких там немає.
"""

_OUTPUT_CONTRACT = """\
[ЩО ПОВЕРНУТИ]
Визнач категорію нового повідомлення клієнта і дай відповідь:
• "case_status" — питання про стан ЙОГО справи (етап, оплати, документи, кроки, строки).
• "faq" — загальне питання про банкрутство/борги/колекторів/АК/суд.
• "off_topic" — питання поза темою банкрутства, боргів чи справи (робота,
  мобілізація/бронь, сторонні теми). Не намагайся відповісти по суті.
• "emotional" — тривога, страх, сором, вигорання, конфлікт із рідними. Тепло,
  підтримуюче, без юридичних деталей.
• "uncertain" — і дані справи, І база знань справді не містять нічого по темі.
  Це КРАЙНІЙ випадок: якщо є хоч щось релевантне — відповідай по суті.

Поверни ЛИШЕ JSON без тексту навколо:
{"category": "case_status|faq|off_topic|emotional|uncertain", "reply": "..."}
"""


def build_system_prompt() -> str:
    """Персона + 3 скіли + тон + запобіжники + доменні уточнення + контракт."""
    return "\n\n".join(
        block.strip() for block in (
            skills.PERSONA,
            skills.LAWYER_SKILL,
            skills.PSYCHOLOGIST_SKILL,
            skills.HUMORIST_SKILL,
            skills.TONE_RULES,
            skills.GUARDRAILS,
            _APP_DOMAIN_NOTES,
            _OUTPUT_CONTRACT,
        )
    )


# ── 2. Детермінований пре-триаж (запобіжник поверх LLM) ───────────────────
def pretriage(user_message: str, *, case_known: bool = False) -> dict | None:
    """Критичні класи, які не можна довіряти моделі. None → відповідає LLM.

    Повертає {"category", "reply", "escalate", "priority"}.
    """
    outcome = _triage(
        IncomingMessage(text=user_message),
        ClientContext(known=case_known),
        faq_hit=False,
    )

    # Розпач/криза — контакт психолога має бути в тексті ЗАВЖДИ, незалежно від
    # того, чи жива зараз модель і що вона вирішила.
    if outcome.category == "distress":
        entry = _faq_entry("psychologist")
        reply = entry.get("proactive_answer") or (
            "Те, що Ви відчуваєте — нормально, і Ви не маєте нести це самі 💛 "
            "У компанії є штатний психолог, Анастасія Петрівна — з нею можна "
            "поговорити безкоштовно за номером +380500360991. Будь ласка, "
            "наберіть її, коли буде важко. Вашу справу ми тримаємо. Бережіть себе 🤍"
        )
        return {"category": CATEGORY_DISTRESS, "reply": reply,
                "escalate": True, "priority": "high"}

    # Гнів/претензія/загроза — бот не сперечається і не виправдовується.
    if outcome.decision is Decision.ESCALATE and outcome.category == "complaint":
        return {"category": CATEGORY_COMPLAINT,
                "reply": ("Дякую, що написали відверто — чую Вас, і Ваше звернення "
                          "не залишиться без відповіді. Я вже передаю його керівнику "
                          "напряму: він особисто розбереться в ситуації та зв'яжеться "
                          "з Вами найближчим часом. Мені шкода, що довелося писати про "
                          "це 🤝"),
                "escalate": True, "priority": "high"}

    return None


def escalation_window_minutes(category: str) -> int:
    """Анти-спам ескалацій. Для кризи вікно коротше: людина, яка повернулась
    із тим самим станом за пів дня, має знову потрапити на очі спеціалісту."""
    return 30 if category == CATEGORY_DISTRESS else 120


# ── 3. Анти-«закрита відповідь» ───────────────────────────────────────────
# Пряма скарга Олега, підтверджена корпусом (1236 діалогів): топ-відповіді
# юристів — «Так» ×1076, «добре» ×944, «.» ×445, «ні» ×295.
_CLOSED_EXACT = {
    "так", "так.", "ні", "ні.", "добре", "добре.", "ок", "ок.", "okay", "ok",
    ".", "?", "..", "...", "дякую", "дякую.", "зрозуміло", "зрозуміло.",
    "чекайте", "чекайте.", "пізніше", "потім", "отримали", "отримала", "отримав",
}
# «Порожні обіцянки» — формально ввічливо, а по суті клієнт лишається ні з чим.
_CLOSED_PATTERNS = (
    r"^\s*дайте\s+мені\s+трохи\s+часу",
    r"^\s*я\s+уточню\b.{0,40}$",
    r"^\s*уточню\s+і\s+повернu?сь",
)


def is_closed_reply(text: str | None) -> bool:
    if not text or not text.strip():
        return True
    norm = " ".join(text.split()).strip().lower()
    if norm in _CLOSED_EXACT:
        return True
    if len(norm) < 25 and not any(ch.isdigit() for ch in norm):
        # надто коротка репліка без жодної конкретики
        return True
    return any(re.search(p, norm) for p in _CLOSED_PATTERNS)


def is_pure_courtesy(user_message: str) -> bool:
    """Чиста подяка/привітання без питання.

    Навіщо: у живому прогоні 26.07 модель класифікувала «Дякую вам велике за
    допомогу!» як "emotional" — а ця категорія ескалюється, тобто на кожне
    «дякую» підтримці падала б задача в Бітрікс. Ловимо детерміновано.
    """
    outcome = _triage(IncomingMessage(text=user_message), ClientContext(), faq_hit=False)
    if outcome.category != "greeting_thanks":
        return False
    return not outcome.signals.get("is_question") and len(user_message.strip()) <= 120


# Фрази, якими модель обіцяє клієнту передати питання людині. Якщо вона так
# сказала — ескалація має статись НАСПРАВДІ, інакше ми збрехали клієнту.
# (живий прогін 26.07: відповідь «я вже помічаю ваше звернення для юриста»
# пішла з категорією "faq", тобто без жодної задачі в Бітріксі).
_HANDOFF_PROMISE = (
    r"переда(?:м|ю|ла|в)\b", r"передаю\s+(?:його|це|Ваше|ваше)", r"поміча(?:ю|ла)\s+.{0,30}юрист",
    r"познач(?:у|ила)\s+.{0,30}(?:юрист|колег|менеджер)", r"уточню\s+у\s+(?:Вашого|вашого)?\s*юрист",
    r"звʼяжеться\s+з\s+Вами", r"зв'яжеться\s+з\s+Вами", r"колега\s+звʼяжеться",
)


def promises_handoff(reply: str | None) -> bool:
    if not reply:
        return False
    low = " ".join(reply.split()).lower()
    return any(re.search(p, low, flags=re.IGNORECASE) for p in _HANDOFF_PROMISE)


def proactive_fallback(category: str) -> str:
    """Заміна «закритої» відповіді: тепло + чесно + наступний крок."""
    if category == CATEGORY_OFF_TOPIC:
        return ("Це питання поза моєю темою — я супроводжую саме Вашу справу про "
                "банкрутство, тож не хочу відповідати навмання 🙂 Я вже передала його "
                "менеджеру, він зв'яжеться з Вами. А якщо є щось по справі, документах "
                "чи кредиторах — питайте, це якраз до мене 🤝")
    if category == CATEGORY_EMOTIONAL:
        return ("Чую Вас, і це справді непросто — Ви не самі в цьому 💛 Ваша справа в "
                "роботі, ми поруч. Якщо хочете, розкажіть детальніше, що зараз тривожить "
                "найбільше, — розберемо разом і я підкажу, що буде далі.")
    return ("Дуже гарне питання — і саме тому не хочу відповісти Вам приблизно 🙂 "
            "Я вже передала його Вашому юристу, він уточнить і ми повернемось до Вас "
            "із конкретикою. Якщо тим часом виникне щось іще — пишіть, я поруч 🤝")


# ── 4. Деградація без LLM ─────────────────────────────────────────────────
_OFFLINE_MAP = {
    "documents": "docs_bank_statements_general",
    "payment_ak": "payment_ak_price",
    "creditors": "creditors_call_forwarding",
    "ecp_signature": "ecp_esign_court",
    "access_app": "cabinet_app",
    "timing": "timing_why_long",
    "greeting_thanks": "greeting_thanks",
}


def offline_reply(user_message: str) -> tuple[str, str]:
    """Коли LLM недоступна — не заглушка, а проактивна заготовка.
    Повертає (category, reply)."""
    outcome = _triage(IncomingMessage(text=user_message), ClientContext(), faq_hit=False)
    entry_id = _OFFLINE_MAP.get(outcome.category)
    if entry_id:
        entry = _faq_entry(entry_id)
        if entry.get("proactive_answer"):
            return CATEGORY_FAQ, entry["proactive_answer"]
    return CATEGORY_UNCERTAIN, proactive_fallback(CATEGORY_UNCERTAIN)
