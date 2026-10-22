"""TEST ONLY: protect load_orders lifecycle in MTA_inventory_TRIGGER_TEST.db.

Does not touch MTA_inventory_TEST.db, production mta.db, or ESP32.
Existing triggers with different SQL cause a rollback rather than overwrite.
"""
from pathlib import Path
import sqlite3

DB = Path(__file__).resolve().parent / 'MTA_inventory_TRIGGER_TEST.db'

TRIGGERS = {
 'trg_load_orders_status_transition': """
CREATE TRIGGER trg_load_orders_status_transition
BEFORE UPDATE OF status ON load_orders
FOR EACH ROW
WHEN NEW.status IS NOT OLD.status AND NOT (
 (OLD.status = 'DRAFT' AND NEW.status = 'PREPARED') OR
 (OLD.status = 'PREPARED' AND NEW.status = 'IN_PROGRESS') OR
 (OLD.status = 'IN_PROGRESS' AND NEW.status IN ('PARTIALLY_LOADED', 'CLOSED')) OR
 (OLD.status = 'PARTIALLY_LOADED' AND NEW.status IN ('IN_PROGRESS', 'CLOSED'))
)
BEGIN
 SELECT RAISE(ABORT, 'Invalid load order status transition');
END""",
 'trg_load_orders_close_guard': """
CREATE TRIGGER trg_load_orders_close_guard
BEFORE UPDATE OF status ON load_orders
FOR EACH ROW
WHEN NEW.status = 'CLOSED' AND OLD.status IS NOT 'CLOSED' AND (
 NOT EXISTS (SELECT 1 FROM load_order_items WHERE load_order_id = OLD.load_order_id)
 OR EXISTS (SELECT 1 FROM load_order_items WHERE load_order_id = OLD.load_order_id AND item_status = 'PREPARED')
 OR EXISTS (SELECT 1 FROM service_sessions WHERE device_id = OLD.device_id AND status IN ('OPEN', 'RECOVERY_REQUIRED'))
)
BEGIN
 SELECT RAISE(ABORT, 'Cannot close load order: unfinished items or active service');
END""",
 'trg_load_orders_identity_guard': """
CREATE TRIGGER trg_load_orders_identity_guard
BEFORE UPDATE OF device_id, channel_id, product_id, operator_id, created_at, closed_at ON load_orders
FOR EACH ROW
WHEN (OLD.status <> 'DRAFT' AND (
 NEW.device_id IS NOT OLD.device_id OR
 NEW.channel_id IS NOT OLD.channel_id OR
 NEW.product_id IS NOT OLD.product_id OR
 NEW.operator_id IS NOT OLD.operator_id OR
 NEW.created_at IS NOT OLD.created_at OR
 (OLD.status = 'CLOSED' AND NEW.closed_at IS NOT OLD.closed_at)
))
BEGIN
 SELECT RAISE(ABORT, 'Load order details are locked');
END""",
 'trg_load_orders_delete_guard': """
CREATE TRIGGER trg_load_orders_delete_guard
BEFORE DELETE ON load_orders
FOR EACH ROW
WHEN OLD.status <> 'DRAFT'
BEGIN
 SELECT RAISE(ABORT, 'Cannot delete confirmed load order');
END"""
}

def normalized(s):
    return ' '.join(s.strip().rstrip(';').split()).casefold()

def main():
    if not DB.is_file():
        raise SystemExit('STOP: test copy not found: ' + str(DB))
    c = sqlite3.connect(DB)
    try:
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('BEGIN IMMEDIATE')
        status = c.execute('SELECT status FROM load_orders WHERE load_order_id=?', ('LOAD-000001',)).fetchone()
        num = c.execute('SELECT COUNT(*) FROM load_order_items WHERE load_order_id=?', ('LOAD-000001',)).fetchone()[0]
        if status != ('CLOSED',) or num != 30:
            raise RuntimeError(f'Unexpected baseline: order={status}, items={num}')
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or c.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Existing database integrity problem')
        for name in ('trg_load_order_items_insert_draft_only', 'trg_load_order_items_delete_draft_only', 'trg_load_order_items_identity_immutable', 'trg_load_order_items_status_transitions'):
            if not c.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('trigger', name)).fetchone():
                raise RuntimeError('Required earlier guard missing: ' + name)
        added = []
        for name, sql in TRIGGERS.items():
            existing = c.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?', ('trigger', name)).fetchone()
            if existing:
                if normalized(existing[0]) != normalized(sql):
                    raise RuntimeError('Existing trigger differs: ' + name)
            else:
                c.execute(sql)
                added.append(name)
        c.commit()
        print('TARGET:', DB.name)
        print('NEW LOAD ORDER GUARDS:', len(added))
        for name in added:
            print(' INSTALLED:', name)
        print('ORDER:', status[0], '| ITEMS:', num)
        print('CONTROL DATABASE WAS NOT MODIFIED')
        print('NOTE: ESP32 synchronization ACK still belongs to application logic.')
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

if __name__ == '__main__':
    main()
