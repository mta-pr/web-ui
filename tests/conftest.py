import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from virtualmta import VirtualMTA

ROOT = Path(__file__).resolve().parent.parent
PRODUCTS = {"COLA": 150, "WATER": 80, "CHIPS": 120}
ADMIN_PASSWORD = "test-admin-password"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _password_hash(password: str) -> str:
    rounds, salt = 200_000, b"test-salt-0123456"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


@pytest.fixture(scope="session")
def device_key() -> str:
    return "k" * 40


@pytest.fixture(scope="session")
def db_path(tmp_path_factory) -> Path:
    """Пустая БД со схемой, созданной миграциями Alembic."""
    path = tmp_path_factory.mktemp("mta") / "test.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "MTA_DB_PATH": str(path)},
        check=True,
    )
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            "INSERT INTO products (product_id, name) VALUES (?, ?)",
            [(pid, pid.title()) for pid in PRODUCTS],
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture(scope="session")
def server_url(db_path, device_key, tmp_path_factory):
    """Запускает настоящий uvicorn с тестовой БД на свободном порту."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "MTA_DB_PATH": str(db_path),
        "MTA_ADMIN_PASSWORD_HASH": _password_hash(ADMIN_PASSWORD),
        "MTA_SESSION_SECRET": "s" * 48,
    }
    # env процесса сервера фиксируется при старте, поэтому ключи выдаём
    # заранее на пул device_id, из которого mta-фикстура берёт по одному.
    env["MTA_DEVICE_KEYS"] = json.dumps(
        {f"MTA-T{i:03d}": device_key for i in range(500)} | {"MTA-001": device_key}
    )
    log = (tmp_path_factory.mktemp("log") / "server.log").open("w")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "mta_project.mta_server:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited early, see {log.name}")
            try:
                urllib.request.urlopen(url + "/login", timeout=1).close()
                break
            except (urllib.error.URLError, ConnectionError):
                if time.monotonic() > deadline:
                    raise RuntimeError(f"server did not start, see {log.name}")
                time.sleep(0.1)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


@pytest.fixture(scope="session")
def _device_counter():
    return iter(range(500))


@pytest.fixture
def db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def mta(server_url, db_path, device_key, _device_counter) -> VirtualMTA:
    """Новый автомат (уникальный device_id) с нулевыми остатками."""
    device_id = f"MTA-T{next(_device_counter):03d}"
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO devices (device_id, name) VALUES (?, ?)",
                (device_id, "virtual"),
            )
            conn.executemany(
                "INSERT INTO inventory (device_id, product_id, quantity) VALUES (?, ?, 0)",
                [(device_id, pid) for pid in PRODUCTS],
            )
    finally:
        conn.close()
    return VirtualMTA(server_url, device_id, device_key, prices=dict(PRODUCTS))


@pytest.fixture
def stocked(mta) -> VirtualMTA:
    """Автомат с 5 шт. каждого товара."""
    for pid in PRODUCTS:
        assert mta.load(pid, 5)[1] == 200
    return mta


def stock(db, device_id, product_id) -> int:
    return db.execute(
        "SELECT quantity FROM inventory WHERE device_id=? AND product_id=?",
        (device_id, product_id),
    ).fetchone()[0]


def new_id() -> str:
    return str(uuid.uuid4())
