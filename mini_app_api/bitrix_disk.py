"""
Bitrix24 Disk storage — same interface shape as drive.py's DriveManager
(get_or_create_client_folder / get_or_create_folder / upload_bytes) so
documents.py can use either with minimal changes. Files land inside the
"CLIENTS" folder that already exists at the root of "Company Drive" (the
"Загальний диск" visible in the UI at /docs/shared/... — confirmed live via
disk.storage.getlist on 2026-07-11: there are *three* ENTITY_TYPE=common
storages on this portal — "Top Management's documents", "Sales and
marketing", and this one, ENTITY_ID="shared_files_s1" — so it must be
targeted by ENTITY_ID, not just ENTITY_TYPE=common). Same folder layout as
the Google Drive version below that: "{full_name} | {phone}" -> subfolders.

API reference: https://apidocs.bitrix24.com/api-reference/disk/
- disk.storage.getlist (filter ENTITY_TYPE=common) -> storage ID + root folder object ID
- disk.storage.getchildren -> list items directly under a storage root
- disk.folder.getchildren / disk.folder.addsubfolder -> list/create nested folders
- disk.folder.uploadfile with fileContent=[name, base64] -> direct upload, no
  separate upload-URL round trip needed
"""
from __future__ import annotations

import base64

from . import bitrix

# "Company Drive" storage, confirmed via a live disk.storage.getlist call.
COMPANY_DRIVE_ENTITY_ID = "shared_files_s1"

CLIENTS_FOLDER_NAME = "CLIENTS"

SUBFOLDERS = {
    "credit": "Кредитні договори",
    "personal": "Особисті документи",
    "declaration": "Декларація",
    "expenses_confirmation": "Підвердження витрат",
    "debt_confirmation": "Підвердження заборгованості",
    "additional": "Додаткові документи",
}


def _sanitize_name(name: str) -> str:
    forbidden = '<>:"/\\|?*\x00-\x1F'
    for char in forbidden:
        name = name.replace(char, " ")
    return " ".join(name.split())


class BitrixDiskManager:
    def __init__(self):
        self._storage_id = None
        self._clients_folder_id = None

    def _ensure_clients_folder(self):
        if self._clients_folder_id is not None:
            return
        result = bitrix._post(
            "disk.storage.getlist",
            {"filter": {"ENTITY_TYPE": "common", "ENTITY_ID": COMPANY_DRIVE_ENTITY_ID}},
        )
        storages = result["result"]
        if not storages:
            raise RuntimeError(
                f"Bitrix24: Company Drive storage (ENTITY_ID={COMPANY_DRIVE_ENTITY_ID!r}) not found "
                "(disk.storage.getlist) — did the storage get renamed/removed?"
            )
        self._storage_id = storages[0]["ID"]

        clients = self._find_child_by_name(self._storage_id, CLIENTS_FOLDER_NAME, at_storage_root=True)
        if not clients:
            raise RuntimeError(
                f"Bitrix24: '{CLIENTS_FOLDER_NAME}' folder not found at the root of Company Drive — "
                "create it once in the Bitrix24 UI (Диск -> Загальний диск)."
            )
        self._clients_folder_id = clients["ID"]

    def _find_child_by_name(self, parent_id, name: str, *, at_storage_root: bool = False):
        method = "disk.storage.getchildren" if at_storage_root else "disk.folder.getchildren"
        result = bitrix._post(method, {"id": parent_id, "filter": {"NAME": name}})
        for item in result["result"]:
            if item["NAME"] == name and item["TYPE"] == "folder":
                return item
        return None

    def get_or_create_client_folder(self, full_name: str, phone: str) -> dict:
        self._ensure_clients_folder()
        # "|" avoided as a separator — some Disk backends reject it in
        # folder names even though Google Drive tolerates it fine.
        name = f"{_sanitize_name(full_name)} - {phone}"
        existing = self._find_child_by_name(self._clients_folder_id, name)
        if existing:
            return {"id": existing["ID"], "webViewLink": existing.get("DETAIL_URL")}
        result = bitrix._post("disk.folder.addsubfolder", {"id": self._clients_folder_id, "data": {"NAME": name}})
        created = result["result"]
        return {"id": created["ID"], "webViewLink": created.get("DETAIL_URL")}

    def get_or_create_folder(self, name: str, parent_id) -> dict:
        existing = self._find_child_by_name(parent_id, name)
        if existing:
            return {"id": existing["ID"], "webViewLink": existing.get("DETAIL_URL")}
        result = bitrix._post("disk.folder.addsubfolder", {"id": parent_id, "data": {"NAME": name}})
        created = result["result"]
        return {"id": created["ID"], "webViewLink": created.get("DETAIL_URL")}

    def upload_bytes(self, data: bytes, filename: str, folder_id, mimetype: str = "application/octet-stream") -> dict:
        encoded = base64.b64encode(data).decode("ascii")
        result = bitrix._post(
            "disk.folder.uploadfile",
            {
                "id": folder_id,
                "data": {"NAME": filename},
                "fileContent": [filename, encoded],
                "generateUniqueName": True,
            },
            timeout=60,
        )
        uploaded = result["result"]
        return {
            "id": uploaded["ID"],
            "name": uploaded.get("NAME", filename),
            "webViewLink": uploaded.get("DETAIL_URL"),
            "size": uploaded.get("SIZE", len(data)),
        }

    def update_file(self, file_id, filename: str, data: bytes) -> dict:
        """Overwrite an existing Disk file in place (new version, same file
        ID) instead of creating a sibling copy — used for ecpass/emailpass
        where re-saving should replace the previous answer, not pile up
        "Пароль (1).txt", "(2).txt", ..."""
        encoded = base64.b64encode(data).decode("ascii")
        result = bitrix._post(
            "disk.file.uploadversion",
            {"id": file_id, "fileContent": [filename, encoded]},
            timeout=60,
        )
        uploaded = result["result"]
        return {
            "id": uploaded["ID"],
            "name": uploaded.get("NAME", filename),
            "webViewLink": uploaded.get("DETAIL_URL"),
            "size": uploaded.get("SIZE", len(data)),
        }
