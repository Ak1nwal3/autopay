"""Tests for the credit/debit/refund Telegram notifications and
the new multi-action Done keyboard.

These cover:
  * `notify_user_of_transaction()` — message format + dispatch
  * Credit fires on `charge.success` webhook (end-to-end)
  * Debit fires on `transfer.success` webhook (end-to-end)
  * Refund fires on `transfer.failed` webhook
  * The Done button is no longer a dead-end: tapping "Check
    balance" shows the user's balance; tapping "Top up again"
    returns to the quick-pick keyboard.
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.database import session_scope
from app.core.security import hash_password
from app.models.bill import Bill
from app.models.enums import (
    BillStatus,
    TransactionStatus,
    TransactionType,
)
from app.models.transaction import Transaction
from app.models.user import User
from app.services import telegram as tg_service


# ── Helpers ────────────────────────────────────────────────────────


def _signed_nomba_body(payload: dict) -> tuple[bytes, str, str]:
    """Return (raw_body, nomba-signature, nomba-timestamp) with a real HMAC.

    The Nomba signature scheme is HMAC-SHA256 over a colon-joined
    payload extracted from the JSON body. See
    `app.services.payments.nomba.verify_nomba_webhook_signature`.
    """
    import base64
    from app.core.config import settings
    from tests.integration._webhook_helpers import sign_nomba_payload

    raw = json.dumps(payload).encode("utf-8")
    timestamp = "1700000000000"
    sig = sign_nomba_payload(
        json.loads(raw.decode()), secret=settings.nomba_webhook_secret, timestamp=timestamp
    )
    return raw, sig, timestamp


def _link_user(session, *, email: str, chat_id: int, balance: str = "0") -> User:
    u = User(
        email=email,
        hashed_password=hash_password("Secret123"),
        first_name="Notif",
        last_name="Tester",
        phone_number=f"0809{chat_id:08d}",
        balance=Decimal(balance),
        telegram_chat_id=str(chat_id),
        is_telegram_linked=True,
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


@pytest.fixture
def bot_with_send_recorder(monkeypatch):
    """Stub the running bot's `send_message` to record calls without
    hitting Telegram. Also installs a fake `_application` so
    `notify_user_of_transaction` finds it."""
    sent: list[dict] = []

    fake_app = MagicMock()
    fake_app.bot.send_message = AsyncMock(
        side_effect=lambda **kwargs: sent.append(kwargs) or None
    )

    # Save + swap the global
    original = tg_service._application
    tg_service._application = fake_app
    try:
        yield sent
    finally:
        tg_service._application = original


# ── notify_user_of_transaction: message format + dispatch ─────────


@pytest.mark.asyncio
async def test_notify_credit_message_format(
    session, bot_with_send_recorder
) -> None:
    """A credit notification should mention the amount, narration,
    and new balance, and use + sign + green 💰."""
    u = _link_user(session, email="notif1@x.com", chat_id=100001, balance="0")
    txn = Transaction(
        user_id=u.id,
        type=TransactionType.CREDIT.value,
        amount=Decimal("5000.00"),
        fee=Decimal("0.00"),
        currency="NGN",
        status=TransactionStatus.SUCCESS.value,
        provider="nomba",
        provider_reference="topup_1_abc",
        narration="Top-up via Nomba Checkout",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    # Bump the user's balance to the new post-credit value (the
    # caller of notify_credit is expected to do this).
    u.balance = Decimal("5000.00")
    session.add(u)
    session.commit()
    session.refresh(u)

    sent = bot_with_send_recorder
    result = await tg_service.notify_credit(user=u, transaction=txn)
    assert result is True
    assert len(sent) == 1

    msg = sent[0]
    assert msg["chat_id"] == "100001"
    body = msg["text"]
    assert "Top-up successful" in body
    assert "+₦5,000.00" in body
    assert "Top-up via Nomba Checkout" in body
    assert "₦5,000.00" in body  # new balance
    # Markdown + keyboard
    assert msg["parse_mode"] == "Markdown"
    assert msg["reply_markup"] is not None


@pytest.mark.asyncio
async def test_notify_debit_message_format(
    session, bot_with_send_recorder
) -> None:
    """A debit notification should mention amount, narration, and
    remaining balance with − sign + 💸."""
    u = _link_user(session, email="notif2@x.com", chat_id=100002, balance="10000")
    txn = Transaction(
        user_id=u.id,
        type=TransactionType.DEBIT.value,
        amount=Decimal("3000.00"),
        fee=Decimal("50.00"),
        currency="NGN",
        status=TransactionStatus.SUCCESS.value,
        provider="nomba",
        provider_reference="autopay_1_xyz",
        narration="DSTV",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    u.balance = Decimal("10000.00")  # post-debit balance
    session.add(u)
    session.commit()
    session.refresh(u)

    sent = bot_with_send_recorder
    result = await tg_service.notify_debit(user=u, transaction=txn)
    assert result is True
    body = sent[0]["text"]
    assert "Bill paid" in body
    assert "−₦3,000.00" in body
    assert "₦50.00" in body  # fee
    assert "DSTV" in body


@pytest.mark.asyncio
async def test_notify_refund_message_format(
    session, bot_with_send_recorder
) -> None:
    """A refund notification should include the refunded amount,
    the failure reason, and the new (post-refund) balance."""
    u = _link_user(session, email="notif3@x.com", chat_id=100003, balance="0")
    txn = Transaction(
        user_id=u.id,
        type=TransactionType.DEBIT.value,
        amount=Decimal("2000.00"),
        fee=Decimal("50.00"),
        currency="NGN",
        status=TransactionStatus.FAILED.value,
        provider="nomba",
        provider_reference="autopay_1_failed",
        narration="DSTV",
        failure_reason="transfer_failed",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    u.balance = Decimal("0.00")  # post-refund
    session.add(u)
    session.commit()
    session.refresh(u)

    sent = bot_with_send_recorder
    result = await tg_service.notify_refund(user=u, transaction=txn)
    assert result is True
    body = sent[0]["text"]
    assert "refunded" in body.lower()
    assert "+₦2,000.00" in body
    # The reason is Markdown-escaped (the underscore gets a backslash
    # in front of it for Telegram's Markdown V1 parser).
    assert "transfer\\_failed" in body or "transfer_failed" in body


@pytest.mark.asyncio
async def test_notify_skips_unlinked_user(
    session, bot_with_send_recorder
) -> None:
    """If the user has no linked Telegram, notification is a no-op."""
    u = User(
        email="notif4@x.com",
        hashed_password=hash_password("Secret123"),
        first_name="U",
        last_name="L",
        phone_number="08099999000",
        balance=Decimal("0"),
        telegram_chat_id=None,
        is_telegram_linked=False,
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    txn = Transaction(
        user_id=u.id,
        type=TransactionType.CREDIT.value,
        amount=Decimal("100"),
        fee=Decimal("0"),
        currency="NGN",
        status=TransactionStatus.SUCCESS.value,
        provider="nomba",
        provider_reference="topup_orphan",
    )
    session.add(txn)
    session.commit()

    result = await tg_service.notify_credit(user=u, transaction=txn)
    assert result is False
    assert len(bot_with_send_recorder) == 0


@pytest.mark.asyncio
async def test_notify_swallows_telegram_errors(
    session, monkeypatch
) -> None:
    """If Telegram raises (rate-limit, network), notify returns
    False but doesn't propagate the exception."""
    u = _link_user(session, email="notif5@x.com", chat_id=100005, balance="100")

    fake_app = MagicMock()
    fake_app.bot.send_message = AsyncMock(
        side_effect=Exception("telegram is down")
    )
    original = tg_service._application
    tg_service._application = fake_app
    try:
        txn = Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("100"),
            fee=Decimal("0"),
            currency="NGN",
            status=TransactionStatus.SUCCESS.value,
            provider="nomba",
            provider_reference="topup_err",
        )
        session.add(txn)
        session.commit()
        session.refresh(txn)

        # Should NOT raise
        result = await tg_service.notify_credit(user=u, transaction=txn)
        assert result is False
    finally:
        tg_service._application = original


# ── Done button: no longer a dead end ──────────────────────────────


@pytest.mark.asyncio
async def test_done_balance_button_shows_balance(session) -> None:
    """Tapping 'Check balance' on the Done keyboard shows the
    current wallet balance."""
    from app.handlers.topup_conversation import handle_done_balance
    from telegram.ext import ConversationHandler

    _link_user(session, email="done1@x.com", chat_id=110001, balance="12345.67")

    query = MagicMock()
    query.answer = AsyncMock()
    query.edits = []

    async def _edit(text, **kwargs):
        query.edits.append(text)
    query.edit_message_text = _edit

    update = type("U", (), {"callback_query": query, "effective_chat": type("C", (), {"id": 110001})()})()
    result = await handle_done_balance(update, MagicMock())

    assert result == ConversationHandler.END
    assert len(query.edits) == 1
    body = query.edits[0]
    assert "₦12,345.67" in body
    assert "wallet balance" in body.lower()


@pytest.mark.asyncio
async def test_done_again_button_returns_to_quick_pick() -> None:
    """Tapping 'Top up again' on the Done keyboard goes back to
    the quick-pick keyboard (TOPUP_PICK state)."""
    from app.handlers.topup_conversation import (
        TOPUP_PICK,
        handle_done_again,
    )

    query = MagicMock()
    query.answer = AsyncMock()
    query.edits = []

    async def _edit(text, **kwargs):
        query.edits.append((text, kwargs))
    query.edit_message_text = _edit

    update = type("U", (), {"callback_query": query})()
    result = await handle_done_again(update, MagicMock())

    assert result == TOPUP_PICK
    assert len(query.edits) == 1
    text, kwargs = query.edits[0]
    assert "Top up your wallet" in text
    # The keyboard should be the quick-pick one (not the done one).
    assert kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_done_close_button_just_acks() -> None:
    """Tapping the final 'Done' just acknowledges the user is done."""
    from app.handlers.topup_conversation import handle_done_close
    from telegram.ext import ConversationHandler

    query = MagicMock()
    query.answer = AsyncMock()
    query.edits = []

    async def _edit(text, **kwargs):
        query.edits.append(text)
    query.edit_message_text = _edit

    update = type("U", (), {"callback_query": query})()
    result = await handle_done_close(update, MagicMock())

    assert result == ConversationHandler.END
    assert len(query.edits) == 1
    body = query.edits[0]
    assert "All set" in body or "in progress" in body.lower()


# ── End-to-end: webhook fires notify_credit ───────────────────────


@pytest.mark.asyncio
async def test_charge_success_webhook_fires_credit_notification(
    client, session, stub_provider, bot_with_send_recorder, monkeypatch
) -> None:
    """A `charge.success` webhook should credit the wallet AND
    push a notification to the user's linked Telegram."""
    from app.core.config import settings
    from app.services import payments as _payments_pkg

    u = _link_user(session, email="e2e1@x.com", chat_id=120001, balance="0")
    user_id = u.id
    # Pending transaction (the user has just clicked "Top up").
    with session_scope() as s:
        s.add(
            Transaction(
                user_id=user_id,
                type=TransactionType.CREDIT.value,
                amount=Decimal("7500.00"),
                fee=Decimal("0.00"),
                currency="NGN",
                status=TransactionStatus.PENDING.value,
                provider="nomba",
                provider_reference="topup_1_e2e_aaa",
                narration="Top-up via Nomba Checkout",
            )
        )
        s.commit()

    raw, sig, ts = _signed_nomba_body({
        "event_type": "payment_success",
        "requestId": "evt_e2e_credit_1",
        "data": {
            "merchant": {"userId": "stub", "walletId": "stub"},
            "transaction": {
                "transactionId": "topup_1_e2e_aaa",
                "merchantTxRef": "topup_1_e2e_aaa",
                "type": "TOPUP",
                "time": "1700000000000",
                "responseCode": "00",
                "transactionAmount": 7500.00,
            },
        },
    })

    response = client.post(
        "/webhooks/nomba",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "nomba-signature": sig,
            "nomba-timestamp": ts,
        },
    )
    assert response.status_code == 200
    assert response.json()["received"] is True

    # The user was credited.
    with session_scope() as verify:
        refreshed = verify.get(User, user_id)
        assert refreshed is not None
        assert refreshed.balance == Decimal("7500.00")

    # And the notification was sent.
    sent = bot_with_send_recorder
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "Top-up successful" in body
    assert "+₦7,500.00" in body
    assert "₦7,500.00" in body  # new balance


# ── End-to-end: transfer success fires notify_debit ───────────────


@pytest.mark.asyncio
async def test_transfer_success_webhook_fires_debit_notification(
    client, session, stub_provider, bot_with_send_recorder
) -> None:
    """A `transfer.success` webhook for a bill payment should mark
    the bill paid AND push a debit notification."""
    from app.core.config import settings

    u = _link_user(session, email="e2e2@x.com", chat_id=120002, balance="10000")
    user_id = u.id
    with session_scope() as s:
        bill = Bill(
            user_id=user_id,
            vendor_name="DSTV",
            amount=Decimal("2000"),
            due_date=__import__("datetime").datetime.now(),
            account_number="0123456789",
            bank_code="058",
            status=BillStatus.PROCESSING.value,
        )
        s.add(bill)
        s.flush()
        bill_id = bill.id
        s.add(
            Transaction(
                user_id=user_id,
                type=TransactionType.DEBIT.value,
                amount=Decimal("2000"),
                fee=Decimal("50"),
                currency="NGN",
                status=TransactionStatus.PROCESSING.value,
                provider="nomba",
                provider_reference="autopay_e2e_debit_1",
                narration="DSTV",
                bill_id=bill_id,
            )
        )
        s.commit()

    raw, sig, ts = _signed_nomba_body({
        "event_type": "payout_success",
        "requestId": "evt_e2e_debit_1",
        "data": {
            "merchant": {"userId": "stub", "walletId": "stub"},
            "transaction": {
                "transactionId": "autopay_e2e_debit_1",
                "merchantTxRef": "autopay_e2e_debit_1",
                "type": "PAYOUT",
                "time": "1700000000000",
                "responseCode": "00",
                "transactionAmount": 2000.00,
            },
        },
    })

    response = client.post(
        "/webhooks/nomba",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "nomba-signature": sig,
            "nomba-timestamp": ts,
        },
    )
    assert response.status_code == 200

    # Bill marked paid.
    with session_scope() as verify:
        refreshed_bill = verify.get(Bill, bill_id)
        assert refreshed_bill is not None
        assert refreshed_bill.status == BillStatus.PAID.value

    # Notification fired.
    sent = bot_with_send_recorder
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "Bill paid" in body
    assert "−₦2,000.00" in body
    assert "DSTV" in body


# ── End-to-end: transfer failed fires notify_refund ──────────────


@pytest.mark.asyncio
async def test_transfer_failed_webhook_fires_refund_notification(
    client, session, stub_provider, bot_with_send_recorder
) -> None:
    """A `transfer.failed` webhook for a bill payment should mark
    the txn failed + refund the user AND push a refund notification."""
    from datetime import datetime
    from app.core.config import settings

    u = _link_user(session, email="e2e3@x.com", chat_id=120003, balance="0")
    user_id = u.id
    # Initial wallet: 0; the bill pay took the user to -2050 then
    # refund brought them back. We just verify balance ends at 0.
    with session_scope() as s:
        bill = Bill(
            user_id=user_id,
            vendor_name="DSTV",
            amount=Decimal("2000"),
            due_date=datetime.now(),
            account_number="0123456789",
            bank_code="058",
            status=BillStatus.PROCESSING.value,
        )
        s.add(bill)
        s.flush()
        bill_id = bill.id
        # The successful debit transaction we expect to be marked failed.
        s.add(
            Transaction(
                user_id=user_id,
                type=TransactionType.DEBIT.value,
                amount=Decimal("2000"),
                fee=Decimal("50"),
                currency="NGN",
                status=TransactionStatus.PROCESSING.value,
                provider="nomba",
                provider_reference="autopay_e2e_refund_1",
                narration="DSTV",
                bill_id=bill_id,
            )
        )
        s.commit()

    raw, sig, ts = _signed_nomba_body({
        "event_type": "payout_failed",
        "requestId": "evt_e2e_refund_1",
        "data": {
            "merchant": {"userId": "stub", "walletId": "stub"},
            "transaction": {
                "transactionId": "autopay_e2e_refund_1",
                "merchantTxRef": "autopay_e2e_refund_1",
                "type": "PAYOUT",
                "time": "1700000000000",
                "responseCode": "99",
                "transactionAmount": 2000.00,
            },
        },
    })

    response = client.post(
        "/webhooks/nomba",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "nomba-signature": sig,
            "nomba-timestamp": ts,
        },
    )
    assert response.status_code == 200

    # Txn marked failed; balance was 0 (we never debited in the
    # webhook handler — the test only simulates the provider-side
    # event). Bill bumped retry count.
    with session_scope() as verify:
        from app.models.transaction import Transaction as _Txn
        from sqlalchemy import select as _sa
        txns = verify.execute(
            _sa(_Txn).where(_Txn.provider_reference == "autopay_e2e_refund_1")
        ).scalars().all()
        assert any(t.status == TransactionStatus.FAILED.value for t in txns)

    # Refund notification fired.
    sent = bot_with_send_recorder
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "refunded" in body.lower()
