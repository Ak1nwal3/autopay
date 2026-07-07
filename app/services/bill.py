"""Bill lifecycle service.

Centralizes the create / cancel / list operations on `Bill` rows
so the FastAPI endpoints, the Telegram bot's `/schedule` command,
and any future channel (CLI, scheduler hook) all call the same
code. Two reasons this matters:

  1. **Audit + validation in one place.** A bill must always be
     accompanied by an audit row (`BILL_CREATED`) and the same
     amount bounds / status rules. Inline duplication in two
     routes would drift.

  2. **Recurrence semantics.** Recurring bills are tricky:
     `is_recurring=True` + `recurrence_interval` set +
     `next_recurrence_date` set up-front so the scheduler picks
     them up. The bot, the API, and the scheduler all need to
     agree on what those fields mean.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlmodel import Session

from app.models.bill import Bill
from app.models.enums import BillStatus
from app.services.audit import audit_bill_created

# Cap on a single scheduled bill. Anything larger needs an
# admin-issued manual top-up. Matches the rest of the app's
# sanity checks.
MAX_BILL_NGN: Decimal = Decimal("10_000_000.00")
MIN_BILL_NGN: Decimal = Decimal("100.00")


class BillValidationError(ValueError):
    """Raised when a bill is missing required fields, has an
    out-of-range amount, or otherwise can't be created."""


@dataclass(frozen=True)
class ScheduleBillInput:
    """Validated input for `create_scheduled_bill`. Constructed by
    the caller (API or bot) after parsing user input. Keeps the
    service signature stable even as we add fields."""

    vendor_name: str
    amount: Decimal
    due_date: datetime
    account_number: str | None = None
    bank_code: str | None = None
    bank_name: str | None = None
    recurrence_interval: str | None = None  # "weekly" | "monthly" | None


def create_scheduled_bill(
    session: Session,
    *,
    user_id: int,
    payload: ScheduleBillInput,
) -> Bill:
    """Create a scheduled (future-dated) bill, with optional
    recurrence. The bill lands in `BillStatus.SCHEDULED` so the
    scheduler picks it up on or after `due_date`. If
    `recurrence_interval` is set, `is_recurring=True` and
    `next_recurrence_date=due_date` — after each successful
    payout, the scheduler's `process_recurring_bills` spawns the
    next occurrence and bumps the original's `next_recurrence_date`
    forward so the same row isn't processed twice.

    Raises:
        BillValidationError: amount out of range, vendor name
            empty, due_date in the past, or recurrence_interval
            not in {"weekly", "monthly"}.
    """
    # ── Validation ────────────────────────────────────────────────
    vendor = (payload.vendor_name or "").strip()
    if not vendor:
        raise BillValidationError("Vendor name is required.")
    if len(vendor) > 255:
        raise BillValidationError(
            "Vendor name is too long (max 255 chars)."
        )

    amount = Decimal(str(payload.amount))
    if amount < MIN_BILL_NGN or amount > MAX_BILL_NGN:
        raise BillValidationError(
            f"Amount must be between ₦{int(MIN_BILL_NGN):,} "
            f"and ₦{int(MAX_BILL_NGN):,}."
        )

    if payload.due_date is None:
        raise BillValidationError("Due date is required.")

    # Allow dates up to 1 minute in the past (clock skew tolerance
    # for chat-server timing). Anything older is a real bug.
    now = datetime.now()
    if payload.due_date < now - timedelta(minutes=1):
        raise BillValidationError(
            "Due date is in the past. Pick a future date."
        )

    if payload.recurrence_interval not in (None, "weekly", "monthly"):
        raise BillValidationError(
            "Recurrence must be 'weekly', 'monthly', or unset."
        )

    # Recurring bills need a payout account — the scheduler can
    # only auto-pay if it knows where to send the money.
    if payload.recurrence_interval and (
        not payload.account_number or not payload.bank_code
    ):
        raise BillValidationError(
            "Recurring bills need an account number and bank code "
            "so the scheduler can auto-pay them."
        )

    # ── Persist ───────────────────────────────────────────────────
    is_recurring = payload.recurrence_interval is not None
    bill = Bill(
        user_id=user_id,
        vendor_name=vendor,
        amount=amount,
        currency="NGN",
        due_date=payload.due_date,
        account_number=(payload.account_number or None),
        bank_code=(payload.bank_code or None),
        bank_name=(payload.bank_name or None),
        status=BillStatus.SCHEDULED.value,
        is_recurring=is_recurring,
        recurrence_interval=payload.recurrence_interval,
        # For recurring bills, prime `next_recurrence_date` to
        # `due_date` so the scheduler picks up the *first*
        # occurrence on/after the due date. The actual
        # process_recurring_bills job in the scheduler looks at
        # `next_recurrence_date <= now` (and `is_recurring=True`)
        # — setting it to `due_date` means the scheduler won't
        # touch the row until the due date arrives. The
        # process_scheduled_bills job uses `due_date <= now`
        # directly to fire the *first* payment.
        next_recurrence_date=payload.due_date if is_recurring else None,
    )
    session.add(bill)
    session.flush()
    audit_bill_created(
        session,
        user_id=user_id,
        bill_id=bill.id,
        amount=float(bill.amount),
        provider="paystack",
    )
    session.commit()
    session.refresh(bill)
    # Telegram /schedule is the current caller; future API endpoints
    # that create scheduled bills will also be counted here.
    from app.core.metrics import record_bill_created

    record_bill_created(trigger="telegram")
    return bill


__all__ = [
    "MAX_BILL_NGN",
    "MIN_BILL_NGN",
    "BillValidationError",
    "ScheduleBillInput",
    "create_scheduled_bill",
]
