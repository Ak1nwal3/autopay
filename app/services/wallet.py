"""Wallet business logic — the operations that modify a user's balance.

Currently exposes one operation (`start_topup`) which both the FastAPI
endpoint at `POST /api/v1/wallet/topup` and the Telegram bot's
`/topup` conversation call. Centralizing here means the validation,
audit, and metrics rules are enforced in exactly one place.

Why a service instead of inline in the API route:
  * The bot needs the same logic, and re-implementing it in the
    ConversationHandler would duplicate (and drift on) the rules.
  * The two callers (API + bot) have different error UX (JSON 4xx vs
    Markdown error message), but the underlying operations are
    identical: validate amount, mint reference, persist pending txn,
    call provider, audit, metrics.

Also exposes `apply_credit_from_provider_event` — the credit path
shared by the webhook route and the manual-verify / scheduler-poll
fallback paths. Hoisted here so all three (webhook + verify + poll)
share the same idempotency guarantees, audit row format, and
race-condition protections.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from sqlmodel import Session, select

from app.core.metrics import record_topup_credited, record_topup_initiated
from app.models.enums import (
    AuditActor,
    AuditEntityType,
    AuditEventType,
    TransactionStatus,
    TransactionType,
)
from app.models.transaction import Transaction
from app.models.user import User
from app.services.audit import audit_wallet_credit, write_audit
from app.services.payments import (
    PaymentError,
    PaymentProvider,
    TopupInit,
    TransactionStatusResult,
    WebhookEvent,
)
# `notify_credit` is imported lazily inside the credit function to
# avoid a circular import: telegram.py → topup_conversation.py →
# wallet.py (the bot handler imports `MAX_TOPUP_NGN` etc. from
# here). The same pattern is used in `app/services/payout.py`.

logger = logging.getLogger(__name__)


# Top-up limits, shared with the API endpoint. Keep these here so the
# bot and the API can't drift apart.
MIN_TOPUP_NGN: Decimal = Decimal("100.00")
MAX_TOPUP_NGN: Decimal = Decimal("1_000_000.00")


class TopupValidationError(ValueError):
    """Raised when the top-up amount is outside the [min, max] range
    or otherwise unusable. Distinct from `PaymentError` (which means
    the provider failed) so callers can render a different message."""


@dataclass(frozen=True)
class TopupStartResult:
    """Return value of `start_topup` — what the bot/API needs to
    hand off to Paystack and the user."""

    authorization_url: str
    reference: str
    transaction_id: int
    amount: Decimal
    currency: str = "NGN"
    provider: str = "paystack"


@dataclass(frozen=True)
class CreditApplyResult:
    """Outcome of `apply_credit_from_provider_event`.

    `credited` is True iff this call actually flipped the transaction
    to SUCCESS and increased the user's balance. False for "already
    applied" (the webhook + poll + verify all share this) and for
    "unknown reference" (orphan credit).
    """

    credited: bool
    new_balance: Decimal
    # "credited" | "already_credited" | "already_failed" |
    # "unknown_reference" | "amount_mismatch" | "provider_<status>"
    status: str
    transaction_id: Optional[int] = None


async def start_topup(
    session: Session,
    *,
    user: User,
    amount: Decimal,
    provider: PaymentProvider,
    callback_url: str | None = None,
) -> TopupStartResult:
    """Mint a unique reference, persist a pending `Transaction` row,
    call `provider.initialize_topup(...)`, write the audit row, bump
    the metrics counter. Returns the URL the user opens to pay.

    Raises:
        TopupValidationError: amount is below `MIN_TOPUP_NGN` or above
            `MAX_TOPUP_NGN`.
        PaymentError: the provider refused to start the top-up (bad
            key, network error, etc.). The pending `Transaction` row
            is rolled back so the user can retry.
    """
    amount = Decimal(str(amount))
    if amount < MIN_TOPUP_NGN:
        raise TopupValidationError(
            f"Minimum top-up is {MIN_TOPUP_NGN} NGN."
        )
    if amount > MAX_TOPUP_NGN:
        raise TopupValidationError(
            f"Maximum top-up is {MAX_TOPUP_NGN} NGN. "
            "Contact support for larger amounts."
        )

    reference = f"topup_{user.id}_{secrets.token_hex(8)}"
    amount_kobo = int((amount * Decimal(100)).quantize(Decimal("1")))

    # Default the callback URL so Nomba knows where to deliver
    # webhooks (the sandbox fires webhooks to the checkout order's
    # callbackUrl). Without this, the webhook never fires because
    # the order is created with callbackUrl="". The caller (API
    # or bot) can override with a browser-friendly redirect URL.
    if not callback_url:
        from app.core.config import get_settings
        s = get_settings()
        callback_url = f"{s.nomba_callback_url.rstrip('/')}/webhooks/nomba"

    txn = Transaction(
        user_id=user.id,
        type=TransactionType.CREDIT.value,
        amount=amount,
        fee=Decimal("0.00"),
        currency="NGN",
        status=TransactionStatus.PENDING.value,
        provider=provider.name,
        provider_reference=reference,
        narration=f"Top-up via {provider.name} Checkout",
    )
    session.add(txn)
    session.flush()

    try:
        init: TopupInit = await provider.initialize_topup(
            amount_kobo=amount_kobo,
            email=user.email,
            reference=reference,
            callback_url=callback_url,
        )
    except PaymentError as exc:
        session.delete(txn)
        session.commit()
        logger.warning("Topup init failed for user %d: %s", user.id, exc)
        raise

    # Save the provider's reference for later use by the
    # transaction-status API. Paystack echoes our reference back
    # (so this is the same as `txn.provider_reference`), but
    # Nomba generates its own UUID — we need to remember it so
    # the verify endpoint and scheduler poll can call
    # `provider.get_transaction(reference=...)` with the right
    # value. We always store it if the provider returned one
    # (even when equal to our reference — this keeps the column
    # populated for queries and analytics).
    if init.reference:
        txn.provider_order_reference = init.reference
        session.add(txn)
        session.commit()
        session.refresh(txn)

    write_audit(
        session,
        actor=AuditActor.USER,
        event_type=AuditEventType.WALLET_CREDITED,
        user_id=user.id,
        entity_type=AuditEntityType.TRANSACTION,
        entity_id=txn.id,
        metadata={
            "trigger": "topup_init",
            "reference": reference,
            "provider_order_reference": txn.provider_order_reference,
            "amount": float(amount),
            "status": "pending",
        },
    )
    session.commit()

    record_topup_initiated()

    # Return OUR internal reference (not the provider's) so the
    # user remembers one consistent ID. The verify endpoint and
    # scheduler know to look up `provider_order_reference`
    # separately when calling the provider's API.
    return TopupStartResult(
        authorization_url=init.authorization_url,
        reference=txn.provider_reference,
        transaction_id=txn.id or 0,
        amount=amount,
        currency="NGN",
        provider=provider.name,
    )


async def apply_credit_from_provider_event(
    session: Session,
    *,
    user: User,
    event: WebhookEvent,
) -> CreditApplyResult:
    """Credit a user's wallet when a `charge.success` (or equivalent)
    event arrives from the provider.

    Called from three places that all share this implementation:

      1. `POST /webhooks/{paystack,nomba}` — the primary path
         (the provider's webhook server POSTs to us).
      2. `POST /wallet/topup/verify` — user-triggered manual verify
         (re-fetches the transaction via `provider.get_transaction`
         and applies the credit if the provider says it's settled).
      3. The scheduler's `poll_pending_nomba_topups` job — polls
         pending CREDIT transactions every 30s and applies credits
         for any the provider has settled.

    RACE-CONDITION FIX: All three callers can race for the same
    `Transaction` row (e.g. webhook arrives at the same instant the
    scheduler polls, and the user also taps "I've paid"). We use
    `SELECT ... FOR UPDATE` to row-lock the pending transaction for
    the duration of the credit. The second call blocks until the
    first commits, then sees `status='success'` and exits cleanly.

    IDEMPOTENCY: even without the row lock, the
    `if txn.status == SUCCESS: return` guard at line ~280 makes
    every call after the first a no-op. The row lock + status guard
    combine to guarantee exactly-once crediting.

    Returns a `CreditApplyResult` so callers (the verify endpoint
    especially) can distinguish "just credited" from "was already
    credited by a parallel path".
    """
    if not event.provider_reference:
        logger.warning("charge.success with no reference: %s", event.raw)
        return CreditApplyResult(
            credited=False,
            new_balance=Decimal(str(user.balance)),
            status="unknown_reference",
        )

    # Row-lock the pending transaction. The lock is held for the
    # remainder of this transaction; concurrent callers wait here
    # and then re-read the (now-updated) status.
    txn = session.execute(
        select(Transaction)
        .where(Transaction.provider_reference == event.provider_reference)
        .with_for_update()
    ).scalar_one_or_none()
    if txn is None:
        # No matching transaction — the top-up arrived before our app
        # created a row (orphan credit). Log and skip.
        write_audit(
            session,
            actor=AuditActor.WEBHOOK,
            event_type=AuditEventType.WALLET_CREDITED,
            user_id=None,
            entity_type=AuditEntityType.TRANSACTION,
            entity_id=None,
            metadata={
                "reference": event.provider_reference,
                "amount_kobo": event.amount_kobo,
                "status": "orphan_credit",
            },
        )
        session.commit()
        return CreditApplyResult(
            credited=False,
            new_balance=Decimal(str(user.balance)),
            status="unknown_reference",
        )

    if txn.status == TransactionStatus.SUCCESS.value:
        # Already applied (concurrent path beat us to it). No-op.
        return CreditApplyResult(
            credited=False,
            new_balance=Decimal(str(user.balance)),
            status="already_credited",
            transaction_id=txn.id,
        )

    if txn.status == TransactionStatus.FAILED.value:
        # Previously rejected (e.g. amount mismatch). No-op so a
        # replayed webhook / retried verify can't resurrect it.
        return CreditApplyResult(
            credited=False,
            new_balance=Decimal(str(user.balance)),
            status="already_failed",
            transaction_id=txn.id,
        )

    # If event arrived for a different user (e.g. user_id in the
    # event doesn't match the transaction's owner), bail. Defensive.
    if txn.user_id != user.id:
        logger.warning(
            "credit event for reference %s has user_id mismatch: "
            "txn.user_id=%d, event.user_id=%d",
            event.provider_reference, txn.user_id, user.id,
        )
        return CreditApplyResult(
            credited=False,
            new_balance=Decimal(str(user.balance)),
            status="unknown_reference",
        )

    # ALWAYS credit the amount recorded on the transaction row
    # (what the user agreed to pay when the top-up was initiated),
    # never the amount reported in the webhook body. The webhook's
    # amount is used only for validation: if it is present and
    # diverges from the transaction's amount, the event is
    # unreliable and the credit is refused. This is the defence
    # against providers (notably the Nomba sandbox) that report a
    # fixed amount (e.g. 4000) and status=success for transactions
    # that were never paid.
    txn_amount_kobo = int(
        (Decimal(str(txn.amount)) * Decimal(100)).quantize(Decimal("1"))
    )
    if (
        event.amount_kobo is not None
        and event.amount_kobo != txn_amount_kobo
    ):
        logger.warning(
            "amount divergence for reference %s: event=%d kobo, "
            "local=%d kobo (txn.amount=%s). Refusing to credit.",
            event.provider_reference,
            event.amount_kobo,
            txn_amount_kobo,
            txn.amount,
        )
        txn.status = TransactionStatus.FAILED.value
        txn.failure_reason = "amount_mismatch"
        session.add(txn)
        write_audit(
            session,
            actor=AuditActor.WEBHOOK,
            event_type=AuditEventType.WALLET_CREDITED,
            user_id=user.id,
            entity_type=AuditEntityType.TRANSACTION,
            entity_id=txn.id,
            metadata={
                "reference": event.provider_reference,
                "event_amount_kobo": event.amount_kobo,
                "txn_amount_kobo": txn_amount_kobo,
                "status": "amount_mismatch",
            },
        )
        session.commit()
        return CreditApplyResult(
            credited=False,
            new_balance=Decimal(str(user.balance)),
            status="amount_mismatch",
            transaction_id=txn.id,
        )

    amount = Decimal(str(txn.amount))
    user.balance = Decimal(str(user.balance)) + amount
    txn.status = TransactionStatus.SUCCESS.value
    session.add(user)
    session.add(txn)
    audit_wallet_credit(
        session,
        user_id=user.id,
        amount=float(amount),
        provider_reference=event.provider_reference,
        new_balance=float(user.balance),
    )
    # Metrics: count the credit. Source = "checkout" if the
    # reference starts with "topup_", "dva" if it matches a virtual
    # account pattern, else "manual" / unknown.
    if event.provider_reference.startswith("topup_"):
        record_topup_credited(source="checkout")
    else:
        record_topup_credited(source="dva")
    session.commit()
    session.refresh(user)
    session.refresh(txn)

    # Best-effort Telegram notification. notify_credit swallows its
    # own errors (Telegram rate-limits, bot offline, etc.) so this
    # call never blocks the credit. Imported lazily to avoid a
    # circular import (telegram → topup_conversation → wallet).
    from app.services.telegram import notify_credit

    await notify_credit(user=user, transaction=txn)

    return CreditApplyResult(
        credited=True,
        new_balance=Decimal(str(user.balance)),
        status="credited",
        transaction_id=txn.id,
    )


async def verify_pending_topup(
    session: Session,
    *,
    user: User,
    reference: str,
    provider: PaymentProvider,
) -> CreditApplyResult:
    """Manually verify a pending top-up by calling the provider's
    transaction-status endpoint and applying the credit if settled.

    Used by `POST /wallet/topup/verify` (the user-facing "I've paid"
    button) and the scheduler's poll job. Returns a
    `CreditApplyResult` so the caller can render a useful message:

      * `credited=True`  → we just credited the wallet
      * `status="already_credited"` → a webhook/poll beat us; show
        the user the current balance
      * `status="unknown_reference"` → the user passed a reference
        that doesn't match any of their pending transactions

    The `reference` argument is matched against EITHER our internal
    `provider_reference` or the provider's `provider_order_reference`.
    This is what makes the manual-verify UX robust to providers that
    (like Nomba) generate their own IDs for each hosted Checkout:
    the user remembers OUR reference, but the call to
    `provider.get_transaction` uses the PROVIDER's reference because
    that's what the provider's transaction-status API expects.
    """
    from sqlalchemy import or_

    # Verify the reference belongs to this user (defensive: prevent
    # a user from probing other users' references). Match against
    # either column so the user can use whichever reference they
    # remember.
    txn = session.exec(
        select(Transaction).where(
            Transaction.user_id == user.id,
            or_(
                Transaction.provider_reference == reference,
                Transaction.provider_order_reference == reference,
            ),
        )
    ).first()
    if txn is None:
        return CreditApplyResult(
            credited=False,
            new_balance=Decimal(str(user.balance)),
            status="unknown_reference",
        )

    # If the transaction is already terminal (SUCCESS/FAILED), don't
    # bother calling the provider — the answer won't change.
    if txn.status == TransactionStatus.SUCCESS.value:
        return CreditApplyResult(
            credited=False,
            new_balance=Decimal(str(user.balance)),
            status="already_credited",
            transaction_id=txn.id,
        )

    if txn.status == TransactionStatus.FAILED.value:
        # Previously rejected (e.g. amount mismatch). Don't re-query
        # the provider or allow a re-verify to resurrect the txn.
        return CreditApplyResult(
            credited=False,
            new_balance=Decimal(str(user.balance)),
            status="already_failed",
            transaction_id=txn.id,
        )

    # Use the provider's reference (if we have one) when calling
    # the provider's transaction-status API. Paystack is happy with
    # either, but Nomba's `GET /v1/transactions/accounts/single`
    # expects `orderReference=<the ID we sent when creating the
    # Checkout>` — which is what we stored in
    # `txn.provider_order_reference`. Fall back to our internal
    # reference for transactions created before the
    # `provider_order_reference` column existed.
    provider_ref = (
        txn.provider_order_reference
        or txn.provider_reference
        or reference
    )

    # Call the provider's transaction-status endpoint.
    try:
        result: TransactionStatusResult = await provider.get_transaction(
            reference=provider_ref,
        )
    except PaymentError as exc:
        # Don't leak the provider message; the user just sees
        # "still pending" and can try again.
        logger.warning(
            "get_transaction failed for user %d reference %s: %s",
            user.id, provider_ref, exc,
        )
        return CreditApplyResult(
            credited=False,
            new_balance=Decimal(str(user.balance)),
            status="unknown_reference",
            transaction_id=txn.id,
        )

    if result.status == "success":
        # Convert the provider's status to a synthetic WebhookEvent
        # and apply the credit via the shared function. This gives
        # the user the same audit row, Telegram notification, and
        # metrics as a real webhook delivery.
        #
        # CRITICAL: `event.provider_reference` must be the value
        # `apply_credit_from_provider_event` will use to find the
        # transaction — which is OUR `provider_reference` (the
        # column it does `SELECT WHERE provider_reference = ...`
        # on). The provider's `orderReference` was used to call
        # the provider's API but the DB lookup needs our ID.
        #
        # AMOUNT: we ALWAYS credit `txn.amount` (what the user
        # agreed to pay), never the provider's reported amount.
        # The provider's amount is used only as a divergence
        # guard: if it is present and differs from the locally
        # recorded amount we refuse to credit and mark the txn
        # FAILED. This is the defence against the Nomba sandbox
        # returning a fixed amount (e.g. 4000) and status=success
        # for transactions that were never actually paid.
        txn_amount_kobo = int(
            (Decimal(str(txn.amount)) * Decimal(100)).quantize(Decimal("1"))
        )
        if (
            result.amount_kobo is not None
            and result.amount_kobo != txn_amount_kobo
        ):
            logger.warning(
                "amount divergence for reference %s: provider=%d kobo, "
                "local=%d kobo (txn.amount=%s). Refusing to credit.",
                txn.provider_reference,
                result.amount_kobo,
                txn_amount_kobo,
                txn.amount,
            )
            txn.status = TransactionStatus.FAILED.value
            txn.failure_reason = "amount_mismatch"
            session.add(txn)
            write_audit(
                session,
                actor=AuditActor.USER,
                event_type=AuditEventType.WALLET_CREDITED,
                user_id=user.id,
                entity_type=AuditEntityType.TRANSACTION,
                entity_id=txn.id,
                metadata={
                    "reference": txn.provider_reference,
                    "provider_amount_kobo": result.amount_kobo,
                    "txn_amount_kobo": txn_amount_kobo,
                    "status": "amount_mismatch",
                },
            )
            session.commit()
            return CreditApplyResult(
                credited=False,
                new_balance=Decimal(str(user.balance)),
                status="amount_mismatch",
                transaction_id=txn.id,
            )
        # Provider amount is None (can't compare) or matches the
        # local amount — either way, credit the local amount.
        event = WebhookEvent(
            event_type="charge.success",
            provider_reference=txn.provider_reference,
            event_id=f"verify:{txn.provider_reference}",
            amount_kobo=txn_amount_kobo,
            provider=provider.name,
        )
        return await apply_credit_from_provider_event(
            session, user=user, event=event,
        )

    # Provider says: still pending, failed, reversed, or unknown.
    # We don't change any state; just report the status back.
    return CreditApplyResult(
        credited=False,
        new_balance=Decimal(str(user.balance)),
        status=f"provider_{result.status}",
        transaction_id=txn.id,
    )


async def reverse_credit_from_provider_event(
    session: Session,
    *,
    user: User,
    event: WebhookEvent,
) -> CreditApplyResult:
    """Reverse a previously credited wallet top-up when a
    `payment_reversal` (chargeback) event arrives from the provider.

    Mirror of `apply_credit_from_provider_event` but in reverse:
      * Row-locks the transaction (`SELECT ... FOR UPDATE`).
      * If already REVERSED → no-op (idempotent).
      * If SUCCESS → debit `txn.amount` from `user.balance`, flip
        txn to REVERSED, write a WALLET_REFUND audit row, fire
        Telegram `notify_refund`.
      * If PENDING → just mark REVERSED (no credit was applied, so
        nothing to debit).

    Amount validation: same divergence guard as the credit path —
    if the event's amount_kobo is present and differs from
    `txn.amount * 100`, we log a warning but still reverse (a
    reversal with the wrong amount is better than leaving the
    wallet credited for a charged-back payment).
    """
    if not event.provider_reference:
        logger.warning("charge.reversed with no reference: %s", event.raw)
        return CreditApplyResult(
            credited=False,
            new_balance=Decimal(str(user.balance)),
            status="unknown_reference",
        )

    txn = session.execute(
        select(Transaction)
        .where(Transaction.provider_reference == event.provider_reference)
        .with_for_update()
    ).scalar_one_or_none()
    if txn is None:
        logger.warning(
            "charge.reversed for unknown reference %s", event.provider_reference,
        )
        return CreditApplyResult(
            credited=False,
            new_balance=Decimal(str(user.balance)),
            status="unknown_reference",
        )

    if txn.status == TransactionStatus.REVERSED.value:
        # Already reversed (replayed webhook). No-op.
        return CreditApplyResult(
            credited=False,
            new_balance=Decimal(str(user.balance)),
            status="already_reversed",
            transaction_id=txn.id,
        )

    if txn.user_id != user.id:
        logger.warning(
            "charge.reversed for reference %s has user_id mismatch: "
            "txn.user_id=%d, event.user_id=%d",
            event.provider_reference, txn.user_id, user.id,
        )
        return CreditApplyResult(
            credited=False,
            new_balance=Decimal(str(user.balance)),
            status="unknown_reference",
        )

    # Amount divergence check (warn but still reverse).
    txn_amount_kobo = int(
        (Decimal(str(txn.amount)) * Decimal(100)).quantize(Decimal("1"))
    )
    if (
        event.amount_kobo is not None
        and event.amount_kobo != txn_amount_kobo
    ):
        logger.warning(
            "amount divergence on reversal for reference %s: "
            "event=%d kobo, local=%d kobo (txn.amount=%s). "
            "Reversing anyway (debiting txn.amount).",
            event.provider_reference,
            event.amount_kobo,
            txn_amount_kobo,
            txn.amount,
        )

    # Only debit if the wallet was actually credited (txn was SUCCESS).
    if txn.status == TransactionStatus.SUCCESS.value:
        amount = Decimal(str(txn.amount))
        user.balance = Decimal(str(user.balance)) - amount
        session.add(user)
    # If the txn was PENDING (credit never applied), just flip to
    # REVERSED — no balance change needed.
    txn.status = TransactionStatus.REVERSED.value
    txn.failure_reason = "payment_reversed"
    session.add(txn)
    write_audit(
        session,
        actor=AuditActor.WEBHOOK,
        event_type=AuditEventType.WALLET_REFUND,
        user_id=user.id,
        entity_type=AuditEntityType.TRANSACTION,
        entity_id=txn.id,
        metadata={
            "reference": event.provider_reference,
            "amount": float(txn.amount),
            "status": "reversed",
        },
    )
    session.commit()
    session.refresh(user)
    session.refresh(txn)

    # Best-effort Telegram notification.
    from app.services.telegram import notify_refund

    await notify_refund(user=user, transaction=txn)

    return CreditApplyResult(
        credited=False,
        new_balance=Decimal(str(user.balance)),
        status="reversed",
        transaction_id=txn.id,
    )


__all__ = [
    "MIN_TOPUP_NGN",
    "MAX_TOPUP_NGN",
    "TopupValidationError",
    "TopupStartResult",
    "CreditApplyResult",
    "start_topup",
    "apply_credit_from_provider_event",
    "reverse_credit_from_provider_event",
    "verify_pending_topup",
]
