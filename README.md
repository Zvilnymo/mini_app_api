# mini_app_api

FastAPI backend for the Zvilnymo Telegram Mini App (`mini_app` repo). Deployed
as its own Render Web Service.

Reuses infrastructure already running for `documents_bot` rather than
duplicating it:
- Same Postgres database (`DATABASE_URL`) — reads `crm.*` (etl_zv warehouse:
  case stage, payments, debt overview) and `docbot.*` (documents_bot's own
  tables: clients, documents).
- Same Google Drive credentials — uploads land in the same client folders
  documents_bot already creates.
- `ai_document_validator.py` and `prompts.py` are copied from `documents_bot`
  (not imported across repos) — keep them in sync by hand if the validation
  prompts change there.
- `BITRIX_WEBHOOK` is used for exactly one write path with no warehouse
  equivalent: creating a Bitrix24 task when a client submits a complaint
  (`POST /api/complaints`). That webhook needs `tasks.task.add` permission.

## Env vars

- `DATABASE_URL` — same Postgres as documents_bot
- `TELEGRAM_BOT_TOKEN` — same bot that hosts documents_bot (validates the
  Mini App's initData)
- `GOOGLE_OAUTH_TOKEN` or `GOOGLE_CREDENTIALS_BASE64` or
  `GOOGLE_CREDENTIALS_FILE` + `ROOT_FOLDER_ID` — same as documents_bot
- `OPENAI_API_KEY` — used by ai_document_validator.py
- `BITRIX_WEBHOOK` — same webhook documents_bot uses, must allow
  `tasks.task.add`
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
