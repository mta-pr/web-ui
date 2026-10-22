"""Install additional warehouse load-order integrity guards (TEST COPY ONLY).

Target: MTA_inventory_TRIGGER_TEST.db alongside this script.
Does NOT open MTA_inventory_TEST.db, live mta.db or communicate with ESP32.
Safe to rerun: existing matching triggers are recognized, differing definitions abort.
"""
from pathlib import Path
import sqlite3

DB = Path(__file__).resolve().parent / 'MTA_inventory_TRIGGER_TEST.db'

TRIGGERS = {
    'trg_load_order_items_delete_draft_only': '''
CREATE TRIGGER trg_load_order_items_delete_draft_only
BEFORE DELETE ON load_order_items
FOR EACH ROW
WHEN NOT EXISTS (
 SELECT 1 FROM load_orders
 WHERE load_order_id = OLD.load_order_id AND status = 'DRAFT'
)
BEGIN
 SELECT RAISE(ABORT, 'Load order manifest is locked');
END''',
    'trg_load_order_items_identity_immutable': '''
CREATE TRIGGER trg_load_order_items_identity_immutable
BEFORE UPDATE OF load_order_id, unit_id, added_at ON load_order_items
FOR EACH ROW
WHEN (
 NEW.load_order_id IS NOT OLD.load_order_id OR
 NEW.unit_id IS NOT OLD.unit_id OR
 NEW.added_at IS NOT OLD.added_at
) AND NOT (
 (SELECT status FROM load_orders WHERE load_order_id = OLD.load_order_id) = 'DRAFT'
 AND (SELECT status FROM load_orders WHERE load_order_id = NEW.load_order_id) = 'DRAFT'
)
BEGIN
 SELECT RAISE(ABORT, 'Load order manifest is locked');
END''',
    'trg_load_order_items_status_transitions': '''
CREATE TRIGGER trg_load_order_items_status_transitions
BEFORE UPDATE OF item_status ON load_order_items
FOR EACH ROW
WHEN NEW.item_status IS NOT OLD.item_status
 AND NOT (
   OLD.item_status = 'PREPARED'
   AND NEW.item_status IN ('LOADED','RETURNED')
   AND (SELECT status FROM load_orders WHERE load_order_id = OLD.load_order_id)
       IN ('IN_PROGRESS','PARTIALLY_LOADED')
 )
BEGIN
 SELECT RAISE(ABORT, 'Invalid load order item status transition');
END'''
}

# Check the earlier INSERT trigger remains installed; this script does not replace it.
INSERT_GUARD = 'trg_load_order_items_insert_draft_only'

def normalize(sql):
    return ' '.join(sql.strip().rstrip(';').split()).casefold()

def main():
    if not DB.is_file():
        raise SystemExit('ERROR: test DB is missing: ' + str(DB))
    con = sqlite3.connect(DB)
    try:
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('BEGIN IMMEDIATE')
        integrity = con.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise RuntimeError('Database integrity check failed: ' + integrity)
        if con.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Existing foreign key errors in test DB')
        row = con.execute('SELECT status FROM load_orders WHERE load_order_id=?', ('LOAD-000001',)).fetchone()
        count = con.execute('SELECT COUNT(*) FROM load_order_items WHERE load_order_id=?', ('LOAD-000001',)).fetchone()[0]
        if row != ('CLOSED',) or count != 30:
            raise RuntimeError(f'Unexpected baseline: status={row}, items={count}')
        existing_insert = con.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (INSERT_GUARD,)).fetchone()
        if not existing_insert:
            raise RuntimeError('Earlier INSERT guard missing; install it first')
        added, previously = [], []
        for name, sql in TRIGGERS.items():
            existing = con.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
            if existing:
                if normalize(existing[0]) != normalize(sql):
                    raise RuntimeError(f'Trigger exists with DIFFERENT SQL: {name}')
                previously.append(name)
            else:
                con.execute(sql)
                added.append(name)
        con.commit()
        print('TARGET:', DB.name)
        print('INSERT GUARD: present')
        print('NEW GUARDS INSTALLED:', len(added))
        for name in added:
            print(' INSTALLED:', name)
        for name in previously:
            print(' ALREADY PRESENT:', name)
        print('ORDER LOAD-000001:', row[0], '| ITEMS:', count)
        print('CONTROL DATABASE WAS NOT MODIFIED')
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

if __name__ == '__main__':
    main()
