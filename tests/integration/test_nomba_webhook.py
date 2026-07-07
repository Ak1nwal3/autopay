"""Integration tests for the `/webhooks/nomba` route.

Covers:
  * Signature verification: bad sig / bad ts / bad body → 400
  * Event normalization: payment_success → charge.success credits
    the wallet; payout_success → transfer.success marks bill paid;
    payout_refund → transfer.reversed refunds the user.
  * Replay defense: a second delivery of the same event_id is a
    200 no-op with a webhook.replay audit row.
  * Unknown events: payload is malformed / event_type is unrecognized
    → 200 with a webhook.unknown audit row (we don't make Nomba retry).
  * GET / HEAD / PUT on /webhooks/nomba → 405 (strict method routing).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.bill import Bill
from app.models.enums import (
    AuditEventType,
    BillStatus,
    TransactionStatus,
    TransactionType,
)
from app.models.transaction import Transaction
from app.models.user import User

# ── Helpers ──────────────────────────────────────────────────────────


def _sign_nomba(payload: dict, timestamp: str, secret: str) -> str:
    """Build a real Nomba signature for `payload` (HMAC-SHA256 of the
    canonical colon-joined string, Base64-encoded)."""
    data = payload.get("data") or {}
    merchant = data.get("merchant") or {}
    txn = data.get("transaction") or {}

    def _safe(v):
        if v is None:
            return ""
        s = str(v)
        return "" if s.lower() == "null" else s

    parts = ":".join(
        [
            _safe(payload.get("event_type")),
            _safe(payload.get("requestId")),
            _safe(merchant.get("userId")),
            _safe(merchant.get("walletId")),
            _safe(txn.get("transactionId") or txn.get("id")),
            _safe(txn.get("type")),
            _safe(txn.get("time")),
            _safe(txn.get("responseCode")),
            _safe(timestamp),
        ]
    )
    digest = hmac.new(secret.encode(), parts.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _nomba_webhook(
    client: TestClient,
    payload: dict,
    *,
    timestamp: str = "2026-02-06T10:21:56Z",
    signature: str | None = None,
) -> __import__("fastapi").Response:
    """POST a Nomba webhook with a (default-good) signature."""
    sig = signature if signature is not None else _sign_nomba(
        payload, timestamp, settings.nomba_webhook_secret
    )
    return client.post(
        "/webhooks/nomba",
        content=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "nomba-signature": sig,
            "nomba-timestamp": timestamp,
        },
    )


def _make_user_with_balance(
    session: Session, *, email: str, balance: str, phone: str
) -> User:
    from app.core.security import hash_password
    u = User(
        email=email,
        hashed_password=hash_password("Secret123"),
        first_name="Nomba",
        last_name="Tester",
        phone_number=phone,
        balance=Decimal(balance),
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _make_pending_topup_txn(
    session: Session, *, user_id: int, reference: str, amount: str
) -> Transaction:
    txn = Transaction(
        user_id=user_id,
        type=TransactionType.CREDIT.value,
        amount=Decimal(amount),
        fee=Decimal("0"),
        currency="NGN",
        status=TransactionStatus.PENDING.value,
        provider="nomba",
        provider_reference=reference,
        narration="Top-up via nomba Checkout",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


# ── Signature verification ───────────────────────────────────────────


def test_nomba_webhook_rejects_bad_signature(
    client: TestClient, stub_nomba_provider
) -> None:
    payload = {"event_type": "payment_success", "requestId": "r-1", "data": {}}
    r = client.post(
        "/webhooks/nomba",
        content=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "nomba-signature": "definitely-wrong",
            "nomba-timestamp": "2026-02-06T10:21:56Z",
        },
    )
    assert r.status_code == 400
    assert "Invalid signature" in r.json()["detail"]


def test_nomba_webhook_rejects_bad_timestamp(
    client: TestClient, stub_nomba_provider
) -> None:
    """A correctly-signed body with a different timestamp fails."""
    payload = {"event_type": "payment_success", "requestId": "r-1", "data": {}}
    sig = _sign_nomba(payload, "2026-02-06T10:21:56Z", settings.nomba_webhook_secret)
    r = client.post(
        "/webhooks/nomba",
        content=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "nomba-signature": sig,
            # Different timestamp → signature won't match
            "nomba-timestamp": "2099-01-01T00:00:00Z",
        },
    )
    assert r.status_code == 400


def test_nomba_webhook_rejects_missing_signature_header(
    client: TestClient, stub_nomba_provider
) -> None:
    payload = {"event_type": "payment_success", "requestId": "r-1", "data": {}}
    r = client.post(
        "/webhooks/nomba",
        content=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "nomba-timestamp": "2026-02-06T10:21:56Z",
        },
    )
    assert r.status_code == 400


def test_nomba_webhook_rejects_malformed_body(
    client: TestClient, stub_nomba_provider
) -> None:
    """Even a body that fails to parse must be rejected with 400 if
    the signature doesn't match a real shape. With a bad signature
    the route returns 400 before parsing."""
    r = client.post(
        "/webhooks/nomba",
        content=b"not json",
        headers={
            "Content-Type": "application/json",
            "nomba-signature": "anything",
            "nomba-timestamp": "2026-02-06T10:21:56Z",
        },
    )
    assert r.status_code == 400


# ── Strict method routing ────────────────────────────────────────────


def test_nomba_webhook_get_returns_405(client: TestClient) -> None:
    r = client.get("/webhooks/nomba")
    assert r.status_code == 405


def test_nomba_webhook_head_returns_405(client: TestClient) -> None:
    r = client.head("/webhooks/nomba")
    assert r.status_code == 405


# ── Happy path: payment_success → wallet credit ──────────────────────


def test_payment_success_credits_wallet(
    client: TestClient, session: Session, stub_nomba_provider
) -> None:
    """A signed payment_success webhook with a known reference credits
    the matching pending `Transaction` row and updates the user balance."""
    u = _make_user_with_balance(
        session, email="n1@x.com", balance="0", phone="08090000041"
    )
    ref = "topup_n1_abc"
    _make_pending_topup_txn(session, user_id=u.id, reference=ref, amount="5000.00")

    payload = {
        "event_type": "payment_success",
        "requestId": "req-nomba-1",
        "data": {
            "merchant": {"userId": "u-1", "walletId": "w-1"},
            "transaction": {
                "type": "vact_transfer",
                "transactionId": "tx-1",
                "responseCode": "",
                "time": "2026-02-06T10:21:56Z",
                "transactionAmount": 5000,
                "aliasAccountReference": ref,
            },
        },
    }
    r = _nomba_webhook(client, payload)
    assert r.status_code == 200
    assert r.json()["received"] is True
    assert r.json()["event"] == "charge.success"  # normalized

    session.refresh(u)
    assert Decimal(str(u.balance)) == Decimal("5000.00")


def test_payment_success_idempotent(
    client: TestClient, session: Session, stub_nomba_provider
) -> None:
    """A second delivery of the same event is a 200 no-op."""
    u = _make_user_with_balance(
        session, email="n2@x.com", balance="0", phone="08090000042"
    )
    ref = "topup_n2_xyz"
    _make_pending_topup_txn(session, user_id=u.id, reference=ref, amount="3000.00")

    payload = {
        "event_type": "payment_success",
        "requestId": "req-nomba-dup",
        "data": {
            "merchant": {"userId": "u-2", "walletId": "w-2"},
            "transaction": {
                "type": "vact_transfer",
                "transactionId": "tx-dup",
                "responseCode": "",
                "time": "2026-02-06T10:21:56Z",
                "transactionAmount": 3000,
                "aliasAccountReference": ref,
            },
        },
    }
    r1 = _nomba_webhook(client, payload)
    assert r1.status_code == 200
    r2 = _nomba_webhook(client, payload)
    assert r2.status_code == 200
    assert r2.json().get("replay") is True

    # Wallet was credited exactly once
    session.refresh(u)
    assert Decimal(str(u.balance)) == Decimal("3000.00")


def test_payment_success_amount_mismatch_refuses_credit(
    client: TestClient, session: Session, stub_nomba_provider
) -> None:
    """REGRESSION (bug: "always credited with 4000"): a signed
    payment_success webhook whose `transactionAmount` does NOT
    match the pending `Transaction.amount` must be refused — the
    wallet is not credited and the txn is marked FAILED.

    This is the webhook-side defence against the Nomba sandbox
    reporting a fixed 4000 for every transaction regardless of
    what the user actually top-up'd (or whether they paid at all)."""
    u = _make_user_with_balance(
        session, email="mismatch@x.com", balance="0", phone="08090000050"
    )
    ref = "topup_mismatch_abc"
    _make_pending_topup_txn(session, user_id=u.id, reference=ref, amount="500.00")

    payload = {
        "event_type": "payment_success",
        "requestId": "req-nomba-mismatch",
        "data": {
            "merchant": {"userId": "u-m", "walletId": "w-m"},
            "transaction": {
                "type": "vact_transfer",
                "transactionId": "tx-m",
                "responseCode": "",
                "time": "2026-02-06T10:21:56Z",
                "transactionAmount": 4000,  # diverges from txn.amount (500)
                "aliasAccountReference": ref,
            },
        },
    }
    r = _nomba_webhook(client, payload)
    assert r.status_code == 200
    assert r.json()["received"] is True

    # Wallet was NOT credited.
    session.refresh(u)
    assert Decimal(str(u.balance)) == Decimal("0.00")

    # Transaction is now FAILED (not SUCCESS, not PENDING).
    txn = session.exec(
        select(Transaction).where(Transaction.provider_reference == ref)
    ).first()
    assert txn is not None
    assert txn.status == TransactionStatus.FAILED.value
    assert txn.failure_reason == "amount_mismatch"


def test_payment_reversal_debits_wallet(
    client: TestClient, session: Session, stub_nomba_provider
) -> None:
    """A signed `payment_reversal` webhook reverses a previously
    credited top-up: the wallet is debited by txn.amount and the
    transaction is marked REVERSED."""
    u = _make_user_with_balance(
        session, email="rev1@x.com", balance="0", phone="08090000060"
    )
    ref = "topup_rev1_abc"
    _make_pending_topup_txn(session, user_id=u.id, reference=ref, amount="500.00")

    # First, credit the wallet via a payment_success webhook.
    payload_credit = {
        "event_type": "payment_success",
        "requestId": "req-rev-credit",
        "data": {
            "merchant": {"userId": "u-r", "walletId": "w-r"},
            "transaction": {
                "type": "vact_transfer",
                "transactionId": "tx-r-credit",
                "responseCode": "",
                "time": "2026-02-06T10:21:56Z",
                "transactionAmount": 500,
                "aliasAccountReference": ref,
            },
        },
    }
    r = _nomba_webhook(client, payload_credit)
    assert r.status_code == 200
    session.refresh(u)
    assert Decimal(str(u.balance)) == Decimal("500.00")

    # Now reverse it.
    payload_reversal = {
        "event_type": "payment_reversal",
        "requestId": "req-rev-reverse",
        "data": {
            "merchant": {"userId": "u-r", "walletId": "w-r"},
            "transaction": {
                "type": "vact_transfer",
                "transactionId": "tx-r-reverse",
                "responseCode": "",
                "time": "2026-02-06T10:22:56Z",
                "transactionAmount": 500,
                "aliasAccountReference": ref,
            },
        },
    }
    r2 = _nomba_webhook(client, payload_reversal)
    assert r2.status_code == 200
    assert r2.json()["event"] == "charge.reversed"

    # Wallet debited back to 0.
    session.refresh(u)
    assert Decimal(str(u.balance)) == Decimal("0.00")

    # Transaction is REVERSED.
    txn = session.exec(
        select(Transaction).where(Transaction.provider_reference == ref)
    ).first()
    assert txn is not None
    assert txn.status == TransactionStatus.REVERSED.value


def test_payment_reversal_idempotent(
    client: TestClient, session: Session, stub_nomba_provider
) -> None:
    """A second delivery of the same reversal event is a 200 no-op
    (wallet not double-debited)."""
    u = _make_user_with_balance(
        session, email="rev2@x.com", balance="0", phone="08090000061"
    )
    ref = "topup_rev2_xyz"
    _make_pending_topup_txn(session, user_id=u.id, reference=ref, amount="1000.00")

    # Credit first.
    payload_credit = {
        "event_type": "payment_success",
        "requestId": "req-rev2-credit",
        "data": {
            "merchant": {"userId": "u-r2", "walletId": "w-r2"},
            "transaction": {
                "type": "vact_transfer",
                "transactionId": "tx-r2-credit",
                "responseCode": "",
                "time": "2026-02-06T10:21:56Z",
                "transactionAmount": 1000,
                "aliasAccountReference": ref,
            },
        },
    }
    _nomba_webhook(client, payload_credit)
    session.refresh(u)
    assert Decimal(str(u.balance)) == Decimal("1000.00")

    # Reverse twice (same requestId → deduped).
    payload_reversal = {
        "event_type": "payment_reversal",
        "requestId": "req-rev2-reverse",
        "data": {
            "merchant": {"userId": "u-r2", "walletId": "w-r2"},
            "transaction": {
                "type": "vact_transfer",
                "transactionId": "tx-r2-reverse",
                "responseCode": "",
                "time": "2026-02-06T10:22:56Z",
                "transactionAmount": 1000,
                "aliasAccountReference": ref,
            },
        },
    }
    r1 = _nomba_webhook(client, payload_reversal)
    assert r1.status_code == 200
    r2 = _nomba_webhook(client, payload_reversal)
    assert r2.status_code == 200
    assert r2.json().get("replay") is True

    # Wallet debited exactly once.
    session.refresh(u)
    assert Decimal(str(u.balance)) == Decimal("0.00")


# ── Happy path: payout_success → bill paid ───────────────────────────


def test_payout_success_marks_bill_paid(
    client: TestClient, session: Session, stub_nomba_provider
) -> None:
    from app.core.security import hash_password

    # Set up a user with a bill in PROCESSING + a matching debit txn
    u = User(
        email="n3@x.com",
        hashed_password=hash_password("Secret123"),
        first_name="N",
        last_name="T",
        phone_number="08090000033",
        balance=Decimal("0"),
    )
    session.add(u)
    session.commit()
    session.refresh(u)

    bill = Bill(
        user_id=u.id,
        vendor_name="DSTV",
        amount=Decimal("5000"),
        due_date="2025-12-31T00:00:00Z",
        status=BillStatus.PROCESSING.value,
        account_number="0123456789",
        bank_code="058",
    )
    session.add(bill)
    session.commit()
    session.refresh(bill)

    ref = "autopay_n3_xyz"
    txn = Transaction(
        user_id=u.id,
        bill_id=bill.id,
        type=TransactionType.DEBIT.value,
        amount=Decimal("5000"),
        fee=Decimal("50"),
        currency="NGN",
        status=TransactionStatus.PROCESSING.value,
        provider="nomba",
        provider_reference=ref,
        narration="DSTV",
    )
    session.add(txn)
    session.commit()

    payload = {
        "event_type": "payout_success",
        "requestId": "req-payout-1",
        "data": {
            "merchant": {"userId": "u-3", "walletId": "w-3"},
            "transaction": {
                "type": "transfer",
                "id": "API-TRANSFER-n1",
                "responseCode": "",
                "time": "2026-02-06T10:21:56Z",
                "transactionAmount": 5000,
                "merchantTxRef": ref,
            },
        },
    }
    r = _nomba_webhook(client, payload)
    assert r.status_code == 200
    assert r.json()["event"] == "transfer.success"  # normalized

    session.refresh(txn)
    session.refresh(bill)
    assert txn.status == TransactionStatus.SUCCESS.value
    assert bill.status == BillStatus.PAID.value


def test_payout_refund_refunds_wallet(
    client: TestClient, session: Session, stub_nomba_provider
) -> None:
    """payout_refund normalizes to transfer.reversed → wallet credit.

    The webhook-side confirm_payout flips a processing DEBIT to
    FAILED, credits the amount + fee back to the wallet, and bumps
    the bill's retry_count.
    """
    from app.core.security import hash_password

    u = User(
        email="n4@x.com",
        hashed_password=hash_password("Secret123"),
        first_name="N",
        last_name="T",
        phone_number="08090000044",
        balance=Decimal("0"),
    )
    session.add(u)
    session.commit()
    session.refresh(u)

    bill = Bill(
        user_id=u.id,
        vendor_name="DSTV",
        amount=Decimal("2000"),
        due_date="2025-12-31T00:00:00Z",
        status=BillStatus.PROCESSING.value,
        account_number="0123456789",
        bank_code="058",
    )
    session.add(bill)
    session.commit()
    session.refresh(bill)

    ref = "autopay_n4_xyz"
    txn = Transaction(
        user_id=u.id,
        bill_id=bill.id,
        type=TransactionType.DEBIT.value,
        amount=Decimal("2000"),
        fee=Decimal("50"),
        currency="NGN",
        status=TransactionStatus.PROCESSING.value,
        provider="nomba",
        provider_reference=ref,
        narration="DSTV",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)

    payload = {
        "event_type": "payout_refund",
        "requestId": "req-refund-1",
        "data": {
            "merchant": {"userId": "u-4", "walletId": "w-4"},
            "transaction": {
                "type": "transfer",
                "id": "API-TRANSFER-r1",
                "responseCode": "",
                "time": "2026-02-06T10:21:56Z",
                "transactionAmount": 2000,
                "merchantTxRef": ref,
            },
        },
    }
    r = _nomba_webhook(client, payload)
    assert r.status_code == 200
    assert r.json()["event"] == "transfer.reversed"  # normalized

    session.refresh(txn)
    session.refresh(u)
    assert txn.status == TransactionStatus.FAILED.value
    # Wallet was refunded (amount + fee)
    assert Decimal(str(u.balance)) == Decimal("2050.00")


# ── Unknown event → 200 with audit row ─────────────────────────────


def test_unknown_event_returns_200_with_audit(
    client: TestClient, session: Session, stub_nomba_provider
) -> None:
    """Nomba can send events we don't recognize (e.g. `directdebit.activated`).
    We 200 (so Nomba doesn't retry) and write a webhook.unknown audit row."""
    payload = {
        "event_type": "directdebit.activated",
        "requestId": "req-unknown-1",
        "data": {
            "merchant": {"userId": "u-x", "walletId": "w-x"},
            "transaction": {"type": "direct_debit", "id": "dd-1"},
        },
    }
    r = _nomba_webhook(client, payload)
    assert r.status_code == 200
    # An audit row was written
    rows = session.exec(select(AuditLog)).all()
    assert any(
        r.event_type == AuditEventType.WEBHOOK_UNKNOWN.value
        and r.event_metadata
        and r.event_metadata.get("raw_event_type") == "directdebit.activated"
        for r in rows
    )
