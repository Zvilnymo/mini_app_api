# mini_app_api

FastAPI backend for the Zvilnymo Telegram Mini App (`mini_app` repo). Deployed
as its own Render Web Service.

Reuses infrastructure already running for `documents_bot` rather than
duplicating it:
- Same Postgres database (`DATABASE_URL`) — reads `crm.*` (etl_zv warehouse:
  case stage, payments, debt overview) and `docbot.*` (documents_bot's own
  tables: clients, documents).
- `ai_document_validator.py` and `prompts.py` are copied from `documents_bot`
  (not imported across repos) — keep them in sync by hand if the validation
  prompts change there.
- `BITRIX_WEBHOOK` is used for two write paths with no warehouse equivalent:
  - `POST /api/complaints` — creates a Bitrix24 task assigned to the case's
    manager. Needs `tasks.task.add` permission.
  - `POST /api/documents/upload` — uploads the file into Bitrix24 Disk
    (company common storage, see `bitrix_disk.py`), same folder layout
    (`{full name} | {phone}` -> subfolders) documents_bot uses on Google
    Drive, but this is a **separate, unrelated destination** — nothing here
    touches documents_bot's Google Drive folders. Needs `disk` (folder read/
    write/upload) permission on the webhook.

## Env vars

- `DATABASE_URL` — same Postgres as documents_bot
- `TELEGRAM_BOT_TOKEN` — same bot that hosts documents_bot (validates the
  Mini App's initData)
- `OPENAI_API_KEY` — used by ai_document_validator.py
- `BITRIX_WEBHOOK` — same webhook documents_bot uses, must allow
  `tasks.task.add` and `disk.*` (folder/file read+write)
- `CORS_ORIGIN` — the mini_app Static Site's URL

## Local dev

```
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... uvicorn mini_app_api.main:app --reload
```

Without `DATABASE_URL` set, `mini_app_api/db.py` falls back to reading
`../.env` (a local JSON credentials file also used by ad-hoc scripts in
`work_zvilnymo/`) — convenient for local testing, not used in production.

## Render

- Start Command: `uvicorn mini_app_api.main:app --host 0.0.0.0 --port $PORT`
- Root Directory: repo root
