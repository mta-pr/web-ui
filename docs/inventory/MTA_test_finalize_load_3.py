"""Standalone test: finalize LS-000002 against MTA_inventory_TEST.db ONLY.
Never run against production. DB synchronization with ESP32 is NOT simulated here. Service remains OPEN.
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent / 'MTA_inventory_TEST.db'
assert DB.is_file(), f'Test database not found: {DB}'

with sqlite3.connect(DB) as db:
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('BEGIN IMMEDIATE')
    try:
        order_id, session_id, svc_id = 'LOAD-000001', 'LS-000002', 'SVC-000002'
        device_id, channel_id, product_id = 'MTA-001', 1, 'PRD-000001'
        operator_id, operation_id = 'admin', 'STOCK-LOAD-LS-000002'
        row = db.execute('SELECT status FROM load_sessions WHERE load_session_id=? AND load_order_id=? AND service_session_id=?', (session_id, order_id, svc_id)).fetchone()
        assert row is not None, 'Load session mismatch'
        if row[0] == 'COMPLETED':
            record = db.execute('SELECT quantity_delta FROM stock_operations WHERE source_session_id=? AND operation_type=?', (session_id, 'LOAD')).fetchone()
            assert record is not None, 'Completed session lacks stock operation!'
            print('ALREADY COMPLETED: no second stock addition')
            print('PREVIOUS LOAD:', record[0])
        else:
            assert row[0] == 'OPEN', 'Unexpected load session state'
            assert db.execute('SELECT status FROM service_sessions WHERE service_session_id=? AND device_id=? AND operation_type=?', (svc_id, device_id, 'LOAD')).fetchone() == ('OPEN',), 'Service must be open'
            assert db.execute('SELECT status FROM load_orders WHERE load_order_id=? AND device_id=? AND channel_id=? AND product_id=?', (order_id,device_id,channel_id,product_id)).fetchone() == ('IN_PROGRESS',), 'Order is not in progress'
            assert db.execute('SELECT COUNT(*) FROM stock_operations WHERE source_session_id=?', (session_id,)).fetchone()[0] == 0, 'Stock operation already exists'
            scanned = [r[0] for r in db.execute('SELECT unit_id FROM load_session_items WHERE load_session_id=? AND load_order_id=? AND scan_status=? ORDER BY unit_id', (session_id,order_id,'SCANNED'))]
            assert len(scanned) == 3, f'Expected 3 scanned, found {len(scanned)}'
            placeholder = ','.join('?' for _ in scanned)
            eligible = db.execute(f'''SELECT COUNT(*) FROM load_order_items li JOIN product_units pu ON pu.unit_id=li.unit_id
                WHERE li.load_order_id=? AND li.item_status='PREPARED'
                AND li.unit_id IN ({placeholder}) AND pu.product_id=?''', (order_id, *scanned, product_id)).fetchone()[0]
            assert eligible == len(scanned), 'Some scanned units are not prepared or mismatch product'
            existing = db.execute('SELECT product_id,quantity,inventory_version FROM inventory WHERE device_id=? AND channel_id=?', (device_id,channel_id)).fetchone()
            assert existing == (product_id, 25, 1), f'Expected stock 25 at version 1, got {existing}'
            before = existing[1] if existing else 0
            db.execute('INSERT INTO stock_operations(operation_id,device_id,channel_id,product_id,operation_type,quantity_delta,source_session_id,operator_id) VALUES(?,?,?,?,?,?,?,?)', (operation_id,device_id,channel_id,product_id,'LOAD',len(scanned),session_id,operator_id))
            if existing is None:
                db.execute('INSERT INTO inventory(device_id,channel_id,product_id,quantity,inventory_version) VALUES(?,?,?,?,?)', (device_id,channel_id,product_id,len(scanned),1))
            else:
                db.execute('UPDATE inventory SET product_id=?,quantity=quantity+?,inventory_version=inventory_version+1 WHERE device_id=? AND channel_id=?', (product_id,len(scanned),device_id,channel_id))
            for unit_id in scanned:
                cur = db.execute('UPDATE load_order_items SET item_status=? WHERE load_order_id=? AND unit_id=? AND item_status=?', ('LOADED',order_id,unit_id,'PREPARED'))
                assert cur.rowcount == 1, f'Unit {unit_id} no longer prepared'
                db.execute('UPDATE product_units SET last_known_status=? WHERE unit_id=?', ('LOADED',unit_id))
                db.execute('INSERT INTO unit_movements(unit_id,operation_id,action,operator_id) VALUES(?,?,?,?)', (unit_id,operation_id,'LOAD',operator_id))
            cur = db.execute("UPDATE load_sessions SET status='COMPLETED', completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE load_session_id=? AND status='OPEN'", (session_id,))
            assert cur.rowcount == 1
            remainder = db.execute("SELECT COUNT(*) FROM load_order_items WHERE load_order_id=? AND item_status='PREPARED'", (order_id,)).fetchone()[0]
            db.execute('UPDATE load_orders SET status=? WHERE load_order_id=?', ('PARTIALLY_LOADED' if remainder else 'IN_PROGRESS',order_id))
            print('CONFIRMED LOAD:',len(scanned))
            print('INVENTORY:',before,'->',before+len(scanned))
            print('UNASSIGNED PREPARED IN ORDER:',remainder)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == [], 'Foreign-key violations!'
        print('LOAD SESSION:', db.execute('SELECT status FROM load_sessions WHERE load_session_id=?',(session_id,)).fetchone()[0])
        print('SERVICE SESSION:', db.execute('SELECT status FROM service_sessions WHERE service_session_id=?',(svc_id,)).fetchone()[0])
        print('ORDER STATUS:', db.execute('SELECT status FROM load_orders WHERE load_order_id=?',(order_id,)).fetchone()[0])
        print('INVENTORY:',db.execute('SELECT quantity,inventory_version FROM inventory WHERE device_id=? AND channel_id=?',(device_id,channel_id)).fetchone())
        print('LOADED MOVEMENTS:',db.execute('SELECT COUNT(*) FROM unit_movements WHERE operation_id=?',(operation_id,)).fetchone()[0])
        db.commit()
    except Exception:
        db.rollback()
        raise
