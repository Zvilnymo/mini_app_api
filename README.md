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
- `BITRIX_WEBHOOK` / `BITRIX_WEBHOOK_TASK` are used for write paths with no
  warehouse equivalent:
  - `POST /api/complaints` — creates a Bitrix24 task routed by department
    (see `complaints.py` for the department -> RESPONSIBLE_ID mapping and
    who's always CC'd via AUDITORS), mirroring `complaint_bot`'s flow. Uses
    `BITRIX_WEBHOOK_TASK` specifically, since the deals webhook's token
    isn't scoped for `tasks.task.add`.
  - `POST /api/documents/upload` — uploads the file into Bitrix24 Disk
    (company common storage, see `bitrix_disk.py`), same folder layout
    (`{full name} | {phone}` -> subfolders) documents_bot uses on Google
    Drive, but this is a **separate, unrelated destination** — nothing here
    touches documents_bot's Google Drive folders. Needs `disk` (folder read/
    write/upload) permission on the webhook.
  - `POST /api/payments/{invoice_id}/receipt` — uploads a payment receipt
    into the same Bitrix Disk client folder, sets it directly on the
    invoice's own file field (`INVOICE_RECEIPT_FIELD` in `bitrix.py`, UF
    code `ufCrm_SMART_INVOICE_1783867026383`), and posts a comment linking
    to the Disk copy on that invoice's timeline (`crm.timeline.comment.add`,
    invoice = Bitrix Smart Process entityTypeId=31, see
    `payments.py`/`bitrix.py`).
    This is new — `documents_bot`'s old receipt flow only ever saved to
    Google Drive and pinged admins over Telegram, it never touched Bitrix.
    The app never changes the invoice's stage itself; a manager reviews the
    receipt on the invoice and moves it manually. Needs `crm` (timeline
    write) permission on the webhook.

## Env vars

- `DATABASE_URL` — same Postgres as documents_bot
- `TELEGRAM_BOT_TOKEN` — same bot that hosts documents_bot (validates the
  Mini App's initData)
- `OPENAI_API_KEY` — used by ai_document_validator.py
- `BITRIX_WEBHOOK` — same webhook documents_bot uses, must allow `disk.*`
  (folder/file read+write) and `crm` (timeline comment write, for invoice
  receipts)
- `BITRIX_WEBHOOK_TASK` — separate webhook token (same `B24_DOMAIN`/
  `B24_USER_ID`, different token) scoped for `tasks.task.add`, used only by
  `POST /api/complaints`. Falls back to `BITRIX_WEBHOOK` if unset.
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
