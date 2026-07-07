# Graph Report - .  (2026-07-07)

## Corpus Check
- 110 files · ~77,579 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1545 nodes · 4249 edges · 64 communities (54 shown, 10 thin omitted)
- Extraction: 92% EXTRACTED · 8% INFERRED · 0% AMBIGUOUS · INFERRED: 360 edges (avg confidence: 0.65)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Schedule Conversation Handler|Schedule Conversation Handler]]
- [[_COMMUNITY_Bill Upload Conversation|Bill Upload Conversation]]
- [[_COMMUNITY_Bill Extraction Loaders|Bill Extraction Loaders]]
- [[_COMMUNITY_Wallet Endpoint Tests|Wallet Endpoint Tests]]
- [[_COMMUNITY_Static Frontend (app.js)|Static Frontend (app.js)]]
- [[_COMMUNITY_Nomba Provider Tests|Nomba Provider Tests]]
- [[_COMMUNITY_KYC  BVN Encryption|KYC / BVN Encryption]]
- [[_COMMUNITY_Payment Provider Protocol|Payment Provider Protocol]]
- [[_COMMUNITY_Database & Migrations|Database & Migrations]]
- [[_COMMUNITY_Wallet API & Enums|Wallet API & Enums]]
- [[_COMMUNITY_Topup Conversation Handler|Topup Conversation Handler]]
- [[_COMMUNITY_Date Parser|Date Parser]]
- [[_COMMUNITY_LangGraph Decision Agent|LangGraph Decision Agent]]
- [[_COMMUNITY_Telegram Handlers & Link Codes|Telegram Handlers & Link Codes]]
- [[_COMMUNITY_Bills API & Metrics|Bills API & Metrics]]
- [[_COMMUNITY_Nomba Provider Implementation|Nomba Provider Implementation]]
- [[_COMMUNITY_Logging & Health Endpoints|Logging & Health Endpoints]]
- [[_COMMUNITY_Auth API|Auth API]]
- [[_COMMUNITY_Audit Logging|Audit Logging]]
- [[_COMMUNITY_Payout State Machine|Payout State Machine]]
- [[_COMMUNITY_Security & JWT|Security & JWT]]
- [[_COMMUNITY_Webhook Handlers|Webhook Handlers]]
- [[_COMMUNITY_Wallet Service|Wallet Service]]
- [[_COMMUNITY_Models (ORM)|Models (ORM)]]
- [[_COMMUNITY_Payout Tests|Payout Tests]]
- [[_COMMUNITY_Settings & Config|Settings & Config]]
- [[_COMMUNITY_Scheduler Jobs|Scheduler Jobs]]
- [[_COMMUNITY_Telegram Service & Bot|Telegram Service & Bot]]
- [[_COMMUNITY_Auth Service|Auth Service]]
- [[_COMMUNITY_Crypto Primitives|Crypto Primitives]]
- [[_COMMUNITY_Refresh Tokens & Session|Refresh Tokens & Session]]
- [[_COMMUNITY_Bill Service & Recurrence|Bill Service & Recurrence]]
- [[_COMMUNITY_Transaction Model|Transaction Model]]
- [[_COMMUNITY_Bill Model & Status|Bill Model & Status]]
- [[_COMMUNITY_KYC Model|KYC Model]]
- [[_COMMUNITY_Telegram Notifications Tests|Telegram Notifications Tests]]
- [[_COMMUNITY_Bills Tests|Bills Tests]]
- [[_COMMUNITY_Auth Endpoints Tests|Auth Endpoints Tests]]
- [[_COMMUNITY_Name Matching|Name Matching]]
- [[_COMMUNITY_User Model|User Model]]
- [[_COMMUNITY_Virtual Account Model|Virtual Account Model]]
- [[_COMMUNITY_BVN Schema|BVN Schema]]
- [[_COMMUNITY_Webhook Event Model|Webhook Event Model]]
- [[_COMMUNITY_Payment Math Tests|Payment Math Tests]]
- [[_COMMUNITY_KYC Endpoint Tests|KYC Endpoint Tests]]
- [[_COMMUNITY_Misc Documentation|Misc Documentation]]
- [[_COMMUNITY_Wallet Transactions Tests|Wallet Transactions Tests]]
- [[_COMMUNITY_Manual SPA Render|Manual SPA Render]]
- [[_COMMUNITY_Webhook Test Helpers|Webhook Test Helpers]]
- [[_COMMUNITY_Name Match Tests|Name Match Tests]]
- [[_COMMUNITY_CORS & Smoke Tests|CORS & Smoke Tests]]
- [[_COMMUNITY_Migrations Versions|Migrations Versions]]
- [[_COMMUNITY_Load Status  CORS Smoke|Load Status / CORS Smoke]]
- [[_COMMUNITY_Alembic Config|Alembic Config]]
- [[_COMMUNITY_Order & Reference|Order & Reference]]
- [[_COMMUNITY_Scheduler Tests|Scheduler Tests]]
- [[_COMMUNITY_Bill Conversation Tests|Bill Conversation Tests]]
- [[_COMMUNITY_Pipeline Tests|Pipeline Tests]]
- [[_COMMUNITY_Telegram Handler Order Tests|Telegram Handler Order Tests]]
- [[_COMMUNITY_Telegram Link Code Model|Telegram Link Code Model]]
- [[_COMMUNITY_Static Assets  Models Tests|Static Assets / Models Tests]]
- [[_COMMUNITY_Entry Point & Seed|Entry Point & Seed]]
- [[_COMMUNITY_Audit & Smoke Tests|Audit & Smoke Tests]]

## God Nodes (most connected - your core abstractions)
1. `User` - 136 edges
2. `Transaction` - 75 edges
3. `session_scope()` - 74 edges
4. `Bill` - 69 edges
5. `hash_password()` - 40 edges
6. `_FakeContext` - 40 edges
7. `NombaProvider` - 39 edges
8. `parse_bill_due_date()` - 38 edges
9. `write_audit()` - 37 edges
10. `PaymentProvider` - 37 edges

## Surprising Connections (you probably didn't know these)
- `AutoPay AI` --semantically_similar_to--> `Nomba Gateway`  [INFERRED] [semantically similar]
  DOCUMENTATION.md → README.md
- `test_record_bill_paid_labels_trigger()` --calls--> `record_bill_paid()`  [EXTRACTED]
  tests/integration/test_metrics.py → app/core/metrics.py
- `test_record_payout_labels_result()` --calls--> `record_payout()`  [EXTRACTED]
  tests/integration/test_metrics.py → app/core/metrics.py
- `_process_scheduled_bills()` --indirect_call--> `session()`  [INFERRED]
  app/core/scheduler.py → tests/integration/conftest.py
- `_process_recurring_bills()` --indirect_call--> `session()`  [INFERRED]
  app/core/scheduler.py → tests/integration/conftest.py

## Import Cycles
- 1-file cycle: `app/services/telegram.py -> app/services/telegram.py`

## Hyperedges (group relationships)
- **AutoPay AI stack components** — fastapi_app, telegram_bot, langgraph_agent, postgresql, apscheduler, paystack [EXTRACTED 1.00]
- **Payout execution flow** — execute_payout, confirm_payout, paystack_provider, transaction_model, bill_model, row_locks_for_payout, debit_then_call_pattern [EXTRACTED 1.00]
- **Webhook security defenses** — webhook_signature_verification, webhook_replay_defense, webhook_event_model, paystack_provider [EXTRACTED 1.00]
- **Decision rule** — langgraph_agent, pay_now_decision, schedule_decision, hold_decision [EXTRACTED 1.00]
- **SQLModel ORM tables (8)** — user_model, kyc_record_model, virtual_account_model, bill_model, transaction_model, audit_log_model, refresh_token_model, telegram_link_code_model, webhook_event_model [EXTRACTED 1.00]

## Communities (64 total, 10 thin omitted)

### Community 0 - "Schedule Conversation Handler"
Cohesion: 0.05
Nodes (100): escape_md(), Escape Telegram Markdown V1 reserved chars. Used whenever we     interpolate use, bank_quick_pick_keyboard(), build_schedule_conversation(), cancel_command(), confirm_keyboard(), date_quick_pick_keyboard(), _go_to_date() (+92 more)

### Community 1 - "Bill Upload Conversation"
Cohesion: 0.06
Nodes (66): build_bill_conversation(), cancel_command(), _cancel_persisted_bill(), handle_cancel(), handle_confirm(), handle_date_quickpick(), handle_edit(), handle_edit_discard() (+58 more)

### Community 2 - "Bill Extraction Loaders"
Cohesion: 0.05
Nodes (59): ABC, _build_loader(), Pick a loader based on what the user sent. Returns None if     this isn't a bill, BillExtractionResult, What the loader + LLM extracted from the upload.      `due_date` is intentionall, BaseLoader, _detect_vendor(), _get_llm_client() (+51 more)

### Community 3 - "Wallet Endpoint Tests"
Cohesion: 0.07
Nodes (60): Session, Context-manager variant for non-FastAPI callers (workers, scripts)., session_scope(), Client, Session, TestClient, Integration tests for the wallet endpoints (DVA provisioning)., POST /wallet/topup returns a Paystack-hosted URL and persists a     pending `Tr (+52 more)

### Community 4 - "Static Frontend (app.js)"
Cohesion: 0.09
Nodes (53): API, billRow(), cancelBill(), doLogout(), esc(), filterBills(), fmtDate(), fmtDateFull() (+45 more)

### Community 5 - "Nomba Provider Tests"
Cohesion: 0.06
Nodes (48): Standalone HMAC verifier for the Nomba webhook route.      Nomba's signature pay, verify_nomba_webhook_signature(), _safe(), _envelope(), Any, Unit tests for `app.services.payments.nomba` and the OAuth helper.  We use `resp, First call fetches; second call uses the cache., A token with 30s lifetime is considered expired (60s leeway)     and triggers a (+40 more)

### Community 6 - "KYC / BVN Encryption"
Cohesion: 0.08
Nodes (43): get_kyc(), Session, KYC API — BVN submission.  Mounted at /api/v1/kyc in `app.main`., Encrypt the BVN with Fernet, store its hash + last4, write audit., submit_bvn(), KycStatusResponse, KycSubmitRequest, KYC DTOs — BVN submission. (+35 more)

### Community 7 - "Payment Provider Protocol"
Cohesion: 0.06
Nodes (22): Payment provider abstraction.  We never want to lock the business logic to a sin, Issue a dedicated virtual account for `customer_code`., Look up the name on `account_number` at `bank_code`., Move `amount_kobo` (1 NGN = 100 kobo) from our balance to recipient.          `a, Start a hosted top-up flow. The user is redirected to the         returned `auth, Verify signature, then parse into a `WebhookEvent`. Raises on bad sig., A dedicated virtual account (DVA) issued to a user by the provider.      `provid, Result of "look up the name behind this account number". (+14 more)

### Community 8 - "Database & Migrations"
Cohesion: 0.08
Nodes (28): Webhook handlers.  Mounted at /webhooks/nomba in `app.main`.  This is the Nomba-, get_session(), init_db(), SQLAlchemy / SQLModel engine + session management., Create all tables. Used in tests; production uses Alembic., FastAPI dependency that yields a session and closes it after the request., Audit log — every state-changing event appends a row.  Rows are inserted in the, # NOTE: 'metadata' is reserved by SQLAlchemy Declarative, so we name the (+20 more)

### Community 9 - "Wallet API & Enums"
Cohesion: 0.14
Nodes (36): TelegramLinkCodeResponse, FastAPI routers — all HTTP endpoints under one namespace., provision_virtual_account(), ProvisionResponse, Wallet API — balance, virtual account provisioning, top-up, transaction history., Idempotent. If the user already has a VA, returns it (200 OK     semantically, b, Body for `POST /wallet/topup`., Wire format for the top-up init response. (+28 more)

### Community 10 - "Topup Conversation Handler"
Cohesion: 0.10
Nodes (40): build_topup_conversation(), cancel_command(), done_keyboard(), handle_cancel(), handle_custom(), handle_custom_amount(), handle_custom_back(), handle_done_again() (+32 more)

### Community 11 - "Date Parser"
Cohesion: 0.10
Nodes (37): _clamp_to_present(), _now(), parse_bill_due_date(), datetime, Robust date parsing for LLM-extracted bill due dates.  LLMs return dates in ever, ISO 8601 — full datetime, date-only, with/without TZ, with/without microseconds., Parse 'today', 'tomorrow', 'in 2 weeks', '5 days from today', etc., Best-effort parse. Never raises; falls back to `datetime.now()`. (+29 more)

### Community 12 - "LangGraph Decision Agent"
Cohesion: 0.11
Nodes (35): build_graph(), Decimal, LangGraph state graph build.  Currently a single-node graph (the rule is the onl, Build the LangGraph state graph., Invoke the graph. Equivalent to calling `decide()` directly —     exists so call, run_agent(), decide(), decide_for_bill() (+27 more)

### Community 13 - "Telegram Handlers & Link Codes"
Cohesion: 0.10
Nodes (36): link_command(), Show the user's last 20 transactions (credits + debits)., transactions_command(), 6-char alphanumeric, uppercase, easy to read/type., TelegramLinkCode, Codes are 6 hex chars (12 chars when decoded from token_hex(3))., test_telegram_link_code_generator(), _FakeMessage (+28 more)

### Community 14 - "Bills API & Metrics"
Cohesion: 0.11
Nodes (29): cancel_bill(), create_bill(), get_bill(), list_bills(), pay_bill(), Session, Bills API — upload, list, get, pay, cancel.  Mounted at /api/v1/bills in `app.ma, Upload a bill (PDF / image) OR paste text, get back an     extracted + agent-dec (+21 more)

### Community 15 - "Nomba Provider Implementation"
Cohesion: 0.08
Nodes (22): ProviderError, Generic 4xx/5xx/network failure from the gateway., NombaProvider, AsyncClient, WebhookEvent, Lazily build the shared httpx client. Reuse across calls         so connection p, Make a Nomba call and return the unwrapped `data` payload.          Nomba's enve, Nomba's DVA endpoint takes the user's identity inline; there         is no separ (+14 more)

### Community 16 - "Logging & Health Endpoints"
Cohesion: 0.08
Nodes (28): metrics(), JSONResponse, Response, Session, Liveness and readiness probes.  - /healthz  liveness: process is up - /readyz, Exposes the registered `prometheus_client` collectors in     text/plain expositi, readyz(), get_logger() (+20 more)

### Community 17 - "Auth API"
Cohesion: 0.13
Nodes (29): create_telegram_link_code(), invalidate_telegram_link_codes(), login(), logout(), me(), Request, Response, Session (+21 more)

### Community 18 - "Audit Logging"
Cohesion: 0.17
Nodes (27): AuditLog, audit_bill_created(), audit_kyc_bvn_submitted(), audit_login(), audit_logout(), audit_payout_failed(), audit_payout_succeeded(), audit_user_signup() (+19 more)

### Community 19 - "Payout State Machine"
Cohesion: 0.13
Nodes (28): _handle_transfer_update(), Our outbound transfer completed / failed / was reversed., Bill, _commit_or_warn(), confirm_payout(), execute_payout(), _new_reference(), PayoutResult (+20 more)

### Community 20 - "Security & JWT"
Cohesion: 0.12
Nodes (27): _async_poll_pending_nomba_topups(), get_scheduler(), _poll_pending_nomba_topups(), _process_recurring_bills(), APScheduler integration.  Runs in the same process as FastAPI (single worker by, Wrap an async function as a sync callable that runs it in its     own event loop, Spawn the next occurrence of every recurring bill whose     `next_recurrence_dat, Async body of the poll job. Runs in the scheduler's private     event loop (see (+19 more)

### Community 21 - "Webhook Handlers"
Cohesion: 0.13
Nodes (27): Refresh token — for JWT auth (lands in Chunk 3).  The actual JWT is short-lived;, RefreshToken, get_current_user(), get_optional_current_user(), _hash_refresh_token(), issue_tokens(), logout_user(), new_refresh_token_string() (+19 more)

### Community 22 - "Wallet Service"
Cohesion: 0.15
Nodes (27): _make_pending_topup_txn(), _make_user_with_balance(), _nomba_webhook(), Response, Session, TestClient, Integration tests for the `/webhooks/nomba` route.  Covers:   * Signature verifi, A correctly-signed body with a different timestamp fails. (+19 more)

### Community 23 - "Models (ORM)"
Cohesion: 0.12
Nodes (24): _FakeCallbackQuery, _FakeContext, _FakeUpdate, Minimal stand-in for a telegram.CallbackQuery.      The handlers call `query.ans, Put a parsed bill + staging snapshot into user_data, as if     receive_bill() ha, A non-numeric amount should bounce back to EDIT_VALUE with an     error, not sil, A non-parseable date should bounce back to EDIT_VALUE., A valid amount should be persisted and return to the list. (+16 more)

### Community 24 - "Payout Tests"
Cohesion: 0.15
Nodes (22): AccountNameMismatch, AuthenticationError, InsufficientFunds, InvalidAccount, KYCRequired, PaymentError, Typed errors raised by payment provider implementations.  Business code (payout,, Base class for all payment-gateway failures. (+14 more)

### Community 25 - "Settings & Config"
Cohesion: 0.12
Nodes (25): AuditLog model, Audit logging in same transaction, Bill model, confirm_payout, Debit-then-call pattern, execute_payout, Groq LLM, hold decision (+17 more)

### Community 26 - "Scheduler Jobs"
Cohesion: 0.11
Nodes (21): Result of looking up a single transaction at the provider.      `status` is norm, Look up a single transaction by our reference (or the         provider's) and re, TransactionStatusResult, _clean_db(), client(), _default_stub_provider(), _install_nomba_stub(), TestClient (+13 more)

### Community 27 - "Telegram Service & Bot"
Cohesion: 0.11
Nodes (15): _CachedToken, OAuth2ClientCredentials, OAuth2Error, AsyncClient, OAuth2 client_credentials token-fetch + cache + refresh.  Reusable for any payme, Return a valid access token. Refreshes on demand.          Concurrency: if 10 ca, Discard the cached token and fetch a new one.          Called by the provider wh, Fetch a new token from the provider. MUST be called with         `self._lock` he (+7 more)

### Community 28 - "Auth Service"
Cohesion: 0.20
Nodes (20): create_access_token(), create_refresh_token(), decode_token(), JWTError_, Any, datetime, Password hashing + JWT helpers (bcrypt + python-jose).  These land in Chunk 2 (p, Raised when a JWT is invalid, expired, or tampered with. (+12 more)

### Community 29 - "Crypto Primitives"
Cohesion: 0.11
Nodes (20): hash_password(), Hash a plaintext password with bcrypt (cost factor 12)., Constant-time bcrypt comparison. Returns False on any error., verify_password(), test_empty_inputs_rejected(), test_password_hash_roundtrip(), test_wrong_password_rejected(), built_app() (+12 more)

### Community 30 - "Refresh Tokens & Session"
Cohesion: 0.10
Nodes (10): Tests for the SQLModel model definitions.  These verify that each model:   - Has, The Python attribute is event_metadata; the SQL column is 'metadata'., NUMERIC(14,2) preserves 0.01 precision (unlike float)., BVN was extracted to kyc_records — must not appear in users., The MVP used 'payaza_reference' — we now use generic 'provider' + 'provider_refe, test_audit_log_metadata_column_named_metadata(), test_decimal_precision_in_arithmetic(), test_transaction_has_provider_column() (+2 more)

### Community 31 - "Bill Service & Recurrence"
Cohesion: 0.14
Nodes (19): `source` is "checkout" | "dva" | "manual"., `result` is "success" | "fail" | "skip"., record_scheduler_job(), record_topup_credited(), record_topup_initiated(), _future_iso(), TestClient, Tests for the Prometheus metrics module.  Covers:   * All defined counters exist (+11 more)

### Community 32 - "Transaction Model"
Cohesion: 0.18
Nodes (19): bills_command(), help_command(), DEFAULT_TYPE, Update, Telegram bot auth-flow handlers: /start, /link, /wallet, /unlink, /bills, /trans, start_command(), unlink_command(), wallet_command() (+11 more)

### Community 33 - "Bill Model & Status"
Cohesion: 0.16
Nodes (19): Transaction, bot_with_send_recorder(), _link_user(), Tests for the credit/debit/refund Telegram notifications and the new multi-actio, A credit notification should mention the amount, narration,     and new balance,, A debit notification should mention amount, narration, and     remaining balance, A refund notification should include the refunded amount,     the failure reason, If the user has no linked Telegram, notification is a no-op. (+11 more)

### Community 34 - "KYC Model"
Cohesion: 0.22
Nodes (18): Session, TestClient, Integration tests for the auth endpoints.  Coverage:   * signup  → 201, tokens r, Swagger UI's 'Authorize' button + the curl 'Authorization' header     in 'Try it, RFC 6750 says a 401 on a Bearer-protected route must include     `WWW-Authentica, _signup_payload(), test_401_includes_www_authenticate_header(), test_login_401_on_bad_password() (+10 more)

### Community 35 - "Telegram Notifications Tests"
Cohesion: 0.21
Nodes (17): _claim_due_scheduled_bill_ids(), _process_scheduled_bills(), Atomically claim all due scheduled bills for processing.      Uses `SELECT ... F, Auto-pay scheduled bills whose due date has arrived.      The flow per bill:, _make_user_with_balance(), Session, Integration tests for the auto-pay scheduler path.  A `scheduled` bill whose `du, A `paid` bill should never be picked up by the scheduler. (+9 more)

### Community 36 - "Bills Tests"
Cohesion: 0.17
Nodes (16): list_transactions(), Session, Mint a unique `reference`, persist a pending `Transaction` row,     call `provid, Re-fetch a pending top-up's status from the payment provider     and apply the c, Return the caller's most recent transactions, newest first.      Query params:, topup_wallet(), verify_topup(), apply_credit_from_provider_event() (+8 more)

### Community 37 - "Auth Endpoints Tests"
Cohesion: 0.26
Nodes (14): User, get_current_active_user(), Optional 'is_active' check goes here. We don't have such a flag     on the model, _make_scheduled_bill(), _make_user_with_balance(), Session, Integration tests for the account-name-mismatch guard.  These tests exercise the, A bill for 'DSTV Nigeria Ltd' resolving to 'DSTV NG LTD' is     a real-world ban (+6 more)

### Community 38 - "Name Matching"
Cohesion: 0.17
Nodes (15): _format_amount(), get_application(), notify_credit(), notify_debit(), _notify_keyboard(), notify_user_of_transaction(), InlineKeyboardMarkup, Request (+7 more)

### Community 39 - "User Model"
Cohesion: 0.29
Nodes (15): _auth_header(), Session, TestClient, Integration tests for the bills endpoints., The /upload endpoint with a `request_bill` form field should     create a bill a, Image upload with the LLM client stubbed to None (forcing the     OCR fallback p, test_cancel_bill(), test_create_bill_via_json() (+7 more)

### Community 40 - "Virtual Account Model"
Cohesion: 0.14
Nodes (14): _get_spa_assets(), SPA smoke tests for the stripped-down bundle.  The frontend → backend wiring has, The HTML and JS are both served at the expected URLs., apiFetch was the entire backend wiring. Its presence means     someone re-introd, The bundle code should not reference any /api/v1/* path.     We strip comments s, Auth was via localStorage tokens. With auth removed, no     localStorage.getItem, The state-machine balance flow (setBalance / loadBalance /     reRenderBalanceCa, End-to-end in Node: load the bundle, no exceptions. (+6 more)

### Community 41 - "BVN Schema"
Cohesion: 0.28
Nodes (14): _insert_txn(), TestClient, Tests for `GET /api/v1/wallet/transactions`., User A can't see User B's transactions — the WHERE clause     filters by `user_i, Signup a new user via the API and return (access_token, user_id)., Insert a transaction row for `user_id` and return its id., No Bearer token → 401., _signup() (+6 more)

### Community 42 - "Webhook Event Model"
Cohesion: 0.20
Nodes (13): _client(), TestClient, Tests for the CORS middleware in `app/main.py`.  CORS is irrelevant for server-t, HEAD /webhooks/nomba must remain 405 even with CORS enabled.     CORS doesn't ch, OPTIONS request from an allowed origin returns 200 with the     `Access-Control-, OPTIONS from a non-allowed origin does NOT get the allow-origin     header. The, A simple (non-preflight) GET from an allowed origin includes     the allow-origi, Nomba webhook POSTs have no `Origin` header. The CORS     middleware must not bl (+5 more)

### Community 43 - "Payment Math Tests"
Cohesion: 0.18
Nodes (11): docker-compose app service, docker-compose db service, docker-compose main stack, docker-compose.test stack, ci, CI Postgres 16 service, CI pytest+ruff job, PostgreSQL (+3 more)

### Community 44 - "KYC Endpoint Tests"
Cohesion: 0.17
Nodes (5): Alembic, downgrade(), No-op. See module docstring., No-op. To wipe the schema, drop and recreate the database., upgrade()

### Community 45 - "Misc Documentation"
Cohesion: 0.26
Nodes (11): _ngn_to_kobo(), ₦ → kobo (integer). Paystack wants whole kobo only., Tests for the payout service's money math.  These are pure unit tests — no DB, n, Pin the rule that we use Decimal (not float) for money math., test_decimal_arithmetic_for_balance(), test_ngn_to_kobo_fractional(), test_ngn_to_kobo_large(), test_ngn_to_kobo_rounds_nearest_kobo() (+3 more)

### Community 46 - "Wallet Transactions Tests"
Cohesion: 0.21
Nodes (10): APScheduler, AutoPay AI, Deferred DVA provisioning, Dedicated Virtual Account (DVA), FastAPI app, Nomba Gateway, process_recurring_bills scheduler job, process_scheduled_bills scheduler job (+2 more)

### Community 47 - "Manual SPA Render"
Cohesion: 0.42
Nodes (9): _auth_header(), Session, TestClient, Integration tests for the KYC endpoints., test_bvn_must_be_11_digits(), test_get_kyc_404_when_absent(), test_get_kyc_returns_status(), test_submit_bvn_409_on_duplicate() (+1 more)

### Community 48 - "Webhook Test Helpers"
Cohesion: 0.32
Nodes (8): _handle_charge_success(), _handle_dva_assigned(), nomba_webhook(), Request, Session, User's VA received money. Credit their wallet and update txn.      The actual cr, DVA was successfully assigned. Signup already created the row;     this is mostl, Receive + verify + dispatch a Nomba webhook event.      Strict 405 on GET/HEAD (

### Community 49 - "Name Match Tests"
Cohesion: 0.40
Nodes (6): _async_autopay(), _autopay_one_bill(), Re-evaluate a single due bill and (if pay_now) execute the     payout. Called fr, Async helper: get a provider, open a session, call execute_payout.      Commits, Direct DB session for tests that need to set up data without HTTP., session()

### Community 50 - "CORS & Smoke Tests"
Cohesion: 0.33
Nodes (6): Return (raw_body, nomba-signature, nomba-timestamp) with a real HMAC.      The N, A `transfer.success` webhook for a bill payment should mark     the bill paid AN, A `transfer.failed` webhook for a bill payment should mark     the txn failed +, _signed_nomba_body(), test_transfer_failed_webhook_fires_refund_notification(), test_transfer_success_webhook_fires_debit_notification()

### Community 51 - "Migrations Versions"
Cohesion: 0.40
Nodes (4): client_ip(), Request, HTTP-related helpers used by multiple routers., Return the client's IP, or None if it isn't a valid IPv4/IPv6.      The `audit_l

### Community 53 - "Alembic Config"
Cohesion: 0.67
Nodes (3): _build_engine(), Create the SQLAlchemy engine with sane pool defaults for Postgres., Engine

## Knowledge Gaps
- **9 isolated node(s):** `nomba-gateway`, `entrypoint.sh script`, `ci`, `Paystack`, `Groq LLM` (+4 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **10 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `User` connect `Auth Endpoints Tests` to `Schedule Conversation Handler`, `Bill Upload Conversation`, `Wallet Endpoint Tests`, `KYC / BVN Encryption`, `Payment Provider Protocol`, `Database & Migrations`, `Wallet API & Enums`, `LangGraph Decision Agent`, `Telegram Handlers & Link Codes`, `Bills API & Metrics`, `Auth API`, `Payout State Machine`, `Security & JWT`, `Webhook Handlers`, `Wallet Service`, `Models (ORM)`, `Crypto Primitives`, `Refresh Tokens & Session`, `Bill Service & Recurrence`, `Transaction Model`, `Bill Model & Status`, `KYC Model`, `Telegram Notifications Tests`, `Bills Tests`, `User Model`, `BVN Schema`, `Manual SPA Render`, `Webhook Test Helpers`, `Name Match Tests`?**
  _High betweenness centrality (0.175) - this node is a cross-community bridge._
- **Why does `pytest` connect `Payment Math Tests` to `Schedule Conversation Handler`, `Bill Upload Conversation`, `Bill Extraction Loaders`, `Nomba Provider Tests`, `KYC / BVN Encryption`, `Date Parser`, `LangGraph Decision Agent`, `Telegram Handlers & Link Codes`, `Payout State Machine`, `Security & JWT`, `Settings & Config`, `Scheduler Jobs`, `Auth Service`, `Crypto Primitives`, `Refresh Tokens & Session`, `Bill Model & Status`, `Auth Endpoints Tests`, `Virtual Account Model`, `BVN Schema`, `Misc Documentation`?**
  _High betweenness centrality (0.146) - this node is a cross-community bridge._
- **Why does `PaymentProvider` connect `Bills API & Metrics` to `Bill Upload Conversation`, `Bills Tests`, `Payment Provider Protocol`, `Database & Migrations`, `Wallet API & Enums`, `Nomba Provider Implementation`, `Webhook Test Helpers`, `Auth API`, `Payout State Machine`, `Security & JWT`, `Payout Tests`, `Scheduler Jobs`?**
  _High betweenness centrality (0.042) - this node is a cross-community bridge._
- **Are the 49 inferred relationships involving `User` (e.g. with `TelegramLinkCodeResponse` and `ProvisionResponse`) actually correct?**
  _`User` has 49 INFERRED edges - model-reasoned connections that need verification._
- **Are the 27 inferred relationships involving `Transaction` (e.g. with `ProvisionResponse` and `TopupRequest`) actually correct?**
  _`Transaction` has 27 INFERRED edges - model-reasoned connections that need verification._
- **Are the 24 inferred relationships involving `Bill` (e.g. with `cancel_bill()` and `get_bill()`) actually correct?**
  _`Bill` has 24 INFERRED edges - model-reasoned connections that need verification._
- **Are the 43 inferred relationships involving `timedelta` (e.g. with `create_telegram_link_code()` and `date_from_quickpick()`) actually correct?**
  _`timedelta` has 43 INFERRED edges - model-reasoned connections that need verification._