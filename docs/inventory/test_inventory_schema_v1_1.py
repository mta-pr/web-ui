"""Standalone in-memory SQL smoke tests; no network, no live database."""
from pathlib import Path
import sqlite3
import unittest

SQL = Path(__file__).with_name('MTA_inventory_schema_v1_1_REVIEW.sql').read_text(encoding='utf-8')

class WarehouseGuards(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('PRAGMA foreign_keys = ON')
        self.db.executescript(SQL)
        self.db.execute('INSERT INTO devices(device_id,name) VALUES (?,?)',('MTA-TEST','Test'))
        self.db.execute('INSERT INTO device_channels(device_id,channel_id) VALUES (?,?)',('MTA-TEST',1))
        self.db.execute('INSERT INTO products(name,gtin) VALUES (?,?)',('Test water','00000000000000'))
        self.product_id=self.db.execute('SELECT product_id FROM products').fetchone()[0]
        for i in range(3):
            self.db.execute('INSERT INTO product_units(product_id,marking_code) VALUES (?,?)', (self.product_id, ('TEST-%02d'%i).encode()))
        self.units=[r[0] for r in self.db.execute('SELECT unit_id FROM product_units ORDER BY unit_id')]
        self.db.execute('INSERT INTO load_orders(load_order_id,device_id,channel_id,product_id,operator_id) VALUES (?,?,?,?,?)',('ORDER','MTA-TEST',1,self.product_id,'test'))
        self.db.execute('INSERT INTO load_order_items(load_order_id,unit_id) VALUES (?,?)',('ORDER',self.units[0]))
        self.db.commit()

    def tearDown(self): self.db.close()

    def advance(self):
        self.db.execute("UPDATE load_orders SET status='PREPARED' WHERE load_order_id='ORDER'")
        self.db.execute("UPDATE load_orders SET status='IN_PROGRESS' WHERE load_order_id='ORDER'")

    def test_structure(self):
        self.assertEqual(self.db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0],13)
        self.assertEqual(self.db.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0],8)
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_edit_draft_then_lock_manifest(self):
        self.db.execute('INSERT INTO load_order_items(load_order_id,unit_id) VALUES (?,?)',('ORDER',self.units[1]))
        self.db.execute('DELETE FROM load_order_items WHERE load_order_id=? AND unit_id=?',('ORDER',self.units[1]))
        self.db.execute("UPDATE load_orders SET status='PREPARED' WHERE load_order_id='ORDER'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('INSERT INTO load_order_items(load_order_id,unit_id) VALUES (?,?)',('ORDER',self.units[1]))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM load_order_items WHERE load_order_id=? AND unit_id=?',('ORDER',self.units[0]))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE load_order_items SET unit_id=? WHERE load_order_id=? AND unit_id=?',(self.units[1],'ORDER',self.units[0]))

    def test_allowed_item_transitions(self):
        self.advance()
        self.db.execute("UPDATE load_order_items SET item_status='RETURNED' WHERE load_order_id='ORDER'")
        self.assertEqual(self.db.execute('SELECT item_status FROM load_order_items').fetchone()[0],'RETURNED')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE load_order_items SET item_status='PREPARED' WHERE load_order_id='ORDER'")

    def test_closing_and_reopening(self):
        self.advance()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE load_orders SET status='CLOSED',closed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE load_order_id='ORDER'")
        self.db.execute("UPDATE load_order_items SET item_status='RETURNED' WHERE load_order_id='ORDER'")
        self.db.execute("UPDATE load_orders SET status='CLOSED',closed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE load_order_id='ORDER'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE load_orders SET status='DRAFT',closed_at=NULL WHERE load_order_id='ORDER'")

    def test_only_one_active_service(self):
        self.db.execute("INSERT INTO service_sessions(service_session_id,device_id,operator_id,operation_type) VALUES ('S1','MTA-TEST','test','LOAD')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO service_sessions(service_session_id,device_id,operator_id,operation_type) VALUES ('S2','MTA-TEST','test','LOAD')")

if __name__ == '__main__': unittest.main(verbosity=2)
