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

import sqlalchemy as sa
from alembic import op

revision: str = "0003_transaction_provider_order_reference"
down_revision: Union[str, None] = "0002_webhook_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("provider_order_reference", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_transactions_provider_order_reference",
        "transactions",
        ["provider_order_reference"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_transactions_provider_order_reference", table_name="transactions"
    )
    op.drop_column("transactions", "provider_order_reference")
