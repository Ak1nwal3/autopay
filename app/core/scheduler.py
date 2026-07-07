"""APScheduler integration.

Runs in the same process as FastAPI (single worker by design — see
Dockerfile). We schedule two jobs:

  1. `process_scheduled_bills` — every minute, picks up bills with
     `status='scheduled'` and `due_date <= now`, runs the decision
     agent again, and on `pay_now` directly calls
     `execute_payout(...)`. Multi-worker safety via
     `SELECT ... FOR UPDATE SKIP LOCKED`.

  2. `process_recurring_bills` — every 6 hours, finds bills with
     `is_recurring=True` whose `next_recurrence_date <= now` and
     spawns a fresh bill for the next period.

Job identifiers are stable so the scheduler can dedup on restart.
The scheduler is a no-op if the database is unreachable at startup
(it logs a warning and continues).

In production we use `AsyncIOScheduler` (needs a running event loop).
In test contexts we fall back to `BackgroundScheduler` so unit tests
don't need a loop.

The async payout call (`execute_payout` is `async def`) needs a
running event loop. We structure the job as a sync wrapper that
calls `asyncio.run()` on a private event loop. This works in both
BackgroundScheduler (job runs on a thread) and AsyncIOScheduler
(job runs in a separate thread of the AsyncIO loop) contexts.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import HTTPException

from app.core.metrics import (
    SCHEDULER_AUTOPAY_SECONDS,
    record_bill_paid,
    record_payout,
    record_scheduler_job,
)

logger = logging.getLogger(__name__)

_scheduler: Optional[object] = None


# ── Public API ──────────────────────────────────────────────────────

def start_scheduler() -> None:
    """Idempotent: a second call is a no-op.

    Detects whether we're inside a running asyncio event loop (FastAPI
    lifespan) or in a test/script context. AsyncIO needs the loop; in
    tests we use a plain BackgroundScheduler."""
    global _scheduler
    if _scheduler is not None:
        return

    try:
        asyncio.get_running_loop()
        scheduler_cls = AsyncIOScheduler
    except RuntimeError:
        scheduler_cls = BackgroundScheduler

    _scheduler = scheduler_cls(timezone="UTC")
    _scheduler.add_job(
        _run_sync_in_private_loop("process_scheduled_bills", _process_scheduled_bills),
        trigger=IntervalTrigger(minutes=1),
        id="process_scheduled_bills",
        name="Auto-pay due scheduled bills",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=None,  # don't fire immediately on boot
    )
    _scheduler.add_job(
        _run_sync_in_private_loop("process_recurring_bills", _process_recurring_bills),
        trigger=IntervalTrigger(hours=6),
        id="process_recurring_bills",
        name="Spawn next recurrence for recurring bills",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=None,
    )
    _scheduler.add_job(
        _run_sync_in_private_loop("poll_pending_nomba_topups", _poll_pending_nomba_topups),
        trigger=IntervalTrigger(seconds=30),
        id="poll_pending_nomba_topups",
        name="Poll Nomba for settled top-ups (webhook fallback)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=None,  # don't fire immediately on boot
    )
    try:
        _scheduler.start()
        logger.info("Scheduler started: %s", [j.id for j in _scheduler.get_jobs()])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Scheduler failed to start: %s", exc)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Scheduler shutdown error: %s", exc)
    finally:
        _scheduler = None


def get_scheduler() -> Optional[object]:
    return _scheduler


# ── Job runners ─────────────────────────────────────────────────────

def _run_sync_in_private_loop(job_id: str, fn):
    """Wrap an async function as a sync callable that runs it in its
    own event loop. Required because the async payout path
    (`execute_payout`) needs a running loop, but APScheduler's
    `BackgroundScheduler` doesn't provide one.

    For `AsyncIOScheduler`, the job runs in a thread pool inside
    AsyncIO's loop; we still use `asyncio.run()` (creates a sub-loop)
    so we don't have to special-case the two schedulers.
    """
    def wrapper() -> None:
        try:
            asyncio.run(fn())
        except Exception:  # noqa: BLE001
            logger.exception("Scheduler job %s raised", job_id)

    return wrapper


def _claim_due_scheduled_bill_ids(session) -> list[int]:
    """Atomically claim all due scheduled bills for processing.

    Uses `SELECT ... FOR UPDATE SKIP LOCKED` so multiple workers
    (or two scheduler runs racing) each pick a disjoint subset. The
    row-level lock is held until the transaction commits, so a
    concurrent worker calling the same query simply skips our claimed
    rows and grabs the next batch.

    Returns a list of `Bill.id` values; the caller is responsible
    for acting on them within the same transaction (or a fresh one).
    """
    from datetime import datetime
    from sqlalchemy import select as _sa_select

    from app.models.bill import Bill
    from app.models.enums import BillStatus

    now = datetime.now()
    # `.with_for_update(skip_locked=True)` is the magic — Postgres
    # skips rows another tx has already locked.
    claimed = session.execute(
        _sa_select(Bill.id)
        .where(
            Bill.status == BillStatus.SCHEDULED.value,
            Bill.due_date <= now,
        )
        .order_by(Bill.due_date.asc())
        .with_for_update(skip_locked=True)
    ).scalars().all()
    return list(claimed)


def _process_scheduled_bills() -> None:
    """Auto-pay scheduled bills whose due date has arrived.

    The flow per bill:
      1. Claim the bill via `SELECT ... FOR UPDATE SKIP LOCKED`
         (multi-worker safety; the lock holds until the tx commits).
      2. Re-evaluate the decision agent. If `pay_now`, call
         `execute_payout` directly. If `hold` (insufficient balance)
         or `schedule` (still too far out), leave the bill in
         `scheduled` for the next run.
      3. Audit every step with `actor=AuditActor.SCHEDULER`.

    Errors per bill are caught and logged so one bad bill doesn't
    poison the loop.
    """
    import logging

    from app.agents.graphs import run_agent
    from app.agents.state import Decision
    from app.core.config import settings
    from app.core.database import session_scope
    from app.models.bill import Bill
    from app.models.enums import (
        AuditActor,
        AuditEntityType,
        AuditEventType,
        BillStatus,
    )
    from app.models.user import User
    from app.services.audit import write_audit
    from app.services.payments import (
        PaymentError,
        PaymentProvider,
        get_payment_provider,
    )
    from app.services.payout import execute_payout

    logger.info("scheduler.process_scheduled_bills.start")

    # Phase 1: claim all due bill ids in a single short transaction.
    with session_scope() as session:
        claimed_ids = _claim_due_scheduled_bill_ids(session)
        # committing releases the row locks, but the *act of claiming*
        # is recorded in our own audit log below.
    if not claimed_ids:
        logger.info("scheduler.process_scheduled_bills.no_due_bills")
        record_scheduler_job(job_id="process_scheduled_bills", result="skip")
        return

    # Phase 2: act on each claimed bill in its own transaction.
    # Each bill gets its own session so a failure in one doesn't
    # block the next.
    for bill_id in claimed_ids:
        try:
            _autopay_one_bill(bill_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "scheduler.process_scheduled_bills.error bill_id=%d err=%s",
                bill_id, exc,
            )
            record_scheduler_job(job_id="process_scheduled_bills", result="fail")


def _autopay_one_bill(bill_id: int) -> None:
    """Re-evaluate a single due bill and (if pay_now) execute the
    payout. Called from the scheduler loop; errors are caught by the
    caller so this never crashes the loop.
    """
    from datetime import datetime
    from decimal import Decimal

    from app.agents.graphs import run_agent
    from app.agents.state import Decision
    from app.core.config import settings
    from app.core.database import session_scope
    from app.models.bill import Bill
    from app.models.enums import AuditActor, AuditEntityType, AuditEventType, BillStatus
    from app.models.user import User
    from app.services.audit import write_audit
    from app.services.payments import (
        PaymentError,
        PaymentProvider,
        get_payment_provider,
    )
    from app.services.payout import execute_payout

    with session_scope() as session:
        db_bill = session.get(Bill, bill_id)
        if db_bill is None or db_bill.status != BillStatus.SCHEDULED.value:
            # Already moved on (race with another worker / manual
            # action). Skip silently.
            return
        user = session.get(User, db_bill.user_id)
        if user is None:
            return

        # Re-evaluate the agent
        now = datetime.now()
        due_date = db_bill.due_date
        if due_date.tzinfo is not None:
            due_date = due_date.replace(tzinfo=None)
        days_until_due = (due_date - now).days
        decision = run_agent(
            user_balance=Decimal(str(user.balance)),
            bill_amount=Decimal(str(db_bill.amount)),
            fee=Decimal(str(settings.payout_fee_ngn)),
            days_until_due=days_until_due,
        )

        if decision.decision != Decision.PAY_NOW:
            # HOLD (insufficient balance) or SCHEDULE (still too far).
            # Leave the bill in `scheduled`; next run will re-check.
            logger.info(
                "scheduler.autopay.skip bill_id=%d reason=%s balance=%s",
                bill_id, decision.reason, user.balance,
            )
            return

        # PAY_NOW. We need a real provider to execute the payout.
        # The execute_payout function is async, but the scheduler
        # job is sync. We run a private event loop in this thread
        # via asyncio.run() — the loop is short-lived and per-bill.
        logger.info(
            "scheduler.autopay.start bill_id=%d amount=%s",
            bill_id, db_bill.amount,
        )

    # The above transaction is closed. The execute_payout call has
    # its own transaction (it does its own SELECT FOR UPDATE).
    # We can't share the session because the provider call is async
    # and blocks on a network call.
    try:
        with SCHEDULER_AUTOPAY_SECONDS.time():
            asyncio.run(_async_autopay(bill_id))
        logger.info("scheduler.autopay.success bill_id=%d", bill_id)
        record_bill_paid(trigger="auto_pay")
        record_payout(result="success")
        record_scheduler_job(job_id="process_scheduled_bills", result="success")
    except HTTPException as exc:
        # 402 (insufficient balance) is expected and recoverable —
        # leave the bill in `scheduled` for the next run.
        if exc.status_code == 402:
            logger.warning(
                "scheduler.autopay.hold bill_id=%d reason=%s",
                bill_id, exc.detail,
            )
            record_payout(result="insufficient")
            record_scheduler_job(job_id="process_scheduled_bills", result="skip")
            with session_scope() as session:
                db_bill = session.get(Bill, bill_id)
                if db_bill and db_bill.status == BillStatus.SCHEDULED.value:
                    write_audit(
                        session,
                        actor=AuditActor.SCHEDULER,
                        event_type=AuditEventType.PAYOUT_FAILED,
                        user_id=db_bill.user_id,
                        entity_type=AuditEntityType.BILL,
                        entity_id=db_bill.id,
                        metadata={
                            "trigger": "scheduled_autopay",
                            "reason": "insufficient_balance",
                            "detail": str(exc.detail),
                        },
                    )
                    session.commit()
        else:
            # 404 (bill gone) / 409 (already paid/cancelled) — race,
            # not a real error. Don't escalate.
            logger.warning(
                "scheduler.autopay.skipped bill_id=%d status=%d detail=%s",
                bill_id, exc.status_code, exc.detail,
            )
            record_payout(result="race")
            record_scheduler_job(job_id="process_scheduled_bills", result="skip")
    except PaymentError as exc:
        # Provider-side failure (network blip, KYC, etc.). The
        # payout service should have already refunded the user.
        # Leave the bill in `scheduled` for the next retry.
        logger.warning(
            "scheduler.autopay.provider_error bill_id=%d err=%s",
            bill_id, exc,
        )
        record_payout(result="provider_error")
        record_scheduler_job(job_id="process_scheduled_bills", result="skip")
    except Exception as exc:  # noqa: BLE001
        # Anything else is a real bug. Log and continue.
        logger.exception(
            "scheduler.autopay.unexpected bill_id=%d err=%s",
            bill_id, exc,
        )
        record_scheduler_job(job_id="process_scheduled_bills", result="fail")


async def _async_autopay(bill_id: int) -> None:
    """Async helper: get a provider, open a session, call execute_payout.

    Commits the session on success so the bill-status update + debit
    Transaction persist. Rolls back on any error so the bill stays
    in `scheduled` and we retry next minute.

    Passes `actor=AuditActor.SCHEDULER` so the audit trail records
    the bill as scheduler-initiated, not user-initiated.
    """
    from app.core.database import session_scope
    from app.models.enums import AuditActor
    from app.services import payments as _payments_module
    from app.services.payout import execute_payout
    from app.services.payments import PaymentProvider

    # Import the function (binds locally) but call the patched
    # version from the package module — that's the one tests
    # monkeypatch. If we did `from ... import get_payment_provider`
    # and called it directly, the patch wouldn't apply.
    provider: PaymentProvider = _payments_module.get_payment_provider()
    with session_scope() as session:
        try:
            await execute_payout(
                session,
                bill_id=bill_id,
                provider=provider,
                actor=AuditActor.SCHEDULER,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise


def _process_recurring_bills() -> None:
    """Spawn the next occurrence of every recurring bill whose
    `next_recurrence_date` is in the past."""
    from datetime import datetime, timedelta
    from sqlalchemy import select

    from app.core.database import session_scope
    from app.models.bill import Bill
    from app.models.enums import AuditActor, AuditEntityType, AuditEventType, BillStatus
    from app.services.audit import write_audit
    from app.services.payout import schedule_recurrence

    now = datetime.now()
    with session_scope() as session:
        from sqlalchemy import select as _sa_select
        recurring_bills = session.execute(
            _sa_select(Bill).where(
                Bill.is_recurring == True,  # noqa: E712
                Bill.next_recurrence_date != None,  # noqa: E711
                Bill.next_recurrence_date <= now,
            )
        ).scalars().all()
        recurring_ids = [b.id for b in recurring_bills]

    for original_id in recurring_ids:
        try:
            with session_scope() as session:
                db_bill = session.get(Bill, original_id)
                if db_bill is None:
                    continue
                next_bill = schedule_recurrence(session, bill=db_bill)
                if next_bill is not None:
                    write_audit(
                        session,
                        actor=AuditActor.SCHEDULER,
                        event_type=AuditEventType.BILL_RECURRENCE_CREATED,
                        user_id=db_bill.user_id,
                        entity_type=AuditEntityType.BILL,
                        entity_id=next_bill.id or 0,
                        metadata={
                            "parent_bill_id": db_bill.id,
                            "next_bill_id": next_bill.id,
                        },
                    )
                    session.commit()
            logger.info("Scheduler: spawned next recurrence for bill %d", original_id)
        except Exception:  # noqa: BLE001
            logger.exception("Scheduler: error recurring bill %d", original_id)


# ── Pending top-up poll (Nomba webhook fallback) ────────────────────


async def _async_poll_pending_nomba_topups() -> None:
    """Async body of the poll job. Runs in the scheduler's private
    event loop (see `_run_sync_in_private_loop` above).

    For every pending CREDIT transaction whose provider is Nomba,
    call `provider.get_transaction(reference=...)`. If the
    provider reports `status='success'`, call the shared
    `verify_pending_topup` function which acquires the
    `SELECT ... FOR UPDATE` row lock and applies the credit.
    """
    from app.core.database import session_scope
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from app.services.payments import get_payment_provider
    from app.services.wallet import verify_pending_topup
    from sqlmodel import select

    with session_scope() as session:
        # Claim a small batch of pending top-ups with SKIP LOCKED so
        # we don't block on rows another worker is processing.
        # We pull BOTH `provider_reference` (our internal ID) and
        # `provider_order_reference` (the provider's ID, used as the
        # query param for Nomba's transaction-status API). Either
        # may be NULL for legacy transactions; `verify_pending_topup`
        # falls back to `provider_reference` if needed.
        pending_refs: list[tuple[int, str, int, str | None]] = session.execute(
            select(
                Transaction.id,
                Transaction.provider_reference,
                Transaction.user_id,
                Transaction.provider_order_reference,
            )
            .where(
                Transaction.status == TransactionStatus.PENDING.value,
                Transaction.type == TransactionType.CREDIT.value,
                Transaction.provider == "nomba",
            )
            .order_by(Transaction.created_at.asc())
            .limit(20)
            .with_for_update(skip_locked=True)
        ).all()
        # `with_for_update(skip_locked=True)` requires a transaction
        # to hold the lock; commit immediately so we don't hold the
        # locks for the duration of the network calls. We've already
        # picked a disjoint batch; the inner `verify_pending_topup`
        # re-locks per-row for the credit write.
        session.commit()

    if not pending_refs:
        return

    # Resolve the provider once; the OAuth token is cached.
    from app.services.payments import get_payment_provider as _gpp

    for txn_id, ref, user_id, provider_order_ref in pending_refs:
        # Prefer the provider's reference for the verify call (so
        # Nomba's API can find the transaction by `orderReference`).
        # Fall back to our internal reference for legacy rows.
        ref_for_provider = provider_order_ref or ref
        try:
            with session_scope() as session:
                txn = session.get(Transaction, txn_id)
                if txn is None or txn.status != TransactionStatus.PENDING.value:
                    continue
                from app.models.user import User as _User
                user = session.get(_User, user_id)
                if user is None:
                    continue
                provider = _gpp()
                await verify_pending_topup(
                    session, user=user, reference=ref_for_provider, provider=provider,
                )
        except Exception:  # noqa: BLE001
            # Don't let one bad row kill the whole job — log and
            # move on. Next tick will retry.
            logger.exception(
                "poll_pending_nomba_topups: error processing txn %d ref %s",
                txn_id, ref,
            )
            record_scheduler_job(job_id="poll_pending_nomba_topups", result="fail")


def _poll_pending_nomba_topups() -> None:
    """Sync wrapper for the scheduler. Delegates to the async body
    via `asyncio.run` (consistent with the other job wrappers)."""
    try:
        asyncio.run(_async_poll_pending_nomba_topups())
        record_scheduler_job(job_id="poll_pending_nomba_topups", result="success")
    except Exception:  # noqa: BLE001
        logger.exception("poll_pending_nomba_topups raised")
        record_scheduler_job(job_id="poll_pending_nomba_topups", result="fail")
