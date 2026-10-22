"""TEST ONLY: simulate NVS update 25/v1 -> 28/v2 and close SVC-000002.

Works exclusively with MTA_inventory_TEST.db and MTA_mock_ESP32_NVS.json
next to this script. Never contacts the physical ESP32 or production server.
"""
import json
import os
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / 'MTA_inventory_TEST.db'
MOCK_NVS = BASE / 'MTA_mock_ESP32_NVS.json'
DEV, CHANNEL, PRODUCT = 'MTA-001', 1, 'PRD-000001'
SVC, LOAD = 'SVC-000002', 'LS-000002'
PREVIOUS = {'device_id': DEV, 'channel_id': CHANNEL, 'product_id': PRODUCT,
            'quantity': 25, 'inventory_version': 1, 'simulated': True}
EXPECTED = {**PREVIOUS, 'quantity': 28, 'inventory_version': 2}

assert DB.is_file(), f'Test database not found: {DB}'
assert MOCK_NVS.is_file(), f'Previous mock ESP32 NVS file not found: {MOCK_NVS}'

with sqlite3.connect(DB) as db:
    db.execute('PRAGMA foreign_keys=ON')
    assert db.execute('SELECT status FROM service_sessions WHERE service_session_id=? AND device_id=? AND operation_type=?',
                      (SVC, DEV, 'LOAD')).fetchone() in (('OPEN',), ('COMPLETED',)), 'Unexpected service status'
    assert db.execute('SELECT status FROM load_sessions WHERE load_session_id=? AND service_session_id=?',
                      (LOAD, SVC)).fetchone() == ('COMPLETED',), 'Second loading is not complete'
    assert db.execute('SELECT quantity_delta FROM stock_operations WHERE source_session_id=? AND operation_type=?',
                      (LOAD, 'LOAD')).fetchone() == (3,), 'Expected three confirmed units in second loading'
    assert db.execute('SELECT product_id,quantity,inventory_version FROM inventory WHERE device_id=? AND channel_id=?',
                      (DEV, CHANNEL)).fetchone() == (PRODUCT, 28, 2), 'Unexpected server inventory'

current_mock = json.loads(MOCK_NVS.read_text(encoding='utf-8'))
if current_mock == EXPECTED:
    print('MOCK ESP32: previous version 2 ACK matches; no second write')
else:
    assert current_mock == PREVIOUS, f'Unexpected mock state; refusing update: {current_mock}'
    temp = MOCK_NVS.with_suffix('.json.tmp')
    with temp.open('w', encoding='utf-8') as out:
        json.dump(EXPECTED, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, MOCK_NVS)
    print('MOCK ESP32: updated simulated NVS from version 1 to version 2')

assert json.loads(MOCK_NVS.read_text(encoding='utf-8')) == EXPECTED, 'Mock NVS readback mismatch'
print('MOCK ACK: MTA-001 channel 1, PRD-000001, quantity 28, version 2')

with sqlite3.connect(DB) as db:
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('BEGIN IMMEDIATE')
    try:
        assert db.execute('SELECT product_id,quantity,inventory_version FROM inventory WHERE device_id=? AND channel_id=?',
                          (DEV, CHANNEL)).fetchone() == (PRODUCT, 28, 2), 'Stock changed during ACK'
        status = db.execute('SELECT status FROM service_sessions WHERE service_session_id=? AND device_id=?',
                            (SVC, DEV)).fetchone()
        if status == ('COMPLETED',):
            print('SERVICE ALREADY COMPLETED: no second close')
        else:
            assert status == ('OPEN',), f'Unexpected service session: {status}'
            result = db.execute("UPDATE service_sessions SET status='COMPLETED',completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE service_session_id=? AND status='OPEN'", (SVC,))
            assert result.rowcount == 1
            print('SERVICE CLOSED AFTER SIMULATED ACK')
        assert db.execute('PRAGMA foreign_key_check').fetchall() == [], 'Foreign key violations'
        db.commit()
    except Exception:
        db.rollback()
        raise
print('TEST ONLY: real ESP32 was NOT contacted or synchronized')
