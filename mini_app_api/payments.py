"""
Payment receipt upload — client attaches a receipt to a specific Bitrix
invoice (Smart Process, entityTypeId=31, see bitrix.py). New capability:
documents_bot's old receipt flow only ever saved to Google Drive and pinged
admins over Telegram, it never touched Bitrix at all (see git history / the
one-off research done for this feature). Here the receipt goes to three
places: Bitrix Disk (same CLIENTS folder structure documents.py already
uses, for the same browsable-archive reason every other document lands
there), the invoice's own file field (INVOICE_RECEIPT_FIELD, so it's
visible right on the invoice card, not just linked from elsewhere), and a
timeline comment on the invoice linking back to the Disk copy. The app
never changes the invoice's stage itself — a manager reviews the receipt
and moves it manually once they've checked it.
"""
from __future__ import annotations

from . import bitrix
from .bitrix_disk import SUBFOLDERS
from .documents import _compress_image_if_needed, get_disk


def upload_receipt(client: dict, invoice_id: int, invoice_title: str, filename: str, content: bytes) -> dict:
    content, filename, mimetype = _compress_image_if_needed(content, filename)
    disk = get_disk()
    client_folder = disk.get_or_create_client_folder(client["full_name"], client["phone"])
    subfolder = disk.get_or_create_folder(SUBFOLDERS["receipts"], client_folder["id"])
    uploaded = disk.upload_bytes(content, filename, subfolder["id"], mimetype)

    bitrix.set_invoice_receipt_file(invoice_id, filename, content)

    comment = (
        f"💳 Клієнт завантажив квитанцію про оплату рахунку «{invoice_title}».\n"
        f"[URL={uploaded['webViewLink']}]{filename}[/URL]"
    )
    bitrix.add_invoice_comment(invoice_id, comment)
    return uploaded
