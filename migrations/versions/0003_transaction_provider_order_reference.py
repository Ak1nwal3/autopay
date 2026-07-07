"""Add provider_order_reference column to transactions.

Revision ID: 0003_transaction_provider_order_reference
Revises: 0002_webhook_events
Create Date: 2026-07-06

Why this migration:
  Paystack echoes our reference back, but Nomba generates its own
  `orderReference` (a UUID) for each Checkout. Our `start_topup`
  function now stores both:
    * `provider_reference`         — our internal reference, returned
                                     to the user, used for webhook
                                     lookups
    * `provider_order_reference`   — the provider's reference, used
                                     as the query parameter when
                                     calling the provider's
                                     transaction-status API

This column is nullable, so existing rows continue to work. The
verify endpoint falls back to `provider_reference` when this is
NULL (i.e. for topups created before this migration).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003_transaction_provider_order_reference"
down_revision: Union[str, None] = "0002_webhook_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # IF NOT EXISTS guards: local dev DBs may already have this column via
    # schema.sql's docker-entrypoint-initdb.d load (see 0001_baseline).
    op.execute(
        "ALTER TABLE transactions "
        "ADD COLUMN IF NOT EXISTS provider_order_reference VARCHAR(128)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_transactions_provider_order_reference "
        "ON transactions(provider_order_reference)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_transactions_provider_order_reference")
    op.execute(
        "ALTER TABLE transactions DROP COLUMN IF EXISTS provider_order_reference"
    )
