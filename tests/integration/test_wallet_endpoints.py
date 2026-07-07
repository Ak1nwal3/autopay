"""Integration tests for the wallet endpoints (DVA provisioning)."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.database import session_scope as _session_scope
from app.models.virtual_account import VirtualAccount


def _signup(client: TestClient, email: str = "wallet@x.com", phone: str = "08090000001") -> str:
    s = client.post(
        "/api/v1/auth/signup",
        json={
            "first_name": "Wallet",
            "last_name": "Tester",
            "email": email,
            "phone_number": phone,
            "password": "Secret123",
        },
    )
    assert s.status_code == 201, s.text
    return f"Bearer {s.json()['access_token']}"


def test_provision_virtual_account_creates_dva(
    client: TestClient, stub_provider, session: Session
) -> None:
    h = _signup(client)
    r = client.post("/api/v1/wallet/provision", headers={"Authorization": h})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["already_existed"] is False
    assert body["virtual_account"]["account_number"]
    assert body["virtual_account"]["provider"] == "nomba"
    # Stub was called for customer + DVA creation
    called = [c[0] for c in stub_provider.calls]
    assert "create_customer" in called
    assert "create_virtual_account" in called
    # DB row exists
    va = session.exec(select(VirtualAccount)).first()
    assert va is not None
    assert va.account_number == body["virtual_account"]["account_number"]


def test_provision_virtual_account_idempotent(
    client: TestClient, stub_provider
) -> None:
    h = _signup(client, email="idem@x.com", phone="08090000002")
    r1 = client.post("/api/v1/wallet/provision", headers={"Authorization": h})
    assert r1.status_code == 201
    first_body = r1.json()
    # second call should NOT hit the provider
    calls_after_first = len(stub_provider.calls)
    r2 = client.post("/api/v1/wallet/provision", headers={"Authorization": h})
    assert r2.status_code == 201
    second_body = r2.json()
    assert second_body["already_existed"] is True
    assert second_body["virtual_account"]["account_number"] == first_body["virtual_account"]["account_number"]
    assert len(stub_provider.calls) == calls_after_first


def test_provision_requires_auth(client: TestClient) -> None:
    r = client.post("/api/v1/wallet/provision")
    assert r.status_code == 401


# ── Telegram link-code ─────────────────────────────────────────────


def test_create_telegram_link_code(client: TestClient) -> None:
    h = _signup(client, email="tgcode@x.com", phone="08090000003")
    r = client.post("/api/v1/auth/telegram/link-code", headers={"Authorization": h})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["code"]) == 6
    assert "expires_at" in body


def test_create_telegram_link_code_requires_auth(client: TestClient) -> None:
    r = client.post("/api/v1/auth/telegram/link-code")
    assert r.status_code == 401


def test_unlink_telegram_clears_chat_id(
    client: TestClient, session: Session
) -> None:
    from app.core.security import hash_password
    from app.models.user import User
    from app.core.database import session_scope
    h = _signup(client, email="ulnk@x.com", phone="08090000004")
    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "ulnk@x.com")).first()
        u.telegram_chat_id = "12345"
        u.is_telegram_linked = True
        s.add(u)

    r = client.delete("/api/v1/auth/telegram/link", headers={"Authorization": h})
    assert r.status_code == 204

    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "ulnk@x.com")).first()
        assert u.telegram_chat_id is None
        assert u.is_telegram_linked is False


# ── Top-up via Checkout (no DVA) ─────────────────────────────────


def test_topup_returns_authorization_url(client: TestClient, stub_provider) -> None:
    """POST /wallet/topup returns a Paystack-hosted URL and persists a
    pending `Transaction` row that the webhook will look up on success."""
    from decimal import Decimal as _Decimal

    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType

    h = _signup(client, email="top1@x.com", phone="08090000011")
    r = client.post(
        "/api/v1/wallet/topup",
        json={"amount": 1500},
        headers={"Authorization": h},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # Pydantic serializes Decimal as a string in JSON
    assert _Decimal(str(body["amount"])) == _Decimal("1500")
    assert body["currency"] == "NGN"
    assert body["authorization_url"].startswith("https://checkout.nomba.com/")
    assert body["reference"].startswith("topup_")
    # The provider's initialize_topup was called
    called = [c[0] for c in stub_provider.calls]
    assert "initialize_topup" in called
    # A pending Transaction row was created
    with _session_scope() as s:
        txn = s.exec(
            select(Transaction).where(Transaction.id == body["transaction_id"])
        ).first()
        assert txn is not None
        assert txn.type == TransactionType.CREDIT.value
        assert txn.status == TransactionStatus.PENDING.value
        assert txn.provider_reference == body["reference"]


def test_topup_rejects_amount_below_minimum(
    client: TestClient, stub_provider
) -> None:
    h = _signup(client, email="top2@x.com", phone="08090000012")
    r = client.post(
        "/api/v1/wallet/topup",
        json={"amount": 50},  # MIN_TOPUP_NGN is 100
        headers={"Authorization": h},
    )
    assert r.status_code == 400
    assert "Minimum" in r.json()["detail"]
    # The provider was NOT called
    assert not any(c[0] == "initialize_topup" for c in stub_provider.calls)


def test_topup_rejects_amount_above_maximum(
    client: TestClient, stub_provider
) -> None:
    h = _signup(client, email="top3@x.com", phone="08090000013")
    r = client.post(
        "/api/v1/wallet/topup",
        json={"amount": 9_999_999},  # MAX_TOPUP_NGN is 1_000_000
        headers={"Authorization": h},
    )
    assert r.status_code == 400
    assert "Maximum" in r.json()["detail"]


def test_topup_requires_auth(client: TestClient) -> None:
    r = client.post("/api/v1/wallet/topup", json={"amount": 1000})
    assert r.status_code == 401


def test_topup_charge_success_webhook_credits_wallet(
    client: TestClient, stub_nomba_provider, session: Session
) -> None:
    """End-to-end: POST /topup → simulate Nomba's payment_success
    webhook → assert the user's wallet is credited and the
    Transaction status flips."""
    from app.core.config import get_settings
    from app.models.user import User
    from app.core.database import session_scope
    from tests.integration._webhook_helpers import (
        build_nomba_topup_payload,
        signed_nomba_post,
    )

    h = _signup(client, email="top4@x.com", phone="08090000014")

    # 1. Top-up: 2000 NGN
    r = client.post(
        "/api/v1/wallet/topup",
        json={"amount": 2000},
        headers={"Authorization": h},
    )
    assert r.status_code == 201
    reference = r.json()["reference"]

    # 2. Simulate Nomba's payment_success webhook
    payload = build_nomba_topup_payload(
        amount=2000,
        reference=reference,
    )
    wr = signed_nomba_post(
        client,
        payload=payload,
        secret=get_settings().nomba_webhook_secret,
    )
    assert wr.status_code == 200, wr.text

    # 3. Wallet is credited
    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "top4@x.com")).first()
        from decimal import Decimal
        assert Decimal(str(u.balance)) == Decimal("2000.00")


# ── POST /wallet/topup/verify (webhook-agnostic manual fallback) ─


def test_verify_topup_credits_pending_transaction(
    client: TestClient, stub_provider, session: Session
) -> None:
    """A pending CREDIT transaction + a successful get_transaction
    from the provider → the verify endpoint credits the wallet and
    flips the transaction to SUCCESS."""
    from app.models.user import User
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from decimal import Decimal
    from datetime import datetime, UTC

    h = _signup(client, email="verify1@x.com", phone="08090000020")

    # 1. Create a pending top-up directly in the DB (bypassing the
    # /topup endpoint so we don't have to set up a separate stub).
    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "verify1@x.com")).first()
        ref = "topup_99_deadbeef"
        txn = Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("500.00"),
            fee=Decimal("0.00"),
            currency="NGN",
            status=TransactionStatus.PENDING.value,
            provider="nomba",
            provider_reference=ref,
            narration="Top-up via Nomba Checkout",
            created_at=datetime.now(tz=UTC),
        )
        s.add(txn)
        s.commit()

    # 2. Call /verify
    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": ref},
        headers={"Authorization": h},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["credited"] is True
    assert body["status"] == "credited"
    assert Decimal(str(body["new_balance"])) == Decimal("500.00")

    # 3. The stub recorded the call
    calls = [c for c in stub_provider.calls if c[0] == "get_transaction"]
    assert len(calls) == 1
    assert calls[0][1]["reference"] == ref

    # 4. The transaction is now SUCCESS
    with _session_scope() as s:
        t = s.exec(
            select(Transaction).where(Transaction.provider_reference == ref)
        ).first()
        assert t.status == TransactionStatus.SUCCESS.value


def test_verify_topup_returns_already_credited(
    client: TestClient, stub_provider, session: Session
) -> None:
    """If the transaction is already SUCCESS (a webhook beat us),
    the verify endpoint returns a clean message without re-crediting."""
    from app.models.user import User
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from decimal import Decimal
    from datetime import datetime, UTC

    h = _signup(client, email="verify2@x.com", phone="08090000021")
    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "verify2@x.com")).first()
        ref = "topup_99_already"
        txn = Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("1000.00"),
            fee=Decimal("0.00"),
            currency="NGN",
            status=TransactionStatus.SUCCESS.value,  # already done
            provider="nomba",
            provider_reference=ref,
            narration="Top-up via Nomba Checkout",
            created_at=datetime.now(tz=UTC),
        )
        s.add(txn)
        s.commit()

    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": ref},
        headers={"Authorization": h},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["credited"] is False
    assert body["status"] == "already_credited"
    # Balance is unchanged (was 0 at signup).
    assert Decimal(str(body["new_balance"])) == Decimal("0.00")


def test_verify_topup_rejects_unknown_reference(
    client: TestClient, stub_provider, session: Session
) -> None:
    """A reference that doesn't match any of the caller's pending
    transactions returns 200 with a clear 'couldn't find' status
    (NOT a 404 — the caller asked us to verify something, we
    return what we found, which is nothing)."""
    h = _signup(client, email="verify3@x.com", phone="08090000022")
    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": "topup_999_nonexistent"},
        headers={"Authorization": h},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["credited"] is False
    assert body["status"] == "unknown_reference"
    # The provider's get_transaction is NOT called for unknown refs
    # (we check ownership first).
    get_calls = [c for c in stub_provider.calls if c[0] == "get_transaction"]
    assert len(get_calls) == 0


def test_verify_topup_rejects_other_users_reference(
    client: TestClient, stub_provider, session: Session
) -> None:
    """A user can only verify their OWN references. A reference
    belonging to another user returns 'unknown_reference' without
    leaking that the reference exists."""
    from app.models.user import User
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from decimal import Decimal
    from datetime import datetime, UTC

    h_alice = _signup(client, email="alice@x.com", phone="08090000023")
    h_bob = _signup(client, email="bob@x.com", phone="08090000024")

    # Alice creates a pending top-up.
    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "alice@x.com")).first()
        ref = "topup_alice_secret"
        s.add(Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("100.00"),
            fee=Decimal("0.00"),
            currency="NGN",
            status=TransactionStatus.PENDING.value,
            provider="nomba",
            provider_reference=ref,
            narration="Top-up via Nomba Checkout",
            created_at=datetime.now(tz=UTC),
        ))
        s.commit()

    # Bob tries to verify Alice's reference.
    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": ref},
        headers={"Authorization": h_bob},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["credited"] is False
    assert body["status"] == "unknown_reference"
    # And Alice's top-up is still pending (Bob didn't touch it).
    with _session_scope() as s:
        t = s.exec(
            select(Transaction).where(Transaction.provider_reference == ref)
        ).first()
        assert t.status == TransactionStatus.PENDING.value


def test_verify_topup_returns_provider_pending(
    client: TestClient, stub_provider, session: Session
) -> None:
    """When the provider says the transaction is still pending,
    the verify endpoint returns a friendly 'still pending' message
    and does not credit the wallet."""
    from app.models.user import User
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from app.services.payments.base import TransactionStatusResult
    from decimal import Decimal
    from datetime import datetime, UTC

    h = _signup(client, email="verify4@x.com", phone="08090000025")
    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "verify4@x.com")).first()
        ref = "topup_99_pending"
        s.add(Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("250.00"),
            fee=Decimal("0.00"),
            currency="NGN",
            status=TransactionStatus.PENDING.value,
            provider="nomba",
            provider_reference=ref,
            narration="Top-up via Nomba Checkout",
            created_at=datetime.now(tz=UTC),
        ))
        s.commit()

    # Override the stub to return 'pending'.
    async def _pending_get_transaction(**kwargs):
        return TransactionStatusResult(
            provider_reference=kwargs["reference"],
            status="pending",
            amount_kobo=None,
            raw={"status": "PENDING_BILLING"},
        )
    stub_provider.get_transaction = _pending_get_transaction  # type: ignore[method-assign]

    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": ref},
        headers={"Authorization": h},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["credited"] is False
    assert body["status"] == "provider_pending"
    # Wallet NOT credited.
    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "verify4@x.com")).first()
        assert Decimal(str(u.balance)) == Decimal("0.00")


def test_verify_topup_requires_auth(client: TestClient) -> None:
    """The verify endpoint requires a valid JWT (no anonymous calls)."""
    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": "topup_1_anything"},
    )
    assert r.status_code == 401


# ── Reference-mismatch regression (Phase 2.1 fix) ────────────────
# Nomba's Checkout generates its own `orderReference` (a UUID)
# that is DIFFERENT from our internal reference. The bug: we
# were sending the user Nomba's UUID but the DB only had our
# internal reference, so `verify_pending_topup` looked up by
# the UUID and got `None` → "couldn't find that top-up".
#
# Fix: store BOTH on the transaction row. The user gets our
# internal reference, the verify endpoint uses the provider's
# reference when calling the provider's transaction-status API.
# Look up by EITHER column so either one works.


def test_verify_topup_works_when_provider_returns_different_reference(
    client: TestClient, stub_provider, session: Session
) -> None:
    """REGRESSION: when Nomba returns a different `orderReference`
    than our internal reference, the user can still verify with
    our reference and the credit is applied.

    Setup: store the transaction with `provider_reference` = our
    internal ID and `provider_order_reference` = Nomba's UUID.
    Then call /verify with our internal reference. The endpoint
    should match by `provider_reference` and pass the provider's
    reference to `provider.get_transaction`."""
    from app.models.user import User
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from decimal import Decimal
    from datetime import datetime, UTC

    h = _signup(client, email="mismatch1@x.com", phone="08090000030")

    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "mismatch1@x.com")).first()
        internal_ref = "topup_99_mismatch"
        provider_ref = "nomba-uuid-7a8b-9c0d-1e2f-3a4b-5c6d-7e8f-9a0b"
        txn = Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("750.00"),
            fee=Decimal("0.00"),
            currency="NGN",
            status=TransactionStatus.PENDING.value,
            provider="nomba",  # doesn't matter for the test
            provider_reference=internal_ref,
            provider_order_reference=provider_ref,  # the bug
            narration="Top-up via Nomba Checkout",
            created_at=datetime.now(tz=UTC),
        )
        s.add(txn)
        s.commit()

    # Call /verify with OUR internal reference (what the user has).
    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": internal_ref},
        headers={"Authorization": h},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["credited"] is True
    assert body["status"] == "credited"
    assert Decimal(str(body["new_balance"])) == Decimal("750.00")

    # Critical: the stub's get_transaction was called with the
    # PROVIDER's reference (Nomba's UUID), not our internal one.
    # This is what the provider's API needs.
    get_calls = [c for c in stub_provider.calls if c[0] == "get_transaction"]
    assert len(get_calls) == 1
    assert get_calls[0][1]["reference"] == provider_ref

    # Transaction is now SUCCESS.
    with _session_scope() as s:
        t = s.exec(
            select(Transaction).where(Transaction.provider_reference == internal_ref)
        ).first()
        assert t.status == TransactionStatus.SUCCESS.value


def test_verify_topup_works_with_provider_reference(
    client: TestClient, stub_provider, session: Session
) -> None:
    """Symmetric to the regression above: the user can also
    verify with the provider's reference (Nomba's UUID) and it
    should still find the right transaction and credit it."""
    from app.models.user import User
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from decimal import Decimal
    from datetime import datetime, UTC

    h = _signup(client, email="mismatch2@x.com", phone="08090000031")

    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "mismatch2@x.com")).first()
        internal_ref = "topup_99_alt"
        provider_ref = "nomba-uuid-aaaa-bbbb-cccc-dddd-eeee-ffff"
        s.add(Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("1000.00"),
            fee=Decimal("0.00"),
            currency="NGN",
            status=TransactionStatus.PENDING.value,
            provider="nomba",
            provider_reference=internal_ref,
            provider_order_reference=provider_ref,
            narration="Top-up via Nomba Checkout",
            created_at=datetime.now(tz=UTC),
        ))
        s.commit()

    # Call /verify with the PROVIDER's reference.
    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": provider_ref},
        headers={"Authorization": h},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["credited"] is True
    assert Decimal(str(body["new_balance"])) == Decimal("1000.00")

    # The stub's get_transaction was called with the provider's
    # reference (consistent: this is what the provider needs).
    get_calls = [c for c in stub_provider.calls if c[0] == "get_transaction"]
    assert len(get_calls) == 1
    assert get_calls[0][1]["reference"] == provider_ref


def test_verify_topup_works_when_provider_order_reference_is_null(
    client: TestClient, stub_provider, session: Session
) -> None:
    """Legacy transactions (created before the
    `provider_order_reference` column existed) have NULL in that
    column. The verify endpoint falls back to our internal
    reference for both the lookup AND the provider API call."""
    from app.models.user import User
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from decimal import Decimal
    from datetime import datetime, UTC

    h = _signup(client, email="legacy@x.com", phone="08090000032")

    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "legacy@x.com")).first()
        ref = "topup_99_legacy"
        s.add(Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("300.00"),
            fee=Decimal("0.00"),
            currency="NGN",
            status=TransactionStatus.PENDING.value,
            provider="nomba",
            provider_reference=ref,
            provider_order_reference=None,  # legacy row
            narration="Top-up via Nomba Checkout",
            created_at=datetime.now(tz=UTC),
        ))
        s.commit()

    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": ref},
        headers={"Authorization": h},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["credited"] is True

    # No provider_order_reference → get_transaction is called
    # with our internal reference (Paystack is happy with this).
    get_calls = [c for c in stub_provider.calls if c[0] == "get_transaction"]
    assert len(get_calls) == 1
    assert get_calls[0][1]["reference"] == ref


def test_verify_topup_refuses_amount_mismatch(
    client: TestClient, stub_provider, session: Session
) -> None:
    """REGRESSION (bug: "always credited with 4000"): when the
    provider reports a successful status but an amount that does
    NOT match the locally-stored `Transaction.amount` (the classic
    Nomba-sandbox behaviour of returning 4000 for every txn), the
    verify endpoint must REFUSE to credit, mark the txn FAILED, and
    return status="amount_mismatch".

    Before the fix the code credited `result.amount_kobo` blindly,
    so every top-up was credited with 4000 even if the user
    intended to pay a different amount (or never paid at all)."""
    from app.models.user import User
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from decimal import Decimal
    from datetime import datetime, UTC

    h = _signup(client, email="mismatch3@x.com", phone="08090000040")

    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "mismatch3@x.com")).first()
        ref = "topup_99_divergence"
        s.add(Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("500.00"),  # user intended to pay 500
            fee=Decimal("0.00"),
            currency="NGN",
            status=TransactionStatus.PENDING.value,
            provider="nomba",
            provider_reference=ref,
            narration="Top-up via Nomba Checkout",
            created_at=datetime.now(tz=UTC),
        ))
        s.commit()

    # Stub mimics the Nomba sandbox: success + a FIXED 4000 NGN
    # amount regardless of what the user actually top-up'd.
    stub_provider.get_transaction_status = "success"
    stub_provider.get_transaction_amount_kobo = 400_000  # 4000 NGN

    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": ref},
        headers={"Authorization": h},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["credited"] is False
    assert body["status"] == "amount_mismatch"

    # Wallet was NOT credited.
    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "mismatch3@x.com")).first()
        assert Decimal(str(u.balance)) == Decimal("0.00")
        t = s.exec(
            select(Transaction).where(Transaction.provider_reference == ref)
        ).first()
        # Txn is now FAILED so it can't be re-verified into a credit.
        assert t.status == TransactionStatus.FAILED.value
        assert t.failure_reason == "amount_mismatch"


def test_verify_topup_credits_txn_amount_not_provider_amount(
    client: TestClient, stub_provider, session: Session
) -> None:
    """REGRESSION (bug: "always credited with 4000"): when the
    provider returns status=success with amount_kobo=None (the
    stub default, mirroring a provider that omits the amount
    field), the verify endpoint credits `Transaction.amount` (what
    the user agreed to pay), NOT 0 and NOT a hard-coded default."""
    from app.models.user import User
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from decimal import Decimal
    from datetime import datetime, UTC

    h = _signup(client, email="amtlocal@x.com", phone="08090000042")

    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "amtlocal@x.com")).first()
        ref = "topup_99_local_amt"
        s.add(Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("750.00"),  # arbitrary, not 4000
            fee=Decimal("0.00"),
            currency="NGN",
            status=TransactionStatus.PENDING.value,
            provider="nomba",
            provider_reference=ref,
            narration="Top-up via Nomba Checkout",
            created_at=datetime.now(tz=UTC),
        ))
        s.commit()

    # Default stub: success, amount_kobo=None → fall back to txn.amount.
    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": ref},
        headers={"Authorization": h},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["credited"] is True
    # Credited with the LOCAL amount (750), not 0 and not 4000.
    assert Decimal(str(body["new_balance"])) == Decimal("750.00")


def test_verify_topup_matching_amount_credits_txn_amount(
    client: TestClient, stub_provider, session: Session
) -> None:
    """When the provider returns a successful status AND an amount
    that matches `Transaction.amount`, the verify endpoint credits
    `Transaction.amount` (which equals the provider's amount here)."""
    from app.models.user import User
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from decimal import Decimal
    from datetime import datetime, UTC

    h = _signup(client, email="matchamt@x.com", phone="08090000043")

    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "matchamt@x.com")).first()
        ref = "topup_99_match_amt"
        s.add(Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("1000.00"),
            fee=Decimal("0.00"),
            currency="NGN",
            status=TransactionStatus.PENDING.value,
            provider="nomba",
            provider_reference=ref,
            narration="Top-up via Nomba Checkout",
            created_at=datetime.now(tz=UTC),
        ))
        s.commit()

    # Provider returns the MATCHING amount (1000 NGN = 100_000 kobo).
    stub_provider.get_transaction_status = "success"
    stub_provider.get_transaction_amount_kobo = 100_000

    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": ref},
        headers={"Authorization": h},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["credited"] is True
    assert Decimal(str(body["new_balance"])) == Decimal("1000.00")


def test_verify_topup_already_failed_is_noop(
    client: TestClient, stub_provider, session: Session
) -> None:
    """A transaction previously marked FAILED (e.g. by an amount
    mismatch) must NOT be re-verified into a credit. The endpoint
    returns status="already_failed" without calling the provider."""
    from app.models.user import User
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from decimal import Decimal
    from datetime import datetime, UTC

    h = _signup(client, email="failedtxn@x.com", phone="08090000044")

    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "failedtxn@x.com")).first()
        ref = "topup_99_already_failed"
        s.add(Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("300.00"),
            fee=Decimal("0.00"),
            currency="NGN",
            status=TransactionStatus.FAILED.value,  # already failed
            provider="nomba",
            provider_reference=ref,
            failure_reason="amount_mismatch",
            narration="Top-up via Nomba Checkout",
            created_at=datetime.now(tz=UTC),
        ))
        s.commit()

    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": ref},
        headers={"Authorization": h},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["credited"] is False
    assert body["status"] == "already_failed"

    # Provider was NOT called (terminal state short-circuits).
    get_calls = [c for c in stub_provider.calls if c[0] == "get_transaction"]
    assert len(get_calls) == 0

    # Balance unchanged.
    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "failedtxn@x.com")).first()
        assert Decimal(str(u.balance)) == Decimal("0.00")


def test_start_topup_returns_internal_reference(
    client: TestClient, stub_provider, session: Session
) -> None:
    """When the provider (Paystack stub) returns a different
    `init.reference` than what we sent, the topup endpoint
    returns our INTERNAL reference, not the provider's.

    This way the user gets a consistent reference to remember
    and the verify endpoint can match it against the DB."""
    r = client.post(
        "/api/v1/wallet/topup",
        json={"amount": 200},
        headers={"Authorization": _signup(
            client, email="start1@x.com", phone="08090000033"
        )},
    )
    assert r.status_code == 201
    body = r.json()
    ref = body["reference"]

    # Our internal refs are formatted as `topup_<user_id>_<hex>`.
    # The Paystack stub echoes back our reference, so the
    # returned value should match our internal format.
    assert ref.startswith("topup_")

    # The DB row should have `provider_reference == ref` and
    # `provider_order_reference == ref` (Paystack echoes).
    from app.models.user import User
    from app.models.transaction import Transaction
    from decimal import Decimal
    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "start1@x.com")).first()
        t = s.exec(
            select(Transaction).where(Transaction.user_id == u.id)
        ).first()
        assert t.provider_reference == ref
        # For Paystack (which echoes our reference), both columns
        # should match.
        assert t.provider_order_reference == ref


def test_start_topup_defaults_callback_url(
    client: TestClient, stub_provider, session: Session
) -> None:
    """When the client doesn't pass a callback_url, the server
    defaults to {nomba_callback_url}/webhooks/nomba so the Nomba
    sandbox knows where to fire webhooks.

    Without this default, the checkout order is created with
    callbackUrl="" and the webhook never fires."""
    r = client.post(
        "/api/v1/wallet/topup",
        json={"amount": 200},  # no callback_url
        headers={"Authorization": _signup(
            client, email="cbdefault@x.com", phone="08090000070"
        )},
    )
    assert r.status_code == 201, r.text

    # The stub recorded the initialize_topup call; check the
    # callback_url that was passed.
    init_calls = [c for c in stub_provider.calls if c[0] == "initialize_topup"]
    assert len(init_calls) == 1
    cb = init_calls[0][1].get("callback_url") or ""
    assert cb.endswith("/webhooks/nomba"), (
        f"Expected callback_url to end with /webhooks/nomba, got {cb!r}"
    )


def test_start_topup_stores_provider_order_reference_when_different(
    client: TestClient, stub_provider, session: Session
) -> None:
    """Override the stub's `initialize_topup` to return a
    DIFFERENT reference (simulating Nomba's behavior). The DB
    row should store BOTH references correctly, and the API
    response should return the internal one."""
    from app.services.payments.base import TopupInit

    async def _different_init(**kwargs):
        # Return a different reference than the one we sent.
        return TopupInit(
            authorization_url="https://checkout.nomba.com/test-uuid",
            reference="nomba-uuid-1234-5678-9abc-def0",
            provider="nomba",
            access_code=None,
        )
    stub_provider.initialize_topup = _different_init  # type: ignore[method-assign]

    r = client.post(
        "/api/v1/wallet/topup",
        json={"amount": 400},
        headers={"Authorization": _signup(
            client, email="start2@x.com", phone="08090000034"
        )},
    )
    assert r.status_code == 201
    body = r.json()
    returned_ref = body["reference"]

    # Returned reference should be our internal one, not Nomba's.
    assert returned_ref.startswith("topup_")

    from app.models.user import User
    from app.models.transaction import Transaction
    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "start2@x.com")).first()
        t = s.exec(
            select(Transaction).where(Transaction.user_id == u.id)
        ).first()
        assert t.provider_reference == returned_ref
        # The provider's reference is stored separately.
        assert t.provider_order_reference == "nomba-uuid-1234-5678-9abc-def0"


# ── Reference-mismatch regression tests (Nomba bug) ──────────────


def test_verify_topup_works_when_provider_returns_different_reference(
    client: TestClient, stub_provider, session: Session
) -> None:
    """REGRESSION TEST for the "couldn't find that top-up" bug.

    Nomba generates its own `orderReference` UUID for each hosted
    Checkout, different from the `topup_<user>_<hex>` reference we
    mint internally. The user-facing flow returns our internal
    reference (which they remember), but the provider's API needs
    the provider's orderReference. This test verifies the lookup
    works when the user passes the INTERNAL reference and the
    transaction row stores a DIFFERENT provider_order_reference.
    """
    from app.models.user import User
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from decimal import Decimal
    from datetime import datetime, UTC

    h = _signup(client, email="regress1@x.com", phone="08090000030")
    internal_ref = "topup_99_internal_abc"
    provider_ref = "nomba-uuid-xyz-123"  # what Nomba generated

    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "regress1@x.com")).first()
        txn = Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("750.00"),
            fee=Decimal("0.00"),
            currency="NGN",
            status=TransactionStatus.PENDING.value,
            provider="nomba",
            provider_reference=internal_ref,
            provider_order_reference=provider_ref,  # different from internal
            narration="Top-up via nomba Checkout",
            created_at=datetime.now(tz=UTC),
        )
        s.add(txn)
        s.commit()

    # User passes the INTERNAL reference (what start_topup returned).
    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": internal_ref},
        headers={"Authorization": h},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["credited"] is True, (
        f"Expected credit to succeed. Body: {body}"
    )
    assert body["status"] == "credited"
    assert Decimal(str(body["new_balance"])) == Decimal("750.00")

    # The provider was called with the PROVIDER's reference
    # (because the provider's API needs `orderReference`).
    calls = [c for c in stub_provider.calls if c[0] == "get_transaction"]
    assert len(calls) == 1
    assert calls[0][1]["reference"] == provider_ref, (
        f"Expected get_transaction to be called with the provider's "
        f"reference {provider_ref!r}, got {calls[0][1]['reference']!r}"
    )

    # Transaction is now SUCCESS.
    with _session_scope() as s:
        t = s.exec(
            select(Transaction).where(Transaction.provider_reference == internal_ref)
        ).first()
        assert t.status == TransactionStatus.SUCCESS.value


def test_verify_topup_works_with_provider_reference_alternative(
    client: TestClient, stub_provider, session: Session
) -> None:
    """The lookup also succeeds when the user passes the PROVIDER's
    reference (e.g. they copied it from a Nomba dashboard). This
    makes the manual-verify UX robust to either reference the user
    has on hand."""
    from app.models.user import User
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from decimal import Decimal
    from datetime import datetime, UTC

    h = _signup(client, email="regress2@x.com", phone="08090000031")
    internal_ref = "topup_99_internal_def"
    provider_ref = "nomba-uuid-456"

    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "regress2@x.com")).first()
        s.add(Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("300.00"),
            fee=Decimal("0.00"),
            currency="NGN",
            status=TransactionStatus.PENDING.value,
            provider="nomba",
            provider_reference=internal_ref,
            provider_order_reference=provider_ref,
            narration="Top-up via nomba Checkout",
            created_at=datetime.now(tz=UTC),
        ))
        s.commit()

    # User passes the PROVIDER's reference (different field).
    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": provider_ref},
        headers={"Authorization": h},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["credited"] is True
    assert body["status"] == "credited"
    assert Decimal(str(body["new_balance"])) == Decimal("300.00")


def test_verify_topup_falls_back_to_internal_reference_when_provider_ref_null(
    client: TestClient, stub_provider, session: Session
) -> None:
    """Legacy transactions (created before the
    `provider_order_reference` column existed) have NULL for that
    field. The verify endpoint must fall back to `provider_reference`
    when calling the provider's API."""
    from app.models.user import User
    from app.models.transaction import Transaction
    from app.models.enums import TransactionStatus, TransactionType
    from decimal import Decimal
    from datetime import datetime, UTC

    h = _signup(client, email="regress3@x.com", phone="08090000032")
    internal_ref = "topup_99_legacy"

    with _session_scope() as s:
        u = s.exec(select(User).where(User.email == "regress3@x.com")).first()
        s.add(Transaction(
            user_id=u.id,
            type=TransactionType.CREDIT.value,
            amount=Decimal("100.00"),
            fee=Decimal("0.00"),
            currency="NGN",
            status=TransactionStatus.PENDING.value,
            provider="nomba",  # Paystack echoes our ref back
            provider_reference=internal_ref,
            provider_order_reference=None,  # legacy row
            narration="Top-up via Nomba Checkout",
            created_at=datetime.now(tz=UTC),
        ))
        s.commit()

    r = client.post(
        "/api/v1/wallet/topup/verify",
        json={"reference": internal_ref},
        headers={"Authorization": h},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["credited"] is True
    # The provider was called with the internal reference
    # (fallback because provider_order_reference is NULL).
    calls = [c for c in stub_provider.calls if c[0] == "get_transaction"]
    assert len(calls) == 1
    assert calls[0][1]["reference"] == internal_ref


def test_start_topup_returns_internal_reference_not_provider_reference(
    client: TestClient, stub_provider, session: Session
) -> None:
    """`POST /wallet/topup` should return the user's INTERNAL
    reference in the response (not the provider's, which may be a
    UUID and unfriendly to remember). The provider's reference is
    stored on the transaction for the verify/scheduler paths."""
    from app.models.transaction import Transaction
    from decimal import Decimal
    from datetime import datetime, UTC

    # Override the stub to return a different reference from the
    # one we send (simulating Nomba's UUID behavior).
    internal_ref = None
    async def _stub_initialize_topup(**kwargs):
        from app.services.payments.base import TopupInit
        stub_provider.calls.append(("initialize_topup", kwargs))
        return TopupInit(
            authorization_url="https://checkout.nomba.com/abc",
            reference="nomba-uuid-from-stub",  # different from what we sent
            provider="nomba",
            access_code=None,
        )
    stub_provider.initialize_topup = _stub_initialize_topup  # type: ignore[method-assign]

    h = _signup(client, email="regress4@x.com", phone="08090000033")
    r = client.post(
        "/api/v1/wallet/topup",
        json={"amount": 200},
        headers={"Authorization": h},
    )
    assert r.status_code == 201
    body = r.json()
    # The returned reference should be OURS (starting with `topup_`),
    # not the provider's UUID.
    returned_ref = body["reference"]
    assert returned_ref.startswith("topup_"), (
        f"Expected internal reference starting with 'topup_', "
        f"got {returned_ref!r}"
    )
    assert returned_ref != "nomba-uuid-from-stub"

    # The provider's reference is stored on the transaction row.
    with _session_scope() as s:
        t = s.exec(
            select(Transaction).where(Transaction.provider_reference == returned_ref)
        ).first()
        assert t is not None
        assert t.provider_order_reference == "nomba-uuid-from-stub"
