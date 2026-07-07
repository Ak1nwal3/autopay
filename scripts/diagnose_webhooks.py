"""Diagnostic script: authenticate with Nomba sandbox, fetch account
details (to find coreUserId), and query webhook event logs.

Usage:
    .venv\\Scripts\\python.exe scripts\\diagnose_webhooks.py
"""
from __future__ import annotations

import json
import sys

import httpx

BASE_URL = "https://sandbox.nomba.com"
CLIENT_ID = "706df6c4-b8bb-4130-88c4-d21b052f8631"
CLIENT_SECRET = "k8UobYk3APgOoxUnNL7VpuxzwTsH4LsXtydfjcHs8RH0YISBB4OMqJsaafG+U8fWETu9YZ96bNXE+DelCDuMPw=="
ACCOUNT_ID = "f666ef9b-888e-4799-85ce-acb505b28023"


def main() -> None:
    client = httpx.Client(base_url=BASE_URL, timeout=30.0)

    # ── 1. Authenticate ──────────────────────────────────────────
    print("=" * 60)
    print("1. AUTHENTICATING with Nomba sandbox...")
    print("=" * 60)
    resp = client.post(
        "/v1/auth/token/issue",
        headers={
            "Content-Type": "application/json",
            "accountId": ACCOUNT_ID,
        },
        json={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )
    print(f"  Status: {resp.status_code}")
    if resp.status_code != 200:
        print(f"  Error: {resp.text}")
        sys.exit(1)

    auth_data = resp.json().get("data", {})
    token = auth_data.get("access_token", "")
    business_id = auth_data.get("businessId", "")
    print(f"  businessId: {business_id}")
    print(f"  access_token: {token[:40]}...")
    print(f"  expiresAt: {auth_data.get('expiresAt')}")

    auth_headers = {
        "Authorization": f"Bearer {token}",
        "accountId": ACCOUNT_ID,
        "Content-Type": "application/json",
    }

    # ── 2. Fetch parent account details ──────────────────────────
    print()
    print("=" * 60)
    print("2. FETCHING parent account details...")
    print("=" * 60)
    resp = client.get("/v1/accounts/parent", headers=auth_headers)
    print(f"  Status: {resp.status_code}")
    if resp.status_code == 200:
        acct = resp.json().get("data", {})
        print(f"  accountId:       {acct.get('accountId')}")
        print(f"  accountHolderId: {acct.get('accountHolderId')}  <-- this is your coreUserId")
        print(f"  accountName:     {acct.get('accountName')}")
        print(f"  status:          {acct.get('status')}")
        print(f"  type:            {acct.get('type')}")
        banks = acct.get("banks", [])
        for b in banks:
            print(f"  bank: {b.get('bankName')} — {b.get('bankAccountNumber')} ({b.get('bankAccountName')})")
        core_user_id = acct.get("accountHolderId") or business_id
    else:
        print(f"  Error: {resp.text}")
        core_user_id = business_id

    print(f"\n  -> Using coreUserId = {core_user_id}")

    # ── 3. Query webhook event logs ──────────────────────────────
    print()
    print("=" * 60)
    print("3. QUERYING webhook event logs (last 7 days)...")
    print("=" * 60)
    resp = client.post(
        "/v1/webhooks/event-logs",
        headers=auth_headers,
        json={
            "coreUserId": core_user_id,
            "limit": 10,
        },
    )
    print(f"  Status: {resp.status_code}")
    if resp.status_code == 200:
        body = resp.json()
        logs = body.get("data", {})
        if isinstance(logs, dict):
            events = logs.get("events") or logs.get("data") or logs.get("logs") or []
        elif isinstance(logs, list):
            events = logs
        else:
            events = []
        print(f"  Found {len(events)} webhook event(s):")
        for i, evt in enumerate(events):
            print(f"    [{i+1}] event_type={evt.get('event_type') or evt.get('eventType')} "
                  f"status={evt.get('status') or evt.get('delivery_status')} "
                  f"url={evt.get('url') or evt.get('webhook_url')} "
                  f"time={evt.get('time') or evt.get('created_at')}")
            print(f"        response_code={evt.get('response_code') or evt.get('responseCode')} "
                  f"request_id={evt.get('requestId') or evt.get('request_id')}")
    else:
        print(f"  Error: {resp.text[:500]}")

    # ── 4. Try with businessId if accountHolderId didn't work ────
    if core_user_id != business_id:
        print()
        print("=" * 60)
        print("4. RETRYING with businessId as coreUserId...")
        print("=" * 60)
        resp = client.post(
            "/v1/webhooks/event-logs",
            headers=auth_headers,
            json={
                "coreUserId": business_id,
                "limit": 10,
            },
        )
        print(f"  Status: {resp.status_code}")
        if resp.status_code == 200:
            print(f"  Response: {json.dumps(resp.json(), indent=2)[:500]}")
        else:
            print(f"  Error: {resp.text[:500]}")

    # ── 5. Try with accountId as coreUserId ──────────────────────
    print()
    print("=" * 60)
    print("5. RETRYING with accountId as coreUserId...")
    print("=" * 60)
    resp = client.post(
        "/v1/webhooks/event-logs",
        headers=auth_headers,
        json={
            "coreUserId": ACCOUNT_ID,
            "limit": 10,
        },
    )
    print(f"  Status: {resp.status_code}")
    if resp.status_code == 200:
        print(f"  Response: {json.dumps(resp.json(), indent=2)[:500]}")
    else:
        print(f"  Error: {resp.text[:500]}")

    client.close()
    print()
    print("=" * 60)
    print("DONE. If no webhook events were found, it means Nomba")
    print("hasn't been sending webhooks — check the dashboard webhook")
    print("setup (Developer -> Webhook Setup) and ensure the URL is")
    print("set to: https://sanctuary-amused-excusably.ngrok-free.dev/webhooks/nomba")
    print("=" * 60)


if __name__ == "__main__":
    main()
