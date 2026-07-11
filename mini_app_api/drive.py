"""
Google Drive access, duplicated from telegram_bot.py's DriveManager rather than
imported from it — telegram_bot.py is a single-file monolith with no confirmed
`if __name__ == '__main__':` guard, so importing it risks starting the bot's
polling loop as a side effect. Same env vars, same folder/file layout, so
mini_app_api and the bot write into the exact same Drive structure.
"""
import base64
import json
import os
from io import BytesIO

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_CREDENTIALS_BASE64 = os.getenv("GOOGLE_CREDENTIALS_BASE64")
GOOGLE_OAUTH_TOKEN = os.getenv("GOOGLE_OAUTH_TOKEN")
ROOT_FOLDER_ID = os.getenv("ROOT_FOLDER_ID")

SUBFOLDERS = {
    "credit": "Кредитні договори",
    "personal": "Особисті документи",
    "declaration": "Декларація",
    "expenses_confirmation": "Підвердження витрат",
    "debt_confirmation": "Підвердження заборгованості",
    "additional": "Додаткові документи",
}


class DriveManager:
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

    def create_folder(self, name, parent_id=None):
        file_metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            file_metadata["parents"] = [parent_id]
        return self.service.files().create(body=file_metadata, fields="id, webViewLink").execute()

    def find_folder_by_name(self, name, parent_id=None):
        query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"
        results = self.service.files().list(q=query, spaces="drive", fields="files(id, name, webViewLink)").execute()
        items = results.get("files", [])
        return items[0] if items else None

    def get_or_create_folder(self, name, parent_id=None):
        return self.find_folder_by_name(name, parent_id) or self.create_folder(name, parent_id)

    def _find_client_folder_by_phone(self, phone):
        query = (
            f"name contains '{phone}' and mimeType='application/vnd.google-apps.folder' "
            f"and '{ROOT_FOLDER_ID}' in parents and trashed=false"
        )
        results = self.service.files().list(q=query, spaces="drive", fields="files(id, name, webViewLink)").execute()
        items = results.get("files", [])
        return items[0] if items else None

    @staticmethod
    def _sanitize_name(name):
        forbidden = '<>:"/\\|?*\x00-\x1F'
        for char in forbidden:
            name = name.replace(char, " ")
        return " ".join(name.split())

    def get_or_create_client_folder(self, full_name: str, phone: str) -> dict:
        """Same lookup order as create_client_folder_structure in telegram_bot.py:
        find existing folder by phone first, only create if truly missing."""
        existing = self._find_client_folder_by_phone(phone)
        if existing:
            return existing
        safe_name = self._sanitize_name(full_name)
        return self.create_folder(f"{safe_name} | {phone}", ROOT_FOLDER_ID)

    def upload_bytes(self, data: bytes, filename: str, folder_id: str, mimetype: str = "application/octet-stream"):
        file_metadata = {"name": filename, "parents": [folder_id]}
        media = MediaIoBaseUpload(BytesIO(data), mimetype=mimetype, resumable=True)
        return self.service.files().create(body=file_metadata, media_body=media, fields="id, name, webViewLink, size").execute()
