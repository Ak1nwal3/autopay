"""Telegram bot setup + lifecycle.

The bot runs as a background task inside the FastAPI app's lifespan.
We support two modes:

  * **Webhook mode** (production): set `WEBHOOK_URL` and the app
    exposes `POST /telegram/webhook` to receive Telegram updates.
  * **Polling mode** (dev): if `WEBHOOK_URL` is empty, the bot
    long-polls Telegram. The FastAPI process owns the poller.

Either way, only one mode runs at a time. The choice is governed by
the `webhook_url` setting; an empty string means polling.

Tests can build a bot without starting it via `build_application()`
and dispatch updates via PTB's `application.process_update(update)`.

This module also exposes `notify_user_of_transaction()` — the
outbound-channel helper used by the webhook handler and the payout
service to push credit/debit events to a linked Telegram chat. It
runs *outside* the bot's update loop (no ConversationHandler state,
just a one-off `send_message`).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
)

from app.core.config import settings
from app.handlers.auth import (
    bills_command,
    help_command,
    link_command,
    start_command,
    transactions_command,
    unlink_command,
    wallet_command,
)
from app.handlers.bill_conversation import build_bill_conversation
from app.handlers.helpers import escape_md
from app.handlers.schedule_conversation import build_schedule_conversation
from app.handlers.topup_conversation import build_topup_conversation
from app.models.enums import TransactionType

logger = logging.getLogger(__name__)

_application: Optional[Application] = None


# ── Application factory ─────────────────────────────────────────────

def build_application(token: str) -> Application:
    """Build a `python-telegram-bot` Application and register all
    handlers. Does NOT start it — the caller decides webhook vs polling."""
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set; cannot build the bot. "
            "Set it in .env or skip bot setup by leaving it empty."
        )

    app = ApplicationBuilder().token(token).build()

    # Register the topup + schedule ConversationHandlers BEFORE
    # the bill ConversationHandler. All three have entry points
    # that can match a text message: the topup + schedule ones
    # match `/topup` and `/schedule` (CommandHandlers, which only
    # match when the message has a `bot_command` entity), and the
    # bill one matches any text/photo/doc message. PTB walks
    # handlers[0] in order and the first matching one wins, so
    # the more specific (command) handlers must come first.
    # Without this, sending `/topup` would be claimed by the bill
    # conversation's entry point and silently swallowed.
    app.add_handler(build_topup_conversation())
    app.add_handler(build_schedule_conversation())
    app.add_handler(build_bill_conversation())
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("link", link_command))
    app.add_handler(CommandHandler("unlink", unlink_command))
    app.add_handler(CommandHandler("wallet", wallet_command))
    app.add_handler(CommandHandler("bills", bills_command))
    app.add_handler(CommandHandler("transactions", transactions_command))

    # Last-resort error handler — logs but does not crash the bot.
    async def _on_error(update: object, context: CallbackContext) -> None:
        logger.exception("Unhandled exception in bot", exc_info=context.error)

    app.add_error_handler(_on_error)
    return app


# ── Lifecycle (used from app/main.py:lifespan) ──────────────────────

async def start_bot() -> Optional[Application]:
    """Initialize and start the bot in webhook or polling mode.
    Returns the Application, or None if there's no token configured.
    """
    global _application
    token = settings.telegram_bot_token
    if not token:
        logger.info("TELEGRAM_BOT_TOKEN unset; Telegram bot disabled.")
        return None

    _application = build_application(token)
    await _application.initialize()
    await _application.start()

    if settings.webhook_url:
        # Webhook mode: Telegram pushes updates to settings.webhook_url.
        # We don't `run_until_complete` here; updates are received via
        # the /telegram/webhook FastAPI route.
        await _application.bot.set_webhook(url=settings.webhook_url)
        logger.info("Telegram bot running in webhook mode → %s", settings.webhook_url)
    else:
        # Polling mode: long-poll Telegram for updates. This runs
        # in the background as part of the bot's start, so we don't
        # block the FastAPI event loop.
        await _application.updater.start_polling()
        logger.info("Telegram bot running in polling mode (no WEBHOOK_URL set).")

    return _application


async def stop_bot() -> None:   
    """Tear down the bot on FastAPI shutdown."""
    global _application
    if _application is None:
        return
    try:
        if _application.updater and _application.updater.running:
            await _application.updater.stop()
        if settings.webhook_url:
            try:
                await _application.bot.delete_webhook()
            except Exception as exc:  # noqa: BLE001
                logger.warning("delete_webhook failed: %s", exc)
        await _application.stop()
        await _application.shutdown()
    finally:
        _application = None


# ── Webhook route ───────────────────────────────────────────────────

webhook_router = APIRouter(tags=["telegram"])


@webhook_router.post(
    "/telegram/webhook",
    summary="Receive Telegram updates (webhook mode only)",
)
async def telegram_webhook(request: Request) -> dict:
    if _application is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram bot is not running.",
        )
    try:
        payload = await request.json()
        update = Update.de_json(payload, _application.bot)
    except Exception as exc:  # noqa: BLE001
        # Don't leak python-telegram-bot library internals (e.g.
        # "AttributeError: 'NoneType' object has no attribute 'chat'")
        # to the caller. Log for ops, return a clean 400.
        logger.warning("Telegram webhook bad payload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bad update payload.",
        ) from exc

    await _application.process_update(update)
    return {"ok": True}


def get_application() -> Optional[Application]:
    """Test/diagnostic accessor for the running application."""
    return _application


# ── Outbound notifications ─────────────────────────────────────────
#
# The webhook handler (credit events) and the payout service (debit
# events) both want to push a message to a linked Telegram chat. We
# expose a single helper, `notify_user_of_transaction`, that builds
# the right message + keyboard for the kind of event and dispatches
# it through the running bot.
#
# Design choices:
#   * **Best-effort delivery**: notifications never block the
#     webhook / payout flow. If the bot is offline, the chat id is
#     unlinked, or Telegram rate-limits us, we log + move on. The
#     authoritative record is the audit log + the `Transaction`
#     row, not the message.
#   * **Idempotency**: the caller passes the transaction. We don't
#     re-notify on retries — once is enough.
#   * **Markdown V1**: matches the rest of the bot's outbound
#     messages. Always escape user-supplied strings (vendor names,
#     failure reasons) via `escape_md`.


_NOTIFY_RETRY_AFTER_KEY = "notification_attempts"


def _format_amount(value) -> str:
    """Format a Decimal/amount as ₦1,234.50. Centralized so
    notifications and the rest of the bot format numbers the same
    way."""
    return f"₦{float(Decimal(str(value))):,.2f}"


def _notify_keyboard() -> InlineKeyboardMarkup:
    """Keyboard attached to every notification. The user can jump
    straight to /transactions or /wallet without typing a command.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💳 Transactions", callback_data="goto_transactions"),
            InlineKeyboardButton("💼 Wallet", callback_data="goto_wallet"),
        ],
    ])


async def notify_user_of_transaction(
    *,
    user: "User",
    transaction: "Transaction",
    kind: str,
) -> bool:
    """Send a credit / debit / refund notification to the user's
    linked Telegram chat.

    Args:
        user: The `User` row (must already be loaded with at least
            `id`, `telegram_chat_id`, `is_telegram_linked`,
            `balance`).
        transaction: The `Transaction` row. We read `amount`, `fee`,
            `currency`, `type`, `narration`, `bill_id` from it.
        kind: One of `"credit"`, `"debit"`, `"refund"`. Determines
            the message copy and emoji.

    Returns:
        `True` if the message was sent (or the user has no linked
        chat — that's a "no-op" not a failure). `False` on Telegram
        errors; the caller can log but should not retry.
    """
    # No bot running? Nothing to do. We don't raise — callers are
    # in the middle of a webhook or payout, and a missing bot is
    # expected in some test environments.
    if _application is None:
        logger.debug("notify_user_of_transaction: bot not running; skipping")
        return False

    # User has no linked Telegram chat. Skip silently — common in
    # pure-API usage.
    if not getattr(user, "is_telegram_linked", False) or not getattr(
        user, "telegram_chat_id", None
    ):
        logger.debug(
            "notify_user_of_transaction: user %d not linked; skipping", user.id
        )
        return False

    # Build the message.
    amount_str = _format_amount(transaction.amount)
    fee_str = _format_amount(getattr(transaction, "fee", 0) or 0)
    balance_str = _format_amount(user.balance)
    narration = getattr(transaction, "narration", "") or ""

    if kind == "credit":
        title = "💰 *Top-up successful*"
        lines = [
            title,
            "",
            f"Amount: *+{amount_str}*",
        ]
        if float(getattr(transaction, "fee", 0) or 0) > 0:
            lines.append(f"Fee: {fee_str}")
        if narration:
            lines.append(f"Reference: `{escape_md(narration)}`")
        lines += [
            "",
            f"_New balance: {balance_str}_",
        ]
    elif kind == "debit":
        title = "💸 *Bill paid*"
        lines = [
            title,
            "",
            f"Amount: *−{amount_str}*",
        ]
        if float(getattr(transaction, "fee", 0) or 0) > 0:
            lines.append(f"Fee: {fee_str}")
        if narration:
            lines.append(f"To: `{escape_md(narration)}`")
        lines += [
            "",
            f"_Remaining balance: {balance_str}_",
        ]
    elif kind == "refund":
        title = "↩️ *Payment refunded*"
        lines = [
            title,
            "",
            f"Refunded: *+{amount_str}*",
        ]
        if narration:
            lines.append(f"Reference: `{escape_md(narration)}`")
        reason = getattr(transaction, "failure_reason", None) or "transfer_failed"
        lines += [
            "",
            f"_Reason: {escape_md(reason)}_",
            f"_New balance: {balance_str}_",
        ]
    else:  # pragma: no cover  (defensive)
        logger.warning("notify_user_of_transaction: unknown kind=%r", kind)
        return False

    text = "\n".join(lines)

    try:
        await _application.bot.send_message(
            chat_id=user.telegram_chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=_notify_keyboard(),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        # Telegram errors are best-effort. Log and move on.
        logger.warning(
            "notify_user_of_transaction: failed for user %d: %s",
            user.id, exc,
        )
        return False


# Re-export so the webhook / payout code can pass the right enum
# value without importing the enum directly.
async def notify_credit(*, user: "User", transaction: "Transaction") -> bool:
    return await notify_user_of_transaction(
        user=user, transaction=transaction, kind="credit",
    )


async def notify_debit(*, user: "User", transaction: "Transaction") -> bool:
    return await notify_user_of_transaction(
        user=user, transaction=transaction, kind="debit",
    )


async def notify_refund(*, user: "User", transaction: "Transaction") -> bool:
    return await notify_user_of_transaction(
        user=user, transaction=transaction, kind="refund",
    )
