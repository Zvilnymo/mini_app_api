"""
Document checklist + upload, mirroring telegram_bot.py's DOCUMENT_TYPES and
upload flow (docbot.documents rows), reusing ai_document_validator.py as-is
(it's a clean, side-effect-free module — unlike telegram_bot.py, safe to
import directly).

Files are stored in Bitrix24 Disk (company common storage), not Google
Drive — documents_bot's own Drive uploads are a separate, unrelated
destination; docbot.clients.drive_folder_id/drive_folder_url stay
Google-only and are never read or written here, to avoid mixing up a
Google Drive folder ID with a Bitrix Disk one.
"""
from __future__ import annotations

import mimetypes
import os
import tempfile

from ai_document_validator import validator as ai_validator

from . import db
from .bitrix_disk import BitrixDiskManager, SUBFOLDERS

# Copied from telegram_bot.py DOCUMENT_TYPES — keep in sync if the bot's
# checklist changes. 'is_text'/'is_text_email' types (ecpass, emailpass) are
# not file uploads and are out of scope for /api/documents/upload for now.
DOCUMENT_TYPES = {
    "ecpass": {"name": "Пароль від ЕЦП", "emoji": "🔐", "folder": "personal", "required": True, "is_text": True},
    "emailpass": {"name": "Пошта та пароль", "emoji": "📧", "folder": "personal", "required": False, "is_text_email": True},
    "ecp": {"name": "ЕЦП (електронний цифровий підпис)", "emoji": "📜", "folder": "personal", "required": True},
    "passport": {"name": "Сканкопія паспорта та РНОКПП (ІПН)", "emoji": "📕", "folder": "personal", "required": True},
    "registration": {"name": "Витяг з реєстру територіальної громади", "emoji": "🏠", "folder": "personal", "required": True},
    "workbook": {"name": "Копія трудової книжки", "emoji": "📗", "folder": "personal", "required": False},
    "credit_contracts": {"name": "Кредитні договори", "emoji": "📑", "folder": "credit", "required": True, "multiple": True},
    "bank_statements": {"name": "Виписки про залишок коштів на рахунках", "emoji": "🏦", "folder": "personal", "required": True},
    "expenses": {"name": "Підтвердження витрат за останні місяці", "emoji": "💰", "folder": "expenses_confirmation", "required": True},
    "story": {"name": "Ваша історія (у форматі Word)", "emoji": "📝", "folder": "personal", "required": True},
    "family_income": {"name": "Доходи членів сім'ї (довідка з податкової)", "emoji": "💵", "folder": "personal", "required": False},
    "debt_certificates": {"name": "Довідки про стан заборгованості", "emoji": "📋", "folder": "debt_confirmation", "required": True},
    "executive": {"name": "Виписки по виконавчих провадженнях", "emoji": "⚖️", "folder": "personal", "required": False},
    "additional_docs": {"name": "Додаткові документи", "emoji": "📎", "folder": "additional", "required": False, "skip_ai_validation": True},
}

UPLOADABLE_TYPES = {k: v for k, v in DOCUMENT_TYPES.items() if not v.get("is_text") and not v.get("is_text_email")}

_disk = None


def get_disk() -> BitrixDiskManager:
    global _disk
    if _disk is None:
        _disk = BitrixDiskManager()
    return _disk


def checklist_for_client(conn, client_id: int | None) -> list[dict]:
    uploaded = {}
    if client_id is not None:
        for doc in db.get_documents_by_client(conn, client_id):
            uploaded.setdefault(doc["document_type"], []).append(doc)

    items = []
    for doc_type, meta in DOCUMENT_TYPES.items():
        docs = uploaded.get(doc_type, [])
        items.append({
            "type": doc_type,
            "name": meta["name"],
            "emoji": meta["emoji"],
            "required": meta["required"],
            "uploadable": doc_type in UPLOADABLE_TYPES,
            "uploaded_count": len(docs),
            "latest_status": docs[0]["validation_status"] if docs else None,
        })
    return items


def upload_document(conn, client: dict, document_type: str, filename: str, content: bytes) -> dict:
    if document_type not in UPLOADABLE_TYPES:
        raise ValueError(f"unknown or non-uploadable document_type: {document_type}")

    meta = DOCUMENT_TYPES[document_type]
    disk = get_disk()

    # Not cached on docbot.clients (that's documents_bot's Google Drive
    # folder id, a different system) — get_or_create_client_folder is
    # idempotent, so resolving it fresh each upload is simple and correct.
    client_folder = disk.get_or_create_client_folder(client["full_name"], client["phone"])
    subfolder = disk.get_or_create_folder(SUBFOLDERS[meta["folder"]], client_folder["id"])

    # Default to "pending" (uploaded, no AI verdict yet) rather than leaving
    # this null — null renders no status badge at all, which reads as "the
    # upload silently did nothing" even though the file made it to Drive.
    validation_status = "pending"
    if not meta.get("skip_ai_validation"):
        suffix = os.path.splitext(filename)[1] or ""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            result = ai_validator.validate_document(tmp_path, document_type)
        finally:
            os.unlink(tmp_path)
        if result is not None:
            validation_status = result.status

    mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    uploaded = disk.upload_bytes(content, filename, subfolder["id"], mimetype)

    row = db.add_document(
        conn,
        client_id=client["id"],
        document_type=document_type,
        file_name=filename,
        drive_file_id=uploaded["id"],
        drive_file_url=uploaded.get("webViewLink"),
        file_size=int(uploaded.get("size", len(content))),
        validation_status=validation_status,
    )
    return {
        "document": row,
        "validation_status": validation_status,
    }
