"""Add webhook_events table for replay defense.

Revision ID: 0002_webhook_events
Revises: 0001_baseline
Create Date: 2026-06-03
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0002_webhook_events"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # IF NOT EXISTS guards: local dev DBs may already have this table via
    # schema.sql's docker-entrypoint-initdb.d load (see 0001_baseline).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_events (
            id           BIGSERIAL    PRIMARY KEY,
            provider     TEXT         NOT NULL,
            event_id     TEXT         NOT NULL,
            event_type   TEXT         NOT NULL,
            received_at  TIMESTAMP    NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_webhook_events_provider_event_id
                UNIQUE (provider, event_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_webhook_events_received_at "
        "ON webhook_events(received_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_webhook_events_received_at")
    op.execute("DROP TABLE IF EXISTS webhook_events")
