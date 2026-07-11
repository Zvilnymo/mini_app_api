"""
"Декларація" questionnaire — copied from documents_bot's DECLARATION_QUESTIONS
(telegram_bot.py). Free-text answers, saved into docbot.declarations (same
table/columns the bot's own /declaration flow uses), then compiled into one
text file and uploaded to the client's "Декларація" subfolder on Bitrix Disk
— same content format as declaration_complete() in the bot, so a completed
declaration looks the same regardless of which surface (bot or mini app)
was used to fill it in.
"""
from __future__ import annotations

from . import db
from .documents import get_disk, resolve_subfolder

QUESTIONS = [
    {
        "key": "email_password",
        "emoji": "📧",
        "question": "Ваша електронна пошта та пароль яку вказували під час оформлення кредитів у разі втрати доступу — до діючої.",
        "required": True,
    },
    {
        "key": "living_address_2022_2025",
        "emoji": "🏠",
        "question": "Адреса фактичного місця проживання з 2022 по 2025 рік",
        "hint": "Якщо фактично 2022-2024 не проживали за місцем реєстрації, напишіть адреси, де проживали по роках конкретно; та адресу місця проживання за 2025 рік.",
        "required": True,
    },
    {
        "key": "registration_change",
        "emoji": "📍",
        "question": "Якщо була зміна адреси реєстрації (прописки) у 2022–2025 то вкажіть стару адресу та дату зміни",
        "required": False,
    },
    {
        "key": "property_alienation_self",
        "emoji": "🏡",
        "question": 'Опишіть чи було відчуження (дарування, продаж і т.д.) майна у вас у 2022–2025 роках. Якщо було — вкажіть деталі (що, коли, кому). Якщо не було — напишіть "Ні".',
        "required": True,
    },
    {
        "key": "property_alienation_family",
        "emoji": "👨‍👩‍👧",
        "question": 'Опишіть чи було відчуження майна у членів вашої сім\'ї у 2022–2025 роках. Якщо було — вкажіть деталі (хто, що, коли). Якщо не було — напишіть "Ні".',
        "required": True,
    },
    {
        "key": "family_vehicles",
        "emoji": "🚗",
        "question": 'Опишіть чи є у членів сім\'ї транспортні засоби у власності. Якщо так — вкажіть марку, рік, на кого зареєстровано. Якщо ні — напишіть "Ні".',
        "required": True,
    },
    {
        "key": "corporate_rights",
        "emoji": "📊",
        "question": 'Опишіть чи є у вас зараз або були у 2022-2024 роках корпоративні права, акції, цінні папери у власності. Якщо так — вкажіть деталі. Якщо ні — напишіть "Ні".',
        "required": True,
    },
    {
        "key": "crypto_foreign_credits",
        "emoji": "💱",
        "question": 'Опишіть чи є у вас кредити у криптовалюті або іноземній валюті. Якщо так — вкажіть деталі (сума, валюта, кредитор). Якщо ні — напишіть "Ні".',
        "required": True,
    },
    {
        "key": "specific_bank_credits",
        "emoji": "💱",
        "question": 'Опишіть чи є у вас кредит в АТ Ощадбанку, OTP bank або розстрочки від Monobank. Якщо так — вкажіть де саме та суму. Якщо ні — напишіть "Ні".',
        "required": True,
    },
    {
        "key": "online_betting",
        "emoji": "🎲",
        "question": 'Опишіть чи ставили ви коли-небудь ставки онлайн. Якщо так — вкажіть де та коли. Якщо ні — напишіть "Ні".',
        "required": True,
    },
    {
        "key": "bank_installments",
        "emoji": "💳",
        "question": 'Опишіть чи були у вас розстрочки в банках. Якщо так — вкажіть в яких банках та на що. Якщо ні — напишіть "Ні".',
        "required": True,
    },
    {
        "key": "creditor_address",
        "emoji": "📌",
        "question": "Яка адреса вказувалася кредиторам (не нова, не чиста)?",
        "required": True,
    },
    {
        "key": "housing_owner",
        "emoji": "🏠",
        "question": "Хто є власником житла, в якому ви зареєстровані/проживаєте?",
        "required": True,
    },
    {
        "key": "marriage_transactions",
        "emoji": "💍",
        "question": 'Опишіть чи куплялося/продавалося щось у шлюбі. Якщо так — вкажіть що саме та коли. Якщо ні або не перебуваєте в шлюбі — напишіть "Ні".',
        "required": True,
    },
    {
        "key": "alienation_documents",
        "emoji": "📑",
        "question": "Якщо було відчуження майна — опишіть, які документи є (договори купівлі/продажу, дарування тощо). Файли поки що можна надіслати менеджеру окремо.",
        "required": False,
    },
    {
        "key": "vehicle_power_of_attorney",
        "emoji": "🚘",
        "question": "Якщо авто досі зареєстроване на вас, але продане по довіреності — напишіть про це.",
        "required": False,
    },
    {
        "key": "alimony_info",
        "emoji": "❗",
        "question": 'Опишіть чи отримуєте аліменти на дітей/сплачуєте аліменти/маєте заборгованість по аліментах. Якщо так — вкажіть деталі. Якщо ні — напишіть "Ні". Можете пропустити це питання.',
        "required": False,
    },
]

QUESTION_BY_KEY = {q["key"]: q for q in QUESTIONS}


def get_answers(conn, client_id: int) -> dict:
    row = db.get_or_create_declaration(conn, client_id)
    return {q["key"]: row.get(q["key"]) or "" for q in QUESTIONS}


def is_complete(conn, client_id: int) -> bool:
    row = db.get_or_create_declaration(conn, client_id)
    return row.get("status") == "completed"


def _compile_text(client: dict, answers: dict) -> str:
    lines = [
        "АНКЕТА ДЛЯ СКЛАДАННЯ ПОДАТКОВОЇ ДЕКЛАРАЦІЇ",
        f"Клієнт: {client['full_name']}",
        f"Телефон: {client['phone']}",
        "=" * 80,
        "",
    ]
    for i, q in enumerate(QUESTIONS, 1):
        answer = (answers.get(q["key"]) or "").strip()
        lines.append(f"{i}. {q['question']}")
        lines.append(answer if answer else "(Пропущено)")
        lines.append("")
    return "\n".join(lines)


def save_and_submit(conn, client: dict, answers: dict) -> None:
    db.save_declaration_answers(conn, client["id"], answers)
    db.complete_declaration(conn, client["id"])

    disk = get_disk()
    subfolder = resolve_subfolder(disk, client, "declaration")
    text = _compile_text(client, answers)
    filename = f"Анкета_{client['full_name']}.txt"
    disk.upload_bytes(text.encode("utf-8"), filename, subfolder["id"], "text/plain")
