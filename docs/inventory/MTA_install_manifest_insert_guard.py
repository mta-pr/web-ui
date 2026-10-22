"""MTA inventory negative test: protect manifest INSERTs after DRAFT.

Only operates on MTA_inventory_TRIGGER_TEST.db in the script's directory.
Does not touch the working server database, the control test DB, or ESP32.
"""
from pathlib import Path
import sqlite3

DB = Path(__file__).resolve().parent / "MTA_inventory_TRIGGER_TEST.db"
TRIGGER = "trg_load_order_items_insert_draft_only"
SQL = """
CREATE TRIGGER trg_load_order_items_insert_draft_only
BEFORE INSERT ON load_order_items
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM load_orders
    WHERE load_order_id = NEW.load_order_id AND status = 'DRAFT'
)
BEGIN
    SELECT RAISE(ABORT, 'Load order manifest is locked');
END;
"""


def main():
    if not DB.is_file():
        raise SystemExit(f"ERROR: TEST DB missing: {DB}")
    connection = sqlite3.connect(DB)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Database integrity check failed: {integrity}")
        status = connection.execute(
            "SELECT status FROM load_orders WHERE load_order_id = ?",
            ("LOAD-000001",),
        ).fetchone()
        if status != ("CLOSED",):
            raise RuntimeError(f"Unexpected baseline order status: {status}")
        quantity = connection.execute(
            "SELECT COUNT(*) FROM load_order_items WHERE load_order_id = ?",
            ("LOAD-000001",),
        ).fetchone()[0]
        if quantity != 30:
            raise RuntimeError(f"Unexpected baseline manifest size: {quantity}")
        existing = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (TRIGGER,),
        ).fetchone()
        if existing:
            print("TRIGGER ALREADY EXISTS; nothing changed:", TRIGGER)
            return
        with connection:
            connection.execute(SQL)
        print("TRIGGER INSTALLED:", TRIGGER)
        print("TARGET:", DB.name)
        print("ORDER STATUS:", status[0])
        print("MANIFEST ITEMS:", quantity)
        print("CONTROL DATABASE WAS NOT MODIFIED")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
