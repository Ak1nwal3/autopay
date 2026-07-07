"""Tests for the CORS middleware in `app/main.py`.

CORS is irrelevant for server-to-server traffic (webhooks, Telegram
bot), but the SPA hosted on a different origin (or a different port
during dev) needs explicit `Access-Control-Allow-*` headers so the
browser lets it call the API.

We pin four behaviors:
  1. Preflight from an allowed origin succeeds.
  2. Preflight from a non-allowed origin is rejected (no
     `Access-Control-Allow-Origin` header in the response).
  3. A simple GET from an allowed origin gets the allow-origin header.
  4. Webhook POSTs (Paystack, Telegram) are unaffected by CORS —
     they have no `Origin` header, and the CORS middleware must
     never reject a valid provider request.

We also pin that HEAD `/webhooks/nomba` still returns 405 even
with CORS enabled — CORS doesn't change method routing, and the
strict-405 invariant is critical.
"""
from __future__ import annotations

import os

# The conftest in tests/integration/ disables the bot; the SPA
# handlers in app/static/ import nothing server-side, so we don't
# need to mock anything else here.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")

from fastapi.testclient import TestClient

from app.main import app


def _client() -> TestClient:
    return TestClient(app)


def test_cors_preflight_from_allowed_origin() -> None:
    """OPTIONS request from an allowed origin returns 200 with the
    `Access-Control-Allow-Origin` header echoed back."""
    c = _client()
    r = c.options(
        "/api/v1/auth/me",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert r.status_code == 200, r.text
    # CORS middleware echoes the allowed origin back.
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_preflight_from_blocked_origin() -> None:
    """OPTIONS from a non-allowed origin does NOT get the allow-origin
    header. The browser would block the actual request."""
    c = _client()
    r = c.options(
        "/api/v1/auth/me",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    # No `Access-Control-Allow-Origin` → browser blocks the request.
    assert r.headers.get("access-control-allow-origin") is None


def test_cors_simple_get_includes_allow_origin() -> None:
    """A simple (non-preflight) GET from an allowed origin includes
    the allow-origin header. The browser uses this to decide whether
    to let the JS read the response body."""
    c = _client()
    r = c.get("/healthz", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_cors_webhook_post_unaffected() -> None:
    """Nomba webhook POSTs have no `Origin` header. The CORS
    middleware must not block them — the signature is the only auth.

    The webhook will return 400 (bad signature) because we sent
    garbage, not 403 (CORS rejection)."""
    c = _client()
    r = c.post(
        "/webhooks/nomba",
        content=b'{"event_type":"payment_success","data":{}}',
        headers={"Content-Type": "application/json"},
    )
    # 400 = bad signature (expected — we didn't sign the body). NOT
    # 403 = CORS rejection.
    assert r.status_code == 400


def test_cors_head_webhook_still_405() -> None:
    """HEAD /webhooks/nomba must remain 405 even with CORS enabled.
    CORS doesn't change method routing — only the Origin-based
    access policy. HEAD on a POST-only route still 405s."""
    c = _client()
    r = c.head("/webhooks/nomba", headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 405
