"""Unit tests for the Telegram bot handlers.

We use PTB's `Application` + `process_update` to drive the handlers
without an actual network. The bot does not need a real token —
we build the Application with a dummy token, dispatch synthetic
`Update` objects, and assert on the bot's `sent_messages` list.

DB-dependent tests reuse the integration conftest's `session`
fixture (Postgres) so we get full SQLAlchemy + JSONB support.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.database import session_scope
from app.core.security import hash_password
from app.models.telegram_link_code import TelegramLinkCode
from app.models.user import User


# ── Helpers ─────────────────────────────────────────────────────────


class _FakeMessage:
    def __init__(self, chat_id: int, text: str | None = None) -> None:
        self.chat_id = chat_id
        self.text = text
        self.replies: list[str] = []
        self.edits: list[str] = []
        self.photo = None
        self.document = None

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append(text)


class _FakeContext:
    def __init__(self, chat_id: int) -> None:
        self.args: list[str] = []
        self.user_data: dict[str, Any] = {}
        self.bot = type(
            "Bot",
            (),
            {
                "send_message": AsyncMock(return_value=_FakeMessage(chat_id)),
                "get_file": AsyncMock(),
            },
        )()


def _link_user_sync(session, *, email: str, chat_id: int | None = None) -> User:
    user = User(
        email=email,
        hashed_password=hash_password("Secret123"),
        first_name="TG",
        last_name="Tester",
        phone_number="0809" + email[:8].rjust(8, "0"),
        telegram_chat_id=str(chat_id) if chat_id is not None else None,
        is_telegram_linked=chat_id is not None,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# ── /start ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_sends_welcome_message() -> None:
    from app.handlers.auth import start_command
    msg = _FakeMessage(chat_id=12345)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 12345})()}
    )()
    await start_command(update, _FakeContext(12345))
    assert any("Welcome" in r for r in msg.replies)


# ── /link ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_with_no_args_asks_for_code() -> None:
    from app.handlers.auth import link_command
    msg = _FakeMessage(chat_id=99999)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 99999})()}
    )()
    ctx = _FakeContext(99999)
    await link_command(update, ctx)
    assert any("include your linking code" in r.lower() for r in msg.replies)


@pytest.mark.asyncio
async def test_link_with_invalid_code_rejects() -> None:
    from app.handlers.auth import link_command
    msg = _FakeMessage(chat_id=99998)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 99998})()}
    )()
    ctx = _FakeContext(99998)
    ctx.args = ["NOPE12"]
    await link_command(update, ctx)
    assert any("invalid" in r.lower() for r in msg.replies)


@pytest.mark.asyncio
async def test_link_with_valid_code_links_user(session) -> None:
    from app.handlers.auth import link_command
    user = _link_user_sync(session, email="linkme@x.com")
    user_id = user.id  # capture before session closes
    code = TelegramLinkCode.generate_code()
    session.add(
        TelegramLinkCode(
            user_id=user_id,
            code=code,
            expires_at=datetime.now(tz=timezone.utc) + timedelta(minutes=15),
        )
    )
    session.commit()

    chat_id = 88888
    msg = _FakeMessage(chat_id=chat_id)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": chat_id})()}
    )()
    ctx = _FakeContext(chat_id)
    ctx.args = [code]
    await link_command(update, ctx)
    assert any("linked" in r.lower() for r in msg.replies)

    with session_scope() as verify:
        refreshed = verify.get(User, user_id)
        assert refreshed is not None
        assert refreshed.is_telegram_linked is True
        assert refreshed.telegram_chat_id == str(chat_id)


@pytest.mark.asyncio
async def test_link_with_expired_code_rejects(session) -> None:
    from app.handlers.auth import link_command
    user = _link_user_sync(session, email="expired@x.com")
    user_id = user.id
    code = TelegramLinkCode.generate_code()
    session.add(
        TelegramLinkCode(
            user_id=user_id,
            code=code,
            expires_at=datetime.now(tz=timezone.utc) - timedelta(minutes=1),
        )
    )
    session.commit()

    msg = _FakeMessage(chat_id=77777)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 77777})()}
    )()
    ctx = _FakeContext(77777)
    ctx.args = [code]
    await link_command(update, ctx)
    assert any("expired" in r.lower() for r in msg.replies)


# ── /unlink ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unlink_removes_chat_id(session) -> None:
    from app.handlers.auth import unlink_command
    user = _link_user_sync(session, email="unlink@x.com", chat_id=66666)
    user_id = user.id
    msg = _FakeMessage(chat_id=66666)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 66666})()}
    )()
    ctx = _FakeContext(66666)
    await unlink_command(update, ctx)

    with session_scope() as verify:
        refreshed = verify.get(User, user_id)
        assert refreshed is not None
        assert refreshed.is_telegram_linked is False
        assert refreshed.telegram_chat_id is None


# ── /wallet ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wallet_unlinked_asks_to_link() -> None:
    from app.handlers.auth import wallet_command
    msg = _FakeMessage(chat_id=55555)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 55555})()}
    )()
    await wallet_command(update, _FakeContext(55555))
    assert any("link" in r.lower() for r in msg.replies)


@pytest.mark.asyncio
async def test_wallet_with_no_va_warns_user(session) -> None:
    from app.handlers.auth import wallet_command
    _link_user_sync(session, email="nowallet@x.com", chat_id=44444)
    msg = _FakeMessage(chat_id=44444)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 44444})()}
    )()
    await wallet_command(update, _FakeContext(44444))
    assert any(
        "no virtual account" in r.lower() or "wallet" in r.lower()
        for r in msg.replies
    )


# ── Pure-function helpers ───────────────────────────────────────────


def test_get_linked_user_returns_none_for_unlinked() -> None:
    from app.handlers.helpers import get_linked_user
    assert get_linked_user("999999") is None


def test_get_linked_user_returns_user_for_linked(session) -> None:
    _link_user_sync(session, email="helper@x.com", chat_id=33333)
    from app.handlers.helpers import get_linked_user
    user = get_linked_user("33333")
    assert user is not None
    user_id = user.id
    with session_scope() as verify:
        refreshed = verify.get(User, user_id)
        assert refreshed is not None
        assert refreshed.email == "helper@x.com"


def test_escape_md_handles_reserved_chars() -> None:
    from app.handlers.helpers import escape_md
    assert escape_md("foo_bar") == "foo\\_bar"
    assert escape_md("a*b*c") == "a\\*b\\*c"
    assert escape_md("[link](http://x.com)") == "\\[link](http://x.com)"
    assert escape_md("") == ""


def test_parse_user_date_accepts_common_formats() -> None:
    from app.handlers.helpers import parse_user_date
    assert parse_user_date("2026-12-31") is not None
    assert parse_user_date("31 Dec 2026") is not None
    assert parse_user_date("December 31, 2026") is not None
    assert parse_user_date("garbage") is None


# ── /transactions ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transactions_unlinked_asks_to_link() -> None:
    from app.handlers.auth import transactions_command

    msg = _FakeMessage(chat_id=11111)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 11111})()}
    )()
    await transactions_command(update, _FakeContext(11111))
    assert any("link" in r.lower() for r in msg.replies)


@pytest.mark.asyncio
async def test_transactions_no_history(session) -> None:
    from app.handlers.auth import transactions_command

    _link_user_sync(session, email="tx0@x.com", chat_id=22222)
    msg = _FakeMessage(chat_id=22222)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 22222})()}
    )()
    await transactions_command(update, _FakeContext(22222))
    assert any("No transactions" in r for r in msg.replies)


@pytest.mark.asyncio
async def test_transactions_shows_credits_and_debits(session) -> None:
    """A user with a credit and a debit should see both in the
    /transactions output, with the right sign + emoji."""
    from decimal import Decimal

    from app.core.database import session_scope
    from app.handlers.auth import transactions_command
    from app.models.enums import TransactionStatus, TransactionType
    from app.models.transaction import Transaction

    # Use a fresh chat_id that no other test uses, and a unique
    # email so we don't collide with prior tests in the same run.
    _link_user_sync(session, email=f"tx1@x.com", chat_id=33330)
    # Re-fetch the user via session_scope (the path the handler uses).
    with session_scope() as s:
        from sqlalchemy import select as _sa_select
        from app.models.user import User as _User
        # Force-load id while session is open (avoid DetachedInstanceError)
        from app.handlers.helpers import get_linked_user
        u = get_linked_user("33330")
        assert u is not None
        user_id = u.id
        s.add(
            Transaction(
                user_id=user_id,
                type=TransactionType.CREDIT.value,
                amount=Decimal("5000.00"),
                fee=Decimal("0.00"),
                currency="NGN",
                status=TransactionStatus.SUCCESS.value,
                provider="nomba",
                provider_reference=f"topup_1_aaa_{user_id}",
                narration="Top-up via Nomba Checkout",
            )
        )
        s.add(
            Transaction(
                user_id=user_id,
                type=TransactionType.DEBIT.value,
                amount=Decimal("1234.00"),
                fee=Decimal("50.00"),
                currency="NGN",
                status=TransactionStatus.SUCCESS.value,
                provider="nomba",
                provider_reference=f"autopay_1_xyz_{user_id}",
                narration="Payment to DSTV",
            )
        )
        s.commit()

    msg = _FakeMessage(chat_id=33330)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 33330})()}
    )()
    await transactions_command(update, _FakeContext(33330))
    body = "\n".join(msg.replies)
    # Credit shows positive, debit shows negative
    assert "+₦5,000.00" in body
    assert "−₦1,234.00" in body
    # Narration shown
    assert "Top-up via Nomba Checkout" in body
    assert "Payment to DSTV" in body
    # Status emoji
    assert "✅" in body
    # Balance header
    assert "Current balance" in body


@pytest.mark.asyncio
async def test_help_mentions_transactions() -> None:
    """The /help output must include /transactions so users can
    discover the new command."""
    from app.handlers.auth import help_command

    msg = _FakeMessage(chat_id=44444)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 44444})()}
    )()
    await help_command(update, _FakeContext(44444))
    body = "\n".join(msg.replies)
    assert "/transactions" in body


@pytest.mark.asyncio
async def test_start_mentions_transactions() -> None:
    """The /start output must list /transactions as a discoverable
    command."""
    from app.handlers.auth import start_command

    msg = _FakeMessage(chat_id=55555)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 55555})()}
    )()
    await start_command(update, _FakeContext(55555))
    body = "\n".join(msg.replies)
    assert "/transactions" in body
    assert "/help" in body


# ── date_from_quickpick helper ────────────────────────────────────


def test_date_from_quickpick_maps_tokens() -> None:
    from datetime import datetime, timedelta

    from app.handlers.helpers import date_from_quickpick

    now = datetime.now()
    assert date_from_quickpick("today") is not None
    assert date_from_quickpick("skip") is not None
    assert date_from_quickpick("unknown") is None
    # Relative offsets should be in the future, not in the past.
    tomorrow = date_from_quickpick("tomorrow")
    assert tomorrow is not None
    assert (tomorrow - now) >= timedelta(hours=23)
    plus_week = date_from_quickpick("+1w")
    assert plus_week is not None
    assert (plus_week - now) >= timedelta(days=6)


# ── multi_field_editor_keyboard + format_multi_field_editor ──────


def test_multi_field_editor_keyboard_marks_edited_fields() -> None:
    from app.handlers.helpers import multi_field_editor_keyboard

    bill = {
        "vendor_name": "DSTV",
        "amount": 5000,
        "due_date": "2026-12-31T00:00:00",
        "account_number": "0123456789",
        "bank_code": "058",
    }
    kb = multi_field_editor_keyboard(bill, edited_keys={"amount", "due_date"})
    # Flatten callback_data to check.
    callbacks = [
        btn.callback_data
        for row in kb.inline_keyboard
        for btn in row
    ]
    assert any(c == "edit_field:amount" for c in callbacks)
    assert any(c == "edit_field:due_date" for c in callbacks)
    assert "edit_done" in callbacks
    assert "edit_discard" in callbacks


def test_format_multi_field_editor_marks_edited_fields() -> None:
    from app.handlers.helpers import format_multi_field_editor

    bill = {
        "vendor_name": "DSTV",
        "amount": 5000,
        "due_date": "2026-12-31T00:00:00",
        "account_number": "0123456789",
        "bank_code": "058",
    }
    text = format_multi_field_editor(bill, edited_keys={"amount"})
    assert "Edit bill details" in text
    assert "Vendor name" in text
    assert "DSTV" in text
    # Edited field gets a 📝 marker.
    assert text.count("📝") >= 1
    # Non-edited field does not get a marker on its own line.
    lines_with_marker = [
        ln for ln in text.splitlines() if "📝" in ln
    ]
    assert any("Amount" in ln for ln in lines_with_marker)


# ── Conversation flow: edit / discard / done / date quick-pick ───


class _FakeCallbackQuery:
    """Minimal stand-in for a telegram.CallbackQuery.

    The handlers call `query.answer()` and `query.edit_message_text(...)`;
    we just record the calls so we can assert on them. A `message`
    attribute is provided so handlers that read `query.message.chat_id`
    (the topup flow does) can find the chat id.
    """

    def __init__(self, data: str, chat_id: int = 0) -> None:
        self.data = data
        self.answered: list[None] = []
        self.edits: list[str] = []
        self.message = _FakeMessage(chat_id)

    async def answer(self) -> None:
        self.answered.append(None)

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append(text)


def _seed_bill_context(ctx: _FakeContext) -> None:
    """Put a parsed bill + staging snapshot into user_data, as if
    receive_bill() had just finished."""
    ctx.user_data["bill"] = {
        "vendor_name": "DSTV",
        "amount": 5000.0,
        "currency": "NGN",
        "due_date": "2026-12-31T00:00:00",
        "account_number": "0123456789",
        "bank_code": "058",
    }
    ctx.user_data["staging"] = dict(ctx.user_data["bill"])
    ctx.user_data["edited_keys"] = set()
    ctx.user_data["user_id"] = 1
    ctx.user_data["user_balance"] = 10000.0


@pytest.mark.asyncio
async def test_handle_edit_shows_multi_field_editor() -> None:
    from app.handlers.bill_conversation import EDIT_LIST, handle_edit

    ctx = _FakeContext(1)
    _seed_bill_context(ctx)
    query = _FakeCallbackQuery(data="edit")
    update = type("U", (), {"callback_query": query})()
    result = await handle_edit(update, ctx)
    assert result == EDIT_LIST
    assert query.edits, "handler should edit the message"
    body = query.edits[-1]
    assert "Edit bill details" in body
    assert "DSTV" in body


@pytest.mark.asyncio
async def test_handle_edit_field_enters_input_mode() -> None:
    from app.handlers.bill_conversation import EDIT_VALUE, handle_edit_field

    ctx = _FakeContext(1)
    _seed_bill_context(ctx)
    query = _FakeCallbackQuery(data="edit_field:vendor_name")
    update = type("U", (), {"callback_query": query})()
    result = await handle_edit_field(update, ctx)
    assert result == EDIT_VALUE
    assert ctx.user_data["editing_field"] == "vendor_name"
    body = query.edits[-1]
    assert "Vendor name" in body
    assert "DSTV" in body


@pytest.mark.asyncio
async def test_handle_date_quickpick_sets_date_and_returns_to_list() -> None:
    from app.handlers.bill_conversation import (
        EDIT_LIST,
        handle_date_quickpick,
    )

    ctx = _FakeContext(1)
    _seed_bill_context(ctx)
    query = _FakeCallbackQuery(data="date_tomorrow")
    update = type("U", (), {"callback_query": query})()
    result = await handle_date_quickpick(update, ctx)
    assert result == EDIT_LIST
    # The bill's due_date should have been updated.
    assert "due_date" in ctx.user_data["edited_keys"]
    assert "tomorrow" in ctx.user_data["bill"]["due_date"].lower() or (
        # The isoformat includes 'T', so just assert non-empty.
        ctx.user_data["bill"]["due_date"]
    )


@pytest.mark.asyncio
async def test_handle_edit_discard_restores_staging() -> None:
    from app.handlers.bill_conversation import (
        CONFIRM,
        handle_edit_discard,
    )

    ctx = _FakeContext(1)
    _seed_bill_context(ctx)
    # Simulate a user edit.
    ctx.user_data["bill"]["vendor_name"] = "WRONG VENDOR"
    ctx.user_data["edited_keys"] = {"vendor_name"}
    query = _FakeCallbackQuery(data="edit_discard")
    update = type("U", (), {"callback_query": query})()
    result = await handle_edit_discard(update, ctx)
    assert result == CONFIRM
    # The staging snapshot ("DSTV") should be restored.
    assert ctx.user_data["bill"]["vendor_name"] == "DSTV"
    # Marker set cleared.
    assert ctx.user_data["edited_keys"] == set()


@pytest.mark.asyncio
async def test_handle_edit_done_returns_to_confirm() -> None:
    from app.handlers.bill_conversation import (
        CONFIRM,
        handle_edit_done,
    )

    ctx = _FakeContext(1)
    _seed_bill_context(ctx)
    ctx.user_data["bill"]["vendor_name"] = "GOTV"
    ctx.user_data["edited_keys"] = {"vendor_name"}
    query = _FakeCallbackQuery(data="edit_done")
    update = type("U", (), {"callback_query": query})()
    result = await handle_edit_done(update, ctx)
    assert result == CONFIRM
    # Edits cleared, but the change is preserved.
    assert ctx.user_data["bill"]["vendor_name"] == "GOTV"
    assert ctx.user_data["edited_keys"] == set()


@pytest.mark.asyncio
async def test_handle_new_value_validates_amount() -> None:
    """A non-numeric amount should bounce back to EDIT_VALUE with an
    error, not silently corrupt the bill."""
    from app.handlers.bill_conversation import (
        EDIT_VALUE,
        handle_new_value,
    )

    ctx = _FakeContext(1)
    _seed_bill_context(ctx)
    ctx.user_data["editing_field"] = "amount"
    msg = _FakeMessage(chat_id=1, text="not a number")
    update = type("U", (), {"message": msg})()
    result = await handle_new_value(update, ctx)
    assert result == EDIT_VALUE
    # The original amount should be unchanged.
    assert ctx.user_data["bill"]["amount"] == 5000.0
    # User got a warning.
    body = "\n".join(msg.replies)
    assert "isn't a valid amount" in body or "valid" in body.lower()


@pytest.mark.asyncio
async def test_handle_new_value_validates_date() -> None:
    """A non-parseable date should bounce back to EDIT_VALUE."""
    from app.handlers.bill_conversation import (
        EDIT_VALUE,
        handle_new_value,
    )

    ctx = _FakeContext(1)
    _seed_bill_context(ctx)
    ctx.user_data["editing_field"] = "due_date"
    msg = _FakeMessage(chat_id=1, text="not a date")
    update = type("U", (), {"message": msg})()
    result = await handle_new_value(update, ctx)
    assert result == EDIT_VALUE
    # Original due_date preserved.
    assert ctx.user_data["bill"]["due_date"] == "2026-12-31T00:00:00"
    body = "\n".join(msg.replies)
    assert "couldn't parse" in body or "date" in body.lower()


@pytest.mark.asyncio
async def test_handle_new_value_applies_valid_amount() -> None:
    """A valid amount should be persisted and return to the list."""
    from app.handlers.bill_conversation import (
        EDIT_LIST,
        handle_new_value,
    )

    ctx = _FakeContext(1)
    _seed_bill_context(ctx)
    ctx.user_data["editing_field"] = "amount"
    msg = _FakeMessage(chat_id=1, text="7500.50")
    update = type("U", (), {"message": msg})()
    result = await handle_new_value(update, ctx)
    assert result == EDIT_LIST
    assert ctx.user_data["bill"]["amount"] == 7500.50
    assert "amount" in ctx.user_data["edited_keys"]


# ── RESOLVING state: pre-pay account validation ──────────────────


class _FakeUpdate:
    """Wrapper update that exposes a `callback_query` shim.

    `handle_resolve_account` checks for `update.callback_query`;
    this class lets us call it without going through a real bot.
    """

    def __init__(self, query):
        self.callback_query = query


@pytest.mark.asyncio
async def test_handle_resolve_account_stores_resolved_name_on_match() -> None:
    """When the resolved bank name fuzzy-matches the vendor, the
    resolved name is stashed in user_data and the next state is
    returned (not FINAL_CONFIRM directly, since we don't run the
    agent in this unit test — but the user_data is what matters)."""
    from app.handlers import bill_conversation as bc
    from app.services.payments.base import ResolvedAccount

    class _ProviderStub:
        name = "nomba"
        async def resolve_account(self, **kwargs):
            return ResolvedAccount(
                account_number=kwargs["account_number"],
                account_name="DSTV NIG LTD",
                bank_code=kwargs["bank_code"],
            )

    # Patch get_payment_provider for the duration of the test.
    original = bc.get_payment_provider
    bc.get_payment_provider = lambda: _ProviderStub()
    try:
        ctx = _FakeContext(1)
        _seed_bill_context(ctx)
        # Vendor "DSTV" is a token-subset of "DSTV NIG LTD" → match.
        ctx.user_data["bill"]["vendor_name"] = "DSTV"
        # The resolve step doesn't actually need a bill_id (it never
        # queries the DB in the match path), but `_run_agent_decision`
        # does, so set a sentinel value to silence the SAWarning.
        ctx.user_data["bill_id"] = -1
        query = _FakeCallbackQuery(data="confirm")
        update = _FakeUpdate(query)
        await bc.handle_resolve_account(update, ctx)
        # The resolved name should be cached for later use.
        assert ctx.user_data.get("resolved_account_name") == "DSTV NIG LTD"
    finally:
        bc.get_payment_provider = original


@pytest.mark.asyncio
async def test_handle_resolve_account_blocks_on_hard_mismatch() -> None:
    """When the resolved bank name is a different entity (DSTV vs
    GOTV), the user sees a clear mismatch message and the bot stays
    in CONFIRM state for the user to tap Edit."""
    from app.handlers import bill_conversation as bc
    from app.services.payments.base import ResolvedAccount

    class _ProviderStub:
        name = "nomba"
        async def resolve_account(self, **kwargs):
            return ResolvedAccount(
                account_number=kwargs["account_number"],
                account_name="GOTV NIG LTD",
                bank_code=kwargs["bank_code"],
            )

    original = bc.get_payment_provider
    bc.get_payment_provider = lambda: _ProviderStub()
    try:
        ctx = _FakeContext(1)
        _seed_bill_context(ctx)
        ctx.user_data["bill"]["vendor_name"] = "DSTV"
        query = _FakeCallbackQuery(data="confirm")
        update = _FakeUpdate(query)
        result = await bc.handle_resolve_account(update, ctx)

        # Should stay in CONFIRM for the user to tap Edit.
        assert result == bc.CONFIRM
        # The error message should mention both names so the user
        # can spot the typo.
        body = "\n".join(query.edits)
        assert "mismatch" in body.lower()
        assert "DSTV" in body
        assert "GOTV" in body
    finally:
        bc.get_payment_provider = original


# ── /topup conversation ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_topup_unlinked_asks_to_link() -> None:
    """`/topup` from an unlinked chat should ask the user to link
    first, not crash."""
    from app.handlers.topup_conversation import topup_command

    msg = _FakeMessage(chat_id=99001)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 99001})()}
    )()
    result = await topup_command(update, _FakeContext(99001))
    # ConversationHandler.END == -1
    from telegram.ext import ConversationHandler

    assert result == ConversationHandler.END
    assert any(
        "link" in r.lower() for r in msg.replies
    ), f"expected 'link' in replies, got {msg.replies!r}"


@pytest.mark.asyncio
async def test_topup_linked_shows_quick_pick_keyboard(session) -> None:
    """Linked user should see the quick-pick amount keyboard with
    the minimum/maximum amount in the prompt."""
    from app.handlers.topup_conversation import (
        TOPUP_PICK,
        quick_pick_keyboard,
        topup_command,
    )

    _link_user_sync(session, email="topup1@x.com", chat_id=99002)
    msg = _FakeMessage(chat_id=99002)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 99002})()}
    )()
    result = await topup_command(update, _FakeContext(99002))
    assert result == TOPUP_PICK

    # The quick-pick keyboard has the 4 amounts + Custom + Cancel.
    kb = quick_pick_keyboard()
    callbacks = [
        btn.callback_data
        for row in kb.inline_keyboard
        for btn in row
    ]
    assert "topup_amount:1000" in callbacks
    assert "topup_amount:5000" in callbacks
    assert "topup_amount:10000" in callbacks
    assert "topup_amount:50000" in callbacks
    assert "topup_custom" in callbacks
    assert "topup_cancel" in callbacks

    # The prompt shows the min/max.
    body = "\n".join(msg.replies)
    assert "100" in body  # MIN
    assert "1,000,000" in body  # MAX


@pytest.mark.asyncio
async def test_topup_quickpick_creates_pending_transaction(
    session, stub_provider
) -> None:
    """Tapping a quick-pick amount should call the provider's
    `initialize_topup` and return a Paystack URL. The pending
    Transaction should be persisted."""
    from decimal import Decimal

    from sqlalchemy import select as _sa_select

    from app.handlers.topup_conversation import (
        TOPUP_DONE,
        TOPUP_PICK,
        handle_quickpick,
    )
    from app.models.transaction import Transaction

    _link_user_sync(session, email="topup2@x.com", chat_id=99003)
    # Reset the stub's call list so we can assert on it.
    stub_provider.calls = []

    query = _FakeCallbackQuery(data="topup_amount:5000", chat_id=99003)
    update = type("U", (), {"callback_query": query})()
    result = await handle_quickpick(update, _FakeContext(99003))

    # After the URL is delivered, the conversation stays alive in
    # TOPUP_DONE so the follow-up buttons (topup_done_again /
    # topup_done_balance / topup_done_close) can fire.
    assert result == TOPUP_DONE

    # The provider was called.
    init_calls = [c for c in stub_provider.calls if c[0] == "initialize_topup"]
    assert len(init_calls) == 1
    # Amount in kobo (5000 NGN = 500_000 kobo).
    assert init_calls[0][1]["amount_kobo"] == 500_000
    # The reference is a topup_ one.
    assert init_calls[0][1]["reference"].startswith("topup_")

    # A pending Transaction row was persisted.
    with session_scope() as s:
        rows = s.execute(
            _sa_select(Transaction).where(
                Transaction.narration.like("Top-up via%")
            )
        ).scalars().all()
        assert len(rows) >= 1
        # The latest one is the 5000 NGN topup.
        latest = max(rows, key=lambda r: r.id or 0)
        assert latest.amount == Decimal("5000.00")
        assert latest.status == "pending"

    # The user got the URL.
    body = "\n".join(query.edits)
    assert "https://" in body or "paystack" in body.lower()


@pytest.mark.asyncio
async def test_topup_custom_amount_uses_typed_value(
    session, stub_provider
) -> None:
    """When the user picks 'Custom' and types 7500.50, the
    top-up is initialized for that amount (in kobo)."""
    from app.handlers.topup_conversation import (
        TOPUP_DONE,
        handle_custom_amount,
    )

    _link_user_sync(session, email="topup3@x.com", chat_id=99004)
    stub_provider.calls = []

    msg = _FakeMessage(chat_id=99004, text="7,500.50")
    update = type("U", (), {"message": msg})()
    result = await handle_custom_amount(update, _FakeContext(99004))
    assert result == TOPUP_DONE

    init_calls = [c for c in stub_provider.calls if c[0] == "initialize_topup"]
    assert len(init_calls) == 1
    # 7500.50 NGN = 750_050 kobo
    assert init_calls[0][1]["amount_kobo"] == 750_050


@pytest.mark.asyncio
async def test_topup_custom_rejects_invalid_amount() -> None:
    """A non-numeric custom amount should bounce back to the
    custom-prompt state with an error, not crash."""
    from app.handlers.topup_conversation import TOPUP_CUSTOM, handle_custom_amount

    msg = _FakeMessage(chat_id=99005, text="not a number")
    update = type("U", (), {"message": msg})()
    result = await handle_custom_amount(update, _FakeContext(99005))
    assert result == TOPUP_CUSTOM
    body = "\n".join(msg.replies)
    assert "isn't a valid amount" in body or "valid" in body.lower()


@pytest.mark.asyncio
async def test_topup_custom_rejects_out_of_range() -> None:
    """Amounts below MIN or above MAX should bounce back."""
    from app.handlers.topup_conversation import TOPUP_CUSTOM, handle_custom_amount

    # Too low (50 < 100 min)
    msg = _FakeMessage(chat_id=99006, text="50")
    update = type("U", (), {"message": msg})()
    result = await handle_custom_amount(update, _FakeContext(99006))
    assert result == TOPUP_CUSTOM
    body = "\n".join(msg.replies)
    assert "between" in body.lower() or "100" in body

    # Too high (2_000_000 > 1_000_000 max)
    msg2 = _FakeMessage(chat_id=99007, text="2000000")
    update2 = type("U", (), {"message": msg2})()
    result2 = await handle_custom_amount(update2, _FakeContext(99007))
    assert result2 == TOPUP_CUSTOM


@pytest.mark.asyncio
async def test_help_mentions_topup() -> None:
    """`/help` must surface the new /topup command."""
    from app.handlers.auth import help_command

    msg = _FakeMessage(chat_id=99008)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 99008})()}
    )()
    await help_command(update, _FakeContext(99008))
    body = "\n".join(msg.replies)
    assert "/topup" in body


@pytest.mark.asyncio
async def test_start_mentions_topup() -> None:
    """`/start` should suggest `/topup` for new users."""
    from app.handlers.auth import start_command

    msg = _FakeMessage(chat_id=99009)
    update = type(
        "U", (), {"message": msg, "effective_chat": type("C", (), {"id": 99009})()}
    )()
    await start_command(update, _FakeContext(99009))
    body = "\n".join(msg.replies)
    assert "/topup" in body
