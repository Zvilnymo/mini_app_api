"""
AI chat assistant — client-facing, persistent, available any time of day.

Grounded on two sources, both provided fresh on every message rather than
baked into a fine-tune: the company's own FAQ knowledge base
(docbot.faq_entries, ~417 real vetted Q&A pairs) via embedding similarity
search, and the client's live case data (same case/payments shape /api/me
already computes) passed in by main.py.

One OpenAI call per message classifies AND answers in one shot (JSON
response format, not a separate classification pass, not tool-calling) —
simpler to reason about and one round-trip instead of two:
  - case_status  — question about the client's own case; answered from the
                    case summary passed in.
  - faq          — general bankruptcy/debt/collector question; answered
                    from the retrieved FAQ snippets, nothing invented.
  - off_topic    — unrelated to bankruptcy/debt/the case (job hunting,
                    military registration, etc.) — the model's own reply is
                    discarded and replaced with a fixed message below, so
                    there is no chance of it improvising an answer anyway.
  - emotional    — distress, shame, family conflict — warm supportive tone,
                    no legal specifics, always escalated.
  - uncertain    — no good FAQ match; honest "I'll check and get back to
                    you" rather than a guess.

off_topic/emotional/uncertain all escalate: a Bitrix task lands on the
person at .../personal/user/2627/, with .../personal/user/594/ (Тетяна
Ніконова, already the support-department responsible elsewhere — see
complaints.py) in copy. Escalations are rate-limited per client+category
(see db.get_recent_escalation) so a client venting for ten messages in a
row doesn't create ten tasks.

Every message is permanently stored in docbot.chat_messages (nothing is
ever deleted) — but sending the *entire* history to OpenAI on every turn
would get slower and more expensive as a conversation grows over months.
Instead the model always sees the last MAX_RAW_HISTORY messages verbatim
plus a running summary (docbot.clients.chat_summary) of everything older,
folded in a few sentences at a time as the conversation outgrows the
window — so context is never silently lost, just compressed.
"""
from __future__ import annotations

import json
import logging
import math
import os

from openai import OpenAI

from . import bitrix, db

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

EMBEDDING_MODEL = "text-embedding-3-small"
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")

# Same two people every complaint already CCs (see complaints.py) — 2627 is
# new, specific to AI-chat escalations, confirmed via their Bitrix profile
# URL (.../personal/user/2627/).
ESCALATION_RESPONSIBLE_ID = 2627
ESCALATION_CC_IDS = [594]

OFF_TOPIC_REPLY = (
    "Це питання не стосується Вашої справи про банкрутство, тож, на жаль, я не зможу з ним допомогти. "
    "Я передам його менеджеру — він зв'яжеться з Вами найближчим часом."
)

CATEGORY_LABELS = {
    "off_topic": "Питання поза темою банкрутства",
    "emotional": "Клієнту потрібна підтримка",
    "uncertain": "AI не знайшов відповіді у базі знань",
}

# How many most-recent messages stay verbatim in every prompt. Once the
# conversation has grown MAX_RAW_HISTORY + SUMMARY_TRIGGER_SLACK messages,
# the oldest excess gets folded into chat_summary — the slack just avoids
# re-summarizing on every single new message once past the window.
MAX_RAW_HISTORY = 24
SUMMARY_TRIGGER_SLACK = 10


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def _embed(text: str) -> list[float]:
    result = _client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return result.data[0].embedding


def ensure_faq_embeddings(conn) -> int:
    """Computes embeddings for any docbot.faq_entries rows that don't have
    one yet — called on startup and periodically, so a freshly loaded FAQ
    table (or one edited later) self-populates without a manual step."""
    if _client is None:
        logger.warning("OPENAI_API_KEY not set — skipping FAQ embedding")
        return 0
    rows = db.get_faq_entries_missing_embedding(conn)
    for row in rows:
        try:
            embedding = _embed(f"{row['question']}\n{row['answer']}")
            db.save_faq_embedding(conn, row["id"], embedding)
        except Exception as e:
            logger.error(f"Failed to embed faq_entries.id={row['id']}: {e}")
    if rows:
        logger.info(f"Computed embeddings for {len(rows)} FAQ entries")
    return len(rows)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# In-process cache — reloaded on startup and by the same periodic scheduler
# that reruns ensure_faq_embeddings, not on every request (417 rows loaded
# fresh every message would be wasteful for data that rarely changes).
_faq_cache: list[dict] | None = None


def reload_faq_cache(conn) -> None:
    global _faq_cache
    rows = db.get_all_faq_entries(conn)
    _faq_cache = [{"id": r["id"], "question": r["question"], "answer": r["answer"], "embedding": r["embedding"]} for r in rows]
    logger.info(f"FAQ cache loaded: {len(_faq_cache)} entries")


def top_faq_matches(query_embedding: list[float], k: int = 5) -> list[dict]:
    if not _faq_cache:
        return []
    scored = [(entry, _cosine(query_embedding, entry["embedding"])) for entry in _faq_cache]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [entry for entry, score in scored[:k]]


# ---------------------------------------------------------------------------
# Case summary — turns the same case/payments shape /api/me returns into a
# short block of readable Ukrainian text for the prompt.
# ---------------------------------------------------------------------------

def build_case_summary(client: dict, case: dict | None, payments: dict | None, days_active: int | None) -> str:
    if not case:
        return f"Клієнт {client['full_name']} ще не має відкритої справи в CRM (можливо, щойно звернувся)."

    lines = [
        f"Ім'я клієнта: {client['full_name']}",
        f"Поточний етап справи: {case['step_label']} (крок {case['step']} з {len(case['steps'])})",
    ]
    if case.get("current_stage_name"):
        lines.append(f"Стадія в CRM: {case['current_stage_name']}")
    if days_active is not None:
        lines.append(f"У процесі: {days_active} днів")
    if payments:
        lines.append(f"Оплачено: {payments['paid_total']} грн, залишок до оплати: {payments['unpaid_total']} грн")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Classification + reply
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Ти — асистент підтримки клієнтів юридичної компанії "Звільнимо", яка супроводжує людей у процедурі банкрутства фізичних осіб в Україні. Спілкуйся тепло, просто, по-людськи, короткими зрозумілими реченнями, без канцеляризмів.

Звертайся до клієнта офіційно, на "Ви" (займенник "Ви"/"Вам"/"Вас" — завжди з великої літери), за іменем і прізвищем (з даних про справу, поле "Ім'я клієнта"). Використовуй ім'я й прізвище в привітанні та коли це природно звучить у відповіді — не встав його в кожне речення. Ніколи не переходь на "ти".

Тобі дається:
1. Дані про поточну справу клієнта.
2. Витяги з бази знань компанії про банкрутство — використовуй ТІЛЬКИ ці дані для фактів, нічого від себе не вигадуй (жодних сум, строків, назв документів, яких там немає).
3. Останні повідомлення розмови — уважно відстежуй, про яку саме тему йдеться. Якщо клієнт запитує коротко ("а скільки це коштує?", "а коли?", "а чому?") — це продовження ПОПЕРЕДНЬОЇ теми розмови, а не нове окреме питання. Ніколи не підміняй тему, про яку щойно запитав клієнт, іншою, навіть спорідненою.

ВАЖЛИВО, часта плутанина: вартість НАШИХ юридичних послуг (орієнтовно 40 000 грн, залежить від суми боргу) і оплата послуг арбітражного керуючого (АК) — це ДВІ РІЗНІ речі. АК — окрема, встановлена законом виплата (5 прожиткових мінімумів за кожен місяць його роботи, авансом за 3 місяці; деякі АК погоджуються на знижку до 50% і розстрочку). Якщо клієнт щойно питав про АК і далі запитує "скільки коштує" — відповідай саме про оплату АК, а не про загальну вартість послуг компанії.

Категорія потрібна лише для того, щоб вирішити, чи передавати розмову менеджеру — вона НЕ обмежує, якими джерелами користуватись для відповіді. Для "case_status" і "faq" завжди використовуй ОБИДВА джерела разом, якщо це доречно: наприклад, "скільки триватиме МОЯ справа" — скажи поточний етап з даних про справу І типовий термін з бази знань, а не тільки щось одне.

НІКОЛИ не давай порожніх відповідей-відмовок на кшталт "я уточню" чи "дам знати пізніше", якщо в наданих матеріалах (дані про справу + база знань) є хоч якась релевантна інформація — завжди спочатку спробуй відповісти по суті з того, що є. Категорія "uncertain" — це справді крайній випадок, коли І дані про справу, І база знань не містять нічого по темі питання.

Якщо нове повідомлення клієнта коротке або схоже на реакцію на ТВОЮ попередню відповідь (наприклад "у кого?", "що?", "не зрозумів", "де?") — це прохання уточнити саме ТВОЮ попередню відповідь, а не нове окреме питання. У такому разі поясни точніше або дай конкретнішу відповідь — ніколи не повторюй ту саму фразу майже дослівно.

Визнач категорію нового повідомлення клієнта і дай відповідь:

- "case_status" — питання про стан ЙОГО справи (етап, оплати, документи, наступні кроки, терміни).
- "faq" — загальне питання про банкрутство/борги/колекторів/арбітражного керуючого/суд.
- "off_topic" — питання, що НЕ стосується банкрутства, боргів чи справи клієнта (робота, мобілізація/бронь, особисті теми, будь-що стороннє). Не намагайся відповісти по суті.
- "emotional" — клієнт демонструє тривогу, страх, сором, вигорання, конфлікт з рідними. Відповідай тепло і підтримуюче, без юридичних деталей.
- "uncertain" — і дані про справу, і база знань справді не містять нічого релевантного по темі питання.

Поверни ЛИШЕ JSON без жодного тексту навколо: {"category": "case_status|faq|off_topic|emotional|uncertain", "reply": "..."}"""


def classify_and_reply(
    *, case_summary: str, faq_matches: list[dict], history: list[dict], user_message: str, prior_summary: str | None = None,
) -> dict:
    if _client is None:
        return {"category": "uncertain", "reply": "Наразі не можу відповісти — спробуйте, будь ласка, трохи пізніше."}

    faq_block = "\n\n".join(f"Q: {m['question']}\nA: {m['answer']}" for m in faq_matches) or "(немає релевантних записів)"

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.append({
        "role": "system",
        "content": f"Дані про справу клієнта:\n{case_summary}\n\nБаза знань (найбільш релевантні записи):\n{faq_block}",
    })
    if prior_summary:
        messages.append({"role": "system", "content": f"Резюме більш ранньої частини цієї розмови:\n{prior_summary}"})
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    response = _client.chat.completions.create(
        model=CHAT_MODEL,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.4,
    )
    try:
        parsed = json.loads(response.choices[0].message.content)
        category = parsed.get("category", "uncertain")
        reply = parsed.get("reply", "").strip()
    except (json.JSONDecodeError, AttributeError):
        category, reply = "uncertain", ""

    if category not in ("case_status", "faq", "off_topic", "emotional", "uncertain"):
        category = "uncertain"
    if category == "off_topic" or not reply:
        # Never trust the model's own wording for off_topic (or an empty
        # reply for any category) — a fixed message guarantees it can't
        # improvise an answer it was told not to give.
        reply = OFF_TOPIC_REPLY if category == "off_topic" else "Дайте мені трохи часу — уточню це і повернуся з відповіддю до Вас."

    return {"category": category, "reply": reply}


# ---------------------------------------------------------------------------
# Escalation — Bitrix task, rate-limited per client+category
# ---------------------------------------------------------------------------

def escalate_if_needed(conn, *, client: dict, category: str, user_message: str) -> None:
    if category not in CATEGORY_LABELS:
        return
    if db.get_recent_escalation(conn, client["id"], category):
        # Already escalated this category recently — don't spam a new
        # Bitrix task for every message of the same kind in a row.
        return
    try:
        task_id = bitrix.create_complaint_task(
            title=f"AI-чат: {CATEGORY_LABELS[category]} — {client['full_name']}",
            description=(
                f"👤 {client['full_name']}\n📱 {client['phone']}\n\n"
                f"Повідомлення клієнта:\n{user_message}\n\n"
                f"Категорія: {category}"
            ),
            responsible_id=ESCALATION_RESPONSIBLE_ID,
            auditors=ESCALATION_CC_IDS,
        )
    except Exception as e:
        logger.error(f"Failed to create escalation task for client {client['id']}: {e}")
        task_id = None
    db.log_chat_escalation(conn, client["id"], category, task_id)


# ---------------------------------------------------------------------------
# Long-term memory — fold anything older than MAX_RAW_HISTORY into a running
# summary instead of either resending it forever or silently dropping it.
# ---------------------------------------------------------------------------

def _fold_older_history_into_summary(conn, client_id: int) -> None:
    if _client is None:
        return
    total = db.count_chat_messages(conn, client_id)
    if total <= MAX_RAW_HISTORY + SUMMARY_TRIGGER_SLACK:
        return
    to_fold = total - MAX_RAW_HISTORY
    older = db.get_chat_messages_range(conn, client_id, offset=0, limit=to_fold)
    if not older:
        return

    prior_summary = db.get_chat_summary(conn, client_id) or ""
    transcript = "\n".join(f"{'Клієнт' if m['role'] == 'user' else 'Асистент'}: {m['content']}" for m in older)
    prompt = (
        "Стисло підсумуй цю частину розмови клієнта з юридичним асистентом (2-5 речень, українською, "
        "тільки факти й домовленості, без вступних фраз)."
        + (f"\n\nПопереднє резюме:\n{prior_summary}" if prior_summary else "")
        + f"\n\nНова частина розмови, яку треба додати до резюме:\n{transcript}"
    )
    try:
        response = _client.chat.completions.create(
            model=CHAT_MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.2,
        )
        db.set_chat_summary(conn, client_id, response.choices[0].message.content.strip())
    except Exception as e:
        logger.error(f"Failed to summarize chat history for client {client_id}: {e}")


def handle_message(conn, *, client: dict, case: dict | None, payments: dict | None, days_active: int | None, user_message: str) -> dict:
    db.add_chat_message(conn, client["id"], "user", user_message)
    _fold_older_history_into_summary(conn, client["id"])

    history = [{"role": h["role"], "content": h["content"]} for h in db.get_chat_history(conn, client["id"], limit=MAX_RAW_HISTORY)][:-1]
    prior_summary = db.get_chat_summary(conn, client["id"])

    case_summary = build_case_summary(client, case, payments, days_active)
    # A short follow-up ("а скільки це коштує?") carries no topic on its own
    # — embedding it alone risks matching the wrong FAQ entry entirely (e.g.
    # the company's own fee instead of the arbitration manager's, when the
    # actual topic was set two turns ago). Folding in the last couple of
    # turns lets the embedding capture what "це"/"воно" actually refers to.
    retrieval_query = "\n".join(h["content"] for h in history[-4:] + [{"content": user_message}])
    query_embedding = _embed(retrieval_query) if _client else []
    faq_matches = top_faq_matches(query_embedding, k=5) if query_embedding else []

    result = classify_and_reply(
        case_summary=case_summary, faq_matches=faq_matches, history=history,
        user_message=user_message, prior_summary=prior_summary,
    )

    db.add_chat_message(conn, client["id"], "assistant", result["reply"], category=result["category"])
    escalate_if_needed(conn, client=client, category=result["category"], user_message=user_message)

    return result
