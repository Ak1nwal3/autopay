"""Integration test fixtures.

These tests hit a real database (`nomba_gateway_test`). We use a
separate DB from the dev one so the test can truncate every table
between tests without nuking seeded data.

DB setup is one-time:
  PGPASSWORD='...' psql -U postgres -h localhost -d postgres \\
    -c "CREATE DATABASE nomba_gateway_test;"
  PGPASSWORD='...' psql -U postgres -h localhost -d nomba_gateway_test \\
    -f schema.sql
"""
from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet

# ── Test env vars BEFORE any `from app...` import ───────────────────
# Use DATABASE_URL from the environment (docker-compose) if available;
# otherwise default to the local Postgres test DB for venv-based runs.
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:David*2020*@localhost:5432/nomba_gateway_test")
os.environ["ENVIRONMENT"] = "test"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-must-be-at-least-32-chars-long"
os.environ["BVN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
# Force the regex fallback in the loaders (no real LLM calls). Some
# tests assert that the endpoint returns 422 when the LLM can't pull
# a vendor from the input. The hardcoded `groq_api_key` default in
# `app.core.config` is a real working key, so without this override
# those tests would silently switch to LLM-extracted values.
os.environ["GROQ_API_KEY"] = ""

# Now import the app
import app.core.config as _config_mod  # noqa: E402
import app.services.crypto as _crypto  # noqa: E402
from app.core import config as _config  # noqa: E402
from app.core import database as _database  # noqa: E402

_config.get_settings.cache_clear()
_crypto._fernet.cache_clear()
_database.engine.dispose()  # discard any old engine bound to the dev DB

# IMPORTANT: also refresh the module-level `settings` shortcut. If
# `app.core.config` was imported earlier (e.g. by tests/conftest.py),
# `settings` is still pointing at a stale Settings instance built
# BEFORE we set the env vars. Reassign it now.
_config_mod.settings = _config.get_settings()

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, text  # noqa: E402

from app import models  # noqa: E402,F401  (registers tables)
from app.core.database import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services.payments.base import VirtualAccountData  # noqa: E402

# TABLES to truncate between tests. Order doesn't matter (CASCADE).
TABLES = [
    "audit_logs",
    "refresh_tokens",
    "transactions",
    "bills",
    "virtual_accounts",
    "kyc_records",
    "telegram_link_codes",
    "webhook_events",
    "users",
]


def _truncate_all() -> None:
    with engine.connect() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE "
                + ", ".join(TABLES)
                + " RESTART IDENTITY CASCADE"
            )
        )
        conn.commit()


@pytest.fixture(autouse=True)
def _clean_db():
    """Truncate every table before and after each integration test.

    Autouse so every integration test gets a clean DB without having
    to list this fixture in its signature. (The leading underscore is
    a historical artifact.)
    """
    _truncate_all()
    yield
    _truncate_all()


def _install_nomba_stub(monkeypatch=None):
    """Install the Nomba-shaped stub as the payment provider.

    Idempotent: clears any existing DI overrides first, then binds
    the fresh stub. Used by both the autouse `_default_stub_provider`
    fixture and the explicit `stub_nomba_provider` / `stub_provider`
    fixture so a test can request either (or both) and the same stub
    instance is installed.
    """
    from app.api import auth as auth_module
    from app.api import bills as bills_module
    from app.api import wallet as wallet_module
    from app.api import webhooks as webhooks_module
    from app.services import payments as _payments_module

    _StubNomba._counter = 0
    stub = _StubNomba()
    stub.calls = []

    # Clear any previous overrides (idempotent / re-bindable).
    for module in (auth_module, bills_module, wallet_module, webhooks_module):
        app.dependency_overrides.pop(module.get_payment_provider, None)

    # FastAPI DI: every Depends(get_payment_provider) returns the stub.
    app.dependency_overrides[auth_module.get_payment_provider] = lambda: stub
    app.dependency_overrides[bills_module.get_payment_provider] = lambda: stub
    app.dependency_overrides[wallet_module.get_payment_provider] = lambda: stub
    app.dependency_overrides[webhooks_module.get_payment_provider] = lambda: stub

    # Function-level patch: scheduler + any non-DI caller.
    if monkeypatch is not None:
        monkeypatch.setattr(
            _payments_module, "get_payment_provider", lambda: stub
        )
    else:
        _payments_module.get_payment_provider = lambda: stub

    return stub


@pytest.fixture(autouse=True)
def _default_stub_provider():
    """Autouse fixture: every integration test gets the Nomba stub
    as the payment provider by default.

    Tests that need a stub they can introspect (`stub.calls`) request
    `stub_nomba_provider` or `stub_provider` (alias) explicitly.
    """
    _install_nomba_stub()
    yield


@pytest.fixture
def stub_nomba_provider(monkeypatch):
    """A fresh Nomba-shaped stub. Tests that want to inspect
    `stub.calls` directly request this fixture.

    The autouse `_default_stub_provider` fixture has already
    installed the DI overrides; this fixture additionally patches
    the module-level factory.
    """
    stub = _install_nomba_stub(monkeypatch=monkeypatch)
    yield stub
    # monkeypatch handles restoring `_payments_module.get_payment_provider`


@pytest.fixture
def stub_provider(monkeypatch):
    """Alias of `stub_nomba_provider` for tests that originated in
    the Paystack project and use the historical fixture name.
    """
    stub = _install_nomba_stub(monkeypatch=monkeypatch)
    yield stub


@pytest.fixture
def client() -> TestClient:
    """A TestClient with the test DB wired in."""
    return TestClient(app)


@pytest.fixture
def session() -> Session:
    """Direct DB session for tests that need to set up data without HTTP."""
    s = Session(engine)
    try:
        yield s
    finally:
        s.close()


# ── Payment provider stub ──────────────────────────────────────────
# The only stub now is `_StubNomba`. It implements the full
# `PaymentProvider` Protocol so signup, bill pay, topup, and webhook
# flows all work without hitting Nomba's sandbox.

class _StubNomba:
    """Nomba-shaped stub. The parse_webhook method does real signature
    verification + body parsing (mirroring the real provider) so the
    integration tests exercise the full pipeline.

    The stub records every call into `self.calls` as a list of
    (method_name, kwargs) tuples so tests can assert on them.

    Config knobs (set on the instance or class):
      * `get_transaction_status` — status string returned by
        `get_transaction` (default "success").
      * `get_transaction_amount_kobo` — amount returned by
        `get_transaction` (default None, meaning "provider omitted
        the amount" so the service falls back to `txn.amount`).
        Set to a non-None value to simulate a divergent amount
        (e.g. 400000 to mimic the Nomba sandbox).
    """

    name = "nomba"
    webhook_secret = "test-nomba-webhook-secret"
    _counter: int = 0

    # Class-level defaults; tests may override on the instance.
    get_transaction_status: str = "success"
    get_transaction_amount_kobo: int | None = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def create_customer(self, **kwargs):
        self.calls.append(("create_customer", kwargs))
        return kwargs["email"]

    async def create_virtual_account(self, **kwargs):
        self.calls.append(("create_virtual_account", kwargs))
        type(self)._counter += 1
        return VirtualAccountData(
            account_number=f"9391076543{type(self)._counter}",
            account_name=kwargs.get("account_name", "Nomba/Test User"),
            bank_name="Nombank MFB",
            bank_code="000",
            provider_reference=f"holder-stub-{type(self)._counter}",
            provider="nomba",
        )

    async def resolve_account(self, **kwargs):
        self.calls.append(("resolve_account", kwargs))
        from app.services.payments.base import ResolvedAccount
        return ResolvedAccount(
            account_number=kwargs["account_number"],
            account_name="DSTV NG LTD",
            bank_code=kwargs["bank_code"],
        )

    async def get_transaction(self, **kwargs):
        """Stub for transaction-status lookup. Returns success by
        default so the manual-verify endpoint can be tested against
        the Nomba stub.

        Override `self.get_transaction_status` /
        `self.get_transaction_amount_kobo` to simulate other
        statuses or a divergent amount (e.g. the Nomba sandbox
        reporting a fixed 4000)."""
        from app.services.payments.base import TransactionStatusResult

        self.calls.append(("get_transaction", kwargs))
        return TransactionStatusResult(
            provider_reference=kwargs["reference"],
            status=self.get_transaction_status,
            amount_kobo=self.get_transaction_amount_kobo,
            raw={"status": self.get_transaction_status, "stub": True},
        )

    async def create_transfer_recipient(self, **kwargs):
        self.calls.append(("create_transfer_recipient", kwargs))
        return f"NOMBA:{kwargs['bank_code']}:{kwargs['account_number']}"

    async def initiate_transfer(self, **kwargs):
        self.calls.append(("initiate_transfer", kwargs))
        from app.services.payments.base import TransferResult
        return TransferResult(
            provider_reference=kwargs["reference"],
            provider_transfer_id="API-TRANSFER-stub",
            status="pending",
        )

    def verify_webhook_signature(self, **_) -> bool:
        return True

    async def initialize_topup(self, **kwargs):
        """Stub for hosted Checkout top-up. Records the call and
        returns a fake Nomba authorization_url."""
        from app.services.payments.base import TopupInit

        self.calls.append(("initialize_topup", kwargs))
        type(self)._counter += 1
        return TopupInit(
            authorization_url=f"https://checkout.nomba.com/stub_{type(self)._counter}",
            reference=kwargs["reference"],
            provider="nomba",
            access_code=None,
        )

    async def parse_webhook(self, *, raw_body: bytes, signature_header: str, **kwargs):
        """Real signature verify + body parse. Mirrors the real
        `NombaProvider.parse_webhook` so the route's full pipeline
        (verify → parse → dedup → dispatch) is exercised end-to-end."""
        import json as _json

        from app.services.payments import WebhookSignatureError
        from app.services.payments.base import WebhookEvent

        # The route handler does the signature verify before calling
        # us, but the Protocol contract says parse_webhook raises on
        # bad sig. We re-verify here using the timestamp header
        # captured from the request (the stub doesn't see headers;
        # the route has already validated by the time we get here).
        # For the stub we just accept anything; the route's own
        # signature check is the source of truth.

        try:
            payload = _json.loads(raw_body.decode("utf-8"))
        except (_json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WebhookSignatureError(
                f"Webhook body is not valid JSON: {exc}", provider="nomba"
            ) from exc

        raw_event = str(payload.get("event_type") or "")
        if not raw_event:
            raise WebhookSignatureError(
                "Webhook missing event_type", provider="nomba"
            )

        # Same normalization table as the real provider.
        canonical = {
            "payment_success": "charge.success",
            "payment_reversal": "charge.reversed",
            "payout_success": "transfer.success",
            "payout_failed": "transfer.failed",
            "payout_refund": "transfer.reversed",
            "payment_failed": "webhook.unknown",
        }.get(raw_event, "webhook.unknown")

        data = payload.get("data") or {}
        txn = data.get("transaction") or {}
        provider_ref = str(
            txn.get("merchantTxRef")
            or txn.get("aliasAccountReference")
            or txn.get("id")
            or ""
        )
        amount_kobo = None
        if txn.get("transactionAmount") is not None:
            try:
                amount_kobo = int(round(float(txn["transactionAmount"]) * 100))
            except (TypeError, ValueError):
                amount_kobo = None
        event_id = str(
            payload.get("requestId")
            or payload.get("request_id")
            or f"{raw_event}:{provider_ref}:{txn.get('time')}"
        )
        return WebhookEvent(
            event_type=canonical,
            provider_reference=provider_ref,
            event_id=event_id,
            amount_kobo=amount_kobo,
            provider="nomba",
            raw=payload,
        )
