# AutoPay AI — Full Project Documentation

> AI-powered bill automation for Nigerian users. A user sends a bill (Telegram photo/PDF/text, or a web upload); an LLM extracts the vendor, amount, and due date; a LangGraph decision agent decides **pay now / schedule / hold**; and if it pays, the money moves out through **Paystack** — a dedicated virtual account (DVA) for top-ups, a bank transfer for payouts, and signed webhooks to confirm the outcome.

This document covers, in order:

1. [High-level architecture](#1-high-level-architecture)
2. [File & folder reference](#2-file--folder-reference) — what every file does
3. [Feature workflows](#3-feature-workflows) — step-by-step for each feature
4. [End-to-end project workflow](#4-end-to-end-project-workflow) — how it all fits together
5. [How the Paystack payment gateway is utilized](#5-how-the-paystack-payment-gateway-is-utilized) — the deep dive
6. [Known issues / gotchas](#6-known-issues--gotchas) — real bugs and risks found while documenting

---

## 1. High-level architecture

```
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│   Telegram Bot   │    │   Web / REST API │    │  Paystack Webhook│
│  (in-process)    │    │     (FastAPI)    │    │  (charge.success │
└────────┬─────────┘    └─────────┬────────┘    │  transfer.*)     │
         │                        │               └────────┬─────────┘
         │                        │                        │
         └────────────────┬───────┴───────────┬────────────┘
                           ▼                   ▼
                   ┌───────────────────────────────────┐
                   │            FastAPI app            │
                   │  /api/v1/auth /bills /kyc          │
                   │  /api/v1/wallet /telegram          │
                   │  /webhooks/paystack                │
                   │  /telegram/webhook                 │
                   └────────────────┬────────────────────┘
                                    │
        ┌───────────────┬──────────┼─────────────┐
        ▼               ▼          ▼              ▼
   ┌─────────┐   ┌─────────────┐ ┌──────────┐ ┌────────┐
   │Postgres │   │  Paystack   │ │ LangGraph│ │ APSched│
   │ 8 tables│   │ DVA/transfer│ │  agent   │ │  jobs  │
   │         │   │ /webhooks   │ │          │ │        │
   └─────────┘   └─────────────┘ └──────────┘ └────────┘
```

Everything — the FastAPI HTTP server, the Telegram bot, and the APScheduler background jobs — runs **in a single process** (see `app/main.py`'s `lifespan`). There is no separate worker process; `start_scheduler()` and `start_bot()` are both started from the same `asyncio` event loop FastAPI owns.

**Stack:** FastAPI + SQLModel/SQLAlchemy + Postgres, Alembic migrations, `python-telegram-bot` v21, LangGraph for the decision agent, Groq (Llama models via `instructor`) for LLM bill extraction, Paystack for money movement, APScheduler for background jobs, Docker/docker-compose for local dev and deploy.

---

## 2. File & folder reference

### Root

| Path | Purpose |
|---|---|
| `app/` | The entire application (see below). |
| `migrations/` | Alembic migration scripts. |
| `scripts/` | `entrypoint.sh` (container boot) and `seed.py` (demo data). |
| `tests/` | pytest suite — `unit/` and `integration/`. |
| `schema.sql` | Human-readable, authoritative Postgres schema. Mounted into the `db` container at first boot (`/docker-entrypoint-initdb.d`); Alembic migrations build on top of it. |
| `pyproject.toml` | Package metadata, all dependencies (runtime + dev), and tool config for `pytest`, `ruff`, `mypy`, `coverage`. |
| `alembic.ini` | Alembic CLI config — points at `migrations/`, sets the default DB URL (dev fallback). |
| `Dockerfile` | Two-stage build (builder compiles wheels for `bcrypt`/`psycopg2`/`cryptography`; runtime is a slim non-root image). Entry point is `scripts/entrypoint.sh`, default command runs `uvicorn`. |
| `docker-compose.yml` | Local/prod-like stack: `db` (Postgres 16, schema auto-loaded) + `app` (built from the Dockerfile). |
| `docker-compose.test.yml` | Isolated test stack: `db-test` (port 5433) + `app-test` which runs `pytest --cov=app` instead of `uvicorn`. |
| `demo.py` | **Not part of the app.** A standalone script that requests an OAuth token from a *Nomba* API (unrelated third-party payments API, not Paystack) using hardcoded client credentials. ⚠️ See [Known issues](#6-known-issues--gotchas) — this is a live secret checked into source control. |
| `README.md` | Env var reference, API route table, deploy instructions (Railway), production checklist. |
| `.env.example` / `.env.demo` | Environment variable templates. |
| `Makefile` | Dev commands (`make up`, `make test`, `make keygen`, `make seed`, `make ngrok`, etc. — see the Makefile itself for the full list). |
| `.github/workflows/ci.yml` | CI: spins up Postgres 16, runs `ruff` lint + `pytest --cov`. |

### `app/` — application package

```
app/
  __init__.py        version string ("0.2.0")
  main.py            FastAPI app, lifespan, router registration
  agents/            LangGraph pay/schedule/hold decision agent
  api/                HTTP routers
  core/               config, DB, security, logging, scheduler, http helpers
  handlers/           Telegram bot conversation handlers
  models/             SQLModel ORM tables
  schemas/            Pydantic request/response DTOs
  services/           business logic, incl. the Paystack integration
```

#### `app/main.py`
FastAPI entry point. The `lifespan` context manager runs on startup/shutdown: sets up logging, calls `init_db()` (dev/test convenience — production relies on Alembic), starts the APScheduler jobs, then starts the Telegram bot (order matters: scheduler first so a job firing on boot doesn't race the bot). Registers every router: unversioned `/healthz` `/readyz` `/`, versioned `/api/v1/{auth,bills,kyc,wallet}`, and unversioned `/webhooks/paystack` + `/telegram/webhook` (provider-side URLs Paystack/Telegram hardcode, so they aren't versioned). A catch-all exception handler returns a generic 500 so stack traces never leak.

#### `app/core/` — low-level primitives shared by everything

| File | Purpose |
|---|---|
| `config.py` | `Settings` (pydantic-settings), loaded once from `.env` into the module-level `settings` singleton. Holds DB, Paystack, Telegram, LLM, JWT, BVN-encryption, and fee config, plus computed flags (`telegram_enabled`, `llm_enabled`, `is_production`). Builds `database_url` from parts if not given directly. |
| `database.py` | SQLAlchemy engine (pool size 10, overflow 20, `pool_pre_ping`, 300s recycle) + `init_db()` (create-all, used by tests/dev) + `get_session()` (FastAPI dependency) + `session_scope()` (context-manager for non-request callers like the bot and the scheduler). |
| `security.py` | bcrypt password hashing (cost 12) + JWT access/refresh token creation and decoding (`python-jose`). |
| `http.py` | `client_ip()` — extracts a validated IP from a `Request` for the audit log's `INET` column (rejects TestClient's fake `"testclient"` host). |
| `logging.py` | `setup_logging()` configures the root logger; quiets noisy third-party loggers (`httpx`, `apscheduler`, `sqlalchemy.engine`, etc.). |
| `scheduler.py` | APScheduler wiring — two recurring jobs, detailed in [§3.7](#37-scheduler-jobs). |

#### `app/models/` — SQLModel ORM tables (mirrors `schema.sql`)

| Model | Table | Purpose |
|---|---|---|
| `User` | `users` | Core account: name, email/phone (unique), bcrypt hash, `balance` (`NUMERIC(14,2)`), Telegram link state. No BVN here. |
| `KycRecord` | `kyc_records` | One-to-one with `User`. Stores BVN as Fernet ciphertext + HMAC hash (uniqueness) + last-4 (display) — never plaintext. |
| `VirtualAccount` | `virtual_accounts` | One-to-one with `User`. The Paystack DVA: `account_number`, `account_name`, `bank_name`, `provider_account_reference` (Paystack's `dedicated_account_id`). |
| `Bill` | `bills` | A vendor invoice to pay: amount, due date, destination account/bank, status (`pending → scheduled/processing → paid/failed/cancelled`), recurrence fields, retry count. |
| `Transaction` | `transactions` | Every wallet credit/debit. `provider` + `provider_reference` (unique) tie it back to a Paystack charge or transfer. |
| `AuditLog` | `audit_logs` | Append-only event log — `actor`, `event_type`, polymorphic `entity_type`/`entity_id`, JSONB `before_state`/`after_state`/`metadata` (column name `metadata`, Python attribute `event_metadata` because `metadata` is reserved by SQLAlchemy). |
| `RefreshToken` | `refresh_tokens` | Only the SHA-256 hash of each refresh JWT is stored, so a DB leak alone can't grant login. |
| `TelegramLinkCode` | `telegram_link_codes` | Short-lived 6-char codes used to link a Telegram chat to a web account. |
| `WebhookEvent` | `webhook_events` | Dedup ledger: `UNIQUE(provider, event_id)` — the mechanism behind Paystack webhook replay defense. |

`app/models/__init__.py` imports every model so `SQLModel.metadata` is fully populated for both Alembic autogenerate and `init_db()`.

#### `app/schemas/` — Pydantic DTOs (the wire format)
Deliberately separate from the ORM models so internal columns (`hashed_password`, `bvn_ciphertext`) can never leak into an API response, and so request validation (e.g. password strength) doesn't pollute the DB model.

- `auth.py` — signup/login/refresh/logout request bodies, `TokenResponse`, `UserPublic`, `WalletBalance`.
- `bill.py` — `BillExtractionResult` (what the LLM/loader produces — `due_date` is a loose `str`, deliberately, because LLMs emit inconsistent date formats), `BillCreateRequest`, `BillResponse`, `BillActionResponse`.
- `kyc.py` — `KycSubmitRequest` (validates 11-digit BVN), `KycStatusResponse`.
- `transaction.py` — `TransactionResponse`.

#### `app/api/` — HTTP routers (all mounted from `app/main.py`)

- **`auth.py`** (`/api/v1/auth`) — signup, login, refresh, logout, `/me`, `/wallet` (balance), Telegram link-code issue/invalidate/unlink. Signup optionally provisions a DVA inline (best-effort, gated by `AUTO_PROVISION_DVA_ON_SIGNUP`).
- **`bills.py`** (`/api/v1/bills`) — upload (file or text) → LLM extraction → decision agent → optional immediate payout; plain JSON create; list/get/pay/cancel.
- **`kyc.py`** (`/api/v1/kyc`) — submit BVN (encrypt + hash + audit), get KYC status.
- **`wallet.py`** (`/api/v1/wallet`) — `POST /provision`, the user-facing escape hatch to create a DVA when signup didn't (idempotent).
- **`webhooks.py`** (`/webhooks/paystack`) — the inbound Paystack webhook receiver: signature verification, replay dedup, dispatch to charge/transfer/DVA handlers. Detailed in [§5](#5-how-the-paystack-payment-gateway-is-utilized).
- **`health.py`** (unversioned) — `/healthz` (liveness), `/readyz` (DB check), `/` (banner).

#### `app/handlers/` — Telegram bot conversation logic
- **`auth.py`** — `/start`, `/link CODE`, `/unlink`, `/wallet`, `/bills`, `/help`.
- **`bill_conversation.py`** — the multi-step bill-upload conversation (`ConversationHandler` state machine: `CONFIRM → CHOOSE_FIELD/EDIT_VALUE → FINAL_CONFIRM`). Detailed in [§3.3](#33-bill-upload--extraction-telegram-or-web).
- **`helpers.py`** — shared: `get_linked_user()` (chat_id → `User`), inline keyboards, Markdown-safe formatting, bot-side date parsing.

#### `app/services/` — business logic
- **`auth.py`** — signup/login/token-issuance logic and the FastAPI auth dependencies (`get_current_active_user` etc.) used by every protected router.
- **`audit.py`** — `write_audit()` plus one convenience wrapper per event type. Every wrapper appends to the *same* SQLAlchemy session as the business write it accompanies (no separate audit worker — atomic by construction).
- **`crypto.py`** — Fernet encryption + HMAC hashing for BVNs.
- **`date_parser.py`** — `parse_bill_due_date()`, a tolerant multi-format date parser for LLM-extracted due dates (see docstring for the full format list); clamps past dates to "now" (treats them as OCR mistakes).
- **`loaders.py`** — `TextLoader` / `PDFLoader` (PyMuPDF) / `ImageLoader` (vision LLM), all producing a `BillExtractionResult`. Falls back to regex extraction if no `GROQ_API_KEY` is set.
- **`payout.py`** — `execute_payout()` / `confirm_payout()` / `schedule_recurrence()` — the payout state machine. Detailed in [§5.3](#53-payouts-outbound-transfers).
- **`telegram.py`** — builds and starts/stops the `python-telegram-bot` `Application`, in either webhook or polling mode; exposes the `/telegram/webhook` route.
- **`payments/`** — the payment-gateway abstraction:
  - `base.py` — the `PaymentProvider` Protocol + frozen-dataclass DTOs (`VirtualAccountData`, `ResolvedAccount`, `TransferResult`, `WebhookEvent`). Business code depends **only** on this interface.
  - `paystack.py` — the concrete `PaystackProvider` implementation (HTTP calls, webhook signature verification, error mapping) + the `get_payment_provider()` factory.
  - `exceptions.py` — typed exception hierarchy (`PaymentError` → `ProviderError`, `AuthenticationError`, `InvalidAccount`, `AccountNameMismatch`, `InsufficientFunds`, `KYCRequired`, `WebhookSignatureError`).

#### `app/agents/` — LangGraph decision agent
- **`state.py`** — `Decision` enum (`pay_now`/`schedule`/`hold`), `AgentState` TypedDict (all numerics as strings — LangGraph checkpoints don't round-trip `Decimal`), `DecisionResult`.
- **`nodes.py`** — `decide()`, the pure decision rule (unit-testable without LangGraph): **hold** if balance < amount+fee; else **pay_now** if due in ≤3 days; else **schedule**.
- **`graphs.py`** — `build_graph()` wraps `decide()` in a (currently single-node) `StateGraph`; `run_agent()` is the public entry point everything else calls.

### `migrations/` (Alembic)
- `env.py` — wires Alembic to `app.core.config.settings.database_url` and `SQLModel.metadata`.
- `versions/0001_baseline.py` — no-op marker (the real baseline is `schema.sql`, loaded by Postgres's init-db mechanism).
- `versions/0002_webhook_events.py` — adds the `webhook_events` table (the replay-defense mechanism was added after the baseline).

### `scripts/`
- `entrypoint.sh` — container boot: waits for Postgres to accept connections (up to 60s), runs `alembic upgrade head` (unless `SKIP_MIGRATIONS=1`), then execs the container `CMD` (uvicorn).
- `seed.py` — idempotent dev seed: 2 demo users (Ada, Tunde) with KYC, a DVA, an initial top-up transaction, and one demo bill for Ada. Skips if any user already exists.

### `tests/`
Split into `unit/` (no DB/network — decision agent, date parser, crypto, security, Paystack provider mocked via `respx`, payment math, models) and `integration/` (real Postgres — auth/bill/kyc/wallet/webhook endpoints, DB rollback behavior). README reports 178 passing tests at last count.

---

## 3. Feature workflows

### 3.1 Signup & authentication

1. `POST /api/v1/auth/signup` → `signup_user()` bcrypt-hashes the password, inserts the `User` row (409 on duplicate email/phone), writes a `user.signup` audit row.
2. If `AUTO_PROVISION_DVA_ON_SIGNUP=true`, `_try_provision_dva()` runs synchronously: creates a Paystack customer, then a dedicated virtual account. Any failure is caught and audit-logged with `status: failed` — **signup still succeeds** (the user row already committed).
3. `issue_tokens()` mints a 15-minute access JWT and a 7-day refresh JWT; the refresh token's SHA-256 hash (not the token itself) is stored in `refresh_tokens`.
4. Client stores both tokens; subsequent requests send `Authorization: Bearer <access_token>`.
5. `POST /api/v1/auth/refresh` exchanges a valid, unrevoked refresh token for a new pair, **revoking the old one** in the same transaction. Presenting an already-revoked or expired refresh token is treated as possible token theft: **every** refresh token for that user is revoked, forcing a fresh login.
6. `POST /api/v1/auth/logout` revokes one refresh token (if given) or all of them (sign-out-everywhere).

### 3.2 Linking a Telegram account

1. Web dashboard calls `POST /api/v1/auth/telegram/link-code` (authenticated) → a fresh 6-char code (15-minute TTL) in `telegram_link_codes`, plus a `t.me/<bot>?start=<code>` deep link.
2. User sends `/link CODE` to the bot.
3. `link_command()` resolves the code (must be unused and unexpired), refuses to steal a chat already linked to a *different* account, then sets `User.telegram_chat_id` + `is_telegram_linked = True` and marks the code used. Writes a `user.telegram_linked` audit row.
4. From here, every bot command resolves the user via `get_linked_user(chat_id)`.

### 3.3 Bill upload & extraction (Telegram or web)

Both entry points converge on the same extraction → decision → payout pipeline; only the UI differs.

**Web** (`POST /api/v1/bills/upload`):
1. Accepts a file (PDF/image) or a `request_bill` text field.
2. `loader_from_upload()` picks `PDFLoader` / `ImageLoader` / `TextLoader` by content-type/extension.
3. `loader.extract()` calls Groq (via `instructor`, structured output into `BillExtractionResult`) if `GROQ_API_KEY` is set; otherwise falls back to regex-only extraction (vendor name is left blank — the LLM is the only path that identifies a vendor).
4. `parse_bill_due_date()` coerces whatever due-date string the LLM returned into a real `datetime`, clamping past dates to "now."
5. The `Bill` row is created (`status=pending`), audited (`bill.created`).
6. `run_agent()` (the LangGraph decision agent) decides `pay_now` / `schedule` / `hold` based on balance, amount+fee, and days until due.
7. `pay_now` → `execute_payout()` runs immediately (payout may still be deferred if funds are insufficient — the bill stays in the DB for a later retry). `schedule` → bill status flips to `scheduled` (the APScheduler job re-evaluates it later). `hold` is just informational at upload time — the caller sees the reason in the response.

**Telegram** (`bill_conversation.py`, a `ConversationHandler` state machine):
1. `receive_bill` — text/photo/PDF triggers extraction (same loaders as web); shows a summary with inline **Confirm / Edit / Cancel** buttons.
2. **Confirm** → `handle_confirm`: persists the `Bill` row, runs the same decision agent.
   - `pay_now` → shows amount/fee/total/balance-after and asks for **final confirmation** (`FINAL_CONFIRM` state) before actually spending money.
   - `schedule` → tells the user it'll be processed automatically when due; conversation ends.
   - `hold` → tells the user to top up; conversation ends.
3. **Edit** → `handle_edit` shows a field picker (`CHOOSE_FIELD`); picking a field prompts for a new value (`EDIT_VALUE`), validates it (amount must parse as a number, date must parse), then returns to the confirm summary.
4. **Final confirm** → `handle_final_confirm` calls `execute_payout()` for real and reports success/failure back into the chat.
5. **Cancel**, at any stage, marks the persisted bill `cancelled` (if one exists) and clears conversation state.

### 3.4 The decision agent (pay now / schedule / hold)

Pure rule in `app.agents.nodes.decide()`, wrapped in a single-node LangGraph graph (room to grow into a multi-step LLM-assisted decision later, but currently deterministic and unit-testable without LangGraph at all):

```
total = bill_amount + fee
if user_balance < total:          → HOLD      ("insufficient balance, shortfall ₦X")
elif days_until_due <= 3:         → PAY_NOW   ("due soon, balance sufficient")
else:                             → SCHEDULE  ("due later, re-evaluate closer to date")
```
A negative `days_until_due` (already overdue) falls into the `PAY_NOW` branch.

### 3.5 KYC (BVN submission)

1. `POST /api/v1/kyc/bvn` (409 if already submitted for this user).
2. `encrypt_bvn()` (Fernet, reversible — needed to resubmit to a provider for validation) and `hash_bvn()` (HMAC-SHA256 with a pepper derived from `BVN_ENCRYPTION_KEY`, one-way, used for uniqueness checks) both run on the plaintext BVN; only ciphertext + hash + last-4 are ever persisted.
3. Audit row `kyc.bvn_submitted` records only the last 4 digits.
4. `bvn_validated` stays `False` until (in a fuller implementation) a provider callback confirms it — that confirmation path is not wired up yet in this codebase.

### 3.6 Wallet / virtual-account provisioning

- `POST /api/v1/wallet/provision` — idempotent: if a `VirtualAccount` already exists for the user, returns it with `already_existed: true`. Otherwise creates a Paystack customer + DVA, persists the `VirtualAccount` row, and audits `va.created`. A Paystack failure here returns `502` (not silently swallowed, unlike the signup-time best-effort path) since the user explicitly asked for provisioning.
- The Telegram `/wallet` command reads the same `VirtualAccount` row and shows the user their top-up bank/account/name so they can fund it via a normal bank transfer.

### 3.7 Scheduler jobs

Two APScheduler jobs, registered in `app.core.scheduler.start_scheduler()`, running in the same process as FastAPI (`AsyncIOScheduler` in production, `BackgroundScheduler` in scripts/tests where there's no running event loop):

1. **`process_scheduled_bills`** — every minute. Finds `Bill` rows with `status='scheduled'` and `due_date <= now`, re-runs the decision agent for each, and if it now says `pay_now`, flips the bill to `pending` (the actual payout call is deliberately deferred to a user action or a follow-up job in this MVP, rather than auto-firing from the scheduler thread).
2. **`process_recurring_bills`** — every 6 hours. Finds bills with `is_recurring=True` whose `next_recurrence_date` has passed, and calls `schedule_recurrence()` to spawn the next occurrence (copies vendor/account/amount, advances the due date by 30 days for `monthly` or 7 for anything else), writing a `bill.recurrence_created` audit row.

Both jobs are wrapped so an exception in one bill's processing is logged and skipped, not fatal to the batch.

### 3.8 Audit logging

Every state-changing operation in the codebase (signup, login, logout, Telegram link, bill created, wallet credit/debit, payout attempted/succeeded/failed, DVA created, KYC submitted, webhook received/replay/unknown) writes one `AuditLog` row via `app.services.audit.write_audit()` (or one of its named wrappers), **inside the same DB transaction** as the business write it documents. There is no separate audit pipeline to fall out of sync — a rollback of the business write rolls back its audit row too.

---

## 4. End-to-end project workflow

A single illustrative journey, tying every feature together:

1. **Onboarding** — user signs up via the web API (`/api/v1/auth/signup`), gets a JWT pair.
2. **Fund the wallet** — user calls `/api/v1/wallet/provision` (or it happened automatically at signup, if enabled). Paystack returns a dedicated NUBAN. The user transfers money to that account number from their own bank.
3. **Paystack confirms the top-up** — Paystack POSTs `charge.success` to `/webhooks/paystack`. The webhook handler verifies the HMAC-SHA512 signature, dedups on `(provider, event_id)`, finds the matching `Transaction` row (created ahead of time if the flow pre-creates one, or logged as an "orphan credit" if not), credits `User.balance`, and writes a `wallet.credited` audit row.
4. **Link Telegram** — user requests a link code from the dashboard and sends `/link CODE` to the bot. Now they can manage bills conversationally.
5. **A bill arrives** — the user photographs a bill and sends it to the bot (or uploads it via the web API). The image is sent to a vision-capable Groq model, which returns vendor/amount/due-date/account/bank. `parse_bill_due_date()` normalizes the due date.
6. **The agent decides** — `run_agent()` compares the user's wallet balance against `amount + payout_fee_ngn` and the days remaining until the due date, returning `pay_now`, `schedule`, or `hold`.
   - If `hold`, the user is told to top up (loop back to step 2/3).
   - If `schedule`, the bill sits at `status=scheduled` until `process_scheduled_bills` (every minute) or the due date itself brings it back into play.
   - If `pay_now`, the flow proceeds to payout — on Telegram, gated by one more explicit confirmation tap.
7. **Payout execution** (`execute_payout`) — locks the bill and user rows (`SELECT ... FOR UPDATE`), re-checks balance, marks the bill `processing`, debits the wallet and records a `processing` `Transaction`, resolves the destination account name with Paystack, creates a transfer recipient, and initiates the transfer. The bill stays `processing` — it is **not** yet `paid`.
8. **Paystack confirms the transfer** — some time later, Paystack POSTs `transfer.success` (or `.failed`/`.reversed`) to the same webhook endpoint. `confirm_payout()` matches the transaction by `provider_reference`, and either marks the bill `paid` (success) or refunds the wallet and reschedules/fails the bill (up to `max_retries`), each with its own audit row.
9. **Recurring bills** (if the bill was flagged recurring) — once paid, `process_recurring_bills` (every 6 hours) eventually spawns the next occurrence, 30 (or 7) days out, and the cycle repeats from step 5 automatically.
10. Throughout, every step above leaves an immutable trail in `audit_logs`, queryable per user, per entity, or per event type — this is the system's ground truth for "what happened and why," independent of application logs.

---

## 5. How the Paystack payment gateway is utilized

Paystack is never called directly from business code. Everything goes through the `PaymentProvider` Protocol (`app/services/payments/base.py`), implemented today by `PaystackProvider` (`app/services/payments/paystack.py`) and injected via the `get_payment_provider()` FastAPI dependency — so the entire payments layer is swappable (a second gateway would just be a second class implementing the same Protocol) and trivially mockable in tests (`respx` mocks the `httpx` calls; see `tests/unit/test_paystack.py`).

All amounts cross the Paystack boundary in **kobo** (`1 NGN = 100 kobo`, integer), via `_ngn_to_kobo()` in `payout.py`; everywhere else in the app, money is `Decimal` NGN.

### 5.1 Customer + Dedicated Virtual Account (DVA) — inbound funding

Two Paystack calls create a user's permanent top-up account:

1. `POST /customer` → `create_customer()` returns a `customer_code`.
2. `POST /dedicated_account` (with that `customer_code`) → `create_virtual_account()` returns the account number, account name, bank name/code, and Paystack's `dedicated_account_id` (stored as `VirtualAccount.provider_account_reference`).

This happens either:
- **Inline at signup**, if `AUTO_PROVISION_DVA_ON_SIGNUP=true` (best-effort — failure is audit-logged, signup still succeeds); or
- **On explicit request**, via `POST /api/v1/wallet/provision` (the recommended default — see the "deferred DVA" architecture decision below).

> **Why deferred by default:** Paystack's Dedicated NUBAN product requires business approval. Turning on inline provisioning before approval means *every* signup silently fails the DVA step. `wallet/provision` lets the team flip this on per-user once approved, without a redeploy.

Once provisioned, the user transfers money to that dedicated account number from any Nigerian bank, exactly like paying a regular bank account. Paystack detects the inbound transfer and fires a webhook — the app never polls for balance.

### 5.2 Inbound confirmation — the `charge.success` webhook

`POST /webhooks/paystack` → `_handle_charge_success()`:
1. Looks up a `Transaction` by `provider_reference` (Paystack's charge reference).
2. If none exists, it's an "orphan credit" (money arrived before the app had a matching row) — logged and audited but not credited (nothing to credit against).
3. If the transaction is already `success`, no-op (idempotent).
4. Otherwise, converts `amount_kobo` to NGN, adds it to `User.balance`, flips the transaction to `success`, and writes a `wallet.credited` audit row — all in one commit.

### 5.3 Payouts (outbound transfers)

`execute_payout()` (`app/services/payout.py`) is the most carefully engineered piece of the payments layer — it is written to guarantee **the wallet is never double-debited and never left silently inconsistent**, even under concurrent requests or a mid-flight Paystack failure:

1. **Row locks**: `SELECT ... FOR UPDATE` on both the `Bill` and the `User` row, in the same transaction, so two concurrent "pay this bill" requests serialize at the database rather than racing on an in-memory check.
2. **State + balance checks**: refuses to pay an already-paid, cancelled, or in-flight bill; refuses if `balance < amount + fee` (writes a `payout.attempted`→failed audit row with the shortfall and returns `402`).
3. **Debit-then-call**: the bill flips to `processing`, a `Transaction` row is created in `processing` status, and *then* Paystack is called — never the other way around, so a webhook that arrives before the HTTP response can't race an uncommitted debit.
4. **Account resolution** (`GET /bank/resolve`) — looks up the real account name behind the destination account number/bank code. If the bill's stored vendor name doesn't match, this is logged (not hard-failed — vendor names are user-entered and often differ from the bank's official name).
5. **Transfer recipient** (`POST /transferrecipient`) — Paystack requires a recipient object before a transfer; `create_transfer_recipient()` creates one from the resolved account.
6. **Transfer** (`POST /transfer`, `source: "balance"`) — `initiate_transfer()` sends the amount in kobo with our own idempotent `reference` (format `autopay_<bill_id>_<uuid12>`), which Paystack echoes back and which becomes the join key for the eventual webhook.
7. **Any typed `PaymentError`** raised at steps 4–6 (`InvalidAccount`, `AccountNameMismatch`, `InsufficientFunds` — meaning *Paystack's* merchant balance, not the user's — or a generic `ProviderError`) triggers `_refund_on_failure()`: the debit transaction is marked `failed` with a reason, the bill's `retry_count` increments (moving to `failed` once `max_retries` is hit, otherwise back to `scheduled`), and a `payout.failed` audit row is written. **The wallet balance is never touched in this path** — it was only ever "reserved" conceptually via the transaction row, not actually decremented yet.
8. **On success**, the wallet balance *is* now decremented by `amount + fee`, and the transaction stays in `processing` — final settlement is deferred to the webhook (step 5.4). The function returns `"Transfer initiated; awaiting provider confirmation."`, not `"paid."`

### 5.4 Outbound confirmation — `transfer.success` / `transfer.failed` / `transfer.reversed`

`_handle_transfer_update()` → `confirm_payout()`:
- Matches the pending `Transaction` by `provider_reference`.
- Idempotent: if the transaction is already in a terminal state (`success`/`failed`), returns "Already reconciled" and changes nothing — protects against Paystack's own webhook retries in addition to the dedup layer below.
- **Success** → transaction → `success`, bill → `paid`, `payout.succeeded` audit row.
- **Failure/reversal** → transaction → `failed` (with the specific failure reason), **the wallet is refunded** (`amount + fee` added back — the fee is refunded too since it was never actually paid out), bill's `retry_count` increments (→ `failed` at `max_retries`, else back to `scheduled` for a future retry), `payout.failed` audit row.

### 5.5 Webhook security: signature verification + replay defense

Both defenses are load-bearing and mandatory before any business logic runs:

- **Signature verification** — Paystack signs the raw request body with HMAC-SHA512 using the account's secret key, sent as `x-paystack-signature`. `verify_webhook_signature()` recomputes the digest over the **raw, unparsed** body (`hmac.compare_digest`, constant-time) — this must happen *before* JSON parsing, or a forged request could be crafted to parse successfully while carrying a bad signature. A bad signature → `400`, nothing is persisted, nothing runs.
- **Replay defense** — Paystack retries webhook delivery on any network blip, so the *same* event can arrive twice. The handler inserts a row into `webhook_events` with a `UNIQUE(provider, event_id)` constraint *before* dispatching to a handler; a second delivery hits an `IntegrityError`, rolls back, writes a `webhook.replay` audit row, and returns `200` (Paystack must see success or it will keep retrying forever) without re-running the charge/transfer logic. `event_id` is Paystack's own `event.id` when present, or a SHA-256 of the raw body as a fallback for older payloads that omit it — either way, the *same* event always produces the *same* `event_id`, so the unique constraint actually catches the duplicate.

### 5.6 Error mapping

Paystack's error responses are just JSON with a human-readable `message` — there's no stable machine-readable error code in the free-tier API. `_map_paystack_error()` in `paystack.py` pattern-matches on the HTTP status and known message substrings (`"Invalid key"` → `AuthenticationError`, `"Account name...mismatch"` → `AccountNameMismatch`, `"Insufficient"`/`"balance"` → `InsufficientFunds`, `"KYC"`/`"BVN"`/`"identity"` → `KYCRequired`, account-resolution failures → `InvalidAccount`, everything else → generic `ProviderError`). This is inherently a little fragile (Paystack could change wording), but it means business code (`payout.py`) can `except InsufficientFunds` etc. instead of string-matching everywhere itself.

### 5.7 Provider abstraction as a design choice

`PaymentProvider` is a `typing.Protocol`, not an ABC — chosen so tests can substitute a duck-typed fake without inheriting from anything, and so a second gateway (Flutterwave, say) is a drop-in `FlutterwaveProvider` class with zero changes anywhere else in the codebase, selected by the (currently missing — see [§6](#6-known-issues--gotchas)) `settings.payment_provider` switch in `get_payment_provider()`.

---

## 6. Known issues / gotchas

Found while reading the code — worth fixing or at least being aware of before relying on this documentation to operate the system:

1. **`settings.payment_provider` does not exist.** `app/services/payments/paystack.py:333` reads `settings.payment_provider`, but `Settings` in `app/core/config.py` has no such field. Every call to `get_payment_provider()` — i.e. every request that touches signup, bills, wallet, or webhooks — will raise `AttributeError` at runtime unless something else patches `Settings` first. This is the single most impactful bug for actually running the app; a fix would add `payment_provider: str = "paystack"` to `Settings`.
2. **`settings.app_name` does not exist.** `app/api/health.py:42` (`GET /`, the banner route) reads `settings.app_name`, also absent from `Settings`. This route will 500 whenever it's hit.
3. **Hardcoded secrets committed in `app/core/config.py`.** Default values for `telegram_bot_token`, `groq_api_key`, `langchain_api_key`, `jwt_secret_key`, and `bvn_encryption_key` are real-looking, non-empty strings baked into the source, not just placeholders — meaning a fresh clone with no `.env` will run with these defaults live. The README's own "Production checklist" flags this ("Replace all hardcoded secrets in `app/core/config.py` with empty defaults + startup assertion in `production` env") — so the team is aware, but it hasn't been done yet. Anyone who has ever cloned this repo has had access to these values; if they were ever real, they should be rotated.
4. **`demo.py` (repo root) contains live-looking OAuth client credentials** for a *Nomba* API account (`client_id`, `client_secret`, `accountId`) — unrelated to Paystack, and unrelated to the rest of the app's functionality. This looks like a leftover exploration script. Treat the embedded credentials as compromised and rotate them; consider deleting the file or moving the credentials to `.env`.
5. **KYC/BVN validation isn't wired to a provider callback.** `KycRecord.bvn_validated` is set on the model and defaults to `False`, but nothing in the current codebase ever flips it to `True` outside of `scripts/seed.py`'s demo data. If BVN validation against Nigeria's BVN registry is a real requirement, that provider integration doesn't exist yet.
6. **The scheduler's `process_scheduled_bills` job stops short of paying.** When a scheduled bill becomes due and the agent says `pay_now`, the job only flips the bill to `pending` — it deliberately does not call `execute_payout()` itself (per the comment in `scheduler.py`). A bill can sit at `pending` indefinitely unless a user (or some other job not present in this codebase) calls `POST /bills/{id}/pay`.
7. **Account name mismatches are logged, not enforced.** In `execute_payout()`, if the bank's resolved account name doesn't match the bill's stored vendor name, the code only logs a warning and proceeds with the transfer. This is a deliberate tradeoff (vendor names are free-text and often differ from bank KYC names) but means a corrupted or malicious `account_number`/`bank_code` pair that happens to resolve to *some* real account will still receive the transfer.
