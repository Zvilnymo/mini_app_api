"""
AI chat assistant — client-facing, persistent, available any time of day.

Мозок чату — модуль `support_agent` (ТЗ Олега 23.07.2026): три скіли
(юрист + психолог + гуморист), проактивний тон замість «закритих» відповідей і
детермінований триаж «фільтр мусору». Уся обвʼязка, що вже працювала, лишилась
без змін: збереження кожного повідомлення в `docbot.chat_messages`, згортка
старої історії в `docbot.clients.chat_summary`, пошук по базі знань компанії
(`docbot.faq_entries`, 440 вивірених Q&A з ембеддингами) і ескалація задачею в
Бітрікс.

Grounded on two sources, both provided fresh on every message rather than
baked into a fine-tune: the company's own FAQ knowledge base via embedding
similarity search, and the client's live case data (same case/payments shape
/api/me already computes) passed in by main.py.

Порядок рішення на кожне повідомлення:

  1. ДЕТЕРМІНОВАНИЙ ПРЕ-ТРИАЖ (`support_agent.pretriage`) — без мережі, до LLM.
     Спрацьовує лише на двох класах, де не можна залежати від моделі:
       • "distress"  — розпач/криза: відповідь із контактом штатного психолога
                       формується локально й гарантовано, + ескалація (вікно 30 хв);
       • "complaint" — гнів/претензія/загроза («це розвод, поверніть гроші»):
                       бот не сперечається, віддає живій людині. Раніше такі
                       повідомлення не ескалювались узагалі — падали в faq.
     Решта — None, відповідає LLM.

  2. LLM (один виклик: класифікація + відповідь у JSON) за системним промптом
     із трьох скілів. Категорії ті самі, що й були:
       - case_status — питання про власну справу; з case summary;
       - faq         — загальне питання; з витягів бази знань, нічого від себе;
       - off_topic   — поза темою: відповідь моделі відкидається, ставиться фікс;
       - emotional   — тривога/сором/конфлікт: тепло, без юридичних деталей;
       - uncertain   — справді немає даних: чесно передаємо юристу.

  3. АНТИ-«ЗАКРИТА ВІДПОВІДЬ» (`support_agent.is_closed_reply`) — пост-перевірка.
     «Так», «добре», «.», «дайте мені трохи часу — уточню» замінюються
     проактивним варіантом (емпатія → суть → наступний крок → підтримка).
     Це пряма вимога ТЗ: у корпусі 1236 діалогів такі відповіді — топ-1.

off_topic/emotional/uncertain/distress/complaint ескалюються: задача в Бітрікс
на .../personal/user/2627/, у копії .../personal/user/594/ (Тетяна Ніконова —
та сама, що й у complaints.py). Ескалації обмежені по клієнту+категорії
(db.get_recent_escalation), щоб десять повідомлень поспіль не створили десять
задач; для кризи вікно коротше (30 хв).

Провайдер LLM перемикається змінною середовища CHAT_LLM_PROVIDER:
"openai" (за замовчуванням — те, що вже налаштоване й оплачене на Render) або
"anthropic" (потрібен ANTHROPIC_API_KEY і пакет `anthropic` у requirements).
Ембеддинги бази знань у будь-якому разі рахує OpenAI — вони вже пораховані для
всіх 440 записів, міняти їх провайдера немає причин.
"""
from __future__ import annotations

import json
import logging
import math
import os

from openai import OpenAI

from . import bitrix, db, support_agent

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

EMBEDDING_MODEL = "text-embedding-3-small"
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")

# Провайдер відповіді. За замовчуванням лишається OpenAI — деплой без змін.
CHAT_LLM_PROVIDER = os.getenv("CHAT_LLM_PROVIDER", "openai").strip().lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")

# Same two people every complaint already CCs (see complaints.py) — 2627 is
# new, specific to AI-chat escalations, confirmed via their Bitrix profile
# URL (.../personal/user/2627/).
ESCALATION_RESPONSIBLE_ID = 2627
ESCALATION_CC_IDS = [594]

OFF_TOPIC_REPLY = support_agent.proactive_fallback(support_agent.CATEGORY_OFF_TOPIC)

CATEGORY_LABELS = {
    "off_topic": "Питання поза темою банкрутства",
    "emotional": "Клієнту потрібна підтримка",
    "uncertain": "AI не знайшов відповіді у базі знань",
    # Нові класи з детермінованого пре-триажу — саме вони найважливіші для людини.
    support_agent.CATEGORY_DISTRESS: "🚨 КРИЗОВИЙ СТАН КЛІЄНТА — потрібна жива людина",
    support_agent.CATEGORY_COMPLAINT: "⚠️ Претензія/недовіра клієнта — потрібен керівник",
    # AI сказав клієнту «передам юристу» — значить, задача має реально зʼявитись.
    "promised_handoff": "AI пообіцяв клієнту передати питання спеціалісту",
}

# Категорія, під якою ескалюємо обіцянку передати питання людині.
PROMISED_HANDOFF = "promised_handoff"

# How many most-recent messages stay verbatim in every prompt. Once the
# conversation has grown MAX_RAW_HISTORY + SUMMARY_TRIGGER_SLACK messages,
# the oldest excess gets folded into chat_summary — the slack just avoids
# re-summarizing on every single new message once past the window.
MAX_RAW_HISTORY = 24
SUMMARY_TRIGGER_SLACK = 10

VALID_CATEGORIES = ("case_status", "faq", "off_topic", "emotional", "uncertain")


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
# that reruns ensure_faq_embeddings, not on every request (440 rows loaded
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

# Три скіли + проактивний тон + доменні уточнення застосунку + JSON-контракт.
SYSTEM_PROMPT = support_agent.build_system_prompt()


def _call_anthropic(messages: list[dict]) -> str | None:
    """Claude замість OpenAI, якщо CHAT_LLM_PROVIDER=anthropic. None при будь-якій
    проблемі — викликач тоді відкотиться на OpenAI/офлайн, чат не падає."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("CHAT_LLM_PROVIDER=anthropic, але пакет `anthropic` не встановлено")
        return None
    # Anthropic бере system окремо і не приймає роль "system" у messages.
    system_blocks = [m["content"] for m in messages if m["role"] == "system"]
    convo = [m for m in messages if m["role"] in ("user", "assistant")]
    # ⚠️ temperature НЕ передаємо: новіші моделі Claude (opus-4-8 і далі) його
    # вже не приймають і відповідають 400. Перевірено наживо 26.07.2026.
    try:
        resp = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1200,
            system="\n\n".join(system_blocks),
            messages=convo,
        )
    except Exception as e:
        logger.error(f"Anthropic call failed: {e}")
        return None
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip() or None


def _call_openai(messages: list[dict]) -> str | None:
    if _client is None:
        return None
    try:
        response = _client.chat.completions.create(
            model=CHAT_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.4,
        )
    except Exception as e:
        logger.error(f"OpenAI call failed: {e}")
        return None
    return response.choices[0].message.content


def classify_and_reply(
    *, case_summary: str, faq_matches: list[dict], history: list[dict], user_message: str, prior_summary: str | None = None,
) -> dict:
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

    raw = _call_anthropic(messages) if CHAT_LLM_PROVIDER == "anthropic" else None
    if raw is None:
        raw = _call_openai(messages)

    if raw is None:
        # Жодна модель не відповіла — не заглушка, а проактивна заготовка.
        category, reply = support_agent.offline_reply(user_message)
        return {"category": category, "reply": reply}

    try:
        parsed = json.loads(raw)
        category = parsed.get("category", "uncertain")
        reply = (parsed.get("reply") or "").strip()
    except (json.JSONDecodeError, AttributeError, TypeError):
        category, reply = "uncertain", ""

    if category not in VALID_CATEGORIES:
        category = "uncertain"

    # Проста подяка/привітання — це НЕ емоційна криза. Без цієї перевірки кожне
    # «дякую вам велике» створювало б задачу підтримці (спіймано в живому
    # прогоні 26.07: модель віддала category="emotional").
    if category == "emotional" and support_agent.is_pure_courtesy(user_message):
        category = "faq"

    # Never trust the model's own wording for off_topic — a fixed message
    # guarantees it can't improvise an answer it was told not to give.
    if category == "off_topic":
        reply = OFF_TOPIC_REPLY
    elif support_agent.is_closed_reply(reply):
        # Головна вимога ТЗ: «закрита» відповідь («так», «добре», «я уточню і
        # повернусь») — це те, на що скаржиться власник. Замінюємо проактивною.
        logger.info(f"Closed reply replaced (category={category}): {reply!r}")
        reply = support_agent.proactive_fallback(category)

    return {"category": category, "reply": reply}


# ---------------------------------------------------------------------------
# Escalation — Bitrix task, rate-limited per client+category
# ---------------------------------------------------------------------------

def escalate_if_needed(conn, *, client: dict, category: str, user_message: str) -> None:
    if category not in CATEGORY_LABELS:
        return
    window = support_agent.escalation_window_minutes(category)
    if db.get_recent_escalation(conn, client["id"], category, window):
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

    # 1. Детермінований запобіжник ДО будь-якої мережі. Криза й претензія не
    #    можуть залежати від того, чи жива зараз модель. Згортку історії тут
    #    свідомо пропускаємо — вона зробиться на наступному звичайному
    #    повідомленні, а кризова відповідь має піти без зайвої затримки.
    forced = support_agent.pretriage(user_message, case_known=bool(case))
    if forced:
        db.add_chat_message(conn, client["id"], "assistant", forced["reply"], category=forced["category"])
        escalate_if_needed(conn, client=client, category=forced["category"], user_message=user_message)
        return {"category": forced["category"], "reply": forced["reply"]}

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

    # Якщо модель пообіцяла клієнту «передам юристу», а сама категорія не
    # ескалюється — все одно створюємо задачу. Інакше обіцянка була б порожньою.
    escalation_category = result["category"]
    if escalation_category not in CATEGORY_LABELS and support_agent.promises_handoff(result["reply"]):
        escalation_category = PROMISED_HANDOFF
    escalate_if_needed(conn, client=client, category=escalation_category, user_message=user_message)

    return result
