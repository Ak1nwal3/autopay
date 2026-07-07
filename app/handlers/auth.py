"""Telegram bot auth-flow handlers: /start, /link, /wallet, /unlink,
/bills, /transactions, /help.

The /link command is the user-facing half of the account-link flow.
The web dashboard generates a short-lived code (see
`app/handlers/__init__.py` for the flow overview) and the user sends
that code back via /link. The bot resolves the code against the
`telegram_link_codes` table, marks the user as linked, and stores the
chat id on the user row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from app.core.database import session_scope
from app.handlers.helpers import (
    escape_md,
    get_linked_user,
    get_user_va,
)
from app.models.enums import AuditActor, AuditEventType, AuditEntityType
from app.models.telegram_link_code import TelegramLinkCode
from app.models.user import User
from app.services.audit import write_audit

logger = logging.getLogger(__name__)


# ── /start ──────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "👋 *Welcome to AutoPay AI!*\n\n"
        "I pay your bills automatically. Send me a bill photo, PDF, or\n"
        "text description and I'll handle the rest.\n\n"
        "*Get started:*\n"
        "1. Sign up at the web dashboard.\n"
        "2. Open Settings → Link Telegram — copy the 6-char code.\n"
        f"3. Send it here: `/link {chr(60)}CODE{chr(62)}`\n\n"
        "Once linked, send me a bill whenever a payment is due.\n\n"
        "*Try:* `/topup`, `/schedule`, `/wallet`, `/bills`, "
        "`/transactions`, `/help`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /link ───────────────────────────────────────────────────────────

async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Please include your linking code.\n"
            "Example: `/link AB9X2K`\n\n"
            "Get your code from the web dashboard (Settings → Link Telegram).",
            parse_mode="Markdown",
        )
        return

    code = context.args[0].strip().upper()
    chat_id = str(update.effective_chat.id)

    if get_linked_user(chat_id):
        await update.message.reply_text(
            "✅ Your account is already linked! Just send me a bill whenever you want to pay one."
        )
        return

    with session_scope() as session:
        # SQLModel 0.0.22 quirk: select(Model).first() can return a
        # single-element Row tuple. Use `scalar_one_or_none` to
        # guarantee a model instance (or None).
        from sqlalchemy import select as _sa_select
        link_code = session.exec(
            _sa_select(TelegramLinkCode).where(
                TelegramLinkCode.code == code,
                TelegramLinkCode.is_used == False,  # noqa: E712
            )
        ).scalar_one_or_none()
        # Force-load all fields while the session is still open
        if link_code is not None:
            _ = (link_code.id, link_code.user_id, link_code.code,
                 link_code.expires_at, link_code.is_used)

        if link_code is None:
            await update.message.reply_text(
                "❌ That code is invalid. Check it and try again, or generate a new one from the web dashboard."
            )
            return

        if link_code.expires_at < datetime.now(tz=timezone.utc):
            await update.message.reply_text(
                "⏰ That code has expired. Generate a new one from the web dashboard."
            )
            return

        # Reject re-link to a different chat — would orphan the previous
        # user.
        from sqlalchemy import select as _sa_select
        existing = session.exec(
            _sa_select(User).where(User.telegram_chat_id == chat_id)
        ).scalar_one_or_none()
        if existing is not None and existing.id != link_code.user_id:
            await update.message.reply_text(
                "⚠️ This Telegram is already linked to a different account. "
                "Run /unlink first if you want to switch accounts."
            )
            return

        user = session.get(User, link_code.user_id)
        if user is None:
            await update.message.reply_text(
                "❌ The user behind that code no longer exists. Generate a new code."
            )
            return

        user.telegram_chat_id = chat_id
        user.is_telegram_linked = True
        link_code.is_used = True
        session.add(user)
        session.add(link_code)
        session.flush()

        write_audit(
            session,
            actor=AuditActor.USER,
            event_type=AuditEventType.USER_TELEGRAM_LINKED,
            user_id=user.id,
            entity_type=AuditEntityType.USER,
            entity_id=user.id,
            metadata={"chat_id": chat_id, "code": code[:2] + "***"},
        )
        session.commit()

    await update.message.reply_text(
        "✅ *Account linked!*\n\n"
        "You can now send me bills (photos, PDFs, or text) and I'll\n"
        "analyze and pay them on your behalf.\n\n"
        "Send `/wallet` to see your balance, or just send a bill to start.",
        parse_mode="Markdown",
    )


# ── /unlink ─────────────────────────────────────────────────────────

async def unlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    with session_scope() as session:
        from sqlalchemy import select as _sa_select
        user = session.exec(
            _sa_select(User).where(
                User.telegram_chat_id == chat_id,
                User.is_telegram_linked == True,  # noqa: E712
            )
        ).scalar_one_or_none()
        if user is None:
            await update.message.reply_text(
                "Your Telegram is not linked. Nothing to do."
            )
            return
        user.telegram_chat_id = None
        user.is_telegram_linked = False
        session.add(user)
        session.commit()
    await update.message.reply_text(
        "🔌 *Telegram unlinked.*\n\n"
        "Your web account is unchanged. Run `/link CODE` to link again.",
        parse_mode="Markdown",
    )


# ── /wallet ─────────────────────────────────────────────────────────

async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    user = get_linked_user(chat_id)
    if user is None:
        await update.message.reply_text(
            "🔒 Link your account first with `/link YOUR_CODE`",
            parse_mode="Markdown",
        )
        return

    va = get_user_va(user)
    balance = float(user.balance)
    if va is None:
        # No DVA — show the balance, then point the user at /topup
        # (the bot's hosted-Checkout flow, which works without a
        # DVA). Users with a DVA provisioned will see their account
        # number below.
        await update.message.reply_text(
            f"💼 *Your AutoPay Wallet*\n\n"
            f"Balance: ₦{balance:,.2f}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"*No virtual account provisioned.*\n"
            f"Top up your wallet with `/topup` (card / bank / USSD via\n"
            f"Paystack Checkout) — no DVA needed.\n\n"
            f"_See `/transactions` for your recent activity._",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        f"💼 *Your AutoPay Wallet*\n\n"
        f"Balance: ₦{balance:,.2f}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"*Fund your wallet via transfer:*\n"
        f"Bank: `{escape_md(va.bank_name or 'N/A')}`\n"
        f"Account: `{escape_md(va.account_number or 'N/A')}`\n"
        f"Name: `{escape_md(va.account_name or 'N/A')}`\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"Or top up instantly with `/topup` (card / USSD / QR).\n"
        f"_Save this DVA as a beneficiary in your bank app for "
        f"quick top-ups._",
        parse_mode="Markdown",
    )


# ── /bills ──────────────────────────────────────────────────────────

async def bills_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.models.bill import Bill
    from app.models.enums import BillStatus
    chat_id = str(update.effective_chat.id)
    user = get_linked_user(chat_id)
    if user is None:
        await update.message.reply_text(
            "🔒 Link your account first with `/link YOUR_CODE`",
            parse_mode="Markdown",
        )
        return

    with session_scope() as session:
        from sqlalchemy import select as _sa_select
        rows = session.execute(
            _sa_select(Bill)
            .where(Bill.user_id == user.id)
            .order_by(Bill.due_date.asc())
            .limit(20)
        ).scalars().all()

    if not rows:
        await update.message.reply_text(
            "📭 You have no bills yet. Send me a bill, or `/schedule` a recurring one."
        )
        return

    lines = ["📋 *Your recent bills*\n"]
    for b in rows:
        try:
            amount = f"₦{float(b.amount):,.2f}"
        except (TypeError, ValueError):
            amount = "N/A"
        recur_marker = " 🔁" if getattr(b, "is_recurring", False) else ""
        lines.append(
            f"• *#{b.id}* `{escape_md(b.vendor_name)}`{recur_marker} — {amount} "
            f"_(status: {b.status}, due {b.due_date.date() if b.due_date else '?'})_"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /transactions ───────────────────────────────────────────────────


async def transactions_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the user's last 20 transactions (credits + debits)."""
    from app.models.enums import TransactionType

    chat_id = str(update.effective_chat.id)
    user = get_linked_user(chat_id)
    if user is None:
        await update.message.reply_text(
            "🔒 Link your account first with `/link YOUR_CODE`",
            parse_mode="Markdown",
        )
        return

    with session_scope() as session:
        from sqlalchemy import select as _sa_select

        from app.models.transaction import Transaction

        # SQLModel 0.0.22 quirk: `session.exec(select(Model)).all()`
        # returns a list of Row tuples. Use `scalars()` to get the
        # model instances.
        rows = (
            session.execute(
                _sa_select(Transaction)
                .where(Transaction.user_id == user.id)
                .order_by(Transaction.created_at.desc())
                .limit(20)
            )
            .scalars()
            .all()
        )

    if not rows:
        await update.message.reply_text(
            "📭 No transactions yet. Top up your wallet to get started.",
            parse_mode="Markdown",
        )
        return

    # Header with current balance so the user has context
    balance = float(user.balance)
    lines = [
        "💳 *Your recent transactions*\n",
        f"_Current balance: ₦{balance:,.2f}_\n",
    ]

    for t in rows:
        # Sign by type
        if t.type == TransactionType.CREDIT.value:
            sign = "+"
            emoji = "🟢"
        else:
            sign = "−"
            emoji = "🔴"

        try:
            amount = f"₦{float(t.amount):,.2f}"
        except (TypeError, ValueError):
            amount = "N/A"

        # Build a short label
        if t.type == TransactionType.CREDIT.value:
            label = t.narration or "Top-up"
        else:
            # Debit — try to surface the vendor name via the bill FK
            label = t.narration or (f"Bill #{t.bill_id}" if t.bill_id else "Payout")

        # Date — show YYYY-MM-DD
        when = t.created_at.date().isoformat() if t.created_at else "?"

        # Status indicator
        status = t.status
        if status == "success":
            status_mark = "✅"
        elif status == "failed":
            status_mark = "❌"
        elif status == "reversed":
            status_mark = "↩️"
        else:
            status_mark = "⏳"

        lines.append(
            f"{emoji} {sign}{amount}  {status_mark} `{escape_md(label)}`\n"
            f"    _{when} · {status}_"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /help ───────────────────────────────────────────────────────────

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "*Commands*\n"
        "/start — welcome message\n"
        "/link `<code>` — link your web account\n"
        "/unlink — disconnect your Telegram\n"
        "/wallet — show balance and DVA details\n"
        "/topup — add money to your wallet (card / USSD / QR)\n"
        "/schedule — set up a future-dated or recurring bill\n"
        "/bills — list recent bills\n"
        "/transactions — show recent credits and debits\n"
        "/cancel — cancel the current conversation\n\n"
        "Just send a bill photo, PDF, or text to pay a bill."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")
