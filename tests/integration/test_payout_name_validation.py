"""Integration tests for the account-name-mismatch guard.

These tests exercise the production hard-block in `execute_payout`
that fires when the bank's resolved account name doesn't fuzzy-match
the bill's vendor. They cover:
  1. Hard mismatch → 422 + refund + audit row + bill stays non-paid.
  2. Close match (suffix variation) → transfer proceeds.
  3. No vendor on the bill → resolve still runs but mismatch is a
     no-op (we don't have anything to compare against).
  4. Stub's resolve_account is actually called.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.database import session_scope
from app.core.security import hash_password
from app.models.audit_log import AuditLog
from app.models.bill import Bill
from app.models.enums import (
    AuditActor,
    BillStatus,
    TransactionStatus,
    TransactionType,
)
from app.models.transaction import Transaction
from app.models.user import User
from app.services.payout import execute_payout


def _make_user_with_balance(session: Session, *, email: str, balance: str) -> User:
    u = User(
        email=email,
        hashed_password=hash_password("Secret123"),
        first_name="N",
        last_name="M",
        phone_number="0809" + email[:8].rjust(8, "0"),
        balance=Decimal(balance),
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _make_scheduled_bill(
    session: Session, *, user_id: int, vendor: str, amount: str = "5000"
) -> int:
    bill = Bill(
        user_id=user_id,
        vendor_name=vendor,
        amount=Decimal(amount),
        currency="NGN",
        due_date=datetime.now(),
        account_number="0123456789",
        bank_code="058",
        status=BillStatus.SCHEDULED.value,
    )
    session.add(bill)
    session.commit()
    session.refresh(bill)
    return bill.id


# ── 1. Hard mismatch: 422 + refund + audit ────────────────────────


def test_payout_blocks_on_hard_name_mismatch(
    session: Session, stub_provider
) -> None:
    """A bill for 'GOTV' resolving to 'DSTV NG LTD' is a real
    difference (different companies). Payout must 422."""
    user = _make_user_with_balance(session, email="mismatch@x.com", balance="100000")
    user_id = user.id
    bill_id = _make_scheduled_bill(
        session, user_id=user_id, vendor="GOTV", amount="5000"
    )

    async def _run() -> None:
        with session_scope() as s:
            await execute_payout(
                s,
                bill_id=bill_id,
                provider=stub_provider,
                actor=AuditActor.SCHEDULER,
            )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_run())

    # The exception should be 422
    assert exc_info.value.status_code == 422
    assert "mismatch" in exc_info.value.detail.lower()

    # The audit row should have been persisted (committed before raise).
    with session_scope() as verify:
        audits = verify.exec(
            select(AuditLog).where(AuditLog.entity_id == bill_id)
        ).all()
        assert any(
            a.event_type == "payout.failed" for a in audits
        ), f"expected payout.failed audit, got {[(a.event_type, a.actor) for a in audits]}"
        # The user balance is unchanged (no debit happened — the
        # transfer recipient was never created).
        refreshed = verify.get(User, user_id)
        assert refreshed is not None
        assert refreshed.balance == Decimal("100000")
        # The bill should be marked scheduled again (retryable).
        refreshed_bill = verify.get(Bill, bill_id)
        assert refreshed_bill is not None
        assert refreshed_bill.status in (
            BillStatus.SCHEDULED.value,
            BillStatus.FAILED.value,
        )


# ── 2. Close match: suffix variation is accepted ──────────────────


def test_payout_allows_close_name_match(
    session: Session, stub_provider
) -> None:
    """A bill for 'DSTV Nigeria Ltd' resolving to 'DSTV NG LTD' is
    a real-world bank-name variation. Payout must proceed."""
    user = _make_user_with_balance(session, email="closematch@x.com", balance="100000")
    user_id = user.id
    bill_id = _make_scheduled_bill(
        session, user_id=user_id, vendor="DSTV Nigeria Ltd", amount="5000"
    )

    async def _run():
        with session_scope() as s:
            result = await execute_payout(
                s,
                bill_id=bill_id,
                provider=stub_provider,
                actor=AuditActor.SCHEDULER,
            )
            s.commit()
        return result

    result = asyncio.run(_run())
    assert result.success is True
    # The wallet should have been debited.
    with session_scope() as verify:
        refreshed = verify.get(User, user_id)
        assert refreshed is not None
        assert refreshed.balance < Decimal("100000")


# ── 3. Empty vendor: no comparison possible → transfer proceeds ──


def test_payout_skips_mismatch_check_when_vendor_blank(
    session: Session, stub_provider
) -> None:
    """If the bill has no vendor name (rare but possible for photo
    uploads that fail to extract a vendor), we don't have anything
    to compare against. The resolve still runs (so we know the
    account is real), but no mismatch is reported."""
    user = _make_user_with_balance(session, email="blankvendor@x.com", balance="100000")
    user_id = user.id
    bill_id = _make_scheduled_bill(
        session, user_id=user_id, vendor="", amount="5000"
    )

    async def _run():
        with session_scope() as s:
            result = await execute_payout(
                s, bill_id=bill_id, provider=stub_provider, actor=AuditActor.USER
            )
            s.commit()
        return result

    result = asyncio.run(_run())
    assert result.success is True
    # Stub's resolve_account was called.
    assert any(c[0] == "resolve_account" for c in stub_provider.calls)


# ── 4. The stub's resolve_account is exercised ────────────────────


def test_payout_calls_resolve_account(
    session: Session, stub_provider
) -> None:
    user = _make_user_with_balance(session, email="resolve@x.com", balance="100000")
    user_id = user.id
    bill_id = _make_scheduled_bill(
        session, user_id=user_id, vendor="GOTV", amount="1000"
    )

    async def _run():
        with session_scope() as s:
            try:
                await execute_payout(
                    s,
                    bill_id=bill_id,
                    provider=stub_provider,
                    actor=AuditActor.SCHEDULER,
                )
                s.commit()
            except HTTPException:
                s.rollback()
                raise

    with pytest.raises(HTTPException):
        asyncio.run(_run())

    resolve_calls = [c for c in stub_provider.calls if c[0] == "resolve_account"]
    assert len(resolve_calls) == 1
    # And the right bank/account was looked up.
    kwargs = resolve_calls[0][1]
    assert kwargs["account_number"] == "0123456789"
    assert kwargs["bank_code"] == "058"
