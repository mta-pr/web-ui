import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from conftest import ADMIN_PASSWORD, PRODUCTS, new_id, stock
from virtualmta import MTAState


def stock_of(db, mta, pid):
    return stock(db, mta.device_id, pid)


def event_count(db, mta):
    return db.execute(
        "SELECT COUNT(*) FROM events WHERE device_id=?", (mta.device_id,)
    ).fetchone()[0]


# --- heartbeat ---------------------------------------------------------------

class TestHeartbeat:
    def test_ok_updates_device(self, mta, db):
        status, answer = mta.heartbeat()
        assert (status, answer) == (200, {"status": "ok"})
        row = db.execute(
            "SELECT machine_state, active_error, last_seen FROM devices WHERE device_id=?",
            (mta.device_id,),
        ).fetchone()
        assert row["machine_state"] == "IDLE"
        assert row["active_error"] == "NONE"
        assert row["last_seen"] is not None

    @pytest.mark.parametrize("key", ["", "x" * 40])
    def test_bad_key_401(self, mta, key):
        status, _ = mta.post(
            "/api/v1/device/heartbeat",
            {"device_id": mta.device_id, "state": "IDLE", "active_error": "NONE"},
            key=key,
        )
        assert status == 401

    def test_unknown_device_rejected(self, mta):
        mta.device_id = "NO-SUCH-DEVICE"
        assert mta.heartbeat()[0] in (401, 404)

    def test_state_is_reported_during_sale(self, mta, db):
        mta.start_sale("COLA")
        assert mta.heartbeat()[0] == 200
        state = db.execute(
            "SELECT machine_state FROM devices WHERE device_id=?", (mta.device_id,)
        ).fetchone()[0]
        assert state == "WAIT_PAYMENT"

    def test_active_error_in_heartbeat(self, mta, db):
        assert mta.set_error("COIN_JAM")[1] == 200
        assert mta.heartbeat()[0] == 200
        row = db.execute(
            "SELECT machine_state, active_error FROM devices WHERE device_id=?",
            (mta.device_id,),
        ).fetchone()
        assert (row[0], row[1]) == ("ERROR", "COIN_JAM")


# --- LOAD / UNLOAD -------------------------------------------------------------

class TestInventory:
    def test_load(self, mta, db):
        _b, status, answer = mta.load("COLA", 7)
        assert (status, answer["status"]) == (200, "accepted")
        assert stock_of(db, mta, "COLA") == 7

    def test_unload(self, stocked, db):
        assert stocked.unload("COLA", 2)[1] == 200
        assert stock_of(db, stocked, "COLA") == 3

    def test_unload_more_than_stock(self, stocked, db):
        assert stocked.unload("COLA", 6)[1] == 400
        assert stock_of(db, stocked, "COLA") == 5

    @pytest.mark.parametrize("qty", [0, -1, 1.5, "3", True])
    def test_invalid_quantity(self, mta, qty):
        assert mta.load("COLA", qty)[1] == 400

    def test_unknown_product(self, mta):
        assert mta.load("NO-SUCH", 1)[1] == 400

    def test_bad_event_key_401(self, mta):
        mta.key = "x" * 40
        assert mta.load("COLA", 1)[1] == 401


# --- SALE ----------------------------------------------------------------------

class TestSale:
    def test_full_cycle(self, stocked, db):
        body, status, answer = stocked.sell("COLA")
        assert (status, answer["status"]) == (200, "accepted")
        assert stocked.state == MTAState.IDLE
        assert stock_of(db, stocked, "COLA") == 4
        sale = db.execute(
            "SELECT * FROM sales WHERE device_id=?", (stocked.device_id,)
        ).fetchone()
        assert sale["session_id"] == body["session_id"]
        assert (sale["product_id"], sale["price"]) == ("COLA", PRODUCTS["COLA"])

    def test_states_along_the_way(self, stocked):
        assert stocked.state == MTAState.IDLE
        stocked.start_sale("WATER")
        assert stocked.state == MTAState.WAIT_PAYMENT
        stocked.pay()
        assert stocked.state == MTAState.DISPENSING
        result = stocked.dispense()
        assert result[1] == 200
        assert stocked.state == MTAState.SUCCESS
        stocked.finish()
        assert stocked.state == MTAState.IDLE

    def test_failed_payment_sends_nothing(self, stocked, db):
        before = event_count(db, stocked)
        stocked.start_sale("COLA")
        stocked.pay(success=False)
        assert stocked.state == MTAState.IDLE
        assert event_count(db, stocked) == before
        assert stock_of(db, stocked, "COLA") == 5

    def test_failed_dispense_goes_to_error(self, stocked, db):
        before = event_count(db, stocked)
        stocked.start_sale("COLA")
        stocked.pay()
        body, status, _a = stocked.dispense(success=False)
        assert status == 200
        assert (body["event_type"], body["data"]) == (
            "ERROR_SET", {"error_code": "DISPENSE_FAILURE"})
        assert stocked.state == MTAState.ERROR
        assert event_count(db, stocked) == before + 1
        assert stock_of(db, stocked, "COLA") == 5

    def test_service_door_clears_firmware_fault(self, stocked):
        stocked.start_sale("COLA")
        stocked.pay()
        stocked.dispense(success=False)
        body, status, _a = stocked.open_service_door()
        assert (status, body["event_type"]) == (200, "ERROR_CLEAR")
        assert stocked.state == MTAState.SERVICE
        assert stocked.active_errors == []
        stocked.close_service_door()
        assert stocked.state == MTAState.IDLE

    def test_service_dispense_sends_no_sale(self, stocked, db):
        before = event_count(db, stocked)
        stocked.open_service_door()
        assert stocked.service_dispense()
        assert stocked.state == MTAState.SERVICE
        assert event_count(db, stocked) == before

    def test_empty_and_tray_states(self, stocked):
        stocked.set_next_can(False)
        stocked.set_tray(False)
        stocked.open_service_door()
        stocked.close_service_door()
        assert stocked.state == MTAState.EMPTY
        stocked.set_next_can(True)
        assert stocked.state == MTAState.IDLE
        stocked.set_tray(True)
        assert stocked.state == MTAState.WAIT_CLEAR_DISPENSER
        stocked.set_tray(False)
        assert stocked.state == MTAState.IDLE

    def test_shutter_timeout_is_error(self, stocked):
        stocked.shutter_raised()
        assert stocked.state == MTAState.WAIT_SHUTTER_CLOSE
        body, *_ = stocked.shutter_timeout()
        assert body["data"] == {"error_code": "UNCLASSIFIED_ERROR"}
        assert stocked.state == MTAState.ERROR

    def test_retry_is_idempotent(self, stocked, db):
        body, *_ = stocked.sell("COLA")
        _b, status, answer = stocked.event(
            "SALE", body["data"], session_id=body["session_id"],
            event_id=body["event_id"], created_at=body["created_at"],
        )
        assert (status, answer["status"]) == (200, "already_accepted")
        assert stock_of(db, stocked, "COLA") == 4

    def test_duplicate_session_id(self, stocked, db):
        body, *_ = stocked.sell("COLA")
        _b, status, _a = stocked.event("SALE", body["data"], session_id=body["session_id"])
        assert status == 400
        assert stock_of(db, stocked, "COLA") == 4

    def test_sale_with_empty_stock(self, mta):
        _b, status, _a = mta.sell("COLA")
        assert status == 400

    def test_last_item_then_empty(self, mta, db):
        mta.load("COLA", 1)
        assert mta.sell("COLA")[1] == 200
        assert mta.sell("COLA")[1] == 400
        assert stock_of(db, mta, "COLA") == 0

    @pytest.mark.parametrize("patch", [
        {"payment": "FAILED"},
        {"dispense": "FAILED"},
        {"price": 0},
        {"price": -5},
        {"price": 1.5},
        {"product_id": "NO-SUCH"},
    ])
    def test_invalid_sale_data(self, stocked, db, patch):
        data = {"product_id": "COLA", "price": 150,
                "payment": "SUCCESS", "dispense": "SUCCESS", **patch}
        _b, status, _a = stocked.event("SALE", data, session_id=new_id())
        assert status == 400
        assert stock_of(db, stocked, "COLA") == 5

    def test_sale_without_session_id(self, stocked):
        data = {"product_id": "COLA", "price": 150,
                "payment": "SUCCESS", "dispense": "SUCCESS"}
        assert stocked.event("SALE", data)[1] == 400

    def test_rejected_sale_does_not_leave_row(self, mta, db):
        mta.sell("COLA")  # пустой склад -> 400
        assert db.execute(
            "SELECT COUNT(*) FROM sales WHERE device_id=?", (mta.device_id,)
        ).fetchone()[0] == 0


# --- ошибки --------------------------------------------------------------------

class TestErrors:
    def test_set_and_clear(self, mta, db):
        assert mta.set_error("COIN_JAM")[1] == 200
        assert mta.state == MTAState.ERROR
        assert mta.active_errors == ["COIN_JAM"]
        assert db.execute(
            "SELECT active_error FROM devices WHERE device_id=?", (mta.device_id,)
        ).fetchone()[0] == "COIN_JAM"
        assert mta.clear_error("COIN_JAM")[1] == 200
        assert mta.state == MTAState.IDLE
        row = db.execute(
            "SELECT active_error FROM devices WHERE device_id=?", (mta.device_id,)
        ).fetchone()
        assert row[0] == "NONE"

    def test_set_twice_is_noop(self, mta, db):
        assert mta.set_error("COIN_JAM")[1] == 200
        assert mta.set_error("COIN_JAM")[1] == 200
        assert db.execute(
            "SELECT COUNT(*) FROM device_errors WHERE device_id=? AND cleared_at IS NULL",
            (mta.device_id,),
        ).fetchone()[0] == 1

    def test_clear_without_active_error(self, mta):
        assert mta.clear_error("COIN_JAM")[1] == 400

    def test_clear_twice(self, mta):
        mta.set_error("COIN_JAM")
        assert mta.clear_error("COIN_JAM")[1] == 200
        assert mta.clear_error("COIN_JAM")[1] == 400

    def test_most_recent_error_wins(self, mta, db):
        mta.set_error("COIN_JAM")
        mta.set_error("COOLING_FAIL")
        assert mta.active_errors == ["COIN_JAM", "COOLING_FAIL"]
        mta.clear_error("COOLING_FAIL")
        assert mta.state == MTAState.ERROR
        assert db.execute(
            "SELECT active_error FROM devices WHERE device_id=?", (mta.device_id,)
        ).fetchone()[0] == "COIN_JAM"
        mta.clear_error("COIN_JAM")
        assert mta.state == MTAState.IDLE

    def test_error_survives_heartbeat_with_none(self, mta, db):
        mta.set_error("COIN_JAM")
        mta.active_errors.clear()  # автомат "забыл" об ошибке; сервер помнит
        mta.heartbeat()
        assert db.execute(
            "SELECT active_error FROM devices WHERE device_id=?", (mta.device_id,)
        ).fetchone()[0] == "COIN_JAM"


# --- валидация события ---------------------------------------------------------

class TestEventValidation:
    def test_unsupported_type(self, mta):
        assert mta.event("REFUND", {})[1] == 400

    def test_created_at_without_timezone(self, stocked):
        _b, status, _a = stocked.event(
            "LOAD", {"product_id": "COLA", "quantity": 1},
            created_at="2026-01-01T00:00:00",
        )
        assert status in (400, 422)

    def test_blank_error_code(self, mta):
        assert mta.event("ERROR_SET", {"error_code": " "})[1] == 400

    def test_idempotent_load(self, mta, db):
        body, *_ = mta.load("COLA", 3)
        _b, status, answer = mta.event(
            "LOAD", body["data"], event_id=body["event_id"], created_at=body["created_at"]
        )
        assert (status, answer["status"]) == (200, "already_accepted")
        assert stock_of(db, mta, "COLA") == 3

    def test_event_for_other_device_rejected(self, mta):
        mta.device_id = "NO-SUCH-DEVICE"
        assert mta.load("COLA", 1)[1] == 401

    def test_inventory_is_per_device(self, stocked, mta, server_url, db, device_key):
        # второй автомат из того же пула не видит остатки первого
        from virtualmta import VirtualMTA
        other_id = "MTA-001"
        db.execute("INSERT OR IGNORE INTO devices (device_id) VALUES (?)", (other_id,))
        db.commit()
        other = VirtualMTA(server_url, other_id, device_key, prices=dict(PRODUCTS))
        assert other.sell("COLA")[1] == 400
        assert stock_of(db, stocked, "COLA") == 5


# --- API оператора -------------------------------------------------------------

class TestOperatorApi:
    @staticmethod
    def _opener(server_url):
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        opener.addheaders = [("Origin", server_url)]
        return opener

    def test_requires_login(self, mta, server_url):
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{server_url}/api/v1/devices/{mta.device_id}")
        assert exc.value.code == 401

    def test_device_view_reflects_virtual_mta(self, mta, server_url):
        mta.set_error("COIN_JAM")
        mta.heartbeat()
        opener = self._opener(server_url)
        data = urllib.parse.urlencode({"username": "admin", "password": ADMIN_PASSWORD})
        opener.open(f"{server_url}/login", data.encode()).close()
        with opener.open(f"{server_url}/api/v1/devices/{mta.device_id}") as resp:
            info = json.load(resp)
        assert info["communication"] == "ONLINE"
        assert info["machine_state"] == "ERROR"
        assert info["active_error"] == "COIN_JAM"

    def test_wrong_password(self, server_url):
        opener = self._opener(server_url)
        data = urllib.parse.urlencode({"username": "admin", "password": "nope"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            opener.open(f"{server_url}/login", data.encode())
        assert exc.value.code == 401
