"""base schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-22 21:09:08.290656

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # IF NOT EXISTS: databases created earlier from mta_schema.sql adopt
    # this revision without changes.
    op.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            name TEXT,
            location TEXT,
            machine_state TEXT NOT NULL DEFAULT 'IDLE',
            active_error TEXT NOT NULL DEFAULT 'NONE',
            last_seen TEXT
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS products (
            product_id TEXT PRIMARY KEY,
            name TEXT
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL REFERENCES devices(device_id),
            event_type TEXT NOT NULL,
            session_id TEXT,
            created_at TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data))
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            device_id TEXT NOT NULL REFERENCES devices(device_id),
            product_id TEXT NOT NULL REFERENCES products(product_id),
            quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
            PRIMARY KEY (device_id, product_id)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            sale_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            device_id TEXT NOT NULL REFERENCES devices(device_id),
            product_id TEXT NOT NULL REFERENCES products(product_id),
            price INTEGER NOT NULL CHECK (price > 0),
            payment TEXT NOT NULL,
            dispense TEXT NOT NULL,
            sold_at TEXT NOT NULL,
            event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS sales_device_time "
        "ON sales (device_id, sold_at DESC)"
    )


def downgrade() -> None:
    for table in ("sales", "inventory", "events", "products", "devices"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
