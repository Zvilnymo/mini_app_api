import os
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from . import bitrix, db, documents, stages
from .telegram_auth import InvalidInitData, validate_init_data

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CORS_ORIGIN = os.getenv("CORS_ORIGIN", "*")

app = FastAPI(title="Zvilnymo mini app API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[CORS_ORIGIN] if CORS_ORIGIN != "*" else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    return {"ok": True}


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


def _case_and_payments(conn, ctx: CaseContext):
    step = stages.compute_step(ctx.deal, ctx.pre_court, ctx.court)
    current_stage_id = ctx.active["stage_id"] if ctx.active else None
    case = {
        "step": step,
        "step_label": stages.STEP_LABELS[step - 1],
        "steps": stages.STEP_LABELS,
        "current_stage_name": db.get_stage_name(conn, current_stage_id) if current_stage_id else None,
    }

    invoices = db.get_invoices(conn, ctx.contact["id"])
    paid_total = sum(float(i["amount"] or 0) for i in invoices if i["stage_id"] in db.PAID_INVOICE_STAGES)
    unpaid_total = sum(float(i["amount"] or 0) for i in invoices if i["stage_id"] not in db.PAID_INVOICE_STAGES)
    payments = {
        "invoices": [dict(i) for i in invoices],
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
            case, payments, debt_overview, days_active = _case_and_payments(conn, ctx)

        checklist = documents.checklist_for_client(conn, client["id"])
        docs_total = len(checklist)
        docs_ready = sum(1 for d in checklist if d["latest_status"] in ("accepted", "pending"))

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


@app.post("/api/complaints")
def create_complaint(text: str = Form(...), authorization: Optional[str] = Header(default=None)):
    user = authenticate(authorization)
    conn = db.get_connection()
    try:
        client = _require_client(conn, user)
        ctx = _load_case_context(conn, client["phone"])
        if not ctx or not ctx.manager_id:
            raise HTTPException(
                422,
                "Не вдалося визначити відповідального менеджера для вашої справи. "
                "Зверніться, будь ласка, через чат.",
            )
        task_id = bitrix.create_complaint_task(
            title=f"Скарга від клієнта {client['full_name']} ({client['phone']})",
            description=text,
            responsible_id=ctx.manager_id,
            deal_id=ctx.deal_id,
        )
        return {"ok": True, "task_id": task_id}
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
        client = db.create_client(conn, user["id"], full_name, phone)
        return {"registered": True, "client": {"id": client["id"], "full_name": client["full_name"], "phone": client["phone"]}}
    finally:
        conn.close()


def _require_client(conn, user: dict) -> dict:
    client = db.get_client_by_telegram_id(conn, user["id"])
    if not client:
        raise HTTPException(404, "client not registered yet, POST /api/register first")
    return client


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
