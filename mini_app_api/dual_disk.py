"""
Writes every document to both Bitrix Disk and Google Drive — explicit
customer requirement (2026-08-02): keep the duplication, don't pick one.
Same design as documents_bot's DualDriveManager (telegram_bot.py) applied
here, since mini_app_api never had a Drive path of its own before this.

Bitrix stays primary/authoritative — docbot.documents.drive_file_id (a
column name that predates this app and is shared with documents_bot) is
still always the Bitrix file id, unchanged. Google Drive is a best-effort
side write: any Drive-side failure (quota, auth, network) is logged and
swallowed, never blocks the upload or surfaces an error to the client.

Folder ids from the two services live in different id spaces, so folder-
returning calls pack both into one opaque string
("<bitrix_id>::gdrive::<drive_id>") that documents.py's resolve_subfolder
and every upload_bytes/update_file call already just threads through
unchanged, same as it already did with a plain Bitrix id.
"""
from __future__ import annotations

import logging

from .bitrix_disk import BitrixDiskManager
from .google_drive import GoogleDriveManager

logger = logging.getLogger(__name__)

_SEP = "::gdrive::"


def _combine(bitrix_id, drive_id) -> str:
    return f"{bitrix_id}{_SEP}{drive_id or ''}"


def _split(folder_id):
    if folder_id and _SEP in str(folder_id):
        b, d = str(folder_id).split(_SEP, 1)
        return b, (d or None)
    return folder_id, None


class DualDiskManager:
    def __init__(self):
        self.bitrix = BitrixDiskManager()
        try:
            self.drive = GoogleDriveManager()
        except Exception as e:
            logger.error(f"Google Drive dual-write disabled (init failed): {e}")
            self.drive = None

    def _drive_safe(self, label, fn, *args, **kwargs):
        if not self.drive:
            return None
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.error(f"Google Drive dual-write failed ({label}): {e}")
            return None

    def get_or_create_client_folder(self, full_name: str, phone: str) -> dict:
        bitrix_folder = self.bitrix.get_or_create_client_folder(full_name, phone)
        drive_folder = self._drive_safe(
            "get_or_create_client_folder", self.drive.get_or_create_client_folder, full_name, phone
        )
        result = dict(bitrix_folder)
        result["id"] = _combine(bitrix_folder["id"], drive_folder["id"] if drive_folder else None)
        result["_drive"] = drive_folder
        return result

    def get_or_create_folder(self, name: str, parent_id) -> dict:
        b_parent, d_parent = _split(parent_id)
        bitrix_folder = self.bitrix.get_or_create_folder(name, b_parent)
        drive_folder = None
        if d_parent:
            drive_folder = self._drive_safe("get_or_create_folder", self.drive.get_or_create_folder, name, d_parent)
        result = dict(bitrix_folder)
        result["id"] = _combine(bitrix_folder["id"], drive_folder["id"] if drive_folder else None)
        result["_drive"] = drive_folder
        return result

    def upload_bytes(self, data: bytes, filename: str, folder_id, mimetype: str = "application/octet-stream") -> dict:
        b_id, d_id = _split(folder_id)
        result = self.bitrix.upload_bytes(data, filename, b_id, mimetype)
        if d_id:
            self._drive_safe("upload_bytes", self.drive.upload_bytes, data, filename, d_id, mimetype)
        return result

    def update_file(self, file_id, filename: str, data: bytes, drive_folder_id=None) -> dict:
        # file_id is always a Bitrix file id (the only one ever persisted,
        # in docbot.documents.drive_file_id) — the Drive copy has no stored
        # id to update in place, so the caller passes the *folder* it lives
        # in instead and we find-or-create by name there.
        result = self.bitrix.update_file(file_id, filename, data)
        if drive_folder_id:
            _, d_folder_id = _split(drive_folder_id)
            if d_folder_id:
                self._drive_safe("update_file", self.drive.upsert_by_name, filename, d_folder_id, data)
        return result
