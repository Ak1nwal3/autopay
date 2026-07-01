# Graph Report - .  (2026-07-01)

## Corpus Check
- Corpus is ~30,560 words - fits in a single context window. You may not need a graph.

## Summary
- 841 nodes · 2124 edges · 53 communities (31 shown, 22 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 79 edges (avg confidence: 0.63)
- Token cost: 56,707 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Wallet API & Provisioning|Wallet API & Provisioning]]
- [[_COMMUNITY_Telegram Link Codes & Logging|Telegram Link Codes & Logging]]
- [[_COMMUNITY_Bills API|Bills API]]
- [[_COMMUNITY_Bill Due Date Parsing|Bill Due Date Parsing]]
- [[_COMMUNITY_KYC API|KYC API]]
- [[_COMMUNITY_Paystack Webhook Handling|Paystack Webhook Handling]]
- [[_COMMUNITY_Health Checks & DB Init|Health Checks & DB Init]]
- [[_COMMUNITY_LangGraph Decision Agent|LangGraph Decision Agent]]
- [[_COMMUNITY_Auth API Endpoints|Auth API Endpoints]]
- [[_COMMUNITY_Custom Exceptions|Custom Exceptions]]
- [[_COMMUNITY_Auth Service & JWT Sessions|Auth Service & JWT Sessions]]
- [[_COMMUNITY_SQLModel Model Tests|SQLModel Model Tests]]
- [[_COMMUNITY_JWT Security Helpers|JWT Security Helpers]]
- [[_COMMUNITY_Payment Provider Abstraction|Payment Provider Abstraction]]
- [[_COMMUNITY_CI & Docker Compose Infra|CI & Docker Compose Infra]]
- [[_COMMUNITY_Paystack Provider Tests|Paystack Provider Tests]]
- [[_COMMUNITY_Payment Provider Interface|Payment Provider Interface]]
- [[_COMMUNITY_Auth Endpoint Integration Tests|Auth Endpoint Integration Tests]]
- [[_COMMUNITY_Paystack Provider Implementation|Paystack Provider Implementation]]
- [[_COMMUNITY_Test Fixtures & Conftest|Test Fixtures & Conftest]]
- [[_COMMUNITY_Payout Money Math Tests|Payout Money Math Tests]]
- [[_COMMUNITY_Password Hashing|Password Hashing]]
- [[_COMMUNITY_Wallet Endpoint Tests|Wallet Endpoint Tests]]
- [[_COMMUNITY_KYC Endpoint Tests|KYC Endpoint Tests]]
- [[_COMMUNITY_Smoke Tests|Smoke Tests]]
- [[_COMMUNITY_System Architecture Overview|System Architecture Overview]]
- [[_COMMUNITY_HTTP Request Helpers|HTTP Request Helpers]]
- [[_COMMUNITY_Baseline Migration|Baseline Migration]]
- [[_COMMUNITY_Transaction DTOs|Transaction DTOs]]
- [[_COMMUNITY_Agents Package Init|Agents Package Init]]
- [[_COMMUNITY_Core Package Init|Core Package Init]]
- [[_COMMUNITY_App Package Init|App Package Init]]
- [[_COMMUNITY_Schemas Package Init|Schemas Package Init]]
- [[_COMMUNITY_Services Package Init|Services Package Init]]
- [[_COMMUNITY_Deferred DVA Decision|Deferred DVA Decision]]
- [[_COMMUNITY_Webhook Replay Defense Decision|Webhook Replay Defense Decision]]
- [[_COMMUNITY_Docker Entrypoint Script|Docker Entrypoint Script]]
- [[_COMMUNITY_DB Integration Tests Init|DB Integration Tests Init]]
- [[_COMMUNITY_Unit Tests Init|Unit Tests Init]]
- [[_COMMUNITY_Project Root|Project Root]]
- [[_COMMUNITY_Auth API Route Group|Auth API Route Group]]
- [[_COMMUNITY_Project README|Project README]]
- [[_COMMUNITY_Bills API Route Group|Bills API Route Group]]
- [[_COMMUNITY_Health Endpoints Route Group|Health Endpoints Route Group]]
- [[_COMMUNITY_KYC API Route Group|KYC API Route Group]]
- [[_COMMUNITY_Ngrok Webhook Testing|Ngrok Webhook Testing]]
- [[_COMMUNITY_Production Checklist|Production Checklist]]
- [[_COMMUNITY_Telegram Bot Commands|Telegram Bot Commands]]
- [[_COMMUNITY_Wallet API Route Group|Wallet API Route Group]]
- [[_COMMUNITY_Webhooks Route Group|Webhooks Route Group]]

## God Nodes (most connected - your core abstractions)
1. `User` - 67 edges
2. `parse_bill_due_date()` - 38 edges
3. `write_audit()` - 34 edges
4. `Bill` - 31 edges
5. `PaystackProvider` - 29 edges
6. `PaymentProvider` - 27 edges
7. `session_scope()` - 26 edges
8. `hash_password()` - 25 edges
9. `AuditActor` - 20 edges
10. `execute_payout()` - 19 edges

## Surprising Connections (you probably didn't know these)
- `CI Postgres Service (postgres:16-alpine)` --semantically_similar_to--> `db-test service (Postgres, test)`  [INFERRED] [semantically similar]
  .github/workflows/ci.yml → docker-compose.test.yml
- `db service (Postgres, dev)` --semantically_similar_to--> `db-test service (Postgres, test)`  [INFERRED] [semantically similar]
  docker-compose.yml → docker-compose.test.yml
- `app service (dev)` --semantically_similar_to--> `app-test service (test)`  [INFERRED] [semantically similar]
  docker-compose.yml → docker-compose.test.yml
- `FakeSession` --uses--> `AuditActor`  [INFERRED]
  tests/unit/test_audit.py → app/models/enums.py
- `FakeSession` --uses--> `AuditEventType`  [INFERRED]
  tests/unit/test_audit.py → app/models/enums.py

## Import Cycles
- 1-file cycle: `app/services/telegram.py -> app/services/telegram.py`

## Hyperedges (group relationships)
- **Postgres 16-alpine service defined per environment (CI, dev, test)** — _github_workflows_ci_postgres_service, docker_compose_db_service, docker_compose_test_db_test_service [INFERRED 0.85]
- **AutoPay AI request flow: bot/webhooks feed the FastAPI app, which drives Postgres, Paystack, LangGraph agent and APScheduler** — readme_telegram_bot, readme_fastapi_app, readme_paystack_webhook, readme_paystack_api, readme_langgraph_agent, readme_apscheduler_jobs, readme_postgres_db [EXTRACTED 1.00]
- **API endpoint groups exposed by the FastAPI app under /api/v1** — readme_auth_endpoints, readme_bills_endpoints, readme_kyc_endpoints, readme_wallet_endpoints, readme_webhooks_endpoints, readme_health_endpoints [EXTRACTED 1.00]

## Communities (53 total, 22 thin omitted)

### Community 0 - "Wallet API & Provisioning"
Cohesion: 0.06
Nodes (79): TelegramLinkCodeResponse, FastAPI routers — all HTTP endpoints under one namespace., provision_virtual_account(), ProvisionResponse, Session, Wallet API — balance, virtual account provisioning.  Mounted at /api/v1/wallet, Wire format for the user's virtual account., Idempotent. If the user already has a VA, returns it (200 OK     semantically, (+71 more)

### Community 1 - "Telegram Link Codes & Logging"
Cohesion: 0.06
Nodes (77): create_telegram_link_code(), Generate a short-lived (15 min) code the user pastes into the     Telegram bot, Context-manager variant for non-FastAPI callers (workers, scripts)., session_scope(), get_logger(), LoggerAdapter, Any, Structured logging configuration.  Call `setup_logging()` once at app startup; (+69 more)

### Community 2 - "Bills API"
Cohesion: 0.07
Nodes (44): ABC, cancel_bill(), create_bill(), get_bill(), list_bills(), pay_bill(), Session, Bills API — upload, list, get, pay, cancel.  Mounted at /api/v1/bills in `app. (+36 more)

### Community 3 - "Bill Due Date Parsing"
Cohesion: 0.08
Nodes (52): _clamp_to_present(), _now(), parse_bill_due_date(), datetime, Robust date parsing for LLM-extracted bill due dates.  LLMs return dates in ev, If the parsed date is in the past, treat it as an OCR mistake     and use `now(, ISO 8601 — full datetime, date-only, with/without TZ, with/without microseconds., Parse 'today', 'tomorrow', 'in 2 weeks', '5 days from today', etc. (+44 more)

### Community 4 - "KYC API"
Cohesion: 0.07
Nodes (47): get_kyc(), Session, KYC API — BVN submission.  Mounted at /api/v1/kyc in `app.main`., Encrypt the BVN with Fernet, store its hash + last4, write audit., submit_bvn(), KycRecord, KYC (Know Your Customer) record — holds encrypted BVN.  The BVN (Bank Verifica, KycStatusResponse (+39 more)

### Community 5 - "Paystack Webhook Handling"
Cohesion: 0.11
Nodes (39): Best-effort DVA provisioning. On any provider error, writes a     `va.created`, _try_provision_dva(), _handle_charge_success(), _handle_dva_assigned(), paystack_webhook(), Request, Session, User's VA received money. Credit their wallet and update txn.      Idempotent: (+31 more)

### Community 6 - "Health Checks & DB Init"
Cohesion: 0.08
Nodes (39): Session, Liveness and readiness probes.  - /healthz  liveness: process is up - /readyz, readyz(), init_db(), Create all tables. Used in tests; production uses Alembic., Configure root logger with a sensible formatter., setup_logging(), get_scheduler() (+31 more)

### Community 7 - "LangGraph Decision Agent"
Cohesion: 0.10
Nodes (36): build_graph(), Decimal, LangGraph state graph build.  Currently a single-node graph (the rule is the o, Build the LangGraph state graph., Invoke the graph. Equivalent to calling `decide()` directly —     exists so cal, run_agent(), decide(), decide_for_bill() (+28 more)

### Community 8 - "Auth API Endpoints"
Cohesion: 0.17
Nodes (24): invalidate_telegram_link_codes(), login(), logout(), me(), Request, Session, Auth API — signup, login, refresh, logout, me.  Mounted at /api/v1/auth in `ap, Create a user, issue tokens. DVA provisioning is best-effort     and gated by ` (+16 more)

### Community 9 - "Custom Exceptions"
Cohesion: 0.17
Nodes (22): AccountNameMismatch, AuthenticationError, InsufficientFunds, InvalidAccount, KYCRequired, PaymentError, ProviderError, Typed errors raised by payment provider implementations.  Business code (payou (+14 more)

### Community 10 - "Auth Service & JWT Sessions"
Cohesion: 0.14
Nodes (24): get_current_user(), get_optional_current_user(), _hash_refresh_token(), issue_tokens(), logout_user(), new_refresh_token_string(), datetime, Session (+16 more)

### Community 11 - "SQLModel Model Tests"
Cohesion: 0.09
Nodes (12): Tests for the SQLModel model definitions.  These verify that each model:   -, The Python attribute is event_metadata; the SQL column is 'metadata'., Codes are 6 hex chars (12 chars when decoded from token_hex(3))., NUMERIC(14,2) preserves 0.01 precision (unlike float)., BVN was extracted to kyc_records — must not appear in users., The MVP used 'payaza_reference' — we now use generic 'provider' + 'provider_refe, test_audit_log_metadata_column_named_metadata(), test_decimal_precision_in_arithmetic() (+4 more)

### Community 12 - "JWT Security Helpers"
Cohesion: 0.20
Nodes (19): create_access_token(), create_refresh_token(), decode_token(), JWTError_, Any, datetime, Password hashing + JWT helpers (bcrypt + python-jose).  These land in Chunk 2, Raised when a JWT is invalid, expired, or tampered with. (+11 more)

### Community 13 - "Payment Provider Abstraction"
Cohesion: 0.14
Nodes (13): Payment provider abstraction.  We never want to lock the business logic to a s, A dedicated virtual account (DVA) issued to a user by the provider.      `prov, Result of "look up the name behind this account number"., The provider's response when we initiated a transfer (payout)., A verified webhook from the provider.      `provider_reference` ties the event, ResolvedAccount, TransferResult, VirtualAccountData (+5 more)

### Community 14 - "CI & Docker Compose Infra"
Cohesion: 0.12
Nodes (20): Coverage Report & Upload Step, CI Environment Config, CI Postgres Service (postgres:16-alpine), Pytest Run Step, Ruff Lint Step, CI Workflow (pytest + ruff), app service (dev), autopay-net network (+12 more)

### Community 15 - "Paystack Provider Tests"
Cohesion: 0.19
Nodes (19): _client(), Tests for the Paystack provider.  We use `respx` to mock httpx calls so no rea, test_create_customer_auth_error(), test_create_customer_happy_path(), test_create_virtual_account_parses_response(), test_initiate_transfer(), test_kyc_required_error_mapping(), test_network_error_wrapped_as_provider_error() (+11 more)

### Community 16 - "Payment Provider Interface"
Cohesion: 0.11
Nodes (12): PaymentProvider, Issue a dedicated virtual account for `customer_code`., Look up the name on `account_number` at `bank_code`., Create a transfer recipient; return provider's recipient_code., Move `amount_kobo` (1 NGN = 100 kobo) from our balance to recipient., Return True iff `signature_header` is a valid HMAC of `raw_body`., Verify signature, then parse into a `WebhookEvent`. Raises on bad sig., The contract every payment-gateway implementation must satisfy. (+4 more)

### Community 17 - "Auth Endpoint Integration Tests"
Cohesion: 0.22
Nodes (18): Session, TestClient, Integration tests for the auth endpoints.  Coverage:   * signup  → 201, token, Swagger UI's 'Authorize' button + the curl 'Authorization' header     in 'Try i, RFC 6750 says a 401 on a Bearer-protected route must include     `WWW-Authentic, _signup_payload(), test_401_includes_www_authenticate_header(), test_login_401_on_bad_password() (+10 more)

### Community 18 - "Paystack Provider Implementation"
Cohesion: 0.20
Nodes (4): PaystackProvider, Make a Paystack call and return the `data` payload.          Raises a typed `P, AsyncClient, WebhookEvent

### Community 19 - "Test Fixtures & Conftest"
Cohesion: 0.19
Nodes (13): _clean_db(), client(), TestClient, Integration test fixtures.  These tests hit a real database (`autopay_test`)., Direct DB session for tests that need to set up data without HTTP., A fresh stub for each test. Overrides `get_payment_provider`., # IMPORTANT: also refresh the module-level `settings` shortcut. If, Truncate every table before each integration test. (+5 more)

### Community 20 - "Payout Money Math Tests"
Cohesion: 0.26
Nodes (11): _ngn_to_kobo(), ₦ → kobo (integer). Paystack wants whole kobo only., Tests for the payout service's money math.  These are pure unit tests — no DB,, Pin the rule that we use Decimal (not float) for money math., test_decimal_arithmetic_for_balance(), test_ngn_to_kobo_fractional(), test_ngn_to_kobo_large(), test_ngn_to_kobo_rounds_nearest_kobo() (+3 more)

### Community 21 - "Password Hashing"
Cohesion: 0.22
Nodes (11): hash_password(), Hash a plaintext password with bcrypt (cost factor 12)., Constant-time bcrypt comparison. Returns False on any error., verify_password(), authenticate_user(), Return the user if email+password match. Raises 401 otherwise., Create a new user. Raises 409 on duplicate email/phone.      Password is bcryp, signup_user() (+3 more)

### Community 22 - "Wallet Endpoint Tests"
Cohesion: 0.38
Nodes (10): Session, TestClient, Integration tests for the wallet endpoints (DVA provisioning)., _signup(), test_create_telegram_link_code(), test_create_telegram_link_code_requires_auth(), test_provision_requires_auth(), test_provision_virtual_account_creates_dva() (+2 more)

### Community 23 - "KYC Endpoint Tests"
Cohesion: 0.42
Nodes (9): _auth_header(), Session, TestClient, Integration tests for the KYC endpoints., test_bvn_must_be_11_digits(), test_get_kyc_404_when_absent(), test_get_kyc_returns_status(), test_submit_bvn_409_on_duplicate() (+1 more)

### Community 24 - "Smoke Tests"
Cohesion: 0.20
Nodes (9): Smoke tests — confirm the app skeleton imports and settings are valid.  These, Settings class can be constructed and exposes expected defaults., Pydantic should strip surrounding quotes from DATABASE_URL., Version literal is set in app/__init__.py., Helpers reflect the current environment.      Conftest sets ENVIRONMENT=test,, test_app_version_is_set(), test_database_url_quoted_handling(), test_is_production_and_is_test_helpers() (+1 more)

### Community 25 - "System Architecture Overview"
Cohesion: 0.29
Nodes (7): APScheduler Jobs, FastAPI App, LangGraph Decision Agent (pay-now/schedule/hold), Paystack API Integration (DVA / transfers / webhooks), Paystack Webhook (incoming charge.success / transfer.*), Postgres Database (8 tables), Telegram Bot (in-process)

### Community 26 - "HTTP Request Helpers"
Cohesion: 0.40
Nodes (4): client_ip(), Request, HTTP-related helpers used by multiple routers., Return the client's IP, or None if it isn't a valid IPv4/IPv6.      The `audit

### Community 27 - "Baseline Migration"
Cohesion: 0.40
Nodes (4): downgrade(), No-op. See module docstring., No-op. To wipe the schema, drop and recreate the database., upgrade()

## Knowledge Gaps
- **27 isolated node(s):** `auto-pay-ai`, `entrypoint.sh script`, `Ruff Lint Step`, `Coverage Report & Upload Step`, `AutoPay AI Project` (+22 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **22 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `User` connect `Bills API` to `Wallet API & Provisioning`, `Telegram Link Codes & Logging`, `Bill Due Date Parsing`, `KYC API`, `Health Checks & DB Init`, `LangGraph Decision Agent`, `Auth API Endpoints`, `Auth Service & JWT Sessions`, `SQLModel Model Tests`, `Payment Provider Abstraction`, `Auth Endpoint Integration Tests`, `Password Hashing`, `Wallet Endpoint Tests`, `KYC Endpoint Tests`?**
  _High betweenness centrality (0.160) - this node is a cross-community bridge._
- **Why does `parse_bill_due_date()` connect `Bill Due Date Parsing` to `Telegram Link Codes & Logging`, `Bills API`?**
  _High betweenness centrality (0.059) - this node is a cross-community bridge._
- **Why does `PaymentProvider` connect `Payment Provider Interface` to `Wallet API & Provisioning`, `Telegram Link Codes & Logging`, `Bills API`, `Paystack Webhook Handling`, `Auth API Endpoints`, `Custom Exceptions`, `Payment Provider Abstraction`, `Paystack Provider Implementation`?**
  _High betweenness centrality (0.057) - this node is a cross-community bridge._
- **Are the 6 inferred relationships involving `User` (e.g. with `TelegramLinkCodeResponse` and `ProvisionResponse`) actually correct?**
  _`User` has 6 INFERRED edges - model-reasoned connections that need verification._
- **What connects `AutoPay AI — bill automation platform.`, `LangGraph decision agent — should we pay, hold, or schedule a bill?  Three-sta`, `LangGraph state graph build.  Currently a single-node graph (the rule is the o` to the rest of the system?**
  _256 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Wallet API & Provisioning` be split into smaller, more focused modules?**
  _Cohesion score 0.05516596540439458 - nodes in this community are weakly interconnected._
- **Should `Telegram Link Codes & Logging` be split into smaller, more focused modules?**
  _Cohesion score 0.05616605616605617 - nodes in this community are weakly interconnected._