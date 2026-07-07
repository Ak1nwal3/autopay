"""Webhook handlers.

Mounted at /webhooks/nomba in `app.main`.

This is the Nomba-only gateway project. Nomba POSTs raw JSON bodies;
we MUST verify the per-provider signature header BEFORE parsing JSON,
to prevent attackers from forging `charge.success` events.

REPLAY DEFENSE: We dedup on (provider, event_id) via the
`webhook_events` table. Nomba retries the same event on a network
blip; the second delivery is a 200 no-op with an audit breadcrumb.

Per-provider signature details:
  * Nomba:   HMAC-SHA256 of a colon-joined custom string built from
             specific fields in the JSON body; header
             `nomba-signature` + `nomba-timestamp`. See
             `verify_nomba_webhook_signature` in
             `app/services/payments/nomba.py` for the full payload
             format.

Event-type normalization: Nomba's `parse_webhook()` method returns
canonical `WebhookEvent.event_type` values
("charge.success", "transfer.success", etc.) so the handlers below
work provider-agnostically.

The actual credit / debit logic is now in `app.services.wallet` and
`app.services.payout` — the routes below are thin dispatchers. The
shared functions use `SELECT ... FOR UPDATE` so that the webhook path
serializes correctly against the manual-verify and scheduler-poll
fallback paths (see `app/services/wallet.py:apply_credit_from_provider_event`).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.database import get_session
from app.models.enums import AuditActor, AuditEntityType, AuditEventType
from app.models.transaction import Transaction
from app.models.user import User
from app.models.webhook_event import WebhookEvent as WebhookEventRow
from app.services.audit import write_audit
from app.services.payments import (
    PaymentProvider,
    WebhookSignatureError,
    get_payment_provider,
)
from app.services.payout import confirm_payout
from app.services.telegram import notify_debit, notify_refund
from app.services.wallet import apply_credit_from_provider_event, reverse_credit_from_provider_event
from app.services.payments.nomba import verify_nomba_webhook_signature

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


# ── Nomba webhook route ─────────────────────────────────────────────

@router.post("/nomba", status_code=status.HTTP_200_OK)
async def nomba_webhook(
    request: Request,
    session: Session = Depends(get_session),
    provider: PaymentProvider = Depends(get_payment_provider),
) -> dict:
    """Receive + verify + dispatch a Nomba webhook event.

    Strict 405 on GET/HEAD (matches the parent project's Paystack
    route's behavior). Nomba uses a custom HMAC-SHA256 payload (not
    the raw body); we verify with the full
    `verify_nomba_webhook_signature` helper because the Protocol's
    `verify_webhook_signature` can't see the `nomba-timestamp`
    header.

    After verification, `provider.parse_webhook` normalizes the event
    type (`payment_success` → `charge.success`, etc.) and we dispatch
    to the same `_handle_charge_success` / `_handle_transfer_update`
    handlers used in the parent project. Replay defense is via the
    same `webhook_events` UNIQUE constraint.
    """
    raw_body = await request.body()
    signature = request.headers.get("nomba-signature") or ""
    timestamp = request.headers.get("nomba-timestamp") or ""

    # Signature verification happens in the route because the
    # Protocol's signature_header parameter can't carry the timestamp.
    from app.core.config import settings
    if not verify_nomba_webhook_signature(
        raw_body=raw_body,
        signature_header=signature,
        timestamp_header=timestamp,
        secret=settings.nomba_webhook_secret,
    ):
        logger.warning("Nomba webhook with bad signature")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid signature",
        )

    try:
        event = await provider.parse_webhook(
            raw_body=raw_body, signature_header=signature
        )
    except WebhookSignatureError as exc:
        logger.warning("Nomba webhook parse failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook payload",
        ) from exc

    # Replay defense: the (provider, event_id) UNIQUE constraint on
    # webhook_events makes a second delivery of the same event a 200
    # no-op. Race-safe via the UNIQUE constraint + IntegrityError.
    try:
        session.add(
            WebhookEventRow(
                provider=provider.name,
                event_id=event.event_id,
                event_type=event.event_type,
            )
        )
        session.flush()
    except IntegrityError:
        session.rollback()
        write_audit(
            session,
            actor=AuditActor.WEBHOOK,
            event_type=AuditEventType.WEBHOOK_REPLAY,
            user_id=None,
            entity_type=AuditEntityType.TRANSACTION,
            entity_id=None,
            metadata={
                "provider": provider.name,
                "event_id": event.event_id,
                "event_type": event.event_type,
            },
        )
        session.commit()
        logger.info("Nomba webhook replay rejected: %s", event.event_id)
        return {"received": True, "replay": True, "event": event.event_type}

    logger.info(
        "Nomba webhook: event=%s canonical=%s reference=%s amount_kobo=%s",
        event.raw.get("event_type"),
        event.event_type,
        event.provider_reference,
        event.amount_kobo,
    )

    # Dispatch on the canonical event_type (already normalized by
    # the provider's parse_webhook).
    if event.event_type == "charge.success":
        await _handle_charge_success(session, event)
    elif event.event_type == "charge.reversed":
        await _handle_charge_reversed(session, event)
    elif event.event_type in ("transfer.success", "transfer.failed", "transfer.reversed"):
        await _handle_transfer_update(session, event)
    elif event.event_type == "dedicatedaccount.assign.success":
        _handle_dva_assigned(session, event)
    else:
        write_audit(
            session,
            actor=AuditActor.WEBHOOK,
            event_type=AuditEventType.WEBHOOK_UNKNOWN,
            user_id=None,
            entity_type=AuditEntityType.TRANSACTION,
            entity_id=None,
            metadata={
                "provider": provider.name,
                "event_type": event.event_type,
                "raw_event_type": event.raw.get("event_type"),
                "event_id": event.event_id,
            },
        )
        session.commit()

    return {"received": True, "event": event.event_type}


# ── Handlers ────────────────────────────────────────────────────────

async def _handle_charge_success(session: Session, event) -> None:
    """User's VA received money. Credit their wallet and update txn.

    The actual credit is delegated to
    `app.services.wallet.apply_credit_from_provider_event` so the
    same logic is shared by the webhook + manual-verify +
    scheduler-poll paths. That function uses
    `SELECT ... FOR UPDATE` to make concurrent credits of the
    same transaction safe.
    """
    if not event.provider_reference:
        logger.warning("charge.success with no reference: %s", event.raw)
        return

    # Resolve the user BEFORE the row lock so we have `user_id` to
    # pass into the credit function (the function needs the User
    # object to update `balance` + fire the Telegram notification).
    txn = session.exec(
        select(Transaction).where(
            Transaction.provider_reference == event.provider_reference
        )
    ).first()
    if txn is None:
        # No matching transaction at all — let the shared function
        # log the orphan credit + return early. We pass a synthetic
        # user; the function's user_id check will trip and write
        # the orphan_credit audit row.
        from app.models.user import User as _User

        synthetic = _User(
            id=0, email="", hashed_password="",
            first_name="", last_name="",
            phone_number="", balance=0,
            currency="NGN",
        )
        await apply_credit_from_provider_event(
            session, user=synthetic, event=event,
        )
        return

    user = session.get(User, txn.user_id)
    if user is None:
        logger.warning(
            "charge.success for reference %s: user %d not found",
            event.provider_reference, txn.user_id,
        )
        return

    await apply_credit_from_provider_event(
        session, user=user, event=event,
    )


async def _handle_charge_reversed(session: Session, event) -> None:
    """A previously credited payment was reversed (chargeback).

    Delegates to `reverse_credit_from_provider_event` so the
    same row-lock + idempotency + audit guarantees apply as the
    credit path. The shared function debits `txn.amount` from
    the user's balance and flips the transaction to REVERSED.
    """
    if not event.provider_reference:
        logger.warning("charge.reversed with no reference: %s", event.raw)
        return

    txn = session.exec(
        select(Transaction).where(
            Transaction.provider_reference == event.provider_reference
        )
    ).first()
    if txn is None:
        logger.warning(
            "charge.reversed for reference %s: no matching transaction",
            event.provider_reference,
        )
        return

    user = session.get(User, txn.user_id)
    if user is None:
        logger.warning(
            "charge.reversed for reference %s: user %d not found",
            event.provider_reference, txn.user_id,
        )
        return

    await reverse_credit_from_provider_event(
        session, user=user, event=event,
    )


async def _handle_transfer_update(session: Session, event) -> None:
    """Our outbound transfer completed / failed / was reversed."""
    success = event.event_type == "transfer.success"
    failure_reason: str | None = None
    if not success:
        failure_reason = event.event_type  # "transfer.failed" | "transfer.reversed"

    payout_result = confirm_payout(
        session,
        provider_reference=event.provider_reference,
        success=success,
        failure_reason=failure_reason,
    )
    session.commit()

    # Best-effort Telegram notification. We only fire this if the
    # payout actually changed state (confirm_payout returns None
    # for no-ops like unknown references or already-reconciled
    # transactions). Refresh user + txn so the notifier sees the
    # new balance.
    if payout_result is not None and payout_result.success is True:
        # Look up the now-reconciled txn so we can hand it to the
        # notifier (which formats amount / narration / fee from it).
        from sqlalchemy import select as _sa_select

        from app.models.transaction import Transaction

        txn = session.execute(
            _sa_select(Transaction).where(
                Transaction.provider_reference == event.provider_reference
            )
        ).scalar_one_or_none()
        if txn is not None:
            user = session.get(User, txn.user_id)
            if user is not None:
                await notify_debit(user=user, transaction=txn)
    elif payout_result is not None and payout_result.success is False:
        # Refund path: the user's balance was credited back.
        from sqlalchemy import select as _sa_select

        from app.models.transaction import Transaction

        txn = session.execute(
            _sa_select(Transaction).where(
                Transaction.provider_reference == event.provider_reference
            )
        ).scalar_one_or_none()
        if txn is not None:
            user = session.get(User, txn.user_id)
            if user is not None:
                await notify_refund(user=user, transaction=txn)


def _handle_dva_assigned(session: Session, event) -> None:
    """DVA was successfully assigned. Signup already created the row;
    this is mostly an audit-log breadcrumb."""
    write_audit(
        session,
        actor=AuditActor.WEBHOOK,
        event_type=AuditEventType.VA_CREATED,
        user_id=None,
        entity_type=AuditEntityType.VIRTUAL_ACCOUNT,
        entity_id=None,
        metadata={"event": "dva_assigned", "reference": event.provider_reference, "event_id": event.event_id},
    )
    session.commit()
