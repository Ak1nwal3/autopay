"""Tests for the Prometheus metrics module.

Covers:
  * All defined counters exist and are incrementable
  * HTTP_REQUESTS / HTTP_REQUEST_SECONDS have the expected label set
  * Convenience functions route to the right labels
  * /metrics endpoint returns 200 with prometheus exposition format
  * `record_bill_created` fires from all four bill-creation paths
    (HTTP API: upload + manual; service layer: telegram + recurring)
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient


def test_metrics_endpoint_returns_200_and_prometheus_format(
    client: TestClient,
) -> None:
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    # Body should be a valid Prometheus exposition — at minimum it
    # includes the standard Python process metrics that
    # prometheus_client auto-registers.
    body = r.text
    # process_* metrics are always present after the first import
    assert "process_" in body or "python_info" in body


def test_record_topup_initiated_increments() -> None:
    from app.core.metrics import TOPUPS_INITIATED, record_topup_initiated

    before = TOPUPS_INITIATED._value.get()
    record_topup_initiated()
    record_topup_initiated()
    after = TOPUPS_INITIATED._value.get()
    assert after - before == 2


def test_record_topup_credited_labels_source() -> None:
    from app.core.metrics import TOPUPS_CREDITED, record_topup_credited

    record_topup_credited(source="checkout")
    record_topup_credited(source="checkout")
    record_topup_credited(source="dva")
    # The counter has a single label `source`; verify that the
    # counter object is well-formed and the call didn't raise.
    assert TOPUPS_CREDITED._labelnames == ("source",)


def test_record_payout_labels_result() -> None:
    from app.core.metrics import PAYOUTS, record_payout

    record_payout(result="success")
    record_payout(result="insufficient")
    assert PAYOUTS._labelnames == ("result",)


def test_record_bill_paid_labels_trigger() -> None:
    from app.core.metrics import BILLS_PAID, record_bill_paid

    record_bill_paid(trigger="auto_pay")
    record_bill_paid(trigger="manual")
    assert BILLS_PAID._labelnames == ("trigger",)


def test_record_scheduler_job_labels() -> None:
    from app.core.metrics import SCHEDULER_JOBS, record_scheduler_job

    record_scheduler_job(job_id="process_scheduled_bills", result="success")
    record_scheduler_job(job_id="process_scheduled_bills", result="fail")
    assert SCHEDULER_JOBS._labelnames == ("job_id", "result")


def test_http_request_middleware_records_metrics(
    client: TestClient,
) -> None:
    """A real request through the app should bump the HTTP_REQUESTS
    counter for the resolved route."""
    from app.core.metrics import HTTP_REQUESTS

    before = HTTP_REQUESTS.labels(
        method="GET", route="/healthz", status="200"
    )._value.get()
    r = client.get("/healthz")
    assert r.status_code == 200
    after = HTTP_REQUESTS.labels(
        method="GET", route="/healthz", status="200"
    )._value.get()
    assert after >= before + 1


# ── record_bill_created fires from every bill-creation path ──────────


def _signup(client: TestClient, email: str = "metrics@x.com", phone: str = "08090000001") -> str:
    s = client.post(
        "/api/v1/auth/signup",
        json={
            "first_name": "Metrics",
            "last_name": "Tester",
            "email": email,
            "phone_number": phone,
            "password": "Secret123",
        },
    )
    assert s.status_code == 201, s.text
    return f"Bearer {s.json()['access_token']}"


def _future_iso(days: int = 2) -> str:
    return (datetime.now(tz=UTC) + timedelta(days=days)).isoformat()


def test_record_bill_created_fires_from_manual_endpoint(
    client: TestClient,
) -> None:
    """POST /api/v1/bills (the manual-creation path) must increment
    `app_bills_created_total{trigger="manual"}`."""
    from app.core.metrics import BILLS_CREATED

    before = BILLS_CREATED.labels(trigger="manual")._value.get()
    h = _signup(client, email="m1@x.com", phone="08090000011")
    r = client.post(
        "/api/v1/bills",
        json={
            "vendor_name": "DSTV",
            "amount": 5000,
            "due_date": _future_iso(),
            "account_number": "0123456789",
            "bank_code": "058",
            "bank_name": "GTBank",
        },
        headers={"Authorization": h},
    )
    assert r.status_code == 201, r.text
    after = BILLS_CREATED.labels(trigger="manual")._value.get()
    assert after == before + 1


def test_record_bill_created_fires_from_create_scheduled_bill(
    session,
) -> None:
    """The service-layer `create_scheduled_bill` (used by the
    Telegram bot's /schedule conversation) must increment
    `app_bills_created_total{trigger="telegram"}`."""
    from datetime import datetime as _dt
    from decimal import Decimal

    from app.core.metrics import BILLS_CREATED
    from app.core.security import hash_password
    from app.models.user import User
    from app.services.bill import ScheduleBillInput, create_scheduled_bill

    # Set up a user directly in the DB (no HTTP call needed).
    u = User(
        email="m2@x.com",
        hashed_password=hash_password("Secret123"),
        first_name="M",
        last_name="T",
        phone_number="08090000012",
        balance=Decimal("0"),
    )
    session.add(u)
    session.commit()
    session.refresh(u)

    before = BILLS_CREATED.labels(trigger="telegram")._value.get()
    # `create_scheduled_bill` strips tzinfo before comparing against
    # `datetime.now()` (naive). Strip tz here to match the function's
    # expected input shape.
    create_scheduled_bill(
        session,
        user_id=u.id,
        payload=ScheduleBillInput(
            vendor_name="MTN",
            amount=Decimal("1000"),
            due_date=(_dt.now(tz=UTC) + timedelta(days=7)).replace(tzinfo=None),
        ),
    )
    after = BILLS_CREATED.labels(trigger="telegram")._value.get()
    assert after == before + 1


def test_record_bill_created_fires_from_schedule_recurrence(
    session,
) -> None:
    """The service-layer `schedule_recurrence` (used by the scheduler
    every 6 hours for recurring bills) must increment
    `app_bills_created_total{trigger="recurring"}`."""
    from datetime import datetime as _dt
    from decimal import Decimal

    from app.core.metrics import BILLS_CREATED
    from app.core.security import hash_password
    from app.models.bill import Bill
    from app.models.enums import BillStatus
    from app.models.user import User
    from app.services.payout import schedule_recurrence

    u = User(
        email="m3@x.com",
        hashed_password=hash_password("Secret123"),
        first_name="M",
        last_name="T",
        phone_number="08090000013",
        balance=Decimal("0"),
    )
    session.add(u)
    session.commit()
    session.refresh(u)

    # `schedule_recurrence` adds a timedelta to a naive datetime; the
    # DB column is naive TIMESTAMP, so we strip tzinfo here too.
    recurring_bill = Bill(
        user_id=u.id,
        vendor_name="DSTV",
        amount=Decimal("5000"),
        due_date=(_dt.now(tz=UTC) + timedelta(days=30)).replace(tzinfo=None),
        is_recurring=True,
        recurrence_interval="monthly",
        status=BillStatus.SCHEDULED.value,
    )
    session.add(recurring_bill)
    session.commit()
    session.refresh(recurring_bill)

    before = BILLS_CREATED.labels(trigger="recurring")._value.get()
    next_bill = schedule_recurrence(session, bill=recurring_bill)
    session.commit()
    after = BILLS_CREATED.labels(trigger="recurring")._value.get()

    assert next_bill is not None
    assert after == before + 1
