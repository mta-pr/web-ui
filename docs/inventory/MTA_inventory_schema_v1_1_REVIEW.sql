-- MTA inventory schema v1.1, FOR REVIEW ONLY. Standalone database, NEVER apply to live mta.db.
-- Integrated v1.0 prototype plus 8 guards tested in isolation; NOT server API or migration.
-- Code marking and sample GTIN must not be treated as real regulatory codes.

-- MTA inventory architecture v1.0 - STANDALONE REVIEW PROTOTYPE, NOT A MIGRATION.
-- Do NOT execute against the live mta.db: table names overlap with existing schema.
-- Requires SQLite 3.31+ for generated columns. Enable PRAGMA foreign_keys on EVERY connection.
PRAGMA foreign_keys = ON;
BEGIN;

CREATE TABLE devices (
    device_id TEXT PRIMARY KEY,
    name TEXT
);

CREATE TABLE products (
    product_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT GENERATED ALWAYS AS ('PRD-' || printf('%06d', product_seq)) STORED UNIQUE,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    gtin TEXT NOT NULL UNIQUE
         CHECK (length(gtin) = 14 AND gtin NOT GLOB '*[^0-9]*'),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','INACTIVE')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Treat code as opaque exact bytes. Never print/log it; parse and validate GS1 in application.
CREATE TABLE product_units (
    unit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL REFERENCES products(product_id),
    marking_code BLOB NOT NULL UNIQUE CHECK (length(marking_code) > 0),
    last_known_status TEXT NOT NULL DEFAULT 'REGISTERED'
         CHECK (last_known_status IN ('REGISTERED','PREPARED','LOADED','UNLOADED','RETURNED','UNKNOWN')),
    first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_product_units_product ON product_units(product_id);

-- One kind of goods per channel, and no automatic physical compatibility checking.
CREATE TABLE device_channels (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    channel_id INTEGER NOT NULL CHECK (channel_id > 0),
    PRIMARY KEY(device_id, channel_id)
);

CREATE TABLE inventory (
    device_id TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    product_id TEXT REFERENCES products(product_id),
    quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    inventory_version INTEGER NOT NULL DEFAULT 0 CHECK (inventory_version >= 0),
    PRIMARY KEY(device_id, channel_id),
    FOREIGN KEY(device_id, channel_id) REFERENCES device_channels(device_id, channel_id),
    CHECK (product_id IS NOT NULL OR quantity = 0)
);

-- Sale price belongs to machine/product configuration, not product catalog.
CREATE TABLE device_prices (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    product_id TEXT NOT NULL REFERENCES products(product_id),
    price_minor_units INTEGER NOT NULL CHECK(price_minor_units > 0),
    PRIMARY KEY(device_id, product_id)
);

CREATE TABLE load_orders (
    load_order_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    channel_id INTEGER NOT NULL,
    product_id TEXT NOT NULL REFERENCES products(product_id),
    operator_id TEXT NOT NULL CHECK (length(trim(operator_id)) > 0),
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK
        (status IN ('DRAFT','PREPARED','IN_PROGRESS','PARTIALLY_LOADED','CLOSED')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    closed_at TEXT,
    FOREIGN KEY(device_id,channel_id) REFERENCES device_channels(device_id,channel_id),
    CHECK ((status = 'CLOSED') = (closed_at IS NOT NULL))
);
CREATE INDEX idx_load_orders_device_status ON load_orders(device_id,status);

CREATE TABLE load_order_items (
    load_order_id TEXT NOT NULL REFERENCES load_orders(load_order_id),
    unit_id INTEGER NOT NULL REFERENCES product_units(unit_id),
    item_status TEXT NOT NULL DEFAULT 'PREPARED'
        CHECK(item_status IN ('PREPARED','LOADED','RETURNED')),
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(load_order_id, unit_id)
);
CREATE INDEX idx_load_order_items_unit ON load_order_items(unit_id);

CREATE TABLE service_sessions (
    service_session_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    operator_id TEXT NOT NULL,
    operation_type TEXT NOT NULL CHECK(operation_type IN ('LOAD','UNLOAD','INVENTORY')),
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK(status IN ('OPEN','RECOVERY_REQUIRED','COMPLETED')),
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at TEXT,
    CHECK ((status = 'COMPLETED') = (completed_at IS NOT NULL)),
    UNIQUE(service_session_id,operation_type)
);
CREATE UNIQUE INDEX idx_one_active_service_per_device ON service_sessions(device_id)
    WHERE status IN ('OPEN','RECOVERY_REQUIRED');

CREATE TABLE load_sessions (
    load_session_id TEXT PRIMARY KEY,
    service_session_id TEXT NOT NULL UNIQUE,
    operation_type TEXT NOT NULL DEFAULT 'LOAD' CHECK(operation_type = 'LOAD'),
    load_order_id TEXT NOT NULL REFERENCES load_orders(load_order_id),
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','COMPLETED')),
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at TEXT,
    UNIQUE(load_session_id,load_order_id),
    FOREIGN KEY(service_session_id,operation_type)
        REFERENCES service_sessions(service_session_id,operation_type),
    CHECK ((status = 'COMPLETED') = (completed_at IS NOT NULL))
);

CREATE TABLE load_session_items (
    load_session_id TEXT NOT NULL,
    load_order_id TEXT NOT NULL,
    unit_id INTEGER NOT NULL REFERENCES product_units(unit_id),
    scan_status TEXT NOT NULL DEFAULT 'SCANNED'
        CHECK(scan_status IN ('SCANNED','REMOVED')),
    scanned_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    removed_at TEXT,
    PRIMARY KEY(load_session_id,unit_id),
    FOREIGN KEY(load_session_id,load_order_id)
        REFERENCES load_sessions(load_session_id,load_order_id),
    FOREIGN KEY(load_order_id,unit_id)
        REFERENCES load_order_items(load_order_id,unit_id),
    CHECK ((scan_status = 'REMOVED') = (removed_at IS NOT NULL))
);

-- Idempotency key is unique. Finalizing load_session, editing items,
-- modifying inventory, and recording stock operation MUST happen in ONE transaction.
CREATE TABLE stock_operations (
    operation_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    product_id TEXT NOT NULL REFERENCES products(product_id),
    operation_type TEXT NOT NULL CHECK(operation_type IN ('LOAD','UNLOAD','ADJUST')),
    quantity_delta INTEGER NOT NULL CHECK(quantity_delta <> 0),
    source_session_id TEXT NOT NULL UNIQUE,
    operator_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(device_id,channel_id) REFERENCES device_channels(device_id,channel_id)
);

-- Append-only audit: a persistent item record or last_known_status is NOT
-- reliable proof that a specific unit remains physically in a machine after sales.
CREATE TABLE unit_movements (
    movement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL REFERENCES product_units(unit_id),
    operation_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN
      ('REGISTER','PREPARE','LOAD','UNLOAD','RETURN','INVENTORY_SEEN','CORRECTION')),
    operator_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_unit_movements_unit_time ON unit_movements(unit_id,occurred_at);
COMMIT;


-- Guard triggers from negative tests on 2026-10-21.
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

CREATE TRIGGER trg_load_order_items_delete_draft_only
BEFORE DELETE ON load_order_items
FOR EACH ROW
WHEN NOT EXISTS (
 SELECT 1 FROM load_orders
 WHERE load_order_id = OLD.load_order_id AND status = 'DRAFT'
)
BEGIN
 SELECT RAISE(ABORT, 'Load order manifest is locked');
END;

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
END;

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
END;

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
END;

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
END;

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
END;

CREATE TRIGGER trg_load_orders_delete_guard
BEFORE DELETE ON load_orders
FOR EACH ROW
WHEN OLD.status <> 'DRAFT'
BEGIN
 SELECT RAISE(ABORT, 'Cannot delete confirmed load order');
END;
