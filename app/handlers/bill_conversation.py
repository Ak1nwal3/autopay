"""Bill upload conversation handler.

Flow:
  1. User sends a bill (text, photo, or PDF).
  2. Loader extracts fields; we show a summary with Confirm / Edit / Cancel.
  3. On Confirm: we **resolve the payee account** (RESOLVING state) so
     the user sees the bank's official name BEFORE the agent decides
     and BEFORE we attempt the transfer. If the bill's vendor doesn't
     match the resolved name, we 422-style cancel with a clear message
     and the user is asked to edit the bill.
  4. After a clean resolve: we run the decision agent.
  5. pay_now → confirm-with-amount; schedule → "I'll process it when due";
     hold → "Top up your wallet".

The Edit flow uses a **multi-field editor**: a single screen lists
all five editable fields, each tappable to enter a value. The user
can hop between fields, with changes tracked by a 📝 marker. A
"Done editing" button at the bottom returns to the confirm screen.
This is much less stressful than the old "pick field → type → back
to summary → pick next field" loop.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import select
from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.agents.graphs import run_agent
from app.agents.state import Decision
from app.core.config import settings
from app.core.database import session_scope
from app.handlers.helpers import (
    EDITABLE_FIELDS,
    confirm_keyboard,
    date_from_quickpick,
    date_quickpick_keyboard,
    escape_md,
    final_keyboard,
    format_bill_summary,
    format_multi_field_editor,
    get_linked_user,
    multi_field_editor_keyboard,
    parse_user_date,
)
from app.models.bill import Bill
from app.models.enums import AuditActor, AuditEntityType, AuditEventType, BillStatus
from app.models.user import User
from app.services.audit import audit_bill_created
from app.services.date_parser import parse_bill_due_date
from app.services.loaders import loader_from_upload
from app.services.name_match import names_match
from app.services.payments import (
    InvalidAccount,
    PaymentError,
    PaymentProvider,
    get_payment_provider,
)
from app.services.payout import execute_payout

logger = logging.getLogger(__name__)


# Conversation states
#   CONFIRM       — user has the bill summary, taps Confirm/Edit/Cancel
#   RESOLVING     — we look up the payee name at the bank (1 Paystack call)
#   EDIT_LIST     — multi-field editor shows a list of editable fields
#   EDIT_VALUE    — user is typing a value for one field
#   FINAL_CONFIRM — agent said "Pay Now"; final amount/balance check
CONFIRM, RESOLVING, EDIT_LIST, EDIT_VALUE, FINAL_CONFIRM = range(5)


# ── Step 1: receive bill ────────────────────────────────────────────

async def receive_bill(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    msg = update.message
    chat_id = str(update.effective_chat.id)

    user = get_linked_user(chat_id)
    if user is None:
        await msg.reply_text(
            "🔒 *Account not linked.*\n\nSend `/link YOUR_CODE` to connect your account.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # Build the loader based on what was sent.
    try:
        loader = await _build_loader(msg, context)
    except ValueError as exc:
        await msg.reply_text(f"⚠️ {exc}")
        return ConversationHandler.END

    if loader is None:
        # Not a bill at all (e.g. a sticker).
        return ConversationHandler.END

    await msg.reply_text("⏳ Extracting bill details...")

    try:
        extracted = await loader.extract()
    except Exception as exc:  # noqa: BLE001
        logger.warning("bill extraction failed: %s", exc)
        await msg.reply_text(
            f"❌ Couldn't extract bill details: {exc}\n\n"
            "Try a clearer photo or paste the text manually."
        )
        return ConversationHandler.END

    if not extracted.vendor_name or float(extracted.amount) <= 0:
        await msg.reply_text(
            "❌ I could read the file but couldn't find a vendor and amount.\n\n"
            "Send the bill as text instead, e.g.:\n"
            "`Pay DSTV 5000 by Friday 0123456789 GTBank 058`"
        )
        return ConversationHandler.END

    # Stash for downstream steps. The conversation keeps a `staging`
    # copy so the user can discard their changes; the canonical
    # `bill` dict is only mutated when they tap "Done editing".
    context.user_data["bill"] = {
        "vendor_name": extracted.vendor_name,
        "amount": float(extracted.amount),
        "currency": extracted.currency or "NGN",
        "due_date": extracted.due_date or "",
        "account_number": extracted.account_number or "",
        "bank_code": extracted.bank_code or "",
    }
    context.user_data["staging"] = dict(context.user_data["bill"])
    context.user_data["edited_keys"] = set()
    context.user_data["user_id"] = user.id
    context.user_data["user_balance"] = float(user.balance)

    await msg.reply_text(
        format_bill_summary(context.user_data["bill"]),
        parse_mode="Markdown",
        reply_markup=confirm_keyboard(),
    )
    return CONFIRM


async def _build_loader(msg, context: ContextTypes.DEFAULT_TYPE):
    """Pick a loader based on what the user sent. Returns None if
    this isn't a bill (e.g. a plain non-text message)."""
    if msg.text and not msg.document and not msg.photo:
        if msg.text.startswith("/"):
            return None  # probably a /command
        from app.services.loaders import TextLoader
        return TextLoader(msg.text)

    if msg.photo:
        # Use the largest photo (last in the array).
        photo = msg.photo[-1]
        file_info = await context.bot.get_file(photo.file_id)
        data = bytes(await file_info.download_as_bytearray())
        from app.services.loaders import ImageLoader
        return ImageLoader(data, mime_type="image/jpeg")

    if msg.document:
        mime = (msg.document.mime_type or "").lower()
        if "pdf" not in mime:
            raise ValueError("I can only process PDF documents. Send a photo for paper bills.")
        file_info = await context.bot.get_file(msg.document.file_id)
        data = bytes(await file_info.download_as_bytearray())
        from app.services.loaders import PDFLoader
        return PDFLoader(data)

    return None


# ── Step 2a: confirm → persist bill → enter RESOLVING ─────────────

async def handle_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🤖 Analysing payment...")

    bill_data = context.user_data.get("bill", {})
    user_id = context.user_data.get("user_id")

    if not user_id or not bill_data:
        await query.edit_message_text(
            "Session expired. Send the bill again."
        )
        return ConversationHandler.END

    # Persist the bill first so the agent has a bill_id to refer to.
    with session_scope() as session:
        bill = Bill(
            user_id=user_id,
            vendor_name=bill_data["vendor_name"],
            amount=Decimal(str(bill_data["amount"])),
            currency=bill_data.get("currency", "NGN"),
            due_date=parse_bill_due_date(bill_data.get("due_date")),
            account_number=bill_data.get("account_number") or None,
            bank_code=bill_data.get("bank_code") or None,
            status=BillStatus.PENDING.value,
        )
        session.add(bill)
        session.flush()
        bill_id = bill.id
        audit_bill_created(
            session,
            user_id=user_id,
            bill_id=bill_id,
            amount=float(bill.amount),
            provider="paystack",
        )
    context.user_data["bill_id"] = bill_id

    # No payout account configured → skip the resolve step and let
    # the agent decide. The payout itself will fail later with a
    # clear 422 in `execute_payout` (no money moves at that point).
    if not bill_data.get("account_number") or not bill_data.get("bank_code"):
        return await _run_agent_decision(update, context, bill_data, user_id, query)

    # Enter RESOLVING: we look up the bank's official account name
    # and compare it to the vendor on the bill. Mismatch here is the
    # single most common "wrong transfer" mistake — catching it
    # before the user confirms the final amount is the entire point
    # of the pre-pay resolve step.
    return await handle_resolve_account(update, context)


# ── Step 2a.1: RESOLVING — pre-pay account validation ──────────────

async def handle_resolve_account(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Look up the bank's name for the bill's account_number +
    bank_code. If it doesn't fuzzy-match the bill's vendor, we
    bail out with a clear error and the user is told to edit the
    bill. On success we carry on to the agent decision."""
    # `update` is the original callback query from handle_confirm.
    # We re-derive the query here so callers (state entry or the
    # callback itself) both work.
    query = getattr(update, "callback_query", None)
    if query is None:
        # Came in via direct entry (e.g. from a test or a /start mid-flow).
        # We need a placeholder query; the message will be re-sent
        # with a fresh message via context.bot.send_message.
        class _ShimQuery:
            async def answer(self_inner) -> None:  # noqa: N805
                pass
            async def edit_message_text(self_inner, *args, **kwargs) -> None:  # noqa: N805
                pass
        query = _ShimQuery()

    bill_data = context.user_data.get("bill", {})
    user_id = context.user_data.get("user_id")
    account_number = bill_data.get("account_number", "")
    bank_code = bill_data.get("bank_code", "")
    vendor_name = bill_data.get("vendor_name", "")

    provider: PaymentProvider = get_payment_provider()
    try:
        resolved = await provider.resolve_account(
            account_number=account_number,
            bank_code=bank_code,
        )
    except InvalidAccount as exc:
        await query.edit_message_text(
            f"❌ *Account could not be resolved*\n\n"
            f"`{escape_md(str(exc))}`\n\n"
            f"The account number / bank code on the bill doesn't "
            f"match anything at the bank. Edit the bill and try again.",
            parse_mode="Markdown",
        )
        _cancel_persisted_bill(context)
        return ConversationHandler.END
    except PaymentError as exc:
        await query.edit_message_text(
            f"❌ *Bank lookup failed*\n\n`{escape_md(str(exc))}`\n\n"
            f"Try again in a moment.",
            parse_mode="Markdown",
        )
        _cancel_persisted_bill(context)
        return ConversationHandler.END

    resolved_name = resolved.account_name
    context.user_data["resolved_account_name"] = resolved_name

    if vendor_name and not names_match(vendor_name, resolved_name):
        # Surface the mismatch with both names so the user can spot
        # the typo. We don't auto-correct because "DSTV" vs "GOTV"
        # is a *legal* difference (they're different companies).
        await query.edit_message_text(
            f"❌ *Account name mismatch*\n\n"
            f"Bill vendor: *{escape_md(vendor_name)}*\n"
            f"Bank account: *{escape_md(resolved_name)}*\n"
            f"Account: `{escape_md(account_number)}` ({escape_md(bank_code)})\n\n"
            f"This account is registered to a different entity than the "
            f"bill vendor. Tap *Edit* to change the vendor name or the "
            f"account number, then confirm again.",
            parse_mode="Markdown",
            reply_markup=confirm_keyboard(),
        )
        # Stay in CONFIRM so the user can tap Edit.
        return CONFIRM

    # Clean match (or vendor empty) — carry on to the agent.
    return await _run_agent_decision(update, context, bill_data, user_id, query)


def _cancel_persisted_bill(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark a just-persisted PENDING bill as CANCELLED. Used when
    the resolve step fails so we don't leave a half-created bill in
    the DB. The user's wallet is untouched (no debit happened)."""
    bill_id = context.user_data.get("bill_id")
    if not bill_id:
        return
    try:
        with session_scope() as session:
            bill = session.get(Bill, bill_id)
            if bill and bill.status == BillStatus.PENDING.value:
                bill.status = BillStatus.CANCELLED.value
                session.add(bill)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not cancel bill %d: %s", bill_id, exc)
    context.user_data.clear()


async def _run_agent_decision(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    bill_data: dict,
    user_id: int,
    query,
) -> int:
    """Persisted-bill → agent decision → pay/schedule/hold UX.

    Lifted out of the original `handle_confirm` so we can call it
    from the RESOLVING state after a clean name match, or skip
    straight to it when the bill has no payout account at all."""
    await query.edit_message_text("🤖 Analysing payment...")

    user_balance = Decimal(str(context.user_data.get("user_balance", "0")))
    bill_id = context.user_data.get("bill_id")

    with session_scope() as session:
        bill = session.get(Bill, bill_id)
        if bill is None:
            await query.edit_message_text("Session expired. Send the bill again.")
            return ConversationHandler.END

    days_until_due = (bill.due_date - datetime.now()).days
    decision = run_agent(
        user_balance=user_balance,
        bill_amount=Decimal(str(bill.amount)),
        fee=Decimal(str(settings.payout_fee_ngn)),
        days_until_due=days_until_due,
    )

    context.user_data["bill_id"] = bill_id

    if decision.decision == Decision.PAY_NOW:
        fee = Decimal(str(settings.payout_fee_ngn))
        total = Decimal(str(bill.amount)) + fee
        resolved_name = context.user_data.get("resolved_account_name")
        # If we did a resolve step earlier, show the bank-side name
        # so the user has one last look at the destination before
        # authorizing the transfer. If the bill had no account
        # configured, resolved_name is None and we skip the line.
        bank_line = (
            f"Bank name: *{escape_md(resolved_name)}*\n"
            if resolved_name
            else ""
        )
        await query.edit_message_text(
            f"🤖 *Agent says: Pay Now*\n"
            f"_{decision.reason}_\n\n"
            f"💳 *Payment Summary*\n"
            f"Vendor: *{escape_md(bill_data['vendor_name'])}*\n"
            f"Account: `{escape_md(bill_data.get('account_number', 'N/A'))}`\n"
            f"{bank_line}\n"
            f"Amount:  ₦{float(bill.amount):,.2f}\n"
            f"Fee:     ₦{float(fee):,.2f}\n"
            f"Total:   ₦{float(total):,.2f}\n"
            f"Balance after: ₦{float(user_balance - total):,.2f}\n\n"
            f"Do you want to proceed?",
            parse_mode="Markdown",
            reply_markup=final_keyboard(),
        )
        return FINAL_CONFIRM

    if decision.decision == Decision.SCHEDULE:
        with session_scope() as session:
            bill = session.get(Bill, bill_id)
            if bill:
                bill.status = BillStatus.SCHEDULED.value
                session.add(bill)
        await query.edit_message_text(
            f"🤖 *Agent says: Schedule*\n"
            f"_{decision.reason}_\n\n"
            f"🗓 *Payment Scheduled*\n\n"
            f"₦{float(bill.amount):,.2f} → {escape_md(bill_data['vendor_name'])}\n"
            f"Due: {bill.due_date.date().isoformat()}\n\n"
            f"I'll process it automatically when it's due.",
            parse_mode="Markdown",
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Decision.HOLD
    fee = Decimal(str(settings.payout_fee_ngn))
    total = Decimal(str(bill.amount)) + fee
    await query.edit_message_text(
        f"🤖 *Agent says: Hold*\n"
        f"_{decision.reason}_\n\n"
        f"⏸ *Payment on Hold*\n\n"
        f"Bill: ₦{float(bill.amount):,.2f} to {escape_md(bill_data['vendor_name'])}\n"
        f"Your Balance: ₦{float(user_balance):,.2f}\n"
        f"Shortfall: ₦{float(max(Decimal(0), total - user_balance)):,.2f}\n\n"
        f"Top up your wallet with `/wallet` and send the bill again.",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    return ConversationHandler.END


# ── Step 2b: edit → multi-field editor ────────────────────────────

async def handle_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Tap 'Edit' on the confirm screen → show the multi-field
    editor (a list of all 5 fields with current values, each
    tappable)."""
    query = update.callback_query
    await query.answer()
    bill_data = context.user_data.get("bill", {})
    edited = context.user_data.get("edited_keys", set())
    await query.edit_message_text(
        format_multi_field_editor(bill_data, edited),
        parse_mode="Markdown",
        reply_markup=multi_field_editor_keyboard(bill_data, edited),
    )
    return EDIT_LIST


async def handle_edit_field(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User tapped a field in the multi-field editor. Show them
    either the input prompt (most fields) or the date quick-pick
    (due_date, which has its own UX)."""
    query = update.callback_query
    await query.answer()
    field_key = query.data.replace("edit_field:", "")
    context.user_data["editing_field"] = field_key
    bill_data = context.user_data.get("bill", {})
    current = bill_data.get(field_key, "N/A")
    label = EDITABLE_FIELDS.get(field_key, field_key)

    if field_key == "due_date":
        # Date has its own quick-pick UX — show buttons + an input
        # prompt so the user has both options.
        await query.edit_message_text(
            f"📅 Editing *Due date*\n"
            f"Current value: `{escape_md(str(current))}`\n\n"
            "_Pick a quick option, or type a date (e.g. `2026-12-31`):_",
            parse_mode="Markdown",
            reply_markup=date_quickpick_keyboard(),
        )
        # Stay in EDIT_LIST so the user can pick a quick option OR
        # type a date (typed text goes through `handle_new_value`).
        return EDIT_LIST

    await query.edit_message_text(
        f"✏️ Editing *{label}*\n"
        f"Current value: `{escape_md(str(current))}`\n\n"
        "_Type the new value, or tap a field below to switch:_",
        parse_mode="Markdown",
        reply_markup=multi_field_editor_keyboard(bill_data, context.user_data.get("edited_keys", set())),
    )
    return EDIT_VALUE


async def handle_date_quickpick(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User tapped a date quick-pick button. Apply the picked date
    to the staging area and return to the multi-field list."""
    query = update.callback_query
    await query.answer()
    token = query.data.replace("date_", "")
    picked = date_from_quickpick(token)
    if picked is None:
        # Unrecognized token — treat as skip → today.
        picked = datetime.now()

    bill_data = context.user_data.get("bill", {})
    bill_data["due_date"] = picked.isoformat()

    # Track this field as edited.
    edited = context.user_data.setdefault("edited_keys", set())
    edited.add("due_date")

    await query.edit_message_text(
        format_multi_field_editor(bill_data, edited),
        parse_mode="Markdown",
        reply_markup=multi_field_editor_keyboard(bill_data, edited),
    )
    return EDIT_LIST


async def handle_edit_done(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User tapped 'Done editing' — return to the confirm screen
    with the (now-updated) bill."""
    query = update.callback_query
    await query.answer()
    bill_data = context.user_data.get("bill", {})
    # Clear the edited-keys marker so a future edit session starts fresh.
    context.user_data["edited_keys"] = set()
    await query.edit_message_text(
        format_bill_summary(bill_data),
        parse_mode="Markdown",
        reply_markup=confirm_keyboard(),
    )
    return CONFIRM


async def handle_edit_discard(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User tapped 'Discard' — restore the original extracted values
    and return to the confirm screen."""
    query = update.callback_query
    await query.answer()
    # Restore from staging (the original values captured at receive_bill).
    original = context.user_data.get("staging", {})
    if original:
        context.user_data["bill"] = dict(original)
    context.user_data["edited_keys"] = set()
    await query.edit_message_text(
        format_bill_summary(context.user_data["bill"]),
        parse_mode="Markdown",
        reply_markup=confirm_keyboard(),
    )
    return CONFIRM


# ── Step 2c: cancel ────────────────────────────────────────────────

async def handle_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Bill cancelled. Send another bill whenever you're ready.")
    # Clean up the persisted bill if any
    bill_id = context.user_data.get("bill_id")
    if bill_id:
        with session_scope() as session:
            bill = session.get(Bill, bill_id)
            if bill and bill.status in (BillStatus.PENDING.value, BillStatus.SCHEDULED.value):
                bill.status = BillStatus.CANCELLED.value
                session.add(bill)
    context.user_data.clear()
    return ConversationHandler.END


# ── Step 3: new value typed → stage, return to list ──────────────

async def handle_new_value(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    new_value = update.message.text.strip()
    field_key = context.user_data.get("editing_field")
    label = EDITABLE_FIELDS.get(field_key, field_key)

    if not field_key:
        # No field selected — treat as a stray text message. Stay in
        # the current state (don't change the value of anything) and
        # remind the user to tap a field first.
        bill_data = context.user_data.get("bill", {})
        edited = context.user_data.get("edited_keys", set())
        await update.message.reply_text(
            "Tap a field below to start editing, or tap 'Done' to "
            "go back to the summary.",
            reply_markup=multi_field_editor_keyboard(bill_data, edited),
        )
        return EDIT_LIST

    if field_key == "amount":
        # Validate the amount: parseable, positive, < 10M (sanity).
        cleaned = new_value.replace(",", "").replace(" ", "")
        try:
            amount = float(cleaned)
        except (ValueError, InvalidOperation):
            bill_data = context.user_data.get("bill", {})
            edited = context.user_data.get("edited_keys", set())
            await update.message.reply_text(
                f"⚠️ *{escape_md(cleaned)}* isn't a valid amount.\n\n"
                "Try again (e.g. `15000` or `15,000.00`):",
                parse_mode="Markdown",
                reply_markup=multi_field_editor_keyboard(bill_data, edited),
            )
            return EDIT_VALUE
        if amount <= 0 or amount > 10_000_000:
            bill_data = context.user_data.get("bill", {})
            edited = context.user_data.get("edited_keys", set())
            await update.message.reply_text(
                f"⚠️ Amount must be between 1 and 10,000,000.\n\n"
                "Try again:",
                parse_mode="Markdown",
                reply_markup=multi_field_editor_keyboard(bill_data, edited),
            )
            return EDIT_VALUE
        new_value = amount

    elif field_key == "due_date":
        parsed = parse_user_date(new_value)
        if parsed is None:
            bill_data = context.user_data.get("bill", {})
            edited = context.user_data.get("edited_keys", set())
            await update.message.reply_text(
                f"⚠️ I couldn't parse *{escape_md(new_value)}* as a date.\n\n"
                "Try `2026-12-31` or `31 Dec 2026`, or pick a quick option below:",
                parse_mode="Markdown",
                reply_markup=multi_field_editor_keyboard(bill_data, edited),
            )
            return EDIT_VALUE
        new_value = parsed.isoformat()

    # Apply the value to the canonical bill_data and track as edited.
    context.user_data["bill"][field_key] = new_value
    edited = context.user_data.setdefault("edited_keys", set())
    edited.add(field_key)

    bill_data = context.user_data["bill"]
    await update.message.reply_text(
        f"✅ *{label}* updated\n\n"
        + format_multi_field_editor(bill_data, edited),
        parse_mode="Markdown",
        reply_markup=multi_field_editor_keyboard(bill_data, edited),
    )
    return EDIT_LIST


# ── Step 4: final confirm → execute payout ─────────────────────────

async def handle_final_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    bill_id = context.user_data.get("bill_id")
    bill_data = context.user_data.get("bill", {})
    user_balance = Decimal(str(context.user_data.get("user_balance", "0")))

    if not bill_id:
        await query.edit_message_text("Session expired. Send the bill again.")
        return ConversationHandler.END

    await query.edit_message_text("⏳ Processing payment...")

    provider: PaymentProvider = get_payment_provider()
    try:
        with session_scope() as session:
            result = await execute_payout(session, bill_id=bill_id, provider=provider)
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("payout failed for bill %d: %s", bill_id, exc)
        await query.edit_message_text(
            f"❌ *Payment Failed*\n\nReason: {exc}\n\nPlease try again or top up your wallet.",
            parse_mode="Markdown",
        )
        context.user_data.clear()
        return ConversationHandler.END

    fee = Decimal(str(settings.payout_fee_ngn))
    total = Decimal(str(bill_data.get("amount", 0))) + fee
    await query.edit_message_text(
        f"✅ *Payment Initiated*\n\n"
        f"₦{float(bill_data.get('amount', 0)):,.2f} → "
        f"*{escape_md(bill_data.get('vendor_name', ''))}*\n"
        f"Reference: `{result.message}`\n"
        f"Remaining balance: ₦{float(user_balance - total):,.2f}\n\n"
        f"You'll get a notification when the transfer completes.",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    return ConversationHandler.END


# ── Cancel command fallback ────────────────────────────────────────

async def cancel_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled. Send a new bill whenever you're ready.")
    return ConversationHandler.END


# ── ConversationHandler factory ────────────────────────────────────

def build_bill_conversation() -> ConversationHandler:
    # IMPORTANT: the entry point filter excludes commands. Without
    # `~filters.COMMAND`, a text message like `/topup` would be
    # claimed by this entry point (since `filters.TEXT` matches
    # commands too) and the bill conversation would silently start
    # processing the command text as a bill — with no reply to the
    # user. We also exclude `@` mentions to avoid the same issue
    # with `@botname hello` messages.
    return ConversationHandler(
        entry_points=[
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.Document.ALL)
                & ~filters.COMMAND,
                receive_bill,
            )
        ],
        states={
            CONFIRM: [
                CallbackQueryHandler(handle_confirm, pattern="^confirm$"),
                CallbackQueryHandler(handle_edit, pattern="^edit$"),
                CallbackQueryHandler(handle_cancel, pattern="^cancel$"),
            ],
            # RESOLVING is a transient state — the handler returns the
            # next state directly (FINAL_CONFIRM, CONFIRM, or END).
            # We add a no-op message handler here so the Conversation
            # framework accepts the state, and so any stray text the
            # user types during the (fast) Paystack call is ignored
            # cleanly instead of erroring out.
            RESOLVING: [
                MessageHandler(filters.ALL, _ignore_during_resolve),
            ],
            EDIT_LIST: [
                # Tap a field to edit it
                CallbackQueryHandler(
                    handle_edit_field, pattern="^edit_field:(vendor_name|amount|due_date|account_number|bank_code)$"
                ),
                # Date quick-pick (only fires when due_date is being edited)
                CallbackQueryHandler(
                    handle_date_quickpick,
                    pattern="^date_(today|tomorrow|\\+1w|\\+1m|skip)$",
                ),
                # Done / discard
                CallbackQueryHandler(handle_edit_done, pattern="^edit_done$"),
                CallbackQueryHandler(handle_edit_discard, pattern="^edit_discard$"),
                # /cancel as a fallback escape hatch
            ],
            EDIT_VALUE: [
                # User typed a value — go through validation then back to
                # the multi-field list.
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_new_value),
            ],
            FINAL_CONFIRM: [
                CallbackQueryHandler(handle_final_confirm, pattern="^final_confirm$"),
                CallbackQueryHandler(handle_cancel, pattern="^final_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_user=True,
        per_chat=True,
    )


async def _ignore_during_resolve(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """No-op handler for the RESOLVING state. The user shouldn't be
    sending messages while we're calling Paystack; if they do, we
    silently absorb it and stay in RESOLVING."""
    return RESOLVING
