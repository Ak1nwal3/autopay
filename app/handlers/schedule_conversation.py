"""Bot /schedule conversation — manual recurring bill setup.

The user runs `/schedule` and walks through a multi-step state
machine to set up a future-dated (optionally recurring) bill:

  1. Vendor name
  2. Amount
  3. Account number (10-digit NUBAN)
  4. Bank code (3-digit, with quick-pick of the most common banks)
  5. First due date (quick-pick or typed)
  6. Recurrence interval (None / weekly / monthly)
  7. Confirm

On confirm, `app.services.bill.create_scheduled_bill()` persists
the bill as `status=scheduled, is_recurring=..., next_recurrence_date=due_date`
so the existing scheduler picks it up on the due date and (if
recurring) spawns the next occurrence after each successful
payout.

Why a dedicated state machine instead of reusing the bill
conversation?
  * The bill conversation is designed for **extracted** bills
    (photo / PDF / text → loader → LLM). The user has no edit
    path on extracted fields. `/schedule` is for bills the user
    **manually** knows — every field is a prompt + typed input.
  * The bill conversation is a single screen with multi-field
    edit. `/schedule` is sequential prompts, one field at a time,
    because the user has nothing to start from.
  * Recurrence is a first-class concept here, not an afterthought
    grafted onto a bill that already has 5 fields of extracted
    data.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.core.database import session_scope
from app.handlers.helpers import (
    date_from_quickpick,
    escape_md,
    get_linked_user,
    parse_user_date,
)
from app.services.bill import (
    BillValidationError,
    ScheduleBillInput,
    create_scheduled_bill,
)

logger = logging.getLogger(__name__)


# Conversation states — sequential prompts.
(
    SCH_VENDOR,
    SCH_AMOUNT,
    SCH_ACCOUNT,
    SCH_BANK,
    SCH_DATE,
    SCH_RECURRENCE,
    SCH_CONFIRM,
) = range(7)


# ── Keyboards ──────────────────────────────────────────────────────


# Top Nigerian banks — quick-pick avoids forcing the user to know
# the 3-digit bank code. `bank_name` is what we display; `bank_code`
# is what the rest of the app uses (Paystack `bank_code`).
_BANK_PRESETS: list[tuple[str, str, str]] = [
    ("GTBank",       "058", "Guaranty Trust Bank"),
    ("Access Bank",  "044", "Access Bank"),
    ("Zenith Bank",  "057", "Zenith Bank"),
    ("UBA",          "033", "United Bank for Africa"),
    ("First Bank",   "011", "First Bank of Nigeria"),
    ("Stanbic IBTC", "221", "Stanbic IBTC Bank"),
    ("Kuda",         "50211", "Kuda Microfinance Bank"),
    ("OPay",         "999992", "OPay"),
]


def bank_quick_pick_keyboard() -> InlineKeyboardMarkup:
    """2-column grid of common banks. Each button's callback_data is
    `sch_bank:<code>:<short_name>` so we can recover both."""
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(_BANK_PRESETS), 2):
        chunk = _BANK_PRESETS[i : i + 2]
        rows.append(
            [
                InlineKeyboardButton(
                    f"{name}",
                    callback_data=f"sch_bank:{code}:{name}",
                )
                for (name, code, _full) in chunk
            ]
        )
    rows.append([
        InlineKeyboardButton(
            "🔢 Other (type code)", callback_data="sch_bank_other"
        )
    ])
    rows.append([
        InlineKeyboardButton("⬅️ Back", callback_data="sch_back_account"),
        InlineKeyboardButton("❌ Cancel", callback_data="sch_cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def date_quick_pick_keyboard(prefix: str = "sch_date") -> InlineKeyboardMarkup:
    """Reuse the same quick-pick UX as the bill editor."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Today",     callback_data=f"{prefix}_today"),
            InlineKeyboardButton("📅 Tomorrow",  callback_data=f"{prefix}_tomorrow"),
        ],
        [
            InlineKeyboardButton("📅 +1 week",  callback_data=f"{prefix}_+1w"),
            InlineKeyboardButton("📅 +1 month", callback_data=f"{prefix}_+1m"),
        ],
        [
            InlineKeyboardButton("⏭ Skip (today)", callback_data=f"{prefix}_skip"),
        ],
        [
            InlineKeyboardButton("⬅️ Back", callback_data="sch_back_bank"),
            InlineKeyboardButton("❌ Cancel", callback_data="sch_cancel"),
        ],
    ])


def recurrence_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Every week",  callback_data="sch_recur:weekly"),
            InlineKeyboardButton("📅 Every month", callback_data="sch_recur:monthly"),
        ],
        [
            InlineKeyboardButton("🔂 One-off (no repeat)", callback_data="sch_recur:none"),
        ],
        [
            InlineKeyboardButton("⬅️ Back", callback_data="sch_back_date"),
            InlineKeyboardButton("❌ Cancel", callback_data="sch_cancel"),
        ],
    ])


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Schedule it", callback_data="sch_confirm_yes"),
            InlineKeyboardButton("✏️ Edit fields", callback_data="sch_confirm_edit"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="sch_cancel")],
    ])


# ── Step 0: /schedule entry point ──────────────────────────────────


async def schedule_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User runs `/schedule` → start the conversation."""
    chat_id = str(update.effective_chat.id)
    user = get_linked_user(chat_id)
    if user is None:
        await update.message.reply_text(
            "🔒 *Account not linked.*\n\n"
            "Send `/link YOUR_CODE` first, then try `/schedule` again.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # Reset state for a fresh run.
    context.user_data["sch"] = {}

    await update.message.reply_text(
        "📅 *Schedule a bill*\n\n"
        "I'll walk you through setting up a future-dated payment. "
        "If you mark it as recurring, I'll create a new occurrence "
        "automatically after each successful payment.\n\n"
        "*Step 1/6 — Vendor name*\n"
        "Who's the bill for? (e.g. `DSTV`, `EKEDC`, `MTN`)",
        parse_mode="Markdown",
    )
    return SCH_VENDOR


# ── Step 1: vendor ────────────────────────────────────────────────


async def handle_vendor(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User typed the vendor name."""
    # Defend against state being missing (e.g. user typed into a
    # stale conversation). The `schedule_command` entry point
    # primes this; if it's not there, we treat the input as the
    # start of a fresh flow.
    context.user_data.setdefault("sch", {})
    vendor = (update.message.text or "").strip()
    if not vendor or len(vendor) > 255:
        await update.message.reply_text(
            "⚠️ Vendor name must be 1–255 characters. Try again:",
            parse_mode="Markdown",
        )
        return SCH_VENDOR
    context.user_data["sch"]["vendor_name"] = vendor

    await update.message.reply_text(
        f"✅ Vendor: *{escape_md(vendor)}*\n\n"
        f"*Step 2/6 — Amount*\n"
        f"How much is the bill? (e.g. `5000` or `12,500.50`)",
        parse_mode="Markdown",
    )
    return SCH_AMOUNT


# ── Step 2: amount ────────────────────────────────────────────────


async def handle_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data.setdefault("sch", {})
    text = (update.message.text or "").strip()
    cleaned = text.replace(",", "").replace(" ", "")
    try:
        amount = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        await update.message.reply_text(
            f"⚠️ *{escape_md(text)}* isn't a valid amount.\n\n"
            f"Try again (e.g. `5000` or `12,500.00`):",
            parse_mode="Markdown",
        )
        return SCH_AMOUNT
    if amount < 100 or amount > 10_000_000:
        await update.message.reply_text(
            "⚠️ Amount must be between ₦100 and ₦10,000,000.\n\nTry again:",
            parse_mode="Markdown",
        )
        return SCH_AMOUNT

    context.user_data["sch"]["amount"] = float(amount)

    await update.message.reply_text(
        f"✅ Amount: *₦{float(amount):,.2f}*\n\n"
        f"*Step 3/6 — Account number*\n"
        f"What's the destination account number? (10–11 digits)",
        parse_mode="Markdown",
    )
    return SCH_ACCOUNT


# ── Step 3: account number ────────────────────────────────────────


async def handle_account(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data.setdefault("sch", {})
    text = (update.message.text or "").strip().replace(" ", "")
    if not text.isdigit() or not (10 <= len(text) <= 11):
        await update.message.reply_text(
            "⚠️ Account number must be 10 or 11 digits. Try again:",
            parse_mode="Markdown",
        )
        return SCH_ACCOUNT
    context.user_data["sch"]["account_number"] = text

    await update.message.reply_text(
        f"✅ Account: `{escape_md(text)}`\n\n"
        f"*Step 4/6 — Bank*\n"
        f"Pick the bank, or tap *Other* to type the 3-digit bank code.",
        parse_mode="Markdown",
        reply_markup=bank_quick_pick_keyboard(),
    )
    return SCH_BANK


# ── Step 4: bank code ─────────────────────────────────────────────


async def handle_bank_picked(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User tapped a quick-pick bank button."""
    context.user_data.setdefault("sch", {})
    query = update.callback_query
    await query.answer()
    # Parse `sch_bank:<code>:<name>`.
    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.edit_message_text("⚠️ Bad bank choice. Run /schedule again.")
        return ConversationHandler.END
    _, code, name = parts
    context.user_data["sch"]["bank_code"] = code
    context.user_data["sch"]["bank_name"] = name
    return await _go_to_date(query, context)


async def handle_bank_other(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User tapped 'Other' — ask them to type a 3-digit code."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🔢 *Type the 3-digit bank code*\n\n"
        "If you don't know it, see "
        "[Paystack's bank list]"
        "(https://paystack.com/payment-gateway/banks) — tap a bank to "
        "see its code.",
        parse_mode="Markdown",
    )
    return SCH_BANK  # stay in this state; typed input goes to handle_bank_typed


async def handle_bank_typed(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User typed a bank code (instead of tapping a quick-pick)."""
    context.user_data.setdefault("sch", {})
    text = (update.message.text or "").strip()
    if not text.isdigit() or not (3 <= len(text) <= 6):
        await update.message.reply_text(
            "⚠️ Bank codes are 3–6 digits. Try again:",
            parse_mode="Markdown",
        )
        return SCH_BANK

    context.user_data["sch"]["bank_code"] = text
    # Look up the friendly name from our preset list, fall back to the code.
    friendly = next(
        (n for (n, c, _full) in _BANK_PRESETS if c == text),
        f"Bank {text}",
    )
    context.user_data["sch"]["bank_name"] = friendly

    # Send a fresh message + the date keyboard (we're in a typed-text
    # state, not a callback-query state, so we can't edit).
    await update.message.reply_text(
        f"✅ Bank: *{escape_md(friendly)}* (`{escape_md(text)}`)\n\n"
        f"*Step 5/6 — First due date*\n"
        f"When should the first payment happen?",
        parse_mode="Markdown",
        reply_markup=date_quick_pick_keyboard(),
    )
    return SCH_DATE


# ── Step 5: due date ──────────────────────────────────────────────


async def handle_date_picked(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User tapped a quick-pick date button."""
    context.user_data.setdefault("sch", {})
    query = update.callback_query
    await query.answer()
    token = query.data.replace("sch_date_", "")
    picked = date_from_quickpick(token)
    if picked is None:
        picked = datetime.now()
    context.user_data["sch"]["due_date"] = picked
    return await _go_to_recurrence(query, context)


async def handle_date_typed(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User typed a date manually."""
    context.user_data.setdefault("sch", {})
    text = (update.message.text or "").strip()
    parsed = parse_user_date(text)
    if parsed is None:
        await update.message.reply_text(
            f"⚠️ I couldn't parse *{escape_md(text)}* as a date.\n\n"
            f"Try `2026-12-31` or `31 Dec 2026`, or pick a quick option:",
            parse_mode="Markdown",
            reply_markup=date_quick_pick_keyboard(),
        )
        return SCH_DATE
    if parsed < datetime.now() - timedelta(minutes=1):
        await update.message.reply_text(
            "⚠️ That date is in the past. Pick a future date:",
            parse_mode="Markdown",
            reply_markup=date_quick_pick_keyboard(),
        )
        return SCH_DATE
    context.user_data["sch"]["due_date"] = parsed
    return await _go_to_recurrence_from_message(update, context)


# ── Step 6: recurrence ───────────────────────────────────────────


async def handle_recurrence(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User picked a recurrence interval."""
    context.user_data.setdefault("sch", {})
    query = update.callback_query
    await query.answer()
    token = query.data.replace("sch_recur:", "")
    interval = None if token == "none" else token
    context.user_data["sch"]["recurrence_interval"] = interval
    return await _show_confirm(query, context)


# ── Step 7: confirm ──────────────────────────────────────────────


async def handle_confirm_yes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User tapped 'Schedule it'. Persist the bill and notify."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Saving your bill...")

    data = context.user_data.get("sch", {})
    chat_id = str(query.message.chat_id)
    user = get_linked_user(chat_id)
    if user is None:
        await query.edit_message_text("🔒 Account not linked. Run /link first.")
        context.user_data.pop("sch", None)
        return ConversationHandler.END

    payload = ScheduleBillInput(
        vendor_name=data.get("vendor_name", ""),
        amount=Decimal(str(data.get("amount", 0))),
        due_date=data.get("due_date"),
        account_number=data.get("account_number"),
        bank_code=data.get("bank_code"),
        bank_name=data.get("bank_name"),
        recurrence_interval=data.get("recurrence_interval"),
    )

    try:
        with session_scope() as session:
            bill = create_scheduled_bill(session, user_id=user.id, payload=payload)
    except BillValidationError as exc:
        await query.edit_message_text(f"⚠️ {exc}\n\nRun /schedule again.")
        context.user_data.pop("sch", None)
        return ConversationHandler.END

    # Summary.
    recur_label = {
        "weekly": "every week",
        "monthly": "every month",
        None: "one-off (no repeat)",
    }[payload.recurrence_interval]
    body = (
        f"✅ *Bill scheduled!*\n\n"
        f"🆔 *Bill #{bill.id}*\n"
        f"🏢 Vendor: *{escape_md(bill.vendor_name)}*\n"
        f"💰 Amount: *₦{float(bill.amount):,.2f}*\n"
        f"🏦 Account: `{escape_md(bill.account_number or 'N/A')}` "
        f"({escape_md(bill.bank_name or bill.bank_code or 'N/A')})\n"
        f"📅 First payment: {bill.due_date.date().isoformat()}\n"
        f"🔁 Repeat: {recur_label}\n\n"
        f"I'll auto-pay it on the due date. Run /bills to see it any time."
    )
    await query.edit_message_text(body, parse_mode="Markdown")
    context.user_data.pop("sch", None)
    return ConversationHandler.END


async def handle_confirm_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User tapped 'Edit fields'. Re-show the multi-field editor
    with the values they've entered so far."""
    query = update.callback_query
    await query.answer()

    data = context.user_data.get("sch", {})
    # Re-emit the keyboard with the current values filled in so
    # the user can see what they entered.
    amount = data.get("amount", 0)
    due_date = data.get("due_date")
    due_label = due_date.date().isoformat() if due_date else "?"
    bank = data.get("bank_name", "N/A")
    recur = data.get("recurrence_interval")
    recur_label = {"weekly": "Weekly", "monthly": "Monthly"}.get(
        recur or "", "One-off"
    )

    text = (
        "✏️ *Edit fields*\n\n"
        f"🏢 Vendor: `{escape_md(data.get('vendor_name', 'N/A'))}`\n"
        f"💰 Amount: `₦{float(amount):,.2f}`\n"
        f"🏦 Account: `{escape_md(str(data.get('account_number', 'N/A')))}` ({escape_md(bank)})\n"
        f"📅 First due: `{escape_md(due_label)}`\n"
        f"🔁 Repeat: `{recur_label}`\n\n"
        "Which field do you want to change?"
    )

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("🏢 Vendor",   callback_data="sch_edit:vendor")],
        [InlineKeyboardButton("💰 Amount",   callback_data="sch_edit:amount")],
        [InlineKeyboardButton("🏦 Account",  callback_data="sch_edit:account")],
        [InlineKeyboardButton("📅 Date",     callback_data="sch_edit:date")],
        [InlineKeyboardButton("🔁 Recurrence", callback_data="sch_edit:recur")],
        [InlineKeyboardButton("⬅️ Back to confirm", callback_data="sch_edit:back")],
        [InlineKeyboardButton("❌ Cancel", callback_data="sch_cancel")],
    ]
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )
    return SCH_CONFIRM


async def handle_edit_field(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User tapped a field in the multi-field editor. Send them
    back to the right state to re-enter it."""
    query = update.callback_query
    await query.answer()
    field = query.data.replace("sch_edit:", "")

    if field == "back":
        return await _show_confirm(query, context)
    if field == "vendor":
        await query.edit_message_text(
            "*Re-enter the vendor name:*\n"
            f"Current: `{escape_md(context.user_data.get('sch', {}).get('vendor_name', 'N/A'))}`",
            parse_mode="Markdown",
        )
        return SCH_VENDOR
    if field == "amount":
        await query.edit_message_text(
            "*Re-enter the amount:*\n"
            f"Current: `₦{float(context.user_data.get('sch', {}).get('amount', 0)):,.2f}`",
            parse_mode="Markdown",
        )
        return SCH_AMOUNT
    if field == "account":
        await query.edit_message_text(
            "*Re-enter the account number:*\n"
            f"Current: `{escape_md(str(context.user_data.get('sch', {}).get('account_number', 'N/A')))}`",
            parse_mode="Markdown",
        )
        return SCH_ACCOUNT
    if field == "date":
        await query.edit_message_text(
            "*Re-pick the first due date:*",
            parse_mode="Markdown",
            reply_markup=date_quick_pick_keyboard(),
        )
        return SCH_DATE
    if field == "recur":
        await query.edit_message_text(
            "*Re-pick the recurrence interval:*",
            parse_mode="Markdown",
            reply_markup=recurrence_keyboard(),
        )
        return SCH_RECURRENCE
    return SCH_CONFIRM  # fallback


# ── Back / Cancel ────────────────────────────────────────────────


async def handle_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User tapped Cancel or sent /cancel mid-flow."""
    query = getattr(update, "callback_query", None)
    if query is not None:
        try:
            await query.answer()
        except Exception:  # noqa: BLE001
            pass
        if hasattr(query, "edit_message_text"):
            await query.edit_message_text(
                "❌ Schedule cancelled. Run /schedule whenever you're ready."
            )
    else:
        await update.message.reply_text(
            "❌ Schedule cancelled. Run /schedule whenever you're ready."
        )
    context.user_data.pop("sch", None)
    return ConversationHandler.END


async def cancel_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data.pop("sch", None)
    await update.message.reply_text(
        "❌ Schedule cancelled. Run /schedule whenever you're ready."
    )
    return ConversationHandler.END


# ── Back-button handlers (each one undoes the last step) ──────────


async def handle_back_account(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "*Step 3/6 — Account number*\n"
        "What's the destination account number? (10–11 digits)",
        parse_mode="Markdown",
    )
    return SCH_ACCOUNT


async def handle_back_bank(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "*Step 4/6 — Bank*\n"
        "Pick the bank, or tap *Other* to type the 3-digit bank code.",
        parse_mode="Markdown",
        reply_markup=bank_quick_pick_keyboard(),
    )
    return SCH_BANK


async def handle_back_date(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "*Step 5/6 — First due date*\n"
        "When should the first payment happen?",
        parse_mode="Markdown",
        reply_markup=date_quick_pick_keyboard(),
    )
    return SCH_DATE


# ── Shared helpers ────────────────────────────────────────────────


async def _go_to_date(query, context) -> int:
    """Common transition: bank picked → show the date keyboard."""
    bank = context.user_data["sch"].get("bank_name", "?")
    code = context.user_data["sch"].get("bank_code", "?")
    await query.edit_message_text(
        f"✅ Bank: *{escape_md(bank)}* (`{escape_md(code)}`)\n\n"
        f"*Step 5/6 — First due date*\n"
        f"When should the first payment happen?",
        parse_mode="Markdown",
        reply_markup=date_quick_pick_keyboard(),
    )
    return SCH_DATE


async def _go_to_recurrence(query, context) -> int:
    """Common transition: date picked → show the recurrence keyboard."""
    due_date = context.user_data["sch"].get("due_date")
    due_label = due_date.date().isoformat() if due_date else "?"
    await query.edit_message_text(
        f"✅ First due: *{escape_md(due_label)}*\n\n"
        f"*Step 6/6 — Repeat*\n"
        f"Should this bill repeat, or is it a one-off?",
        parse_mode="Markdown",
        reply_markup=recurrence_keyboard(),
    )
    return SCH_RECURRENCE


async def _go_to_recurrence_from_message(update, context) -> int:
    """Same as `_go_to_recurrence` but the trigger was a typed
    message (not a callback), so we reply with a fresh message."""
    due_date = context.user_data["sch"].get("due_date")
    due_label = due_date.date().isoformat() if due_date else "?"
    await update.message.reply_text(
        f"✅ First due: *{escape_md(due_label)}*\n\n"
        f"*Step 6/6 — Repeat*\n"
        f"Should this bill repeat, or is it a one-off?",
        parse_mode="Markdown",
        reply_markup=recurrence_keyboard(),
    )
    return SCH_RECURRENCE


async def _show_confirm(query, context) -> int:
    """Render the final confirm screen with the values entered so
    far. Used after both the recurrence pick AND the 'Back to
    confirm' editor button."""
    data = context.user_data.get("sch", {})
    amount = data.get("amount", 0)
    due_date = data.get("due_date")
    due_label = due_date.date().isoformat() if due_date else "?"
    bank = data.get("bank_name", "N/A")
    recur = data.get("recurrence_interval")
    recur_label = {"weekly": "Every week", "monthly": "Every month"}.get(
        recur or "", "One-off"
    )

    text = (
        "📋 *Review your bill*\n\n"
        f"🏢 Vendor: *{escape_md(data.get('vendor_name', 'N/A'))}*\n"
        f"💰 Amount: *₦{float(amount):,.2f}*\n"
        f"🏦 Account: `{escape_md(str(data.get('account_number', 'N/A')))}`\n"
        f"   Bank: {escape_md(bank)}\n"
        f"📅 First payment: *{escape_md(due_label)}*\n"
        f"🔁 Repeat: *{recur_label}*\n\n"
        f"Looks good?"
    )
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=confirm_keyboard()
    )
    return SCH_CONFIRM


# ── ConversationHandler factory ──────────────────────────────────


def build_schedule_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("schedule", schedule_command)],
        states={
            SCH_VENDOR: [
                # Reject commands so a stray /start doesn't end up here.
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, handle_vendor
                ),
                CallbackQueryHandler(handle_cancel, pattern=r"^sch_cancel$"),
            ],
            SCH_AMOUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, handle_amount
                ),
                CallbackQueryHandler(handle_cancel, pattern=r"^sch_cancel$"),
            ],
            SCH_ACCOUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, handle_account
                ),
                CallbackQueryHandler(handle_cancel, pattern=r"^sch_cancel$"),
            ],
            SCH_BANK: [
                CallbackQueryHandler(handle_bank_picked, pattern=r"^sch_bank:\d+:.+$"),
                CallbackQueryHandler(handle_bank_other, pattern=r"^sch_bank_other$"),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, handle_bank_typed
                ),
                CallbackQueryHandler(handle_back_account, pattern=r"^sch_back_account$"),
                CallbackQueryHandler(handle_cancel, pattern=r"^sch_cancel$"),
            ],
            SCH_DATE: [
                CallbackQueryHandler(handle_date_picked, pattern=r"^sch_date_(today|tomorrow|\+1w|\+1m|skip)$"),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, handle_date_typed
                ),
                CallbackQueryHandler(handle_back_bank, pattern=r"^sch_back_bank$"),
                CallbackQueryHandler(handle_cancel, pattern=r"^sch_cancel$"),
            ],
            SCH_RECURRENCE: [
                CallbackQueryHandler(handle_recurrence, pattern=r"^sch_recur:(weekly|monthly|none)$"),
                CallbackQueryHandler(handle_back_date, pattern=r"^sch_back_date$"),
                CallbackQueryHandler(handle_cancel, pattern=r"^sch_cancel$"),
            ],
            SCH_CONFIRM: [
                CallbackQueryHandler(handle_confirm_yes, pattern=r"^sch_confirm_yes$"),
                CallbackQueryHandler(handle_confirm_edit, pattern=r"^sch_confirm_edit$"),
                CallbackQueryHandler(handle_edit_field, pattern=r"^sch_edit:(vendor|amount|account|date|recur|back)$"),
                CallbackQueryHandler(handle_cancel, pattern=r"^sch_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_user=True,
        per_chat=True,
    )
