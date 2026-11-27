"""MTA server v1.5 (stage 2): device and operator authentication (SQLite3)."""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory=STATIC_DIR / "templates")


def require_device_key(device_id: str, supplied_key: str | None) -> None:
    """Authenticate device requests against per-device keys in MTA_DEVICE_KEYS.

    Environment example: MTA_DEVICE_KEYS={"MTA-001":"long-random-secret"}
    Fail closed if configuration is missing or invalid. Never log keys.
    """
    raw = os.getenv("MTA_DEVICE_KEYS", "")
    try:
        keys = json.loads(raw)
    except ValueError, TypeError:
        keys = None
    if not isinstance(keys, dict) or not keys:
        logger.error("Device authentication is not configured")
        raise HTTPException(
            status_code=503, detail="Device authentication not configured"
        )
    expected = keys.get(device_id)
    if (
        not isinstance(expected, str)
        or len(expected) < 32
        or not isinstance(supplied_key, str)
        or not hmac.compare_digest(expected, supplied_key)
    ):
        raise HTTPException(status_code=401, detail="Invalid device credentials")


# --- Operator login: local testing on 127.0.0.1 only. Use HTTPS + secure cookies on VPS. ---
COOKIE_NAME = "mta_operator_session"
SESSION_LIFETIME = 8 * 3600
LOGIN_ATTEMPTS = defaultdict(deque)  # Local MVP only; use shared storage on VPS.


def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _operator_config():
    """Expected hash: pbkdf2_sha256$iterations$salt_hex$hash_hex."""
    digest = os.getenv("MTA_ADMIN_PASSWORD_HASH", "")
    signing_key = os.getenv("MTA_SESSION_SECRET", "")
    if not digest.startswith("pbkdf2_sha256$") or len(signing_key) < 40:
        raise HTTPException(503, "Operator authentication not configured")
    return digest, signing_key.encode("utf-8")


def _verify_password(password, stored):
    try:
        scheme, rounds, salt, expected = stored.split("$")
        if scheme != "pbkdf2_sha256" or int(rounds) < 200000:
            return False
        computed = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds)
        )
        return hmac.compare_digest(computed, bytes.fromhex(expected))
    except ValueError, TypeError:
        return False


def _sign_session(signing_key):
    payload = "admin:{}:{}".format(
        int(time.time()) + SESSION_LIFETIME, secrets.token_hex(16)
    )
    body = _b64(payload.encode("utf-8"))
    signature = _b64(
        hmac.new(signing_key, body.encode("ascii"), hashlib.sha256).digest()
    )
    return body + "." + signature


def _has_session(request):
    try:
        _, signing_key = _operator_config()
        body, signature = request.cookies.get(COOKIE_NAME, "").split(".", 1)
        expected = _b64(
            hmac.new(signing_key, body.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return False
        role, expiry, nonce = _unb64(body).decode("utf-8").split(":", 2)
        return role == "admin" and len(nonce) == 32 and time.time() < int(expiry)
    except ValueError, UnicodeError, TypeError:
        return False


def require_operator(request: Request):
    if not _has_session(request):
        raise HTTPException(status_code=401, detail="Operator login required")


def _same_origin(request):
    origin = request.headers.get("origin")
    if origin:
        return origin == str(request.base_url).rstrip("/")
    referer = request.headers.get("referer", "")
    return referer.startswith(str(request.base_url))


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _has_session(request):
        return RedirectResponse("/monitor", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={
        "error_placeholder": "",
    })


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not _same_origin(request):
        raise HTTPException(403, "Origin check failed")
    digest, signing_key = _operator_config()
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = LOGIN_ATTEMPTS[client]
    while attempts and now - attempts[0] > 300:
        attempts.popleft()
    if len(attempts) >= 5:
        raise HTTPException(429, "Too many login attempts. Try again later")
    valid = hmac.compare_digest(username, "admin") and _verify_password(
        password, digest
    )
    if not valid:
        attempts.append(now)
        return templates.TemplateResponse(
            request=request, 
            name="login.html", 
            status_code=401, 
            headers={"Cache-Control": "no-store"}, 
            context={
                "error_placeholder": '<div class="err">Неверный логин или пароль</div>',
            }
        )
    attempts.clear()
    response = RedirectResponse("/monitor", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        _sign_session(signing_key),
        max_age=SESSION_LIFETIME,
        httponly=True,
        samesite="strict",
        secure=os.getenv("MTA_HTTPS", "0") == "1",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/logout")
def logout(request: Request):
    if not _same_origin(request):
        raise HTTPException(403, "Origin check failed")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


class Heartbeat(BaseModel):
    device_id: str
    state: str
    active_error: str


class DeviceEvent(BaseModel):
    event_id: str
    device_id: str
    event_type: str
    session_id: str | None = None
    created_at: datetime
    data: dict = Field(default_factory=dict)


def get_db_path():
    return os.getenv("MTA_DB_PATH", "mta.db")


@contextmanager
def get_db_connection():
    """Open a connection; commit on success, roll back on error, always close."""
    conn = sqlite3.connect(get_db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def to_db_time(value):
    """Fixed-width UTC ISO 8601 text, so string order equals time order."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def from_db_time(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def require_positive_int(value, field):
    if type(value) is not int or value <= 0:
        raise ValueError("{field} must be a positive integer")
    return value


def validate_event(event):
    require_text(event.event_id, "event_id")
    require_text(event.device_id, "device_id")

    if event.created_at.tzinfo is None or event.created_at.utcoffset() is None:
        raise ValueError("created_at must include timezone")

    if event.event_type not in (
        "SALE",
        "LOAD",
        "UNLOAD",
        "ERROR_SET",
        "ERROR_CLEAR",
    ):
        raise ValueError("unsupported event_type")

    if event.event_type in ("LOAD", "UNLOAD"):
        require_text(event.data.get("product_id"), "data.product_id")
        require_positive_int(event.data.get("quantity"), "data.quantity")

    if event.event_type in ("ERROR_SET", "ERROR_CLEAR"):
        require_text(event.data.get("error_code"), "data.error_code")

    if event.event_type == "SALE":
        require_text(event.session_id, "session_id")
        require_text(event.data.get("product_id"), "data.product_id")
        require_positive_int(event.data.get("price"), "data.price")

        if event.data.get("payment") != "SUCCESS":
            raise ValueError("SALE requires payment=SUCCESS")

        if event.data.get("dispense") != "SUCCESS":
            raise ValueError("SALE requires dispense=SUCCESS")


def bad_request(event_id, reason):
    return HTTPException(
        status_code=400,
        detail={
            "status": "bad_request",
            "event_id": event_id,
            "reason": reason,
        },
    )


@app.post("/api/v1/device/heartbeat")
def heartbeat(data: Heartbeat, x_mta_key: str | None = Header(default=None)):
    require_device_key(data.device_id, x_mta_key)
    try:
        require_text(data.device_id, "device_id")
        require_text(data.state, "state")
        require_text(data.active_error, "active_error")
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    try:
        with get_db_connection() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                """
                    UPDATE devices
                    SET machine_state = ?,
                        active_error = COALESCE(
                            (
                                SELECT error_code
                                FROM device_errors
                                WHERE device_id = ?
                                  AND cleared_at IS NULL
                                ORDER BY occurred_at DESC, error_id DESC
                                LIMIT 1
                            ),
                            CASE
                                WHEN ? = 'ERROR' THEN ?
                                ELSE 'NONE'
                            END
                        ),
                        last_seen = ?
                    WHERE device_id = ?
                    """,
                (
                    data.state,
                    data.device_id,
                    data.state,
                    data.active_error,
                    to_db_time(datetime.now(timezone.utc)),
                    data.device_id,
                ),
            )

            if cur.rowcount == 0:
                raise HTTPException(
                    status_code=404,
                    detail="Device not found",
                )

    except sqlite3.Error:
        logger.exception("Heartbeat database failure")
        raise HTTPException(
            status_code=503,
            detail="Database temporarily unavailable",
        )

    return {"status": "ok"}


@app.get("/api/v1/devices/{device_id}")
def get_device(device_id: str, _operator=Depends(require_operator)):
    try:
        with get_db_connection() as conn, closing(conn.cursor()) as cur:
            cur.execute(
                """
                    SELECT device_id, machine_state, active_error, last_seen
                    FROM devices
                    WHERE device_id = ?
                    """,
                (device_id,),
            )

            row = cur.fetchone()

    except sqlite3.Error:
        logger.exception("Device lookup database failure")
        raise HTTPException(
            status_code=503,
            detail="Database temporarily unavailable",
        )

    if row is None:
        return {
            "status": "not_found",
            "device_id": device_id,
        }

    found_id, machine_state, active_error, last_seen = tuple(row)
    last_seen = from_db_time(last_seen)

    seconds_since_last_seen = None

    if last_seen is not None:
        seconds_since_last_seen = max(
            0.0,
            (datetime.now(timezone.utc) - last_seen).total_seconds(),
        )

    communication = (
        "ONLINE"
        if (seconds_since_last_seen is not None and seconds_since_last_seen <= 30)
        else "OFFLINE"
    )

    return {
        "device_id": found_id,
        "communication": communication,
        "machine_state": machine_state,
        "active_error": active_error,
        "last_seen": last_seen,
        "seconds_since_last_seen": (
            round(seconds_since_last_seen, 1)
            if seconds_since_last_seen is not None
            else None
        ),
    }


@app.post("/api/v1/device/events")
def receive_event(event: DeviceEvent, x_mta_key: str | None = Header(default=None)):
    require_device_key(event.device_id, x_mta_key)
    try:
        validate_event(event)
    except ValueError as error:
        raise bad_request(event.event_id, str(error))

    try:
        with get_db_connection() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(
                    """
                    INSERT INTO events (
                        event_id,
                        device_id,
                        event_type,
                        session_id,
                        created_at,
                        data
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (event_id) DO NOTHING
                    RETURNING event_id
                    """,
                    (
                        event.event_id,
                        event.device_id,
                        event.event_type,
                        event.session_id,
                        to_db_time(event.created_at),
                        json.dumps(event.data),
                    ),
                )

                if not cur.fetchall():
                    return {
                        "status": "already_accepted",
                        "event_id": event.event_id,
                    }

                if event.event_type == "SALE":
                    cur.execute(
                        """
                        INSERT INTO sales (
                            session_id,
                            device_id,
                            product_id,
                            price,
                            payment,
                            dispense,
                            sold_at,
                            event_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.session_id,
                            event.device_id,
                            event.data["product_id"],
                            event.data["price"],
                            event.data["payment"],
                            event.data["dispense"],
                            to_db_time(event.created_at),
                            event.event_id,
                        ),
                    )

                    cur.execute(
                        """
                        UPDATE inventory
                        SET quantity = quantity - 1
                        WHERE device_id = ?
                          AND product_id = ?
                          AND quantity > 0
                        RETURNING quantity
                        """,
                        (
                            event.device_id,
                            event.data["product_id"],
                        ),
                    )

                    if not cur.fetchall():
                        raise ValueError("Product not found or inventory is empty")

                elif event.event_type == "LOAD":
                    cur.execute(
                        """
                        UPDATE inventory
                        SET quantity = quantity + ?
                        WHERE device_id = ?
                          AND product_id = ?
                        RETURNING quantity
                        """,
                        (
                            event.data["quantity"],
                            event.device_id,
                            event.data["product_id"],
                        ),
                    )

                    if not cur.fetchall():
                        raise ValueError("Inventory record not found")

                elif event.event_type == "UNLOAD":
                    cur.execute(
                        """
                        UPDATE inventory
                        SET quantity = quantity - ?
                        WHERE device_id = ?
                          AND product_id = ?
                          AND quantity >= ?
                        RETURNING quantity
                        """,
                        (
                            event.data["quantity"],
                            event.device_id,
                            event.data["product_id"],
                            event.data["quantity"],
                        ),
                    )

                    if not cur.fetchall():
                        raise ValueError("Product not found or insufficient inventory")

                elif event.event_type == "ERROR_SET":
                    cur.execute(
                        """
                        INSERT INTO device_errors (
                            device_id,
                            error_code,
                            occurred_at,
                            set_event_id,
                            details
                        )
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT (device_id, error_code)
                            WHERE cleared_at IS NULL
                        DO NOTHING
                        """,
                        (
                            event.device_id,
                            event.data["error_code"],
                            to_db_time(event.created_at),
                            event.event_id,
                            json.dumps(event.data),
                        ),
                    )

                    cur.execute(
                        """
                        UPDATE devices
                        SET active_error = ?
                        WHERE device_id = ?
                        """,
                        (
                            event.data["error_code"],
                            event.device_id,
                        ),
                    )

                elif event.event_type == "ERROR_CLEAR":
                    cur.execute(
                        """
                        UPDATE device_errors
                        SET cleared_at = ?,
                            clear_event_id = ?
                        WHERE error_id = (
                            SELECT error_id
                            FROM device_errors
                            WHERE device_id = ?
                              AND error_code = ?
                              AND cleared_at IS NULL
                        )
                        RETURNING error_id
                        """,
                        (
                            to_db_time(event.created_at),
                            event.event_id,
                            event.device_id,
                            event.data["error_code"],
                        ),
                    )

                    if not cur.fetchall():
                        raise ValueError("No active fault with this error_code")

                    cur.execute(
                        """
                        UPDATE devices
                        SET active_error = COALESCE(
                            (
                                SELECT error_code
                                FROM device_errors
                                WHERE device_id = ?
                                  AND cleared_at IS NULL
                                ORDER BY occurred_at DESC, error_id DESC
                                LIMIT 1
                            ),
                            'NONE'
                        )
                        WHERE device_id = ?
                        """,
                        (
                            event.device_id,
                            event.device_id,
                        ),
                    )

        return {
            "status": "accepted",
            "event_id": event.event_id,
        }

    except ValueError as error:
        raise bad_request(event.event_id, str(error))

    except sqlite3.IntegrityError as error:
        code = getattr(error, "sqlite_errorname", "")
        if code == "SQLITE_CONSTRAINT_FOREIGNKEY":
            reason = "Unknown device or product"
        elif code in ("SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY"):
            reason = "Conflicting sale or session_id"
        elif code == "SQLITE_CONSTRAINT_CHECK":
            reason = "Invalid fault timestamps or constraints"
        else:
            logger.exception("Event integrity failure: %s", event.event_id)
            raise HTTPException(
                status_code=503,
                detail="Database temporarily unavailable",
            )
        raise bad_request(event.event_id, reason)

    except sqlite3.Error:
        logger.exception(
            "Event database failure: ?",
            event.event_id,
        )
        raise HTTPException(
            status_code=503,
            detail="Database temporarily unavailable",
        )


# --- MTA Monitor 1.0: read-only dashboard, using existing SQLite tables ---
@app.get("/api/v1/monitor/{device_id}")
def monitor_device(device_id: str, _operator=Depends(require_operator)):
    try:
        with get_db_connection() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(
                    """
                    SELECT device_id, name, location, machine_state,
                           active_error, last_seen
                    FROM devices WHERE device_id = ?
                """,
                    (device_id,),
                )
                device = cur.fetchone()
                if device is None:
                    raise HTTPException(status_code=404, detail="Device not found")

                cur.execute(
                    """
                    SELECT i.product_id, i.quantity
                    FROM inventory AS i
                    WHERE i.device_id = ?
                    ORDER BY i.product_id
                """,
                    (device_id,),
                )
                inventory = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT session_id, product_id, price, sold_at
                    FROM sales WHERE device_id = ?
                    ORDER BY sold_at DESC, session_id DESC LIMIT 10
                """,
                    (device_id,),
                )
                sales = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT error_code, occurred_at, cleared_at
                    FROM device_errors WHERE device_id = ?
                    ORDER BY occurred_at DESC, error_id DESC LIMIT 10
                """,
                    (device_id,),
                )
                errors = [dict(row) for row in cur.fetchall()]
    except sqlite3.Error:
        logger.exception("Monitor database failure")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")

    device = dict(device)
    last_seen = from_db_time(device["last_seen"])
    age = (
        max(0.0, (datetime.now(timezone.utc) - last_seen).total_seconds())
        if last_seen is not None
        else None
    )
    device["communication"] = "ONLINE" if age is not None and age <= 30 else "OFFLINE"
    device["seconds_since_last_seen"] = round(age, 1) if age is not None else None
    return {"device": device, "inventory": inventory, "sales": sales, "errors": errors}


@app.get("/monitor", response_class=HTMLResponse)
def monitor_page(request: Request):
    if not _has_session(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request=request, name="monitor.html", headers={"Cache-Control": "no-store"}
    )


def load_env_file(path):
    """Load KEY=VALUE lines into os.environ; variables already set win."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)
    return True


def migrate_database():
    """Bring the database to the latest Alembic revision."""
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parent / "migrations")
    )
    command.upgrade(config, "head")


def main():
    """Entry point for `uv run server`; host/port from UVICORN_HOST/UVICORN_PORT."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the MTA server")
    parser.add_argument("--env-file", default=".env", help="default: .env")
    args = parser.parse_args()
    if not load_env_file(args.env_file):
        logger.warning("env file %s not found", args.env_file)

    migrate_database()
    uvicorn.run(
        app,
        host=os.getenv("UVICORN_HOST", "127.0.0.1"),
        port=int(os.getenv("UVICORN_PORT", "8001")),
    )

