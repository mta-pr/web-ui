"""emulator device

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-22 21:27:50.337747

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # OR IGNORE: the row may already exist in databases filled by hand.
    op.execute("""
        INSERT OR IGNORE INTO devices
            (device_id, name, location, machine_state, active_error, last_seen)
        VALUES
            ('MTA-001', 'Emulated vending machine', 'Test lab', 'IDLE', 'NONE',
             '2026-10-22T17:26:24.674270Z')
    """)


def downgrade() -> None:
    # Fails on FK if the device already has events, sales or inventory.
    op.execute("DELETE FROM devices WHERE device_id = 'MTA-001'")
