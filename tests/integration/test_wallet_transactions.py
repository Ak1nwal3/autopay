"""Tests for `GET /api/v1/wallet/transactions`."""
from __future__ import annotations

import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select as sa_select

from app.core.database import session_scope
from app.models.transaction import Transaction
from app.models.enums import TransactionStatus, TransactionType


def _signup(c: TestClient) -> tuple[str, int]:
    """Signup a new user via the API and return (access_token, user_id)."""
    nonce = int(time.time() * 1000)
    email = f"txhist_{nonce}@x.com"
    phone = f"+1{str(nonce)[-10:]}"
    r = c.post(
        "/api/v1/auth/signup",
        json={
            "first_name": "T",
            "last_name": "X",
            "email": email,
            "phone_number": phone,
            "password": "Secret123",
        },
    )
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    # Look up the user id.
    from app.models.user import User
    with session_scope() as s:
        u = s.execute(sa_select(User).where(User.email == email)).scalar_one()
        user_id = u.id
    return token, user_id


def _insert_txn(user_id: int, type_: str, amount: str, status: str, **kw) -> int:
    """Insert a transaction row for `user_id` and return its id."""
    with session_scope() as s:
        t = Transaction(
            user_id=user_id,
            type=type_,
            amount=Decimal(amount),
            fee=Decimal(str(kw.get("fee", 0))),
            currency="NGN",
            status=status,
            provider=kw.get("provider", "nomba"),
            provider_reference=kw.get("provider_reference"),
            narration=kw.get("narration"),
        )
        s.add(t)
        s.flush()
        tid = t.id
        s.commit()  # session_scope() doesn't auto-commit on exit
    return tid


def test_list_transactions_requires_auth() -> None:
    """No Bearer token → 401."""
    from app.main import app
    c = TestClient(app)
    r = c.get("/api/v1/wallet/transactions")
    assert r.status_code == 401


def test_list_transactions_returns_empty_for_new_user() -> None:
    from app.main import app
    c = TestClient(app)
    token, _ = _signup(c)
    r = c.get(
        "/api/v1/wallet/transactions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json() == []


def test_list_transactions_returns_user_rows_newest_first() -> None:
    from app.main import app
    c = TestClient(app)
    token, user_id = _signup(c)
    # Insert 3 transactions: a credit, a debit, a refund (failed).
    _insert_txn(user_id, "credit", "5000.00", "success",
                narration="Top-up", provider_reference="t1")
    _insert_txn(user_id, "debit",  "1500.00", "success",
                narration="DSTV", provider_reference="d1")
    _insert_txn(user_id, "debit",  "2000.00", "failed",
                narration="MTN", provider_reference="d2",
                failure_reason="transfer_failed")

    r = c.get(
        "/api/v1/wallet/transactions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 3
    # Newest first (created_at desc); since we inserted back-to-back
    # the LAST inserted is the FIRST returned. All are in the same
    # second so we just check the order matches insertion reverse.
    refs = [row["provider_reference"] for row in rows]
    assert refs == ["d2", "d1", "t1"]
    # Shape check — the schema fields are all there
    for row in rows:
        assert "id" in row
        assert "type" in row
        assert "amount" in row
        assert "fee" in row
        assert "currency" in row
        assert "status" in row
        assert "narration" in row
        assert "created_at" in row


def test_list_transactions_filter_by_type() -> None:
    from app.main import app
    c = TestClient(app)
    token, user_id = _signup(c)
    _insert_txn(user_id, "credit", "5000.00", "success",
                provider_reference="t1")
    _insert_txn(user_id, "debit",  "1500.00", "success",
                provider_reference="d1")
    _insert_txn(user_id, "credit", "2000.00", "success",
                provider_reference="t2")

    # Credits only
    r = c.get(
        "/api/v1/wallet/transactions?type=credit",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert all(row["type"] == "credit" for row in rows)

    # Debits only
    r = c.get(
        "/api/v1/wallet/transactions?type=debit",
        headers={"Authorization": f"Bearer {token}"},
    )
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["type"] == "debit"


def test_list_transactions_limit() -> None:
    from app.main import app
    c = TestClient(app)
    token, user_id = _signup(c)
    for i in range(5):
        _insert_txn(user_id, "credit", "100.00", "success",
                    provider_reference=f"t{i}")

    r = c.get(
        "/api/v1/wallet/transactions?limit=2",
        headers={"Authorization": f"Bearer {token}"},
    )
    rows = r.json()
    assert len(rows) == 2


def test_list_transactions_only_returns_caller_rows() -> None:
    """User A can't see User B's transactions — the WHERE clause
    filters by `user_id = current_user.id`."""
    from app.main import app
    c = TestClient(app)
    token_a, user_id_a = _signup(c)
    # User A has 1 txn
    _insert_txn(user_id_a, "credit", "100.00", "success",
                provider_reference="a1")
    # Sign up user B and give them a txn too
    token_b, user_id_b = _signup(c)
    _insert_txn(user_id_b, "credit", "200.00", "success",
                provider_reference="b1")

    r = c.get(
        "/api/v1/wallet/transactions",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["provider_reference"] == "a1"

    r = c.get(
        "/api/v1/wallet/transactions",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["provider_reference"] == "b1"
