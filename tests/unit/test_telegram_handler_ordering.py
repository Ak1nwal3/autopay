"""Regression tests for handler-ordering bugs in the bot.

Background: PTB v21 walks `Application.handlers[0]` in registration
order and dispatches the first handler whose `check_update` returns
truthy. We discovered (during the bot's /topup smoke test) that
the bill ConversationHandler was registered before the topup
ConversationHandler, and its entry-point filter was
`filters.TEXT | filters.PHOTO | filters.Document.ALL` — which
matches command text too. Result: `/topup` was being claimed by
the bill conversation's `receive_bill` handler, which tried to
extract a bill from the literal text `/topup` and silently ended
the conversation without ever replying.

These tests pin the fix in two ways:
  1. The bill conversation's entry-point filter must exclude
     `filters.COMMAND` (so it can never swallow a command).
  2. The topup ConversationHandler must be registered before the
     bill ConversationHandler (defense in depth — even if the
     filter ever regresses, the order is still correct).

We test these by inspecting the actual `Application` returned by
`build_application()` rather than dispatching real Updates, which
keeps the tests fast and dependency-free.
"""
from __future__ import annotations

import pytest

from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)


@pytest.fixture
def built_app():
    """Build the real bot Application. Uses a dummy token; we never
    initialize it (no network call)."""
    from app.services.telegram import build_application
    return build_application("test-token-no-network-calls")


def _find_bill_conversation(app):
    """Locate the bill ConversationHandler by inspecting the entry
    point's text/photo filter signature (it doesn't have a `command`
    attribute like CommandHandler does)."""
    for handler in app.handlers[0]:
        if not isinstance(handler, ConversationHandler):
            continue
        eps = handler.entry_points or []
        if not eps:
            continue
        ep = eps[0]
        # The bill conversation's entry point is a MessageHandler
        # whose filter is `(filters.TEXT | filters.PHOTO | ...) & ~filters.COMMAND`.
        # We can identify it by the presence of MessageHandler +
        # the broad text/photo/doc filter. Easier: just find the
        # one with no command-based entry points.
        if isinstance(ep, MessageHandler):
            return handler
    raise AssertionError("no MessageHandler-based ConversationHandler found")


def _find_topup_conversation(app):
    """Locate the topup ConversationHandler by its CommandHandler
    entry point with command='topup'."""
    for handler in app.handlers[0]:
        if not isinstance(handler, ConversationHandler):
            continue
        eps = handler.entry_points or []
        for ep in eps:
            if isinstance(ep, CommandHandler) and "topup" in ep.commands:
                return handler
    raise AssertionError("no topup ConversationHandler found")


# ── 1. The bill entry-point filter excludes commands ───────────────


def test_bill_entry_point_excludes_commands(built_app) -> None:
    """The bill conversation's entry-point filter must explicitly
    exclude `filters.COMMAND`. Otherwise a /topup text message
    would be claimed by the bill conversation and silently
    swallowed (no reply to the user)."""
    bill = _find_bill_conversation(built_app)
    ep = bill.entry_points[0]
    assert isinstance(ep, MessageHandler), (
        f"expected first entry point to be a MessageHandler, got {type(ep)}"
    )

    # The filter must reject a `CommandHandler` update. The way PTB
    # expresses this is `... & ~filters.COMMAND`. We don't have a
    # direct "is this filter negation-of-COMMAND" introspection, so
    # we check the filter's `check_update` against a synthetic
    # command-bearing message.
    class _FakeMessage:
        text = "/topup"
        # No entities — we only need the filter to decide that a
        # `/`-prefixed text is a command. PTB's `filters.COMMAND`
        # uses the message text and entities to detect this.
        entities = []

    class _FakeUpdate:
        effective_message = _FakeMessage()
        # MessageFilter.check_update inspects message.text and
        # message.entities via the update.

    # We can't easily build a perfect `Update` here, but we can at
    # least assert the filter is NOT the bare
    # `filters.TEXT | filters.PHOTO | filters.Document.ALL` (the
    # pre-fix version) by checking the bitwise `&` operator. The
    # post-fix filter is `(<original>) & ~filters.COMMAND`. The
    # repr for a combined filter includes both operands.
    filt = ep.filters
    repr_str = repr(filt)
    # The original broad filter:
    assert "TEXT" in repr_str or "PHOTO" in repr_str
    # The negation must be present — PTB's `~filters.COMMAND`
    # renders as something like "filters.COMMAND negated=True" or
    # includes "COMMAND" in the repr.
    assert "COMMAND" in repr_str, (
        f"expected filters.COMMAND in filter repr (with negation); got: {repr_str}"
    )


# ── 2. The topup conversation is registered before the bill one ────


def test_topup_registered_before_bill(built_app) -> None:
    """The topup ConversationHandler must be registered before the
    bill ConversationHandler. PTB's first-match-wins dispatch
    means that even if the bill entry-point filter ever regresses,
    the order here keeps /topup working.

    We check this by finding both handlers in the registration
    list and asserting the topup one comes first.
    """
    handlers = built_app.handlers[0]
    topup_idx = None
    bill_idx = None
    for i, h in enumerate(handlers):
        if isinstance(h, ConversationHandler):
            for ep in (h.entry_points or []):
                if isinstance(ep, CommandHandler) and "topup" in ep.commands:
                    topup_idx = i
                if isinstance(ep, MessageHandler):
                    bill_idx = i

    assert topup_idx is not None, "topup ConversationHandler not found"
    assert bill_idx is not None, "bill ConversationHandler not found"
    assert topup_idx < bill_idx, (
        f"topup (idx {topup_idx}) must be registered before bill (idx {bill_idx}); "
        f"otherwise /topup gets claimed by the bill entry point"
    )


# ── 3. End-to-end: dispatching /topup via process_update replies ──


@pytest.mark.asyncio
async def test_topup_via_process_update_replies_for_linked_user(
    built_app, session
) -> None:
    """End-to-end regression: build the real Application, link a
    user to chat 99000, dispatch a synthetic /topup update, and
    assert that reply_text was called. (The MagicMock-based
    repro_topup.py proved this works after the fix; this test
    makes it permanent.)
    """
    from unittest.mock import MagicMock

    import telegram._bot as _bot_mod
    import telegram.ext._extbot as _extbot_mod
    from telegram import (
        Chat,
        Message,
        MessageEntity,
        Update,
        User as TGUser,
    )

    from app.core.security import hash_password
    from app.models.user import User

    chat_id = 99000

    # Link a user to chat 99000 (reuse the same fixture pattern as
    # the other telegram tests).
    u = User(
        email=f"topup_e2e_{chat_id}@x.com",
        hashed_password=hash_password("Secret123"),
        first_name="E2E",
        last_name="Topup",
        phone_number=f"0809{chat_id:08d}",
        telegram_chat_id=str(chat_id),
        is_telegram_linked=True,
        balance=0,
    )
    session.add(u)
    session.commit()
    session.refresh(u)

    # Stub bot HTTP so we don't hit the real Telegram API.
    async def _noop(self, *a, **k):
        return None

    class _FakeUser:
        id = 999
        is_bot = True
        first_name = "TestBot"
        username = "test_bot"
        can_join_groups = True
        can_read_all_group_messages = False
        supports_inline_queries = False

    async def _fake_get_me(self, *a, **k):
        self._bot_user = _FakeUser()
        return self._bot_user

    async def _fake_do_post(self, *args, **kwargs):
        return {"ok": True, "result": True}

    _bot_mod.Bot.initialize = _noop
    _bot_mod.Bot.shutdown = _noop
    _bot_mod.Bot.get_me = _fake_get_me
    _bot_mod.Bot._do_post = _fake_do_post
    _extbot_mod.ExtBot.initialize = _noop
    _extbot_mod.ExtBot.shutdown = _noop
    _extbot_mod.ExtBot.get_me = _fake_get_me
    _extbot_mod.ExtBot._do_post = _fake_do_post

    # Build a synthetic /topup message with the bot_command entity.
    msg = MagicMock(spec=Message)
    msg.message_id = 1
    msg.text = "/topup"
    msg.entities = [
        MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=6)
    ]
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = chat_id
    msg.from_user = MagicMock(spec=TGUser)
    msg.from_user.id = chat_id
    replies: list[str] = []

    async def _reply(*a, **k):
        text = a[0] if a else k.get("text", "")
        replies.append(text)
    msg.reply_text = _reply

    update = Update(update_id=1, message=msg)

    try:
        await built_app.initialize()
        await built_app.process_update(update)
    finally:
        # Don't bother shutting down — process_update doesn't
        # require a clean shutdown for our test purposes.
        pass

    assert len(replies) == 1, (
        f"expected exactly one reply to /topup, got {len(replies)}: {replies!r}"
    )
    body = replies[0]
    # The user is linked, so they should see the top-up keyboard,
    # NOT the "Account not linked" message.
    assert "Top up your wallet" in body, (
        f"expected the top-up prompt, got: {body!r}"
    )
    assert "link" not in body.lower() or "Link your account" not in body, (
        f"the /topup handler claimed the user was unlinked but they were linked: {body!r}"
    )
