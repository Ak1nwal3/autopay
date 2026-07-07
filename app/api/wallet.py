"""Wallet API — balance, virtual account provisioning, top-up,
transaction history.

Mounted at /api/v1/wallet in `app.main`.

Four endpoints:
  * `GET  /wallet`             — current balance (auth)
  * `GET  /wallet/transactions` — last 20 transactions (auth)
  * `POST /wallet/provision`   — DVA provision (auth, deprecated once
                                   your Paystack business is approved)
  * `POST /wallet/topup`       — start a Checkout-based top-up (auth)

The top-up flow:
  1. Client POSTs `{amount}` to /wallet/topup.
  2. We mint a unique `reference`, persist a pending `Transaction`
     row, call `provider.initialize_topup(...)`, return the
     `authorization_url` for the client to open in a browser.
  3. User pays on the Paystack-hosted page (card / bank / USSD / QR).
  4. Paystack fires `charge.success` with `data.reference == our ref`.
  5. Existing `_handle_charge_success` looks up the `Transaction` by
     `provider_reference`, credits the wallet, flips status to success,
     writes the audit row.

The top-up business logic (validation, persistence, audit, metrics)
lives in `app.services.wallet.start_topup`; this route is a thin
adapter that turns the JSON body into a service call and translates
service errors into HTTP responses.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.database import get_session
from app.models.user import User
from app.models.virtual_account import VirtualAccount
from app.models.enums import (
    AuditActor,
    AuditEntityType,
    AuditEventType,
)
from app.models.transaction import Transaction
from app.schemas.transaction import TransactionResponse
from app.services.audit import (
    audit_va_created,
    write_audit,
)
from app.services.auth import get_current_active_user
from app.services.payments import (
    PaymentError,
    PaymentProvider,
    get_payment_provider,
)
from app.services.wallet import (
    MAX_TOPUP_NGN,
    MIN_TOPUP_NGN,
    TopupValidationError,
    start_topup,
    verify_pending_topup,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["wallet"])


# ── Provision schemas (DVA path) ───────────────────────────────────

class VirtualAccountPublic(BaseModel):
    """Wire format for the user's virtual account."""

    account_number: Optional[str] = None
    account_name: Optional[str] = None
    bank_name: Optional[str] = None
    bank_code: Optional[str] = None
    provider: str
    provider_reference: str


class ProvisionResponse(BaseModel):
    virtual_account: VirtualAccountPublic
    already_existed: bool
    message: str


@router.post(
    "/provision",
    response_model=ProvisionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Provision a virtual account for the logged-in user",
)
async def provision_virtual_account(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_active_user),
    provider: PaymentProvider = Depends(get_payment_provider),
) -> ProvisionResponse:
    """Idempotent. If the user already has a VA, returns it (200 OK
    semantically, but 201 status code for both cases — the field
    `already_existed` distinguishes). On a real provider error, returns
    502 with the error in the audit log; the user can retry.
    """
    existing = session.exec(
        select(VirtualAccount).where(VirtualAccount.user_id == user.id)
    ).first()
    if existing is not None:
        return ProvisionResponse(
            virtual_account=VirtualAccountPublic(
                account_number=existing.account_number,
                account_name=existing.account_name,
                bank_name=existing.bank_name,
                bank_code=None,  # not stored on the model today
                provider=existing.provider,
                provider_reference=existing.provider_account_reference,
            ),
            already_existed=True,
            message="Virtual account already provisioned.",
        )

    try:
        customer_code = await provider.create_customer(
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            phone=user.phone_number,
        )
        va_data = await provider.create_virtual_account(customer_code=customer_code)
    except PaymentError as exc:
        # Log the failure with the full provider message; expose a
        # clean 502 to the user.
        write_audit(
            session,
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.VA_CREATED,
            user_id=user.id,
            entity_type=AuditEntityType.USER,
            entity_id=user.id,
            metadata={
                "provider": provider.name,
                "error": str(exc),
                "status": "failed",
                "trigger": "explicit_provision",
            },
        )
        session.commit()
        logger.exception("DVA provision failed for user %d", user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not create your virtual account. Please try again.",
        ) from exc

    va = VirtualAccount(
        user_id=user.id,
        provider=provider.name,
        provider_account_reference=va_data.provider_reference,
        account_number=va_data.account_number,
        account_name=va_data.account_name,
        bank_name=va_data.bank_name,
    )
    session.add(va)
    session.flush()
    audit_va_created(
        session,
        user_id=user.id,
        va_id=va.id or 0,
        provider=provider.name,
        account_number=va_data.account_number,
    )
    session.commit()
    session.refresh(va)

    return ProvisionResponse(
        virtual_account=VirtualAccountPublic(
            account_number=va.account_number,
            account_name=va.account_name,
            bank_name=va.bank_name,
            bank_code=None,
            provider=va.provider,
            provider_reference=va.provider_account_reference,
        ),
        already_existed=False,
        message="Virtual account provisioned.",
    )


@router.get(
    "/virtual-account",
    response_model=Optional[VirtualAccountPublic],
    summary="Fetch the logged-in user's virtual account (if any)",
)
def get_virtual_account(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_active_user),
) -> Optional[VirtualAccountPublic]:
    """Returns the user's virtual account details, or `null` if they
    haven't provisioned one yet. The SPA uses this to decide whether
    to show the account details or a "Provision" button."""
    va = session.exec(
        select(VirtualAccount).where(VirtualAccount.user_id == user.id)
    ).first()
    if va is None:
        return None
    return VirtualAccountPublic(
        account_number=va.account_number,
        account_name=va.account_name,
        bank_name=va.bank_name,
        bank_code=None,
        provider=va.provider,
        provider_reference=va.provider_account_reference,
    )


# ── Top-up via Checkout (no DVA required) ──────────────────────────


class TopupRequest(BaseModel):
    """Body for `POST /wallet/topup`."""

    amount: Decimal = Field(
        ...,
        gt=0,
        description="Amount in NGN. Must be between 100 and 1,000,000.",
    )
    callback_url: Optional[str] = Field(
        default=None,
        description="Where to redirect the user after the Paystack page. "
        "Defaults to a deep-link back to the dashboard / bot.",
    )


class TopupResponse(BaseModel):
    """Wire format for the top-up init response."""

    authorization_url: str  # Paystack-hosted Checkout page
    reference: str  # pass to /webhooks/paystack as data.reference
    transaction_id: int  # the pending Transaction row id
    amount: Decimal
    currency: str = "NGN"
    message: str


class TopupVerifyRequest(BaseModel):
    """Body for `POST /wallet/topup/verify`. The reference is the
    one returned by `POST /wallet/topup`."""

    reference: str = Field(..., min_length=4, max_length=128)


class TopupVerifyResponse(BaseModel):
    """Wire format for the verify-topup endpoint. Tells the caller
    what happened and what the current balance is."""

    credited: bool
    status: str  # "credited" | "already_credited" | "provider_pending" |
                 # "provider_failed" | "unknown_reference" | "provider_unknown"
    new_balance: Decimal
    message: str


@router.post(
    "/topup",
    response_model=TopupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a hosted top-up via Paystack Checkout",
)
async def topup_wallet(
    payload: TopupRequest,
    request: Optional[object] = None,  # noqa: ARG001  (placeholder; replace with Request if you want IP logging)
    session: Session = Depends(get_session),
    user: User = Depends(get_current_active_user),
    provider: PaymentProvider = Depends(get_payment_provider),
) -> TopupResponse:
    """Mint a unique `reference`, persist a pending `Transaction` row,
    call `provider.initialize_topup(...)`, return the `authorization_url`
    for the client to open. The `charge.success` webhook credits the
    wallet when the user completes payment.

    Idempotent at the Paystack level (same reference → same session).
    At our level, two POSTs with the same amount produce two distinct
    `Transaction` rows (different `reference`s). That's by design —
    the user can have multiple in-flight top-ups.

    Business logic is delegated to `app.services.wallet.start_topup`;
    this route is a thin adapter that translates HTTP errors to
    service errors and back.
    """
    try:
        result = await start_topup(
            session,
            user=user,
            amount=Decimal(str(payload.amount)),
            provider=provider,
            callback_url=payload.callback_url,
        )
    except TopupValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except PaymentError as exc:
        logger.exception("Topup init failed for user %d", user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Top-up could not be started. Please try again.",
        ) from exc

    return TopupResponse(
        authorization_url=result.authorization_url,
        reference=result.reference,
        transaction_id=result.transaction_id,
        amount=result.amount,
        currency="NGN",
        message=(
            "Open the URL in a browser to complete payment. "
            "Your wallet will be credited when Paystack confirms."
        ),
    )


# ── Verify topup (webhook-agnostic fallback) ──────────────────────


@router.post(
    "/topup/verify",
    response_model=TopupVerifyResponse,
    summary="Manually verify a pending top-up via the provider's API",
)
async def verify_topup(
    payload: TopupVerifyRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_active_user),
    provider: PaymentProvider = Depends(get_payment_provider),
) -> TopupVerifyResponse:
    """Re-fetch a pending top-up's status from the payment provider
    and apply the credit if the provider says it has settled.

    This is the manual-verify fallback for the case when the
    provider's webhook delivery is delayed or unavailable (e.g.
    Nomba's dashboard URL hasn't been validated yet). The SPA
    shows an "I've paid" button on the wallet page that calls
    this endpoint; the bot's `/topup` flow shows a similar
    inline button.

    Safe to call concurrently with the webhook handler: both
    paths use `SELECT ... FOR UPDATE` in the shared
    `verify_pending_topup` service function to serialize on the
    `Transaction` row, so the wallet is credited exactly once
    no matter which path wins.
    """
    try:
        result = await verify_pending_topup(
            session,
            user=user,
            reference=payload.reference,
            provider=provider,
        )
    except PaymentError as exc:
        logger.exception(
            "verify_topup failed for user %d reference %s",
            user.id, payload.reference,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not verify the top-up. Please try again.",
        ) from exc

    # Map the service result to a user-facing message.
    if result.credited:
        message = (
            f"Payment confirmed! Your wallet is now "
            f"\u20a6{result.new_balance:,.2f}."
        )
    elif result.status == "already_credited":
        message = (
            f"Already credited. Your wallet is "
            f"\u20a6{result.new_balance:,.2f}."
        )
    elif result.status == "already_failed":
        message = (
            "This top-up was rejected due to a payment mismatch. "
            "Please start a new top-up from the wallet page."
        )
    elif result.status == "amount_mismatch":
        message = (
            "Payment verification failed: the amount reported by the "
            "provider does not match the top-up amount. Your wallet "
            "was not credited. Please start a new top-up or contact "
            "support if you believe this is an error."
        )
    elif result.status == "unknown_reference":
        message = (
            "We couldn't find that top-up. Double-check the reference "
            "or wait a moment and try again."
        )
    elif result.status == "provider_pending":
        message = (
            "The provider hasn't confirmed the payment yet. "
            "Give it a few seconds and tap \u201cI've paid\u201d again."
        )
    elif result.status == "provider_failed":
        message = (
            "The provider says the payment failed. Please retry "
            "from the top-up page."
        )
    elif result.status == "provider_reversed":
        message = (
            "The provider reversed this payment. Your wallet was "
            "not credited. Please contact support if this is wrong."
        )
    else:  # provider_unknown
        message = (
            "The provider returned an unexpected status. "
            "Please try again in a moment."
        )

    return TopupVerifyResponse(
        credited=result.credited,
        status=result.status,
        new_balance=result.new_balance,
        message=message,
    )


# ── Transaction history ────────────────────────────────────────


@router.get(
    "/transactions",
    response_model=list[TransactionResponse],
    summary="List the caller's recent transactions (last 20)",
)
def list_transactions(
    limit: int = Query(20, ge=1, le=100),
    type: Optional[str] = Query(
        None,
        description="Filter by transaction type ('credit' or 'debit')",
    ),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_active_user),
) -> list[Transaction]:
    """Return the caller's most recent transactions, newest first.

    Query params:
      * `limit` (default 20, max 100) — how many rows to return.
      * `type`  (optional) — filter to just `credit` (top-ups) or
        just `debit` (bill payments + refunds).

    Mirrors the bot's `/transactions` command so the web dashboard
    sees the same data.
    """
    from app.models.transaction import Transaction
    from sqlalchemy import select as _sa_select

    q = _sa_select(Transaction).where(Transaction.user_id == user.id)
    if type:
        # Normalize to the enum value (lowercase, e.g. "credit").
        q = q.where(Transaction.type == type.lower())
    q = q.order_by(Transaction.created_at.desc()).limit(limit)
    # SQLModel 0.0.22 quirk: `session.exec(select(Model)).all()` returns
    # a list of Row tuples. Use `scalars()` to get the model instances.
    rows = session.execute(q).scalars().all()
    return rows
