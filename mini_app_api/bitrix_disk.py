"""
Bitrix24 Disk storage — same interface shape as drive.py's DriveManager
(get_or_create_client_folder / get_or_create_folder / upload_bytes) so
documents.py can use either with minimal changes. Files land inside the
"CLIENTS" folder that already exists at the root of "Загальний диск"
(the company's common Disk storage, ENTITY_TYPE=common) — not the storage
root itself. Same folder layout as the Google Drive version below that:
"{full_name} | {phone}" -> subfolders.

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
        result = bitrix._post("disk.storage.getlist", {"filter": {"ENTITY_TYPE": "common"}})
        storages = result["result"]
        if not storages:
            raise RuntimeError("Bitrix24: no common Disk storage found (disk.storage.getlist)")
        self._storage_id = storages[0]["ID"]

        clients = self._find_child_by_name(self._storage_id, CLIENTS_FOLDER_NAME, at_storage_root=True)
        if not clients:
            raise RuntimeError(
                f"Bitrix24: '{CLIENTS_FOLDER_NAME}' folder not found at the root of Загальний диск — "
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
        name = f"{_sanitize_name(full_name)} | {phone}"
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
