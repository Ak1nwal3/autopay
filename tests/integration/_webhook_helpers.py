"""Helpers for signing + posting Nomba webhooks in tests.

Mirrors the real `verify_nomba_webhook_signature` from
`app.services.payments.nomba` so integration tests can send
authentic-looking payloads that the route accepts.

Usage:
    from tests.integration._webhook_helpers import signed_nomba_post

    r = signed_nomba_post(
        client,
        event_type="payment_success",
        merchant={...},
        transaction={...},
        request_id="req-123",
        amount=200000,  # kobo
        reference="topup_user_abc",
    )
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from typing import Any

import httpx


_NOMBA_FIELDS_FOR_SIG = (
    "event_type",
    "requestId",
    "merchant.userId",
    "merchant.walletId",
    "transaction.transactionId",
    "transaction.type",
    "transaction.time",
    "transaction.responseCode",
    # 9th field is the timestamp header itself
)


def _safe(v: Any) -> str:
    if v is None:
        return ""
    s = str(v)
    if s.lower() == "null":
        return ""
    return s


def _get(payload: dict, dotted: str) -> Any:
    cur: Any = payload
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def sign_nomba_payload(
    payload: dict, *, secret: str, timestamp: str
) -> str:
    """Compute the Nomba webhook signature for a JSON payload dict.

    Returns the Base64-encoded HMAC-SHA256 string the route compares
    against the `nomba-signature` header.
    """
    data = payload.get("data") or {}
    merchant = data.get("merchant") or {}
    txn = data.get("transaction") or {}
    hashing_payload = ":".join(
        [
            _safe(payload.get("event_type")),
            _safe(payload.get("requestId") or payload.get("request_id")),
            _safe(merchant.get("userId")),
            _safe(merchant.get("walletId")),
            _safe(txn.get("transactionId") or txn.get("id")),
            _safe(txn.get("type")),
            _safe(txn.get("time")),
            _safe(txn.get("responseCode")),
            _safe(timestamp),
        ]
    )
    digest = hmac.new(
        secret.encode("utf-8"),
        hashing_payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_nomba_topup_payload(
    *,
    amount: int,
    reference: str,
    request_id: str | None = None,
    time_ms: int = 0,
    response_code: str = "00",
) -> dict:
    """Build a `payment_success` webhook body for a top-up.

    `amount` is in naira (integer). The payload nests it under
    `transaction.transactionAmount` in naira to match the real
    provider, which the verifier converts to kobo internally.
    """
    return {
        "event_type": "payment_success",
        "requestId": request_id or str(uuid.uuid4()),
        "data": {
            "merchant": {
                "userId": "stub-user-id",
                "walletId": "stub-wallet-id",
            },
            "transaction": {
                "transactionId": reference,
                "merchantTxRef": reference,
                "type": "TOPUP",
                "time": str(time_ms or 1700000000000),
                "responseCode": response_code,
                "transactionAmount": float(amount),
            },
        },
    }


def signed_nomba_post(
    client: httpx.Client,
    *,
    payload: dict,
    secret: str,
    timestamp: str = "1700000000000",
) -> httpx.Response:
    """POST a Nomba-shaped webhook to the local test server with
    a real signature + timestamp header.

    `client` is the FastAPI TestClient.
    """
    body = json.dumps(payload).encode("utf-8")
    sig = sign_nomba_payload(payload, secret=secret, timestamp=timestamp)
    return client.post(
        "/webhooks/nomba",
        content=body,
        headers={
            "nomba-signature": sig,
            "nomba-timestamp": timestamp,
            "Content-Type": "application/json",
        },
    )
