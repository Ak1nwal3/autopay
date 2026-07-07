"""Baseline migration.

Creates the base schema (matching `../schema.sql`) idempotently, using
`IF NOT EXISTS` guards throughout. This makes `alembic upgrade head` safe
to run in two different situations:

  1. Fresh environments that never had `schema.sql` applied out-of-band
     (Railway, Render, CI, any managed Postgres without a
     `docker-entrypoint-initdb.d` hook) — this migration creates every
     table from scratch.
  2. Local Docker dev, where the Postgres container already loaded
     `schema.sql` via `/docker-entrypoint-initdb.d` — every statement
     here is a no-op since the tables already exist.

`webhook_events` and `transactions.provider_order_reference` are
intentionally NOT created here — they belong to 0002 and 0003, which are
themselves idempotent so they behave correctly in both situations above.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-01
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id                   BIGSERIAL    PRIMARY KEY,
            first_name           TEXT         NOT NULL,
            last_name            TEXT         NOT NULL,
            email                TEXT         NOT NULL UNIQUE,
            phone_number         TEXT         NOT NULL UNIQUE,
            hashed_password      TEXT         NOT NULL,
            telegram_chat_id     TEXT         UNIQUE,
            is_telegram_linked   BOOLEAN      NOT NULL DEFAULT FALSE,
            balance              NUMERIC(14,2) NOT NULL DEFAULT 0,
            currency             CHAR(3)      NOT NULL DEFAULT 'NGN',
            address              TEXT,
            created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_email ON users(LOWER(email))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_telegram_chat_id "
        "ON users(telegram_chat_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS kyc_records (
            id                BIGSERIAL    PRIMARY KEY,
            user_id           BIGINT       NOT NULL UNIQUE
                                           REFERENCES users(id) ON DELETE CASCADE,
            bvn_ciphertext    BYTEA        NOT NULL,
            bvn_last4         CHAR(4)      NOT NULL,
            bvn_hash          CHAR(64)     NOT NULL UNIQUE,
            bvn_validated     BOOLEAN      NOT NULL DEFAULT FALSE,
            validated_at      TIMESTAMPTZ,
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_kyc_records_user_id ON kyc_records(user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_kyc_records_bvn_hash ON kyc_records(bvn_hash)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS virtual_accounts (
            id                          BIGSERIAL    PRIMARY KEY,
            user_id                     BIGINT       NOT NULL UNIQUE
                                                    REFERENCES users(id) ON DELETE CASCADE,
            provider                    TEXT         NOT NULL DEFAULT 'paystack',
            provider_account_reference  TEXT         NOT NULL UNIQUE,
            account_number              TEXT         UNIQUE,
            account_name                TEXT,
            bank_name                   TEXT,
            currency                    CHAR(3)      NOT NULL DEFAULT 'NGN',
            created_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_virtual_accounts_account_number "
        "ON virtual_accounts(account_number)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bills (
            id                    BIGSERIAL       PRIMARY KEY,
            user_id               BIGINT          NOT NULL
                                                 REFERENCES users(id) ON DELETE CASCADE,
            vendor_name           TEXT            NOT NULL,
            account_number        TEXT,
            bank_code             TEXT,
            bank_name             TEXT,
            amount                NUMERIC(14,2)   NOT NULL,
            currency              CHAR(3)         NOT NULL DEFAULT 'NGN',
            due_date              TIMESTAMPTZ     NOT NULL,
            status                TEXT            NOT NULL DEFAULT 'pending'
                                                 CHECK (status IN
                                                    ('pending','scheduled','processing',
                                                     'paid','failed','cancelled')),
            is_recurring          BOOLEAN         NOT NULL DEFAULT FALSE,
            recurrence_interval   TEXT,
            next_recurrence_date  TIMESTAMPTZ,
            retry_count           INTEGER         NOT NULL DEFAULT 0,
            max_retries           INTEGER         NOT NULL DEFAULT 3,
            created_at            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at            TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_bills_user_id ON bills(user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_bills_status_due "
        "ON bills(due_date) WHERE status = 'scheduled'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id                  BIGSERIAL       PRIMARY KEY,
            user_id             BIGINT          NOT NULL
                                               REFERENCES users(id) ON DELETE RESTRICT,
            bill_id             BIGINT          REFERENCES bills(id) ON DELETE SET NULL,
            type                TEXT            NOT NULL
                                               CHECK (type IN ('credit','debit')),
            amount              NUMERIC(14,2)   NOT NULL,
            fee                 NUMERIC(14,2)   NOT NULL DEFAULT 0,
            currency            CHAR(3)         NOT NULL DEFAULT 'NGN',
            status              TEXT            NOT NULL DEFAULT 'pending'
                                               CHECK (status IN
                                                  ('pending','processing','success',
                                                   'failed','reversed')),
            provider            TEXT            NOT NULL DEFAULT 'paystack',
            provider_reference  TEXT            UNIQUE,
            retry_count         INTEGER         NOT NULL DEFAULT 0,
            failure_reason      TEXT,
            narration           TEXT,
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_user_status "
        "ON transactions(user_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_created_at "
        "ON transactions(created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_provider_reference "
        "ON transactions(provider_reference)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id            BIGSERIAL    PRIMARY KEY,
            user_id       BIGINT       REFERENCES users(id) ON DELETE SET NULL,
            actor         TEXT         NOT NULL,
            event_type    TEXT         NOT NULL,
            entity_type   TEXT,
            entity_id     BIGINT,
            before_state  JSONB,
            after_state   JSONB,
            metadata      JSONB,
            ip_address    INET,
            created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs(user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_event_type ON audit_logs(event_type)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at "
        "ON audit_logs(created_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS refresh_tokens (
            id           BIGSERIAL    PRIMARY KEY,
            user_id      BIGINT       NOT NULL
                                     REFERENCES users(id) ON DELETE CASCADE,
            token_hash   TEXT         NOT NULL UNIQUE,
            expires_at   TIMESTAMPTZ  NOT NULL,
            revoked      BOOLEAN      NOT NULL DEFAULT FALSE,
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user_id "
        "ON refresh_tokens(user_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_link_codes (
            id          BIGSERIAL    PRIMARY KEY,
            user_id     BIGINT       NOT NULL
                                    REFERENCES users(id) ON DELETE CASCADE,
            code        TEXT         NOT NULL UNIQUE,
            expires_at  TIMESTAMPTZ  NOT NULL,
            is_used     BOOLEAN      NOT NULL DEFAULT FALSE,
            created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_link_codes_user_id "
        "ON telegram_link_codes(user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS telegram_link_codes")
    op.execute("DROP TABLE IF EXISTS refresh_tokens")
    op.execute("DROP TABLE IF EXISTS audit_logs")
    op.execute("DROP TABLE IF EXISTS transactions")
    op.execute("DROP TABLE IF EXISTS bills")
    op.execute("DROP TABLE IF EXISTS virtual_accounts")
    op.execute("DROP TABLE IF EXISTS kyc_records")
    op.execute("DROP TABLE IF EXISTS users")
