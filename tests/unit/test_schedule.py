"""Tests for the /schedule conversation + the
create_scheduled_bill service.

We exercise the conversation at two levels:

  * **Service level** (`create_scheduled_bill` directly) — fast
    unit tests that pin the validation rules (amount bounds,
    past-date rejection, recurring-needs-account, etc.) without
    touching Telegram.

  * **Handler level** (the conversation state machine) — unit
    tests using the same `_FakeMessage` / `_FakeCallbackQuery`
    shims as the rest of the bot tests. We test the per-step
    handlers individually so failures are easy to read.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.database import session_scope
from app.core.security import hash_password
from app.models.bill import Bill
from app.models.enums import BillStatus
from app.models.user import User
from app.services.bill import (
    BillValidationError,
    MAX_BILL_NGN,
    MIN_BILL_NGN,
    ScheduleBillInput,
    create_scheduled_bill,
)


# ── Helpers ────────────────────────────────────────────────────────


def _link_user(session, *, email: str, chat_id: int) -> User:
    u = User(
        email=email,
        hashed_password=hash_password("Secret123"),
        first_name="Sch",
        last_name="Tester",
        phone_number=f"0809{chat_id:08d}",
        telegram_chat_id=str(chat_id),
        is_telegram_linked=True,
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


class _FakeMessage:
    def __init__(self, chat_id: int, text: str | None = None) -> None:
        self.chat_id = chat_id
        self.text = text
        self.replies: list[str] = []
        self.edits: list[str] = []
        self.photo = None
        self.document = None

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


class _FakeCallbackQuery:
    def __init__(self, data: str, chat_id: int = 0) -> None:
        self.data = data
        self.answered: list[None] = []
        self.edits: list[str] = []
        self.message = _FakeMessage(chat_id)

    async def answer(self):
        self.answered.append(None)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


class _FakeContext:
    def __init__(self):
        self.user_data: dict = {}


# ── Service layer: create_scheduled_bill ──────────────────────────


def test_create_one_off_scheduled_bill(session) -> None:
    """A non-recurring scheduled bill lands as `status=scheduled`
    with no recurrence fields set."""
    u = _link_user(session, email="svc1@x.com", chat_id=200001)
    due = datetime.now() + timedelta(days=7)
    bill = create_scheduled_bill(
        session,
        user_id=u.id,
        payload=ScheduleBillInput(
            vendor_name="DSTV",
            amount=Decimal("5000"),
            due_date=due,
            account_number="0123456789",
            bank_code="058",
            bank_name="GTBank",
        ),
    )
    assert bill.id is not None
    assert bill.status == BillStatus.SCHEDULED.value
    assert bill.is_recurring is False
    assert bill.recurrence_interval is None
    assert bill.next_recurrence_date is None
    # The DB column is TIMESTAMPTZ; the test passes a naive datetime
    # which Postgres attaches the local TZ to on read. Compare on
    # the date (which is what the user cares about).
    assert bill.due_date.date() == due.date()
    assert bill.vendor_name == "DSTV"
    assert float(bill.amount) == 5000.0


def test_create_recurring_scheduled_bill_sets_next_recurrence(session) -> None:
    """A recurring bill must have `is_recurring=True`,
    `recurrence_interval` set, and `next_recurrence_date=due_date`
    so the scheduler picks up the *first* occurrence on/after the
    due date."""
    u = _link_user(session, email="svc2@x.com", chat_id=200002)
    due = datetime.now() + timedelta(days=14)
    bill = create_scheduled_bill(
        session,
        user_id=u.id,
        payload=ScheduleBillInput(
            vendor_name="EKEDC",
            amount=Decimal("12000"),
            due_date=due,
            account_number="0123456789",
            bank_code="058",
            recurrence_interval="monthly",
        ),
    )
    assert bill.is_recurring is True
    assert bill.recurrence_interval == "monthly"
    # The DB column is TIMESTAMPTZ; compare on date.
    assert bill.next_recurrence_date is not None
    assert bill.next_recurrence_date.date() == due.date()


def test_create_rejects_empty_vendor(session) -> None:
    u = _link_user(session, email="svc3@x.com", chat_id=200003)
    with pytest.raises(BillValidationError, match="[Vv]endor"):
        create_scheduled_bill(
            session, user_id=u.id,
            payload=ScheduleBillInput(
                vendor_name="   ",
                amount=Decimal("5000"),
                due_date=datetime.now() + timedelta(days=1),
            ),
        )


def test_create_rejects_amount_below_minimum(session) -> None:
    u = _link_user(session, email="svc4@x.com", chat_id=200004)
    with pytest.raises(BillValidationError, match="between"):
        create_scheduled_bill(
            session, user_id=u.id,
            payload=ScheduleBillInput(
                vendor_name="X",
                amount=Decimal("50"),  # < MIN
                due_date=datetime.now() + timedelta(days=1),
            ),
        )


def test_create_rejects_amount_above_maximum(session) -> None:
    u = _link_user(session, email="svc5@x.com", chat_id=200005)
    with pytest.raises(BillValidationError, match="between"):
        create_scheduled_bill(
            session, user_id=u.id,
            payload=ScheduleBillInput(
                vendor_name="X",
                amount=Decimal("20_000_000"),  # > MAX
                due_date=datetime.now() + timedelta(days=1),
            ),
        )


def test_create_rejects_past_due_date(session) -> None:
    u = _link_user(session, email="svc6@x.com", chat_id=200006)
    with pytest.raises(BillValidationError, match="past"):
        create_scheduled_bill(
            session, user_id=u.id,
            payload=ScheduleBillInput(
                vendor_name="X",
                amount=Decimal("5000"),
                due_date=datetime.now() - timedelta(days=1),
            ),
        )


def test_create_rejects_invalid_recurrence_interval(session) -> None:
    u = _link_user(session, email="svc7@x.com", chat_id=200007)
    with pytest.raises(BillValidationError, match="[Rr]ecurrence"):
        create_scheduled_bill(
            session, user_id=u.id,
            payload=ScheduleBillInput(
                vendor_name="X",
                amount=Decimal("5000"),
                due_date=datetime.now() + timedelta(days=1),
                recurrence_interval="yearly",  # not allowed
                account_number="0123456789",
                bank_code="058",
            ),
        )


def test_create_rejects_recurring_without_account(session) -> None:
    """A recurring bill must have an account number + bank code
    (the scheduler auto-pays these, so it needs to know where)."""
    u = _link_user(session, email="svc8@x.com", chat_id=200008)
    with pytest.raises(BillValidationError, match="account number"):
        create_scheduled_bill(
            session, user_id=u.id,
            payload=ScheduleBillInput(
                vendor_name="X",
                amount=Decimal("5000"),
                due_date=datetime.now() + timedelta(days=1),
                recurrence_interval="monthly",
                # No account / bank
            ),
        )


# ── Conversation: per-step handlers ───────────────────────────────


@pytest.mark.asyncio
async def test_schedule_unlinked_asks_to_link() -> None:
    """`/schedule` from an unlinked chat should ask the user to
    link first, not crash."""
    from app.handlers.schedule_conversation import schedule_command

    msg = _FakeMessage(chat_id=300001)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 300001})()}
    )()
    from telegram.ext import ConversationHandler

    result = await schedule_command(update, _FakeContext())
    assert result == ConversationHandler.END
    assert any("link" in r.lower() for r in msg.replies)


@pytest.mark.asyncio
async def test_schedule_prompts_vendor_after_entry(session) -> None:
    """`/schedule` for a linked user should set up the state and
    ask for the vendor name (state SCH_VENDOR)."""
    from app.handlers.schedule_conversation import (
        SCH_VENDOR,
        schedule_command,
    )

    _link_user(session, email="conv1@x.com", chat_id=300002)
    msg = _FakeMessage(chat_id=300002)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 300002})()}
    )()
    ctx = _FakeContext()
    result = await schedule_command(update, ctx)
    assert result == SCH_VENDOR
    body = "\n".join(msg.replies)
    assert "Vendor" in body
    assert "DSTV" in body  # example in the prompt
    # State should be primed.
    assert "sch" in ctx.user_data
    assert ctx.user_data["sch"] == {}


@pytest.mark.asyncio
async def test_handle_vendor_advances_to_amount() -> None:
    from app.handlers.schedule_conversation import (
        SCH_AMOUNT,
        handle_vendor,
    )
    ctx = _FakeContext()
    msg = _FakeMessage(chat_id=300003, text="DSTV")
    update = type("U", (), {"message": msg})()
    result = await handle_vendor(update, ctx)
    assert result == SCH_AMOUNT
    assert ctx.user_data["sch"]["vendor_name"] == "DSTV"


@pytest.mark.asyncio
async def test_handle_vendor_rejects_empty() -> None:
    from app.handlers.schedule_conversation import (
        SCH_VENDOR,
        handle_vendor,
    )
    ctx = _FakeContext()
    msg = _FakeMessage(chat_id=300004, text="   ")
    update = type("U", (), {"message": msg})()
    result = await handle_vendor(update, ctx)
    assert result == SCH_VENDOR  # stays in same state
    body = "\n".join(msg.replies)
    assert "1" in body and "255" in body  # length message


@pytest.mark.asyncio
async def test_handle_amount_parses_with_commas() -> None:
    from app.handlers.schedule_conversation import (
        SCH_ACCOUNT,
        handle_amount,
    )
    ctx = _FakeContext()
    msg = _FakeMessage(chat_id=300005, text="12,500.50")
    update = type("U", (), {"message": msg})()
    result = await handle_amount(update, ctx)
    assert result == SCH_ACCOUNT
    assert ctx.user_data["sch"]["amount"] == 12500.50


@pytest.mark.asyncio
async def test_handle_amount_rejects_garbage() -> None:
    from app.handlers.schedule_conversation import (
        SCH_AMOUNT,
        handle_amount,
    )
    ctx = _FakeContext()
    msg = _FakeMessage(chat_id=300006, text="not a number")
    update = type("U", (), {"message": msg})()
    result = await handle_amount(update, ctx)
    assert result == SCH_AMOUNT
    body = "\n".join(msg.replies)
    assert "isn't a valid amount" in body or "valid" in body.lower()


@pytest.mark.asyncio
async def test_handle_amount_rejects_out_of_range() -> None:
    from app.handlers.schedule_conversation import (
        SCH_AMOUNT,
        handle_amount,
    )
    ctx = _FakeContext()
    msg = _FakeMessage(chat_id=300007, text="50")  # < MIN
    update = type("U", (), {"message": msg})()
    result = await handle_amount(update, ctx)
    assert result == SCH_AMOUNT
    body = "\n".join(msg.replies)
    assert "between" in body.lower()


@pytest.mark.asyncio
async def test_handle_account_rejects_short() -> None:
    from app.handlers.schedule_conversation import (
        SCH_ACCOUNT,
        handle_account,
    )
    ctx = _FakeContext()
    msg = _FakeMessage(chat_id=300008, text="123")  # too short
    update = type("U", (), {"message": msg})()
    result = await handle_account(update, ctx)
    assert result == SCH_ACCOUNT
    body = "\n".join(msg.replies)
    assert "10" in body and "11" in body


@pytest.mark.asyncio
async def test_handle_bank_picked_stores_code() -> None:
    from app.handlers.schedule_conversation import (
        SCH_DATE,
        handle_bank_picked,
    )
    ctx = _FakeContext()
    query = _FakeCallbackQuery(data="sch_bank:058:GTBank", chat_id=300009)
    update = type("U", (), {"callback_query": query})()
    result = await handle_bank_picked(update, ctx)
    assert result == SCH_DATE
    assert ctx.user_data["sch"]["bank_code"] == "058"
    assert ctx.user_data["sch"]["bank_name"] == "GTBank"


@pytest.mark.asyncio
async def test_handle_bank_typed_validates_digits() -> None:
    from app.handlers.schedule_conversation import (
        SCH_BANK,
        handle_bank_typed,
    )
    ctx = _FakeContext()
    msg = _FakeMessage(chat_id=300010, text="12ab")  # non-digit
    update = type("U", (), {"message": msg})()
    result = await handle_bank_typed(update, ctx)
    assert result == SCH_BANK
    body = "\n".join(msg.replies)
    assert "3" in body and "6" in body


@pytest.mark.asyncio
async def test_handle_date_picked_today_advances() -> None:
    from app.handlers.schedule_conversation import (
        SCH_RECURRENCE,
        handle_date_picked,
    )
    ctx = _FakeContext()
    query = _FakeCallbackQuery(data="sch_date_today", chat_id=300011)
    update = type("U", (), {"callback_query": query})()
    result = await handle_date_picked(update, ctx)
    assert result == SCH_RECURRENCE
    assert "due_date" in ctx.user_data["sch"]


@pytest.mark.asyncio
async def test_handle_recurrence_monthly() -> None:
    from app.handlers.schedule_conversation import (
        SCH_CONFIRM,
        handle_recurrence,
    )
    ctx = _FakeContext()
    query = _FakeCallbackQuery(data="sch_recur:monthly", chat_id=300012)
    update = type("U", (), {"callback_query": query})()
    result = await handle_recurrence(update, ctx)
    assert result == SCH_CONFIRM
    assert ctx.user_data["sch"]["recurrence_interval"] == "monthly"


@pytest.mark.asyncio
async def test_handle_recurrence_none_means_one_off() -> None:
    from app.handlers.schedule_conversation import (
        SCH_CONFIRM,
        handle_recurrence,
    )
    ctx = _FakeContext()
    query = _FakeCallbackQuery(data="sch_recur:none", chat_id=300013)
    update = type("U", (), {"callback_query": query})()
    result = await handle_recurrence(update, ctx)
    assert result == SCH_CONFIRM
    assert ctx.user_data["sch"]["recurrence_interval"] is None


@pytest.mark.asyncio
async def test_handle_confirm_yes_persists_scheduled_bill(session) -> None:
    """The full happy path: user walks through every step, taps
    'Schedule it', and a Bill row lands in the DB as
    `status=scheduled, is_recurring=..., next_recurrence_date=...`."""
    from app.handlers.schedule_conversation import (
        ConversationHandler,
        handle_amount,
        handle_account,
        handle_bank_picked,
        handle_confirm_yes,
        handle_date_picked,
        handle_recurrence,
        handle_vendor,
    )

    u = _link_user(session, email="full@x.com", chat_id=300014)
    user_id = u.id
    ctx = _FakeContext()
    ctx.user_data["sch"] = {}

    # Step 1: vendor
    msg = _FakeMessage(chat_id=300014, text="EKEDC")
    update = type("U", (), {"message": msg})()
    assert await handle_vendor(update, ctx) == 1  # SCH_AMOUNT
    # Step 2: amount
    msg = _FakeMessage(chat_id=300014, text="15000")
    update = type("U", (), {"message": msg})()
    assert await handle_amount(update, ctx) == 2  # SCH_ACCOUNT
    # Step 3: account
    msg = _FakeMessage(chat_id=300014, text="0123456789")
    update = type("U", (), {"message": msg})()
    assert await handle_account(update, ctx) == 3  # SCH_BANK
    # Step 4: bank picked
    query = _FakeCallbackQuery(data="sch_bank:058:GTBank", chat_id=300014)
    update = type("U", (), {"callback_query": query})()
    assert await handle_bank_picked(update, ctx) == 4  # SCH_DATE
    # Step 5: date picked (tomorrow)
    query = _FakeCallbackQuery(data="sch_date_tomorrow", chat_id=300014)
    update = type("U", (), {"callback_query": query})()
    assert await handle_date_picked(update, ctx) == 5  # SCH_RECURRENCE
    # Step 6: recurrence
    query = _FakeCallbackQuery(data="sch_recur:monthly", chat_id=300014)
    update = type("U", (), {"callback_query": query})()
    assert await handle_recurrence(update, ctx) == 6  # SCH_CONFIRM
    # Step 7: confirm
    query = _FakeCallbackQuery(data="sch_confirm_yes", chat_id=300014)
    update = type("U", (), {"callback_query": query})()
    result = await handle_confirm_yes(update, ctx)
    assert result == ConversationHandler.END
    # The user got a confirmation message.
    body = "\n".join(query.edits)
    assert "Bill scheduled" in body or "scheduled" in body.lower()
    assert "EKEDC" in body
    assert "₦15,000.00" in body
    assert "Every month" in body or "every month" in body.lower()

    # And a row landed in the DB.
    with session_scope() as verify:
        from sqlalchemy import select as _sa_select
        bills = verify.execute(
            _sa_select(Bill).where(Bill.user_id == user_id)
        ).scalars().all()
        assert len(bills) == 1
        b = bills[0]
        assert b.vendor_name == "EKEDC"
        assert float(b.amount) == 15000.0
        assert b.status == BillStatus.SCHEDULED.value
        assert b.is_recurring is True
        assert b.recurrence_interval == "monthly"
        assert b.next_recurrence_date is not None
        assert b.account_number == "0123456789"
        assert b.bank_code == "058"


@pytest.mark.asyncio
async def test_handle_cancel_clears_state() -> None:
    from app.handlers.schedule_conversation import (
        ConversationHandler,
        handle_cancel,
    )
    ctx = _FakeContext()
    ctx.user_data["sch"] = {"vendor_name": "X"}
    query = _FakeCallbackQuery(data="sch_cancel", chat_id=300015)
    update = type("U", (), {"callback_query": query})()
    result = await handle_cancel(update, ctx)
    assert result == ConversationHandler.END
    assert "sch" not in ctx.user_data
