import logging
import os
from datetime import date, datetime
from typing import Optional

import psycopg2
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Body, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from ai_document_validator import validator as ai_validator

from . import bitrix, complaints, conferences, db, declaration, documents, notifications, payments, stages
from .telegram_auth import InvalidInitData, validate_init_data

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CORS_ORIGIN = os.getenv("CORS_ORIGIN", "*")
# Mirrors documents_bot's admin deep-link (/start admin_<code>) — same
# secret code, reached through the mini app instead of the bot's /start.
ADMIN_SECRET_CODE = os.getenv("ADMIN_SECRET_CODE")
# Second, independent admin panel (Зустрічі) — its own code/scope, so
# document-admins and conference-admins aren't automatically the same
# people (see docbot.admins' scope column).
CONF_ADMIN_SECRET_CODE = os.getenv("CONF_ADMIN_SECRET_CODE")

app = FastAPI(title="Zvilnymo mini app API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[CORS_ORIGIN] if CORS_ORIGIN != "*" else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _send_conference_reminders() -> None:
    # Own connection — runs on the scheduler's own thread, not a request.
    conn = db.get_connection()
    try:
        conferences.send_reminders(conn)
    except Exception as e:
        logger.error(f"Failed to send conference reminders: {e}")
    finally:
        conn.close()


@app.on_event("startup")
def _start_scheduler():
    # Mirrors conf_bot's own in-process APScheduler (it has no separate
    # worker service either) — checks every 5 minutes for 'going' clients
    # due a 24h or 60m reminder (see db.get_events_needing_reminder).
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(_send_conference_reminders, "interval", minutes=5, id="conference_reminders")
    scheduler.start()


def authenticate(authorization: Optional[str] = Header(default=None)) -> dict:
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(500, "server misconfigured: TELEGRAM_BOT_TOKEN not set")
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "missing 'Authorization: tma <initData>' header")
    init_data = authorization[len("tma "):]
    try:
        parsed = validate_init_data(init_data, TELEGRAM_BOT_TOKEN)
    except InvalidInitData as e:
        raise HTTPException(401, f"invalid initData: {e}")
    if "user" not in parsed:
        raise HTTPException(401, "initData has no user")
    return parsed["user"]


@app.get("/api/health")
def health():
    # ai_validation_enabled surfaces whether OPENAI_API_KEY actually made it
    # into this deploy's env — without it, uploads silently skip AI checks
    # and every file looks "accepted", which is easy to mistake for a broken
    # validator instead of a missing env var. No secret values are exposed.
    return {"ok": True, "ai_validation_enabled": ai_validator.enabled}


class CaseContext:
    """Everything derived from the client's CRM contact match, computed once
    and reused by /api/me (dashboard + cabinet) and /api/complaints (needs
    the responsible manager_id)."""

    def __init__(self, contact, deal, pre_court, court):
        self.contact = contact
        self.deal = deal
        self.pre_court = pre_court
        self.court = court
        self.active = court or pre_court or deal  # most-advanced known record

    @property
    def manager_id(self) -> Optional[int]:
        return self.active["manager_id"] if self.active else None

    @property
    def deal_id(self) -> Optional[int]:
        # Only crm.fact_deals rows are genuine Bitrix "deal" entities — the
        # pre-court/court funnels may be a different CRM entity type, so we
        # only link Bitrix tasks back to a real deal id, never guess.
        return self.deal["id"] if self.deal else None


def _load_case_context(conn, phone: str) -> Optional[CaseContext]:
    contact = db.get_contact_by_phone(conn, db.normalize_phone(phone))
    if not contact:
        return None
    deal = db.get_deal(conn, contact["id"])
    pre_court = db.get_pre_court_deal(conn, contact["id"])
    court = db.get_court_deal(conn, contact["id"])
    return CaseContext(contact, deal, pre_court, court)


def _case_and_payments(conn, ctx: CaseContext, client_id: int):
    step = stages.compute_step(ctx.deal, ctx.pre_court, ctx.court)
    current_stage_id = ctx.active["stage_id"] if ctx.active else None
    case = {
        "step": step,
        "step_label": stages.STEP_LABELS[step - 1],
        "steps": stages.STEP_LABELS,
        "current_stage_name": db.get_stage_name(conn, current_stage_id) if current_stage_id else None,
    }

    invoices = db.get_invoices(conn, ctx.contact["id"])
    pending_receipt_ids = db.get_pending_receipt_invoice_ids(conn, client_id)
    paid_total = sum(float(i["amount"] or 0) for i in invoices if i["stage_id"] in db.PAID_INVOICE_STAGES)
    unpaid_total = sum(float(i["amount"] or 0) for i in invoices if i["stage_id"] not in db.PAID_INVOICE_STAGES)
    payments = {
        "invoices": [{**dict(i), "receipt_pending": i["id"] in pending_receipt_ids} for i in invoices],
        "paid_total": paid_total,
        "unpaid_total": unpaid_total,
    }

    debt_source = ctx.active or {}
    total_debt = float(debt_source.get("total_debt") or 0)
    debt_overview = {
        "total_debt": total_debt,
        "to_be_written_off": float(ctx.court["debt_to_write_off"]) if ctx.court and ctx.court.get("debt_to_write_off") else total_debt,
        "creditors_count": debt_source.get("creditors_count"),
        "banks_count": debt_source.get("banks_count"),
    }

    earliest = ctx.deal or ctx.pre_court or ctx.court
    days_active = None
    if earliest and earliest.get("date_create"):
        created = earliest["date_create"]
        created_date = created.date() if isinstance(created, datetime) else created
        days_active = (date.today() - created_date).days

    return case, payments, debt_overview, days_active


@app.get("/api/me")
def get_me(authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        client = db.get_client_by_telegram_id(conn, user["id"])
        if not client:
            return {"registered": False}

        ctx = _load_case_context(conn, client["phone"])
        case = payments = debt_overview = None
        days_active = None
        if ctx:
            case, payments, debt_overview, days_active = _case_and_payments(conn, ctx, client["id"])

        # The debt amount as originally declared at the lead stage — shown on
        # Home as the client's headline debt figure (per business request:
        # it's the "big number that will soon shrink"), independent of
        # whether a deal/case record exists yet.
        lead_debt = db.get_lead_debt(conn, db.normalize_phone(client["phone"]))

        checklist = documents.checklist_for_client(conn, client["id"])
        # The declaration questionnaire isn't a DOCUMENT_TYPES entry (it's
        # its own free-text form, not a file upload) but counts as one more
        # required item everywhere document progress is shown.
        docs_total = len(checklist) + 1
        docs_ready = sum(1 for d in checklist if d["latest_status"] in ("accepted", "pending"))
        if declaration.is_complete(conn, client["id"]):
            docs_ready += 1

        # Prefer the CRM's full name over whatever Telegram display name was
        # stored at registration time — the CRM name is the client's real
        # legal name, Telegram's first/last name can be a nickname.
        full_name = ctx.contact["full_name"] if ctx and ctx.contact and ctx.contact.get("full_name") else client["full_name"]

        return {
            "registered": True,
            "screening_completed": db.is_screening_complete(client),
            "client": {
                "id": client["id"],
                "full_name": full_name,
                "phone": client["phone"],
            },
            "case": case,
            "payments": payments,
            "debt_overview": debt_overview,
            "lead_debt": lead_debt,
            "days_active": days_active,
            "docs_ready": docs_ready,
            "docs_total": docs_total,
        }
    finally:
        conn.close()


@app.post("/api/screening")
def submit_screening(
    has_gambling_crypto: bool = Form(...),
    is_fraud_victim: bool = Form(...),
    has_sold_property: bool = Form(...),
    income_over_30k: bool = Form(...),
    authorization: Optional[str] = Header(default=None),
):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        client = _require_client(conn, user)
        db.update_client_screening(
            conn,
            client["id"],
            has_gambling_crypto=has_gambling_crypto,
            is_fraud_victim=is_fraud_victim,
            has_sold_property=has_sold_property,
            income_over_30k=income_over_30k,
        )
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/complaints/departments")
def list_departments(authorization: Optional[str] = Header(default=None)):
    authenticate(authorization)
    return {"departments": [{"key": key, "name": d["name"]} for key, d in complaints.DEPARTMENTS.items()]}


@app.post("/api/complaints")
def create_complaint(
    department: str = Form(...),
    employee_name: str = Form(...),
    text: str = Form(...),
    authorization: Optional[str] = Header(default=None),
):
    user = authenticate(authorization)
    dept = complaints.DEPARTMENTS.get(department)
    if not dept:
        raise HTTPException(400, f"unknown department: {department}")
    if not dept["responsible_id"]:
        raise HTTPException(500, f"server misconfigured: no responsible_id set for department {department}")

    conn = db.get_connection()
    try:
        client = _require_client(conn, user)
        ctx = _load_case_context(conn, client["phone"])
        description = (
            f"📌 Суть скарги:\n{text}\n\n"
            f"👤 Співробітник: {employee_name}\n"
            f"🙍‍♂️ Клієнт: {client['full_name']}\n"
            f"📬 Зв'язок: {client['phone']}"
        )
        # Don't list the department head twice — once as RESPONSIBLE_ID and
        # again in AUDITORS — for departments where they're the same person.
        auditors = [uid for uid in complaints.ALWAYS_CC_IDS if uid != dept["responsible_id"]]
        task_id = bitrix.create_complaint_task(
            title=f"Скарга на {dept['name']}",
            description=description,
            responsible_id=dept["responsible_id"],
            deal_id=ctx.deal_id if ctx else None,
            auditors=auditors,
        )
        return {"ok": True, "task_id": task_id}
    finally:
        conn.close()


@app.post("/api/payments/{invoice_id}/receipt")
async def upload_payment_receipt(
    invoice_id: int,
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(default=None),
):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        client = _require_client(conn, user)
        ctx = _load_case_context(conn, client["phone"])
        if not ctx:
            raise HTTPException(422, "Не вдалося знайти вашу справу в CRM.")
        # Only let a client attach a receipt to one of their own invoices —
        # never trust invoice_id from the request alone.
        invoice = next((i for i in db.get_invoices(conn, ctx.contact["id"]) if i["id"] == invoice_id), None)
        if not invoice:
            raise HTTPException(404, "Рахунок не знайдено")

        content = await file.read()
        try:
            uploaded = payments.upload_receipt(client, invoice_id, invoice["title"] or "Рахунок", file.filename, content)
        except Exception as e:
            raise HTTPException(502, f"Не вдалося зберегти квитанцію: {e}")

        db.mark_receipt_submitted(conn, invoice_id, client["id"])

        notifications.notify_admins(
            conn,
            f"💳 <b>Клієнт завантажив квитанцію про оплату</b>\n\n"
            f"👤 {client['full_name']}\n"
            f"📱 {client['phone']}\n"
            f"📄 Рахунок: {invoice['title'] or invoice_id}\n"
            f'📁 <a href="{uploaded.get("webViewLink")}">Переглянути квитанцію</a>',
        )
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/register")
def register(phone: str = Form(...), authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        # Prefer the CRM's name for the entered phone over the Telegram
        # display name — falls back to Telegram name if there's no CRM
        # contact yet (e.g. brand-new lead not synced by etl_zv yet).
        crm_contact = db.get_contact_by_phone(conn, db.normalize_phone(phone))
        full_name = (
            crm_contact["full_name"]
            if crm_contact and crm_contact.get("full_name")
            else " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or user.get("username") or "Client"
        )
        try:
            client = db.create_client(conn, user["id"], full_name, phone)
        except (db.PhoneAlreadyLinked, psycopg2.errors.UniqueViolation):
            conn.rollback()
            raise HTTPException(
                409,
                "Цей номер телефону вже прив'язано до іншого Telegram-акаунта. "
                "Увійдіть у застосунок з того акаунта або зверніться до адміністратора.",
            )
        return {"registered": True, "client": {"id": client["id"], "full_name": client["full_name"], "phone": client["phone"]}}
    finally:
        conn.close()


@app.post("/api/admin/register")
def register_admin(code: str = Form(...), authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    if ADMIN_SECRET_CODE and code == ADMIN_SECRET_CODE:
        scope = "documents"
    elif CONF_ADMIN_SECRET_CODE and code == CONF_ADMIN_SECRET_CODE:
        scope = "conferences"
    else:
        raise HTTPException(403, "invalid code")
    full_name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or user.get("username")
    conn = db.get_connection()
    try:
        db.register_admin(conn, user["id"], full_name, scope=scope)
        return {"ok": True, "scope": scope}
    finally:
        conn.close()


def _require_client(conn, user: dict) -> dict:
    client = db.get_client_by_telegram_id(conn, user["id"])
    if not client:
        raise HTTPException(404, "client not registered yet, POST /api/register first")
    return client


def _require_admin(conn, user: dict, scope: str) -> None:
    if not db.is_admin(conn, user["id"], scope):
        raise HTTPException(403, f"not an admin for scope={scope}")


@app.get("/api/documents")
def list_documents(authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        client = db.get_client_by_telegram_id(conn, user["id"])
        client_id = client["id"] if client else None
        return {"documents": documents.checklist_for_client(conn, client_id)}
    finally:
        conn.close()


@app.post("/api/documents/upload")
async def upload_document(
    document_type: str = Form(...),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(default=None),
):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        client = _require_client(conn, user)
        content = await file.read()
        try:
            result = documents.upload_document(conn, client, document_type, file.filename, content)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {
            "validation_status": result["validation_status"],
            "document": {
                "id": result["document"]["id"],
                "document_type": result["document"]["document_type"],
                "file_name": result["document"]["file_name"],
                "drive_file_url": result["document"]["drive_file_url"],
            },
        }
    finally:
        conn.close()


@app.get("/api/declaration")
def get_declaration(authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        client = _require_client(conn, user)
        return {
            "questions": declaration.QUESTIONS,
            "answers": declaration.get_answers(conn, client["id"]),
            "completed": declaration.is_complete(conn, client["id"]),
        }
    finally:
        conn.close()


@app.post("/api/declaration")
def submit_declaration(answers: dict = Body(...), authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        client = _require_client(conn, user)
        declaration.save_and_submit(conn, client, answers)
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/documents/upload-text")
def upload_text_document(
    document_type: str = Form(...),
    text: str = Form(...),
    authorization: Optional[str] = Header(default=None),
):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        client = _require_client(conn, user)
        if not text.strip():
            raise HTTPException(400, "text is empty")
        try:
            result = documents.upload_text_document(conn, client, document_type, text.strip())
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {
            "validation_status": result["validation_status"],
            "document": {
                "id": result["document"]["id"],
                "document_type": result["document"]["document_type"],
                "file_name": result["document"]["file_name"],
                "drive_file_url": result["document"]["drive_file_url"],
            },
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Зустрічі (conferences) — client-facing: view invitations, RSVP, leave
# feedback after a meeting. See conferences.py's module docstring for why
# this is all in-app rather than Telegram inline-keyboard callbacks.
# ---------------------------------------------------------------------------

def _serialize_event_row(row: dict) -> dict:
    return {
        "event_id": row["event_id"],
        "title": row["title"],
        "description": row["description"],
        "start_at": row["start_at"].isoformat() if row["start_at"] else None,
        "duration_min": row["duration_min"],
        "format": row["format"],
        "link": row["link"],
        "person_name": row["person_name"],
        "person_role": row["person_role"],
        "rsvp": row["rsvp"],
        "rsvp_at": row["rsvp_at"].isoformat() if row.get("rsvp_at") else None,
        "attended": row.get("attended"),
        "feedback_stars": row.get("feedback_stars"),
        "feedback_comment": row.get("feedback_comment"),
    }


@app.get("/api/conferences")
def list_conferences(authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        client = _require_client(conn, user)
        rows = db.get_client_events(conn, client["id"])
        checklist = db.get_client_checklist(conn, client["id"])
        return {
            "events": [_serialize_event_row(r) for r in rows],
            "checklist": [
                {"type_code": r["type_code"], "title": r["title"], "description": r["description"], "completed": r["completed"]}
                for r in checklist
            ],
        }
    finally:
        conn.close()


@app.post("/api/conferences/{event_id}/rsvp")
def submit_conference_rsvp(event_id: int, rsvp: str = Body(..., embed=True), authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    if rsvp not in ("going", "declined"):
        raise HTTPException(400, "rsvp must be 'going' or 'declined'")
    conn = db.get_connection()
    try:
        client = _require_client(conn, user)
        event = db.get_client_event(conn, event_id, client["id"])
        if not event:
            raise HTTPException(404, "you weren't invited to this event")
        db.submit_rsvp(conn, event_id, client["id"], rsvp)
        conferences.notify_admins_new_rsvp(conn, client_name=client["full_name"], event_title=event["title"], rsvp=rsvp)
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/conferences/{event_id}/feedback")
def submit_conference_feedback(
    event_id: int,
    stars: int = Body(..., embed=True),
    comment: Optional[str] = Body(default=None, embed=True),
    authorization: Optional[str] = Header(default=None),
):
    user = authenticate(authorization)
    if not 1 <= stars <= 5:
        raise HTTPException(400, "stars must be 1-5")
    conn = db.get_connection()
    try:
        client = _require_client(conn, user)
        event = db.get_client_event(conn, event_id, client["id"])
        if not event:
            raise HTTPException(404, "you weren't invited to this event")
        db.submit_feedback(conn, event_id, client["id"], stars, comment)
        return {"ok": True}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Зустрічі — admin panel. Reached via startapp=confadmin_<CONF_ADMIN_SECRET_CODE>
# (see AdminRegister.tsx), gated by docbot.admins' scope='conferences'
# rather than the document-admin scope.
# ---------------------------------------------------------------------------

@app.get("/api/admin/conferences/types")
def admin_list_event_types(authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        _require_admin(conn, user, "conferences")
        return {"types": db.list_event_types(conn, active_only=False)}
    finally:
        conn.close()


@app.get("/api/admin/conferences/events")
def admin_list_events(upcoming: bool = True, authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        _require_admin(conn, user, "conferences")
        events = db.list_events(conn, upcoming=upcoming)
        return {"events": [{**e, "start_at": e["start_at"].isoformat()} for e in events]}
    finally:
        conn.close()


@app.get("/api/admin/conferences/events/{event_id}")
def admin_get_event(event_id: int, authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        _require_admin(conn, user, "conferences")
        event = db.get_event(conn, event_id)
        if not event:
            raise HTTPException(404, "event not found")
        rsvp_rows = db.get_event_rsvp_rows(conn, event_id)
        return {
            "event": {**event, "start_at": event["start_at"].isoformat()},
            "invitees": [
                {
                    "client_id": r["client_id"],
                    "full_name": r["full_name"],
                    "phone": r["phone"],
                    "rsvp": r["rsvp"],
                    "rsvp_at": r["rsvp_at"].isoformat() if r["rsvp_at"] else None,
                }
                for r in rsvp_rows
            ],
        }
    finally:
        conn.close()


@app.post("/api/admin/conferences/events")
def admin_create_event(fields: dict = Body(...), authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        _require_admin(conn, user, "conferences")
        try:
            start_at = datetime.fromisoformat(fields["start_at"])
        except (KeyError, ValueError) as e:
            raise HTTPException(400, f"invalid start_at: {e}")
        event = db.create_event(
            conn,
            type_code=fields.get("type_code"),
            title=fields["title"],
            description=fields.get("description"),
            start_at=start_at,
            duration_min=int(fields.get("duration_min", 30)),
            format=fields.get("format", "video"),
            link=fields.get("link"),
            person_name=fields.get("person_name"),
            person_role=fields.get("person_role"),
            created_by=user["id"],
        )
        client_ids = fields.get("client_ids") or []
        if client_ids:
            conferences.send_invites(conn, event, client_ids)
        return {"ok": True, "event_id": event["event_id"]}
    finally:
        conn.close()


@app.patch("/api/admin/conferences/events/{event_id}")
def admin_update_event(
    event_id: int, field: str = Body(...), value: str = Body(...), authorization: Optional[str] = Header(default=None)
):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        _require_admin(conn, user, "conferences")
        try:
            db.update_event_field(conn, event_id, field, value)
        except ValueError as e:
            raise HTTPException(400, str(e))
        conferences.notify_event_update(conn, event_id, f"{field}: {value}")
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/admin/conferences/events/{event_id}")
def admin_cancel_event(event_id: int, authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        _require_admin(conn, user, "conferences")
        event = db.get_event(conn, event_id)
        if not event:
            raise HTTPException(404, "event not found")
        conferences.notify_event_cancel(conn, event_id, event["title"])
        db.delete_event(conn, event_id)
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/admin/conferences/clients/search")
def admin_search_clients(q: str = "", authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        _require_admin(conn, user, "conferences")
        return {"clients": db.search_clients(conn, q)}
    finally:
        conn.close()


@app.post("/api/admin/conferences/events/{event_id}/invite")
def admin_invite_clients(event_id: int, client_ids: list[int] = Body(..., embed=True), authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        _require_admin(conn, user, "conferences")
        event = db.get_event(conn, event_id)
        if not event:
            raise HTTPException(404, "event not found")
        conferences.send_invites(conn, event, client_ids)
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/admin/conferences/events/{event_id}/attendance")
def admin_mark_attendance(
    event_id: int,
    client_id: int = Body(..., embed=True),
    attended: bool = Body(..., embed=True),
    authorization: Optional[str] = Header(default=None),
):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        _require_admin(conn, user, "conferences")
        db.mark_attendance(conn, event_id, client_id, attended)
        return {"ok": True}
    finally:
        conn.close()
