"""Shared helpers for the Telegram bot handlers.

`get_linked_user(chat_id)` is the single point where a Telegram chat
ID gets resolved to a `User` row. Every conversation handler calls
this on entry; the unauthenticated case returns None and the handler
tells the user to run /link.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.database import session_scope
from app.models.user import User
from app.models.virtual_account import VirtualAccount


# ── Editable fields the user can correct on the extracted bill ──────

EDITABLE_FIELDS = {
    "vendor_name": "🏢 Vendor name",
    "amount": "💰 Amount",
    "due_date": "📅 Due date",
    "account_number": "🏦 Account number",
    "bank_code": "🔢 Bank code",
}


# ── Keyboards ───────────────────────────────────────────────────────

def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data="confirm"),
            InlineKeyboardButton("✏️ Edit", callback_data="edit"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


def field_keyboard() -> InlineKeyboardMarkup:
    """Legacy single-column field picker. Kept for the conversation
    state's pattern compatibility; the new multi-field editor uses
    `multi_field_editor_keyboard` instead."""
    rows = [
        [InlineKeyboardButton(label, callback_data=f"field:{key}")]
        for key, label in EDITABLE_FIELDS.items()
    ]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def multi_field_editor_keyboard(
    bill_data: dict, edited_keys: set[str] | None = None
) -> InlineKeyboardMarkup:
    """The new edit-mode list. Each field is a button labeled with
    its current value; tapping one enters edit mode for that field.
    A "Done" button at the bottom returns to the confirm screen.

    `edited_keys` (optional) is a set of field names that have been
    changed in this session; those rows get a small "📝" marker so
    the user can see what they touched.
    """
    edited_keys = edited_keys or set()
    rows: list[list[InlineKeyboardButton]] = []
    for key, label in EDITABLE_FIELDS.items():
        marker = " 📝" if key in edited_keys else ""
        # Truncate long values for button label readability.
        value = bill_data.get(key, "N/A")
        if value is None or value == "":
            value = "N/A"
        text = f"{label}: `{str(value)[:32]}`{marker}"
        # Inline Markdown in buttons is fine in PTB; we'll let the
        # rendered label be plain for safety.
        text = text.replace("`", "")
        rows.append(
            [InlineKeyboardButton(text, callback_data=f"edit_field:{key}")]
        )
    rows.append(
        [
            InlineKeyboardButton("✅ Done editing", callback_data="edit_done"),
            InlineKeyboardButton("↩️ Discard", callback_data="edit_discard"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def date_quickpick_keyboard() -> InlineKeyboardMarkup:
    """Inline buttons for common due-date choices. The callback_data
    encodes the choice (`date_today`, `date_tomorrow`, `date_+1w`,
    `date_+1m`, `date_skip`); the handler maps each to a real
    datetime value."""
    from telegram import InlineKeyboardButton
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Today", callback_data="date_today"),
            InlineKeyboardButton("📅 Tomorrow", callback_data="date_tomorrow"),
        ],
        [
            InlineKeyboardButton("📅 +1 week", callback_data="date_+1w"),
            InlineKeyboardButton("📅 +1 month", callback_data="date_+1m"),
        ],
        [
            InlineKeyboardButton("⏭ Skip (use today)", callback_data="date_skip"),
        ],
    ])


def final_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Pay now", callback_data="final_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="final_cancel"),
        ],
    ])


# ── Formatters ──────────────────────────────────────────────────────

def format_bill_summary(data: dict) -> str:
    amount = data.get("amount")
    try:
        amount_str = f"{float(amount):,.2f}" if amount is not None else "N/A"
    except (TypeError, ValueError):
        amount_str = str(amount)
    return (
        "📄 *Extracted Bill Details*\n\n"
        f"🏢 Vendor: `{data.get('vendor_name', 'N/A')}`\n"
        f"💰 Amount: `{amount_str} {data.get('currency', 'NGN')}`\n"
        f"📅 Due date: `{data.get('due_date', 'N/A')}`\n"
        f"🏦 Account: `{data.get('account_number', 'N/A')}`\n"
        f"🔢 Bank code: `{data.get('bank_code', 'N/A')}`\n\n"
        "Is this correct?"
    )


def format_multi_field_editor(
    data: dict, edited_keys: set[str] | None = None
) -> str:
    """Header for the multi-field editor. Lists all 5 fields with
    their current values and a 📝 marker on any field the user has
    changed in this session."""
    edited_keys = edited_keys or set()
    lines = ["✏️ *Edit bill details*\n"]
    lines.append("_Tap a field to change it. Changes are saved when you tap 'Done'._\n")
    for key, label in EDITABLE_FIELDS.items():
        value = data.get(key, "N/A")
        if value is None or value == "":
            value = "N/A"
        marker = "  📝" if key in edited_keys else ""
        # Use plain backticks (no nested code) for safety.
        lines.append(f"{label}: `{value}`{marker}")
    return "\n".join(lines)


# ── Auth helper ─────────────────────────────────────────────────────

def get_linked_user(chat_id: str | int) -> Optional[User]:
    """Look up the user linked to a Telegram chat id. Returns None if
    the chat hasn't been linked yet.

    Eager-loads common fields (id, balance, telegram_chat_id,
    is_telegram_linked, first_name, last_name, email) so the returned
    object is usable after the session closes — otherwise
    SQLAlchemy would raise DetachedInstanceError on first attribute
    access.
    """
    from sqlalchemy import select as _sa_select
    with session_scope() as session:
        user = session.exec(
            _sa_select(User).where(
                User.telegram_chat_id == str(chat_id),
                User.is_telegram_linked == True,  # noqa: E712
            )
        ).scalar_one_or_none()
        if user is not None:
            # Force-load the fields the handlers care about.
            _ = (user.id, user.balance, user.currency, user.telegram_chat_id,
                 user.first_name, user.last_name, user.email)
        return user


def get_user_va(user: User) -> Optional[VirtualAccount]:
    from sqlalchemy import select as _sa_select
    with session_scope() as session:
        return session.exec(
            _sa_select(VirtualAccount).where(VirtualAccount.user_id == user.id)
        ).scalar_one_or_none()


# ── Date parsing (bot-side) ─────────────────────────────────────────

def parse_user_date(text: str) -> Optional[datetime]:
    """Try a few common formats. Returns naive datetime on success.
    The bot only needs to read user input; the API's date_parser has
    the full LLM-aware logic."""
    from dateutil import parser as dateparser
    try:
        dt = dateparser.parse(text, fuzzy=True)
        return dt.replace(tzinfo=None) if dt else None
    except (ValueError, TypeError, OverflowError):
        return None


def date_from_quickpick(token: str) -> Optional[datetime]:
    """Map a date-quick-pick callback_data token to a real datetime.

    `token` is the part after the `date_` prefix, e.g. `today`,
    `tomorrow`, `+1w`, `+1m`, `skip`. Returns naive UTC datetime, or
    None if the token is unrecognized.
    """
    now = datetime.now()
    if token == "today":
        return now
    if token == "tomorrow":
        return now + timedelta(days=1)
    if token == "+1w":
        return now + timedelta(weeks=1)
    if token == "+1m":
        return now + timedelta(days=30)
    if token == "skip":
        # Caller interprets skip as "fall back to today".
        return now
    return None


# ── Markdown escape ─────────────────────────────────────────────────

def escape_md(text: str) -> str:
    """Escape Telegram Markdown V1 reserved chars. Used whenever we
    interpolate user-supplied data into a Markdown message."""
    if not text:
        return ""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


__all__ = [
    "EDITABLE_FIELDS",
    "confirm_keyboard",
    "field_keyboard",
    "multi_field_editor_keyboard",
    "date_quickpick_keyboard",
    "final_keyboard",
    "format_bill_summary",
    "format_multi_field_editor",
    "get_linked_user",
    "get_user_va",
    "parse_user_date",
    "date_from_quickpick",
    "escape_md",
]
