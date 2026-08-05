"""
Google Drive storage — restores the duplicate-write-to-Drive requirement
(2026-08-02) for mini_app_api's own document uploads, which never had a
Drive path at all (this app was built Bitrix-only from the start; see
documents.py's module docstring). Mirrors documents_bot's own DriveManager
class as closely as possible — same credential precedence, same client
folder naming ("{full_name} | {phone}", not Bitrix's " - " separator) —
so files land in the *same* Drive folder tree the bot and the earlier
Google Drive -> Bitrix migration already populated, not a second parallel
one.

Same public method names as bitrix_disk.py's BitrixDiskManager
(get_or_create_client_folder / get_or_create_folder / upload_bytes /
update_file) so documents.py's dual-write wrapper can call either
uniformly.
"""
from __future__ import annotations

import base64
import json
import os
from io import BytesIO

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# Same env var precedence and same "CLIENTS" root folder documents_bot uses
# (default is that folder's real Drive id, confirmed live during the
# Google Drive -> Bitrix migration on 2026-08-01) — mini_app_api's own
# Drive writes land in the exact same tree, not a separate one.
ROOT_FOLDER_ID = os.getenv("ROOT_FOLDER_ID", "1H6wAUbwoq5mB11wgPGCT0WGWUkLrZeUs")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_CREDENTIALS_BASE64 = os.getenv("GOOGLE_CREDENTIALS_BASE64")
GOOGLE_OAUTH_TOKEN = os.getenv("GOOGLE_OAUTH_TOKEN")

SUBFOLDER_NAME_BY_KEY = {
    "credit": "Кредитні договори",
    "personal": "Особисті документи",
    "declaration": "Декларація",
    "expenses_confirmation": "Підвердження витрат",
    "debt_confirmation": "Підвердження заборгованості",
    "additional": "Додаткові документи",
    "receipts": "Квитанції про оплату",
}


def _sanitize_name(name: str) -> str:
    forbidden = '<>:"/\\|?*\x00-\x1F'
    for char in forbidden:
        name = name.replace(char, " ")
    return " ".join(name.split())


class GoogleDriveManager:
    def __init__(self):
        if GOOGLE_OAUTH_TOKEN:
            token_data = json.loads(GOOGLE_OAUTH_TOKEN)
            credentials = Credentials(
                token=token_data.get("token"),
                refresh_token=token_data.get("refresh_token"),
                token_uri=token_data.get("token_uri"),
                client_id=token_data.get("client_id"),
                client_secret=token_data.get("client_secret"),
                scopes=token_data.get("scopes"),
            )
        elif GOOGLE_CREDENTIALS_BASE64:
            creds_dict = json.loads(base64.b64decode(GOOGLE_CREDENTIALS_BASE64))
            credentials = service_account.Credentials.from_service_account_info(
                creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
            )
        else:
            credentials = service_account.Credentials.from_service_account_file(
                GOOGLE_CREDENTIALS_FILE, scopes=["https://www.googleapis.com/auth/drive"]
            )
        self.service = build("drive", "v3", credentials=credentials)

    def _find_child_by_name(self, name: str, parent_id: str, *, folders_only: bool):
        type_clause = "mimeType='application/vnd.google-apps.folder'" if folders_only else "mimeType!='application/vnd.google-apps.folder'"
        query = f"name='{name}' and {type_clause} and '{parent_id}' in parents and trashed=false"
        results = self.service.files().list(q=query, spaces="drive", fields="files(id, name, webViewLink)").execute()
        items = results.get("files", [])
        return items[0] if items else None

    def get_or_create_folder(self, name: str, parent_id: str) -> dict:
        existing = self._find_child_by_name(name, parent_id, folders_only=True)
        if existing:
            return {"id": existing["id"], "webViewLink": existing.get("webViewLink")}
        folder = self.service.files().create(
            body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
            fields="id, webViewLink",
        ).execute()
        return {"id": folder["id"], "webViewLink": folder.get("webViewLink")}

    def _find_client_folder_by_phone(self, phone: str):
        query = (
            f"name contains '{phone}' and mimeType='application/vnd.google-apps.folder' "
            f"and '{ROOT_FOLDER_ID}' in parents and trashed=false"
        )
        results = self.service.files().list(q=query, spaces="drive", fields="files(id, name, webViewLink)").execute()
        items = results.get("files", [])
        return items[0] if items else None

    def get_or_create_client_folder(self, full_name: str, phone: str) -> dict:
        existing = self._find_client_folder_by_phone(phone)
        if existing:
            return {"id": existing["id"], "webViewLink": existing.get("webViewLink")}
        safe_name = _sanitize_name(full_name)
        return self.get_or_create_folder(f"{safe_name} | {phone}", ROOT_FOLDER_ID)

    def upload_bytes(self, data: bytes, filename: str, folder_id: str, mimetype: str = "application/octet-stream") -> dict:
        media = MediaIoBaseUpload(BytesIO(data), mimetype=mimetype, resumable=True)
        file = self.service.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id, name, webViewLink, size",
        ).execute()
        return {
            "id": file["id"], "name": file.get("name", filename),
            "webViewLink": file.get("webViewLink"), "size": file.get("size", len(data)),
        }

    def update_file(self, file_id: str, filename: str, data: bytes) -> dict:
        media = MediaIoBaseUpload(BytesIO(data), mimetype="text/plain", resumable=True)
        file = self.service.files().update(
            fileId=file_id, media_body=media, fields="id, name, webViewLink, size"
        ).execute()
        return {
            "id": file["id"], "name": file.get("name", filename),
            "webViewLink": file.get("webViewLink"), "size": file.get("size", len(data)),
        }

    def upsert_by_name(self, filename: str, folder_id: str, data: bytes, mimetype: str = "text/plain") -> dict:
        """update_file needs a Drive file id, which the dual-write wrapper
        doesn't have (only the Bitrix file id ever gets persisted to
        docbot.documents) — finds the file by name in the target folder
        instead and updates it in place if present, creates it otherwise.
        Used for the ecpass/emailpass "re-saving replaces, doesn't pile up
        copies" behavior on the Drive side."""
        existing = self._find_child_by_name(filename, folder_id, folders_only=False)
        if existing:
            return self.update_file(existing["id"], filename, data)
        return self.upload_bytes(data, filename, folder_id, mimetype)
