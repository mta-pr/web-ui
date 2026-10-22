"""Виртуальный торговый автомат для тестов и интерактивный эмулятор.

Класс VirtualMTA используется в pytest-тестах; запуск как скрипта даёт
интерактивный эмулятор: heartbeat по таймеру, клавиши 1-9 генерируют события.

Автомат MTA-001, товар WATER_05 и нулевой остаток создаёт миграция Alembic
(`alembic upgrade head`, её же применяет `uv run server`).

Настройка через переменные окружения или аргументы:

    MTA_URL         (по умолчанию http://127.0.0.1:8001)
    MTA_DEVICE_ID   (по умолчанию MTA-001)
    MTA_DEVICE_KEY  ключ устройства (тот же, что в MTA_DEVICE_KEYS сервера)

Пример:

    MTA_DEVICE_KEY=... uv run python tests/virtualmta.py --heartbeat 5
"""

import argparse
import contextlib
import enum
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

from mta_web_ui.models import EventType


class MTAState(enum.StrEnum):
    IDLE = enum.auto()
    WAIT_PAYMENT = enum.auto()
    DISPENSING = enum.auto()
    SUCCESS = enum.auto()
    EMPTY = enum.auto()
    SERVICE = enum.auto()
    SERVICE_DISPENSING = enum.auto()
    WAIT_CLEAR_DISPENSER = enum.auto()
    WAIT_SHUTTER_CLOSE = enum.auto()
    ERROR = enum.auto()


# Коды, которые прошивка (v0.3.10) выставляет сама при входе в ERROR.
FAULT_DISPENSE_FAILURE = "DISPENSE_FAILURE"
FAULT_UNCLASSIFIED = "UNCLASSIFIED_ERROR"

# Состояния, из которых открытие сервисной двери ведёт в SERVICE (как в прошивке);
# из остальных (оплата, выдача, SUCCESS) - в ERROR.
_DOOR_TO_SERVICE = (
    MTAState.IDLE,
    MTAState.EMPTY,
    MTAState.ERROR,
    MTAState.WAIT_CLEAR_DISPENSER,
    MTAState.WAIT_SHUTTER_CLOSE,
)


class VirtualMTA:
    def __init__(
        self,
        base_url: str,
        device_id: str,
        key: str,
        *,
        timeout: float = 5.0,
        state: MTAState = MTAState.IDLE,
        active_errors: list[str] | None = None,
        prices: dict[int, float] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id
        self.key = key
        self.timeout = timeout
        self.state = state
        self.active_errors = active_errors or []
        self.prices = prices or {}

        self.product_id: str | None = None
        self.session_id: str | None = None
        self.payment_ok = False

        # Датчики и контекст возврата, как в прошивке.
        self.next_can_present = True
        self.tray_occupied = False
        self._return_state = MTAState.IDLE
        self._fault: str | None = None

    # --- транспорт ---------------------------------------------------------

    def post(self, path: str, body: dict, key: str | None = None) -> tuple[int, Any]:
        """Вернуть (http_status, json_или_текст)."""
        headers = {"Content-Type": "application/json"}
        supplied = self.key if key is None else key
        if supplied:
            headers["X-MTA-Key"] = supplied
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return resp.status, self._decode(resp.read())
        except urllib.error.HTTPError as error:
            return error.code, self._decode(error.read())

    @staticmethod
    def _decode(raw: bytes) -> Any:
        text = raw.decode(errors="replace")
        try:
            return json.loads(text)
        except ValueError:
            return text

    # --- сообщения ---------------------------------------------------------

    def heartbeat(self) -> tuple[int, Any]:
        # Прошивка шлёт UNCLASSIFIED_ERROR для любого ERROR, иначе NONE.
        if self.active_errors:
            error = self.active_errors[-1]
        else:
            error = FAULT_UNCLASSIFIED if self.state == MTAState.ERROR else "NONE"
        return self.post(
            "/api/v1/device/heartbeat",
            {
                "device_id": self.device_id,
                "state": self.state.upper(),
                "active_error": error,
            },
        )

    def event(
        self,
        event_type: EventType,
        data: dict,
        session_id: str | None = None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> tuple[dict, int, Any]:
        body = {
            "event_id": event_id or str(uuid.uuid4()),
            "device_id": self.device_id,
            "event_type": event_type,
            "session_id": session_id,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        status, answer = self.post("/api/v1/device/events", body)
        return body, status, answer

    # --- складские события -------------------------------------------------

    def load(self, product_id: str, quantity: int) -> tuple[dict, int, Any]:
        return self.event(EventType.LOAD, {"product_id": product_id, "quantity": quantity})

    def unload(self, product_id: str, quantity: int) -> tuple[dict, int, Any]:
        return self.event(EventType.UNLOAD, {"product_id": product_id, "quantity": quantity})

    # --- ошибки ------------------------------------------------------------

    def set_error(self, code: str) -> tuple[dict, int, Any]:
        result = self.event(EventType.ERROR_SET, {"error_code": code})
        if result[1] == 200:
            if code not in self.active_errors:
                self.active_errors.append(code)
            self.state = MTAState.ERROR
        return result

    def clear_error(self, code: str) -> tuple[dict, int, Any]:
        result = self.event(EventType.ERROR_CLEAR, {"error_code": code})
        if result[1] == 200:
            if code in self.active_errors:
                self.active_errors.remove(code)
            if not self.active_errors:
                self.state = MTAState.IDLE
        return result

    # --- сценарий продажи --------------------------------------------------
    # IDLE -> WAIT_PAYMENT -> DISPENSING -> SUCCESS -> IDLE

    def _require_state(self, *allowed: MTAState) -> None:
        if self.state not in allowed:
            raise RuntimeError(
                f"invalid state {self.state}, expected one of {[str(s) for s in allowed]}"
            )

    def start_sale(self, product_id: str) -> None:
        """Клиент выбрал товар: ждём оплату."""
        self._require_state(MTAState.IDLE)
        if product_id not in self.prices:
            raise KeyError(f"no price for product {product_id!r}")
        self.product_id = product_id
        self.session_id = str(uuid.uuid4())
        self.state = MTAState.WAIT_PAYMENT

    def pay(self, *, success: bool = True) -> None:
        """Результат оплаты. Неуспешная оплата отменяет сессию без события."""
        self._require_state(MTAState.WAIT_PAYMENT)
        self.payment_ok = success
        if success:
            self.state = MTAState.DISPENSING
        else:
            self._cancel_session()

    def dispense(self, *, success: bool = True) -> tuple[dict, int, Any] | None:
        """Выдача товара; при успехе отправляет SALE, при сбое - ERROR_SET.

        Возвращает (body, status, answer) отправленного события.
        """
        if (price := self.prices.get(self.product_id)) is None:
            raise RuntimeError(
                f"Unexpected {price=} ({self.product_id=}, {self.prices=})"
            )

        self._require_state(MTAState.DISPENSING)
        if not success:
            self._reset_session()
            return self._transition(MTAState.ERROR)
        self.state = MTAState.SUCCESS
        self.tray_occupied = True  # банка лежит в лотке до тех пор, пока её не заберут

        result = self.event(
            EventType.SALE,
            {
                "product_id": self.product_id,
                "price": price,
                "payment": "SUCCESS",
                "dispense": "SUCCESS",
            },
            session_id=self.session_id,
        )
        self._reset_session()
        return result

    def finish(self) -> None:
        """Клиент забрал банку: SUCCESS -> проверка готовности (или ERROR, если есть ошибки)."""
        self._require_state(MTAState.SUCCESS)
        self.tray_occupied = False
        if self.active_errors:
            self.state = MTAState.ERROR
        else:
            self._readiness(MTAState.IDLE)

    def sell(self, product_id: str) -> tuple[dict, int, Any]:
        """Полная успешная продажа за один вызов."""
        self.start_sale(product_id)
        self.pay()
        result = self.dispense()
        self.finish()
        assert result is not None
        return result

    def _reset_session(self) -> None:
        self.product_id = None
        self.session_id = None
        self.payment_ok = False

    def _cancel_session(self) -> None:
        self._reset_session()
        self.state = MTAState.ERROR if self.active_errors else MTAState.IDLE

    # --- состояния прошивки и датчики --------------------------------------

    def _transition(self, new: MTAState) -> tuple[dict, int, Any] | None:
        """Сменить состояние; ERROR_SET/ERROR_CLEAR шлются как в прошивке."""
        old, self.state = self.state, new
        if new == MTAState.ERROR and old != MTAState.ERROR:
            self._fault = (
                FAULT_DISPENSE_FAILURE
                if old == MTAState.DISPENSING
                else FAULT_UNCLASSIFIED
            )
            if self._fault not in self.active_errors:
                self.active_errors.append(self._fault)
            return self.event(EventType.ERROR_SET, {"error_code": self._fault})
        if old == MTAState.ERROR and new != MTAState.ERROR and self._fault:
            fault, self._fault = self._fault, None
            if fault in self.active_errors:
                self.active_errors.remove(fault)
            return self.event(EventType.ERROR_CLEAR, {"error_code": fault})
        return None

    def _readiness(
        self, requested: MTAState = MTAState.IDLE
    ) -> tuple[dict, int, Any] | None:
        """Проверка готовности (без шторки и концевика выхода): EMPTY, лоток, затем цель."""
        if not self.next_can_present:
            return self._transition(MTAState.EMPTY)
        if self.tray_occupied:
            self._return_state = requested
            return self._transition(MTAState.WAIT_CLEAR_DISPENSER)
        return self._transition(requested)

    def set_next_can(self, present: bool) -> tuple[dict, int, Any] | None:
        """Датчик следующей банки. Из EMPTY выходит только по возвращении банки."""
        self.next_can_present = present
        if present and self.state == MTAState.EMPTY:
            return self._readiness(MTAState.IDLE)
        return None

    def set_tray(self, occupied: bool) -> tuple[dict, int, Any] | None:
        """Датчик лотка: банка в лотке блокирует продажу, пока её не заберут."""
        self.tray_occupied = occupied
        if occupied and self.state in (MTAState.IDLE, MTAState.WAIT_PAYMENT):
            self._return_state = self.state
            return self._transition(MTAState.WAIT_CLEAR_DISPENSER)
        if not occupied:
            if self.state == MTAState.SUCCESS:
                self.finish()
                return None
            if self.state == MTAState.WAIT_CLEAR_DISPENSER:
                return self._readiness(self._return_state)
        return None

    def open_service_door(self) -> tuple[dict, int, Any] | None:
        if self.state == MTAState.SERVICE:
            return None
        if self.state in _DOOR_TO_SERVICE or self.state == MTAState.SERVICE_DISPENSING:
            return self._transition(MTAState.SERVICE)
        self._reset_session()  # оплата/выдача/SUCCESS: дверь открыта некстати
        return self._transition(MTAState.ERROR)

    def close_service_door(self) -> tuple[dict, int, Any] | None:
        if self.state == MTAState.SERVICE_DISPENSING:
            return self._transition(MTAState.ERROR)
        self._require_state(MTAState.SERVICE)
        return self._readiness(MTAState.IDLE)

    def service_dispense(self) -> bool:
        """Сервисная выдача (дверь открыта): без оплаты и без события SALE.

        Условия старта как у короткого нажатия GPIO17: банка есть, лоток пуст.
        Возвращает True, если мотор запускался; цикл завершается обратно в SERVICE.
        """
        self._require_state(MTAState.SERVICE)
        if not self.next_can_present or self.tray_occupied:
            return False
        self.state = MTAState.SERVICE_DISPENSING
        self.state = MTAState.SERVICE
        return True

    def shutter_raised(self) -> tuple[dict, int, Any] | None:
        """Шторку подняли вне выдачи: защитное состояние с зуммером."""
        if self.state == MTAState.WAIT_CLEAR_DISPENSER:
            return self._transition(MTAState.WAIT_SHUTTER_CLOSE)
        if self.state in (
            MTAState.IDLE,
            MTAState.WAIT_PAYMENT,
            MTAState.SUCCESS,
            MTAState.EMPTY,
        ):
            self._return_state = self.state
            return self._transition(MTAState.WAIT_SHUTTER_CLOSE)
        return None

    def shutter_lowered(self) -> tuple[dict, int, Any] | None:
        if self.state == MTAState.WAIT_SHUTTER_CLOSE:
            return self._readiness(self._return_state)
        return None

    def shutter_timeout(self) -> tuple[dict, int, Any] | None:
        """15 секунд шторка не закрыта: ERROR (UNCLASSIFIED_ERROR)."""
        self._require_state(MTAState.WAIT_SHUTTER_CLOSE)
        return self._transition(MTAState.ERROR)


# --- интерактивный эмулятор ---------------------------------------------

DEFAULT_PRODUCTS = {
    "WATER_05": ("Water 0.5L", 80),  # единственный товар прошивки v0.3.10
}
ERROR_CODES = ("COIN_JAM", "DISPENSER_STUCK", "COOLING_FAIL")
LOAD_QUANTITY = 10


# --- run -------------------------------------------------------------------


def make_machine(args):
    if not args.key:
        sys.exit("device key is required: set MTA_DEVICE_KEY or pass --key")
    prices = {pid: price for pid, (_n, price) in DEFAULT_PRODUCTS.items()}
    return VirtualMTA(args.url, args.device_id, args.key, prices=prices)


def log(message):
    print("{}  {}".format(datetime.now().strftime("%H:%M:%S"), message), flush=True)


def report(label, result):
    _body, status, answer = result
    log("{:<12} -> {} {}".format(label, status, answer))


PRODUCT_KEYS = dict(zip("1", DEFAULT_PRODUCTS))
HELP = """\
клавиши (Enter не нужен):
  1    SALE: WATER_05
  5    LOAD: +{} шт. каждого товара
  6    повтор последней продажи (тот же event_id)
  7    ERROR_SET: следующий код из {}
  8    ERROR_CLEAR: снять последнюю ошибку
  9    UNLOAD: -1 шт. каждого товара
  d    сервисная дверь открыть/закрыть
  s    сервисная выдача (дверь открыта)
  e    датчик следующей банки: нет/есть (EMPTY)
  h    эта справка, q - выход""".format(LOAD_QUANTITY, ", ".join(ERROR_CODES))


def handle_hardware_key(machine, key):
    if key == "d":
        if machine.state in (MTAState.SERVICE, MTAState.SERVICE_DISPENSING):
            result, label = machine.close_service_door(), "door closed"
        else:
            result, label = machine.open_service_door(), "door open"
    elif key == "s":
        started = machine.service_dispense()
        log("service dispense: {}".format("выдано" if started else "условия не выполнены"))
        return
    else:
        result = machine.set_next_can(not machine.next_can_present)
        label = "next can " + ("present" if machine.next_can_present else "absent")
    log("{} -> state={}".format(label, machine.state))
    if result:
        report("  " + result[0]["event_type"], result)


def handle_key(machine, key, last_sale):
    """Выполнить действие клавиши; вернуть тело последней продажи."""
    if key in PRODUCT_KEYS:
        try:
            result = machine.sell(PRODUCT_KEYS[key])
        except RuntimeError as error:
            log("продажа невозможна: {}".format(error))
            return last_sale
        report("SALE " + PRODUCT_KEYS[key], result)
        return result[0] if result[1] == 200 else last_sale
    if key == "5":
        for product_id in DEFAULT_PRODUCTS:
            report("LOAD " + product_id, machine.load(product_id, LOAD_QUANTITY))
    elif key == "6":
        if last_sale is None:
            log("нет продажи для повтора")
        else:
            report(
                "SALE retry",
                machine.event(
                    EventType.SALE,
                    last_sale["data"],
                    session_id=last_sale["session_id"],
                    event_id=last_sale["event_id"],
                    created_at=last_sale["created_at"],
                ),
            )
    elif key == "7":
        free = [c for c in ERROR_CODES if c not in machine.active_errors]
        if free:
            report("ERROR_SET " + free[0], machine.set_error(free[0]))
        else:
            log("все коды ошибок уже активны")
    elif key == "8":
        if machine.active_errors:
            code = machine.active_errors[-1]
            report("ERROR_CLEAR " + code, machine.clear_error(code))
        else:
            log("активных ошибок нет")
    elif key == "9":
        for product_id in DEFAULT_PRODUCTS:
            report("UNLOAD " + product_id, machine.unload(product_id, 1))
    elif key in ("d", "s", "e"):
        try:
            handle_hardware_key(machine, key)
        except RuntimeError as error:
            log("действие невозможно: {}".format(error))
    return last_sale


@contextlib.contextmanager
def key_reader():
    """Дать функцию read_key(timeout): символ, None по таймауту или "" на EOF.

    Клавиши читаются без Enter: msvcrt на Windows, termios/select на POSIX.
    """
    if sys.platform == "win32":
        import msvcrt

        def read_key(timeout):
            deadline = time.monotonic() + timeout
            while not msvcrt.kbhit():
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.05)
            return msvcrt.getwch()

        yield read_key
        return

    import select
    import termios
    import tty

    interactive = sys.stdin.isatty()
    saved = termios.tcgetattr(sys.stdin) if interactive else None

    def read_key(timeout):
        if not select.select([sys.stdin], [], [], timeout)[0]:
            return None
        return os.read(sys.stdin.fileno(), 1).decode(errors="ignore")

    try:
        if interactive:
            tty.setcbreak(sys.stdin.fileno())
        yield read_key
    finally:
        if interactive:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, saved)


def cmd_run(args):
    machine = make_machine(args)
    last_sale = None
    next_heartbeat = 0.0
    print(HELP)
    try:
        with key_reader() as read_key:
            while True:
                if time.monotonic() >= next_heartbeat:
                    status, answer = machine.heartbeat()
                    log(
                        "heartbeat    -> {} {} state={}".format(
                            status, answer, machine.state
                        )
                    )
                    next_heartbeat = time.monotonic() + args.heartbeat
                wait = max(0.0, next_heartbeat - time.monotonic())
                key = read_key(wait)
                if key is None:
                    continue
                if key in ("q", "\x03", ""):  # q, Ctrl+C, EOF
                    break
                if key == "h":
                    print(HELP)
                else:
                    last_sale = handle_key(machine, key, last_sale) or last_sale
    except KeyboardInterrupt:
        pass
    log("stopped")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default=os.getenv("MTA_URL", "http://127.0.0.1:8001"))
    parser.add_argument("--device-id", default=os.getenv("MTA_DEVICE_ID", "MTA-001"))
    parser.add_argument("--key", default=os.getenv("MTA_DEVICE_KEY"))
    parser.add_argument(
        "--heartbeat", type=float, default=10.0, help="период heartbeat, с"
    )
    cmd_run(parser.parse_args(argv))


if __name__ == "__main__":
    main()
