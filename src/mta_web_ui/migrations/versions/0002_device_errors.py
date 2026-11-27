"""device errors

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22 21:09:08.482273

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS device_errors (
            error_id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL REFERENCES devices(device_id),
            error_code TEXT NOT NULL,
            -- ISO 8601 UTC text in a fixed format, so text comparison
            -- matches time order.
            occurred_at TEXT NOT NULL,
            cleared_at TEXT,
            set_event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
            clear_event_id TEXT UNIQUE REFERENCES events(event_id),
            details TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details)),
            CONSTRAINT device_errors_times_valid CHECK (
                cleared_at IS NULL OR cleared_at >= occurred_at
            ),
            CONSTRAINT device_errors_clear_consistency CHECK (
                (cleared_at IS NULL) = (clear_event_id IS NULL)
            )
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS device_errors_one_active_per_code
        ON device_errors (device_id, error_code)
        WHERE cleared_at IS NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS device_errors_device_timeline
        ON device_errors (device_id, occurred_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS device_errors")
