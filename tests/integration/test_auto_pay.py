"""Integration tests for the auto-pay scheduler path.

A `scheduled` bill whose `due_date` has arrived should be:
  * claimed by `SELECT FOR UPDATE SKIP LOCKED`
  * re-evaluated via the decision agent
  * if `pay_now`, auto-executed via `execute_payout` (no user action)
  * audited with `actor='scheduler'`
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.database import session_scope
from app.core.scheduler import _process_scheduled_bills
from app.core.security import hash_password
from app.models.audit_log import AuditLog
from app.models.bill import Bill
from app.models.enums import (
    AuditActor,
    AuditEventType,
    BillStatus,
    TransactionStatus,
    TransactionType,
)
from app.models.transaction import Transaction
from app.models.user import User


def _make_user_with_balance(session: Session, *, email: str, balance: str) -> User:
    u = User(
        email=email,
        hashed_password=hash_password("Secret123"),
        first_name="A",
        last_name="P",
        phone_number="08099999" + email[:4].rjust(4, "0"),
        balance=Decimal(balance),
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def test_scheduled_bill_auto_pays_when_due(
    session: Session, stub_provider
) -> None:
    """A scheduled, due, fully-funded bill should be auto-paid by
    the scheduler and end up in `processing` (or `paid` if a
    transfer.success webhook has fired). The stub provider's
    `initiate_transfer` returns `status='pending'`, so we expect the
    bill to land in `processing`."""
    user = _make_user_with_balance(session, email="auto1@x.com", balance="100000")
    user_id = user.id
    bill = Bill(
        user_id=user_id,
        vendor_name="DSTV",
        amount=Decimal("5000"),
        due_date=datetime.now() - timedelta(days=1),  # overdue
        account_number="0123456789",
        bank_code="058",
        status=BillStatus.SCHEDULED.value,
    )
    session.add(bill)
    session.commit()
    bill_id = bill.id

    _process_scheduled_bills()

    with session_scope() as verify:
        b = verify.get(Bill, bill_id)
        assert b.status == BillStatus.PROCESSING.value, (
            f"expected processing, got {b.status}"
        )
        # Audit row written with the SCHEDULER actor
        audits = verify.exec(
            select(AuditLog).where(
                AuditLog.entity_id == bill_id,
                AuditLog.event_type == AuditEventType.PAYOUT_ATTEMPTED.value,
            )
        ).all()
        assert any(a.actor == AuditActor.SCHEDULER.value for a in audits), (
            f"expected a SCHEDULER-actor audit row, got {[a.actor for a in audits]}"
        )


def test_scheduled_bill_stays_scheduled_on_insufficient_balance(
    session: Session, stub_provider
) -> None:
    """Bill is due, scheduled, but user has no balance — the
    scheduler should leave it in `scheduled` and audit a hold."""
    user = _make_user_with_balance(session, email="auto2@x.com", balance="0")
    user_id = user.id
    bill = Bill(
        user_id=user_id,
        vendor_name="DSTV",
        amount=Decimal("5000"),
        due_date=datetime.now() - timedelta(days=1),
        account_number="0123456789",
        bank_code="058",
        status=BillStatus.SCHEDULED.value,
    )
    session.add(bill)
    session.commit()
    bill_id = bill.id

    _process_scheduled_bills()

    with session_scope() as verify:
        b = verify.get(Bill, bill_id)
        # Bill stays in `scheduled` for the next run
        assert b.status == BillStatus.SCHEDULED.value


def test_scheduled_bill_skipped_when_already_paid(
    session: Session, stub_provider
) -> None:
    """A `paid` bill should never be picked up by the scheduler."""
    user = _make_user_with_balance(session, email="auto3@x.com", balance="0")
    user_id = user.id
    bill = Bill(
        user_id=user_id,
        vendor_name="DSTV",
        amount=Decimal("5000"),
        due_date=datetime.now() - timedelta(days=1),
        account_number="0123456789",
        bank_code="058",
        status=BillStatus.PAID.value,  # already paid
    )
    session.add(bill)
    session.commit()
    bill_id = bill.id

    _process_scheduled_bills()  # should be a no-op

    with session_scope() as verify:
        b = verify.get(Bill, bill_id)
        assert b.status == BillStatus.PAID.value  # unchanged


def test_scheduler_no_due_bills_is_noop(
    session: Session, stub_provider
) -> None:
    """A scheduler run with no due bills is a fast no-op (no errors,
    no audit rows, no balance changes)."""
    user = _make_user_with_balance(session, email="auto4@x.com", balance="0")
    user_id = user.id
    # A scheduled bill that's NOT due yet
    bill = Bill(
        user_id=user_id,
        vendor_name="Future",
        amount=Decimal("1000"),
        due_date=datetime.now() + timedelta(days=30),  # 30 days out
        account_number="0123456789",
        bank_code="058",
        status=BillStatus.SCHEDULED.value,
    )
    session.add(bill)
    session.commit()

    _process_scheduled_bills()  # no-op

    with session_scope() as verify:
        b = verify.get(Bill, bill.id)
        assert b.status == BillStatus.SCHEDULED.value
        # No payout-attempted audit rows
        audits = verify.exec(
            select(AuditLog).where(
                AuditLog.event_type == AuditEventType.PAYOUT_ATTEMPTED.value
            )
        ).all()
        assert audits == []


def test_scheduler_writes_scheduler_actor_audit(
    session: Session, stub_provider
) -> None:
    """The audit trail must record the bill as scheduler-initiated
    when auto-pay fires."""
    user = _make_user_with_balance(session, email="auto5@x.com", balance="100000")
    user_id = user.id
    bill = Bill(
        user_id=user_id,
        vendor_name="Vendor X",
        amount=Decimal("3000"),
        due_date=datetime.now() - timedelta(days=1),
        account_number="0123456789",
        bank_code="058",
        status=BillStatus.SCHEDULED.value,
    )
    session.add(bill)
    session.commit()
    bill_id = bill.id

    _process_scheduled_bills()

    with session_scope() as verify:
        audits = verify.exec(
            select(AuditLog).where(AuditLog.entity_id == bill_id)
        ).all()
        # The bill's audit trail should have at least one row with
        # actor='scheduler'
        scheduler_audits = [a for a in audits if a.actor == AuditActor.SCHEDULER.value]
        assert len(scheduler_audits) >= 1, (
            f"no SCHEDULER-actor audit row, found: {[(a.actor, a.event_type) for a in audits]}"
        )
