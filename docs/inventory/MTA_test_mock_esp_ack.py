"""TEST ONLY: emulate ESP32 NVS acknowledgment and close SVC-000001.

Uses only MTA_inventory_TEST.db and MTA_mock_ESP32_NVS.json beside this script.
No network, ESP32, or production database is accessed.
"""
import json
import os
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / 'MTA_inventory_TEST.db'
MOCK_NVS = BASE / 'MTA_mock_ESP32_NVS.json'
DEV, CHANNEL, PRODUCT = 'MTA-001', 1, 'PRD-000001'
SVC, LOAD = 'SVC-000001', 'LS-000001'

if not DB.is_file():
    raise SystemExit('ERROR: MTA_inventory_TEST.db not found alongside script')

# Read expected target from confirmed DB stock. Never copy a guessed quantity.
with sqlite3.connect(DB) as db:
    db.execute('PRAGMA foreign_keys=ON')
    status = db.execute('SELECT status FROM service_sessions WHERE service_session_id=? AND device_id=? AND operation_type=?', (SVC, DEV, 'LOAD')).fetchone()
    assert status is not None and status[0] in ('OPEN', 'COMPLETED'), f'Unexpected service session: {status}'
    completed_load = db.execute('SELECT status FROM load_sessions WHERE load_session_id=? AND service_session_id=?', (LOAD, SVC)).fetchone()
    assert completed_load == ('COMPLETED',), f'Load not completed: {completed_load}'
    operation = db.execute('SELECT quantity_delta FROM stock_operations WHERE source_session_id=? AND operation_type=? AND device_id=? AND channel_id=? AND product_id=?', (LOAD, 'LOAD', DEV, CHANNEL, PRODUCT)).fetchone()
    assert operation == (25,), f'Expected previously confirmed load of 25: {operation}'
    record = db.execute('SELECT product_id,quantity,inventory_version FROM inventory WHERE device_id=? AND channel_id=?', (DEV, CHANNEL)).fetchone()
    assert record == (PRODUCT, 25, 1), f'Unexpected inventory for test: {record}'
    quantity, version = record[1], record[2]

# Emulate durable ESP NVS write using a local JSON file (NOT real ESP NVS).
ack = {'device_id': DEV, 'channel_id': CHANNEL, 'product_id': PRODUCT,
       'quantity': quantity, 'inventory_version': version, 'simulated': True}
if MOCK_NVS.exists():
    existing = json.loads(MOCK_NVS.read_text(encoding='utf-8'))
    assert existing == ack, f'Simulated ESP state differs from database: {existing}'
    print('MOCK ESP32: previous ACK matches; no second write needed')
else:
    tmp = MOCK_NVS.with_suffix('.json.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(ack, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, MOCK_NVS)
    print('MOCK ESP32: persisted test NVS file')

# Verify persisted ACK before allowing service closure.
persisted = json.loads(MOCK_NVS.read_text(encoding='utf-8'))
assert persisted == ack, 'Mock acknowledgment validation failed'
print(f'MOCK ACK: {DEV} channel {CHANNEL}, {PRODUCT}, quantity {quantity}, version {version}')

with sqlite3.connect(DB) as db:
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('BEGIN IMMEDIATE')
    try:
        current = db.execute('SELECT product_id,quantity,inventory_version FROM inventory WHERE device_id=? AND channel_id=?', (DEV, CHANNEL)).fetchone()
        assert current == record, 'Inventory changed during simulated synchronization'
        status = db.execute('SELECT status FROM service_sessions WHERE service_session_id=? AND device_id=?', (SVC, DEV)).fetchone()
        if status == ('COMPLETED',):
            print('SERVICE ALREADY COMPLETED: no second close')
        else:
            assert status == ('OPEN',), f'Service unexpectedly changed: {status}'
            r = db.execute("UPDATE service_sessions SET status='COMPLETED',completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE service_session_id=? AND status='OPEN'", (SVC,))
            assert r.rowcount == 1
            print('SERVICE CLOSED AFTER SIMULATED ACK')
        assert not db.execute('PRAGMA foreign_key_check').fetchall(), 'Foreign key violation'
        db.commit()
    except Exception:
        db.rollback()
        raise
print('TEST ONLY: real ESP32 was NOT contacted or synchronized')
