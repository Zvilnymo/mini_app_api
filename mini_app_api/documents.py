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
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from ai_document_validator import validator as ai_validator

from . import db
from .bitrix_disk import BitrixDiskManager, SUBFOLDERS

# Phone camera photos routinely come in at 10-20+ MB. Base64-encoding the
# original bytes for disk.folder.uploadfile then roughly triples the request
# body size — large enough on a slow mobile connection to blow past Render's/
# Cloudflare's proxy timeout, which surfaces to the client as a bare "Failed
# to fetch" with no server-side error to debug. Cap the same way
# ai_document_validator._encode_image already does for its own OpenAI copy,
# so the Disk copy stays a readable, reasonably sized JPEG instead of the
# untouched original.
_MAX_IMAGE_DIMENSION = (2048, 2048)
_IMAGE_JPEG_QUALITY = 85


def _compress_image_if_needed(content: bytes, filename: str) -> tuple[bytes, str, str]:
    """Downscale+recompress photo uploads before they leave this server.
    Returns (content, filename, mimetype) — untouched for non-image files."""
    try:
        with Image.open(BytesIO(content)) as img:
            img.load()
            if img.mode != "RGB":
                img = img.convert("RGB")  # JPEG has no alpha channel
            img.thumbnail(_MAX_IMAGE_DIMENSION, Image.Resampling.LANCZOS)
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=_IMAGE_JPEG_QUALITY)
            compressed = buffer.getvalue()
    except UnidentifiedImageError:
        return content, filename, mimetypes.guess_type(filename)[0] or "application/octet-stream"

    base_name = os.path.splitext(filename)[0]
    return compressed, f"{base_name}.jpg", "image/jpeg"

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
TEXT_TYPES = {k: v for k, v in DOCUMENT_TYPES.items() if v.get("is_text") or v.get("is_text_email")}

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
            "text_input": doc_type in TEXT_TYPES,
            "uploaded_count": len(docs),
            "latest_status": docs[0]["validation_status"] if docs else None,
        })
    return items


def resolve_subfolder(disk: BitrixDiskManager, client: dict, folder_key: str) -> dict:
    # Not cached on docbot.clients (that's documents_bot's Google Drive
    # folder id, a different system) — get_or_create_client_folder is
    # idempotent, so resolving it fresh each upload is simple and correct.
    client_folder = disk.get_or_create_client_folder(client["full_name"], client["phone"])
    return disk.get_or_create_folder(SUBFOLDERS[folder_key], client_folder["id"])


def upload_document(conn, client: dict, document_type: str, filename: str, content: bytes) -> dict:
    if document_type not in UPLOADABLE_TYPES:
        raise ValueError(f"unknown or non-uploadable document_type: {document_type}")

    meta = DOCUMENT_TYPES[document_type]
    disk = get_disk()
    subfolder = resolve_subfolder(disk, client, meta["folder"])

    # Downscale photos before they touch the network at all — both the AI
    # validation temp file and the Bitrix Disk upload below use this same
    # compressed copy, not the original multi-megabyte phone camera file.
    content, filename, mimetype = _compress_image_if_needed(content, filename)

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


def upload_text_document(conn, client: dict, document_type: str, text: str) -> dict:
    """ecpass ("Пароль від ЕЦП") / emailpass ("Пошта та пароль") — no file,
    just short text the client types in, saved as a .txt on Disk. No AI
    validation applies to these (nothing to visually verify).

    Unlike file uploads (which keep every version as its own docbot.documents
    row), text answers only ever have one meaningful value — re-saving
    overwrites the same Disk file (via disk.file.uploadversion) and the same
    docbot.documents row, instead of piling up "Пароль (1).txt", "(2).txt", ...
    """
    if document_type not in TEXT_TYPES:
        raise ValueError(f"unknown or non-text document_type: {document_type}")

    meta = TEXT_TYPES[document_type]
    disk = get_disk()
    filename = f"{meta['name']}.txt"
    content = text.encode("utf-8")

    existing = db.get_latest_document(conn, client["id"], document_type)
    if existing:
        uploaded = disk.update_file(existing["drive_file_id"], filename, content)
        row = db.update_document_file(
            conn,
            existing["id"],
            drive_file_id=uploaded["id"],
            drive_file_url=uploaded.get("webViewLink"),
            file_size=int(uploaded.get("size", len(content))),
        )
    else:
        subfolder = resolve_subfolder(disk, client, meta["folder"])
        uploaded = disk.upload_bytes(content, filename, subfolder["id"], "text/plain")
        row = db.add_document(
            conn,
            client_id=client["id"],
            document_type=document_type,
            file_name=filename,
            drive_file_id=uploaded["id"],
            drive_file_url=uploaded.get("webViewLink"),
            file_size=int(uploaded.get("size", len(content))),
            validation_status="pending",
        )
    return {"document": row, "validation_status": "pending"}
