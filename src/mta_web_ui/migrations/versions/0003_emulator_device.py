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
    # Data for the emulator in tests/virtualmta.py. OR IGNORE: the rows may
    # already exist in databases filled by hand.
    op.execute("""
        INSERT OR IGNORE INTO devices
            (device_id, name, location, machine_state, active_error, last_seen)
        VALUES
            ('MTA-001', 'Emulated vending machine', 'Test lab', 'IDLE', 'NONE',
             '2026-10-22T17:26:24.674270Z')
    """)
    op.execute("""
        INSERT OR IGNORE INTO products (product_id, name)
        VALUES ('WATER_05', 'Water 0.5L')
    """)
    op.execute("""
        INSERT OR IGNORE INTO inventory (device_id, product_id, quantity)
        VALUES ('MTA-001', 'WATER_05', 0)
    """)


def downgrade() -> None:
    # Fails on FK if the device already has events or sales; the product
    # is removed only if no other device holds it.
    op.execute(
        "DELETE FROM inventory WHERE device_id = 'MTA-001' AND product_id = 'WATER_05'"
    )
    op.execute("DELETE FROM devices WHERE device_id = 'MTA-001'")
    op.execute("""
        DELETE FROM products WHERE product_id = 'WATER_05'
        AND NOT EXISTS (SELECT 1 FROM inventory WHERE product_id = 'WATER_05')
        AND NOT EXISTS (SELECT 1 FROM sales WHERE product_id = 'WATER_05')
    """)
