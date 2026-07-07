"""Payment provider abstraction.

We never want to lock the business logic to a single gateway. The
`PaymentProvider` Protocol below is the only interface business code
should depend on. Concrete implementations live alongside this file
(`paystack.py`, etc.).

DTOs are kept as `dataclass(frozen=True)` (not Pydantic) because:
  * they are pure data crossing an internal boundary, no validation
    needed beyond typing;
  * they must be cheap to construct in tests;
  * they are returned by *the provider* and validated by the caller,
    so the provider never has to know our app's request validation
    rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# ── DTOs ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VirtualAccountData:
    """A dedicated virtual account (DVA) issued to a user by the provider.

    `provider_reference` is the gateway's ID for this DVA (e.g. Paystack
    `dedicated_account_id`). It is what we store on `virtual_accounts`
    as the FK to gateway reality.
    """

    account_number: str
    account_name: str
    bank_name: str
    bank_code: str
    provider_reference: str  # gateway-side ID
    provider: str  # "paystack", "flutterwave", ...


@dataclass(frozen=True)
class ResolvedAccount:
    """Result of "look up the name behind this account number"."""

    account_number: str
    account_name: str
    bank_code: str


@dataclass(frozen=True)
class TransferResult:
    """The provider's response when we initiated a transfer (payout)."""

    provider_reference: str  # our reference that the provider echoed back
    provider_transfer_id: str  # gateway's transfer ID
    status: str  # "pending" | "success" | "failed" | "reversed"
    raw_response: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WebhookEvent:
    """A verified webhook from the provider.

    `provider_reference` ties the event back to our own records
    (transaction.provider_reference, bill.provider_reference, etc.).
    `event_type` is normalized to a small closed set so business code
    can switch on it safely. Every provider's `parse_webhook()` must
    return one of these canonical names so downstream code stays
    provider-agnostic:

      * "charge.success"           — user topped up (inbound)
      * "charge.reversed"          — inbound payment reversed/charged back
      * "transfer.success"         — our outgoing transfer settled
      * "transfer.failed"          — our outgoing transfer failed
      * "transfer.reversed"        — our outgoing transfer was reversed
      * "dedicatedaccount.assign.success" — DVA provisioning confirmed

    `event_id` is a stable id from the provider used to dedup retries —
    either the provider's `event.id` or a SHA-256 of the raw body when
    the provider omits the field.

    `provider` records which gateway emitted the event. Useful for
    per-provider audit / metric labels.
    """

    event_type: str
    provider_reference: str
    event_id: str
    amount_kobo: int | None = None
    provider: str = ""  # "paystack" | "nomba" — empty = legacy callers
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TopupInit:
    """A provider-hosted top-up flow that the user is about to enter.

    `authorization_url` is where the user is redirected (Paystack's
    hosted page, Stripe Checkout, Nomba Checkout, etc.). `reference`
    is the provider's own reference for the transaction; the same
    value is echoed in the `charge.success` webhook and is the lookup
    key our handler uses to credit the matching pending `Transaction`
    row in our DB.
    `access_code` is provider-specific (Paystack returns it; Nomba
    doesn't) and is useful for inline / popup integrations.
    """

    authorization_url: str
    reference: str
    provider: str  # "paystack" | "nomba" | future
    access_code: str | None = None


@dataclass(frozen=True)
class TransactionStatusResult:
    """Result of looking up a single transaction at the provider.

    `status` is normalized to a small closed set so callers can
    switch on it without knowing provider-specific strings:

      * "success"  — settled / credited
      * "pending"  — not yet settled (poll again later)
      * "failed"   — provider rejected (do not retry)
      * "reversed" — was credited then reversed (refund path)
      * "unknown"  — provider returned a status we don't recognize

    `amount_kobo` is the provider's view of the amount in kobo (1 NGN
    = 100 kobo). May differ from our pending `Transaction.amount`
    (e.g. the Nomba sandbox reports a fixed 4000 for every txn).
    Callers MUST credit the locally-stored `Transaction.amount`
    (what the user agreed to pay) and treat any non-None divergence
    between `amount_kobo` and `Transaction.amount * 100` as
    unreliable: refuse the credit, mark the txn FAILED, and log a
    warning. A `None` amount_kobo means the provider omitted the
    field; in that case fall back to `Transaction.amount`.

    Used by the manual `POST /wallet/topup/verify` endpoint and the
    scheduler's pending-topup poll job as a webhook-agnostic fallback
    when the provider's webhook delivery is delayed or unavailable
    (e.g. dashboard URL not yet validated by Nomba organizers).
    """

    provider_reference: str
    status: str
    amount_kobo: int | None = None
    raw: dict = field(default_factory=dict)


# ── Protocol ────────────────────────────────────────────────────────

@runtime_checkable
class PaymentProvider(Protocol):
    """The contract every payment-gateway implementation must satisfy."""

    name: str  # "paystack" | "flutterwave" | ...

    async def create_customer(
        self,
        *,
        email: str,
        first_name: str,
        last_name: str,
        phone: str | None = None,
    ) -> str:
        """Create a customer at the provider; return provider's customer_id/code."""
        ...

    async def create_virtual_account(
        self,
        *,
        customer_code: str,
        preferred_bank: str | None = None,
    ) -> VirtualAccountData:
        """Issue a dedicated virtual account for `customer_code`."""
        ...

    async def resolve_account(
        self,
        *,
        account_number: str,
        bank_code: str,
    ) -> ResolvedAccount:
        """Look up the name on `account_number` at `bank_code`."""
        ...

    async def create_transfer_recipient(
        self,
        *,
        account_number: str,
        bank_code: str,
        account_name: str,
    ) -> str:
        """Create a transfer recipient; return provider's recipient_code."""
        ...

    async def initiate_transfer(
        self,
        *,
        amount_kobo: int,
        recipient_code: str,
        reference: str,
        reason: str,
        account_name: str = "",
    ) -> TransferResult:
        """Move `amount_kobo` (1 NGN = 100 kobo) from our balance to recipient.

        `account_name` is the bank-side name on the destination
        account (from `resolve_account`). Paystack's two-step flow
        bakes the name into `recipient_code` and ignores it; Nomba's
        one-step flow needs the name in the request body. Default
        empty so Paystack callers don't need to change.
        """
        ...

    async def initialize_topup(
        self,
        *,
        amount_kobo: int,
        email: str,
        reference: str,
        callback_url: str | None = None,
    ) -> TopupInit:
        """Start a hosted top-up flow. The user is redirected to the
        returned `authorization_url`, pays via card / bank / USSD / QR,
        and Paystack fires `charge.success` on completion. Our webhook
        handler (`_handle_charge_success`) credits the wallet using
        `reference` as the lookup key.
        """
        ...

    def verify_webhook_signature(
        self,
        *,
        raw_body: bytes,
        signature_header: str,
    ) -> bool:
        """Return True iff `signature_header` is a valid HMAC of `raw_body`."""
        ...

    async def parse_webhook(
        self,
        *,
        raw_body: bytes,
        signature_header: str,
    ) -> WebhookEvent:
        """Verify signature, then parse into a `WebhookEvent`. Raises on bad sig."""
        ...

    async def get_transaction(
        self,
        *,
        reference: str,
    ) -> TransactionStatusResult:
        """Look up a single transaction by our reference (or the
        provider's) and return its current status.

        Used as a polling fallback when the webhook delivery is
        delayed or unavailable (e.g. provider dashboard URL not
        yet validated). Implementations should:

          1. Call the provider's transaction-status endpoint
             (Paystack `GET /transaction/verify/{ref}`,
              Nomba `GET /v1/transactions/accounts/single?orderReference={ref}`).
          2. Normalize the status string to the closed set
             ("success" | "pending" | "failed" | "reversed" | "unknown").
          3. Return a `TransactionStatusResult`; raise `ProviderError`
             on transport / 4xx / 5xx / unknown reference.

        Implementations MUST be safe to call concurrently for the
        same reference (idempotency is enforced upstream by a
        `SELECT ... FOR UPDATE` row lock in the caller).
        """
        ...
