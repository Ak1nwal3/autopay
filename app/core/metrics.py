"""Prometheus metrics registry for the AutoPay AI app.

Exposes a small, focused set of counters and histograms that cover
the things an operator actually wants to know about in production:

  * `app_http_requests_total`    — request count by method, route, status
  * `app_http_request_seconds`   — request latency by method, route
  * `app_bills_created_total`     — bills created (by trigger: manual | upload | scheduled)
  * `app_bills_paid_total`        — bills auto-paid or manually paid
  * `app_payouts_total`           — payouts initiated (by result: success | fail | insufficient)
  * `app_topups_initiated_total`  — Checkout top-ups started
  * `app_topups_credited_total`   — Checkout top-ups confirmed by webhook
  * `app_scheduler_jobs_total`    — scheduler job runs (by result: success | fail | skip)
  * `app_scheduler_autopay_seconds` — time spent in the auto-pay path

The default Python process metrics (memory, GC, file descriptors) are
auto-registered by prometheus_client itself. We don't expose secrets
or user data — only request counts, latencies, and business event
counters.

`/metrics` is mounted in `app/api/health.py` so the scrape endpoint
sits next to `/healthz` and `/readyz`.
"""
from __future__ import annotations

from prometheus_client import Counter, Histogram

# ── HTTP ────────────────────────────────────────────────────────────

HTTP_REQUESTS = Counter(
    "app_http_requests_total",
    "Total HTTP requests, labeled by method, route, and status code.",
    labelnames=("method", "route", "status"),
)

HTTP_REQUEST_SECONDS = Histogram(
    "app_http_request_seconds",
    "HTTP request latency in seconds, labeled by method and route.",
    labelnames=("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# ── Business events ─────────────────────────────────────────────────

BILLS_CREATED = Counter(
    "app_bills_created_total",
    "Bills created, by trigger (manual | upload | scheduled | recurring).",
    labelnames=("trigger",),
)

BILLS_PAID = Counter(
    "app_bills_paid_total",
    "Bills that completed a payout, by trigger (manual | auto_pay | scheduled).",
    labelnames=("trigger",),
)

PAYOUTS = Counter(
    "app_payouts_total",
    "Payout attempts, by result (success | insufficient | provider_error | race).",
    labelnames=("result",),
)

TOPUPS_INITIATED = Counter(
    "app_topups_initiated_total",
    "Top-up flows started via POST /wallet/topup.",
)

TOPUPS_CREDITED = Counter(
    "app_topups_credited_total",
    "Top-ups confirmed by the charge.success webhook (wallet credited).",
    labelnames=("source",),  # "checkout" | "dva" | "manual"
)

# ── Scheduler ───────────────────────────────────────────────────────

SCHEDULER_JOBS = Counter(
    "app_scheduler_jobs_total",
    "Scheduler job runs, by job_id and result.",
    labelnames=("job_id", "result"),
)

SCHEDULER_AUTOPAY_SECONDS = Histogram(
    "app_scheduler_autopay_seconds",
    "Time spent in the auto-pay path per bill, including DB, agent, "
    "and provider calls.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)


__all__ = [
    "HTTP_REQUESTS",
    "HTTP_REQUEST_SECONDS",
    "BILLS_CREATED",
    "BILLS_PAID",
    "PAYOUTS",
    "TOPUPS_INITIATED",
    "TOPUPS_CREDITED",
    "SCHEDULER_JOBS",
    "SCHEDULER_AUTOPAY_SECONDS",
    "record_topup_initiated",
    "record_topup_credited",
    "record_bill_created",
    "record_bill_paid",
    "record_payout",
    "record_scheduler_job",
]


# ── Convenience functions ──────────────────────────────────────────
# The counters above are labeled; call sites would have to know
# the exact label names. These wrappers keep the call site short
# and make refactoring (renaming labels, adding buckets) cheap.

def record_topup_initiated() -> None:
    TOPUPS_INITIATED.inc()


def record_topup_credited(*, source: str) -> None:
    """`source` is "checkout" | "dva" | "manual"."""
    TOPUPS_CREDITED.labels(source=source).inc()


def record_bill_created(*, trigger: str) -> None:
    """`trigger` is "manual" | "upload" | "scheduled" | "recurring"."""
    BILLS_CREATED.labels(trigger=trigger).inc()


def record_bill_paid(*, trigger: str) -> None:
    """`trigger` is "manual" | "auto_pay"."""
    BILLS_PAID.labels(trigger=trigger).inc()


def record_payout(*, result: str) -> None:
    """`result` is "success" | "insufficient" | "provider_error" | "race"."""
    PAYOUTS.labels(result=result).inc()


def record_scheduler_job(*, job_id: str, result: str) -> None:
    """`result` is "success" | "fail" | "skip"."""
    SCHEDULER_JOBS.labels(job_id=job_id, result=result).inc()
