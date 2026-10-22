# MTA-project

Формальное описание проекта.

## 1. Общие сведения

| Параметр | Значение |
|---|---|
| Название пакета | `mta-project` v0.1.0 (автор: syncro128) |
| Назначение | Серверная часть системы мониторинга торговых автоматов (MTA). Автоматы отправляют на сервер события и heartbeat, оператор просматривает состояние через веб-панель. |
| Язык и версия | Python ≥ 3.14 (`.python-version`: 3.14) |
| Менеджер проекта | `uv` (`uv.lock`, сборка через `uv_build`) |
| Зависимости | `fastapi`, `uvicorn`, `python-multipart` (формы логина) |
| СУБД | SQLite3, файл `mta.db` (проект мигрировал с PostgreSQL) |
| Версии компонентов | Сервер — «v1.5, stage 2»; монитор — «MTA Monitor 1.0»; миграция ошибок — «v1.3» |

## 2. Структура репозитория

```
MTA-project/
├── pyproject.toml          # метаданные, зависимости, сборка
├── uv.lock, .python-version, .gitignore
├── README.md               # этот файл
├── alembic.ini             # конфигурация Alembic
├── migrations/versions/    # 0001 — базовая схема, 0002 — таблица device_errors
├── mta.db                  # SQLite-база (закоммичена как временная)
├── .env.example            # шаблон конфигурации (рабочий .env в git не попадает)
├── src/mta_project/
│   ├── __init__.py         # пустой
│   └── mta_server.py       # FastAPI-приложение
└── tests/
    ├── virtualmta.py       # виртуальный автомат + интерактивный эмулятор (п. 6)
    ├── conftest.py         # фикстуры: тестовый сервер, БД, автомат
    └── test_api.py         # pytest-тесты API
```

## 3. Модель данных (SQLite)

Все времена хранятся как фиксированный ISO 8601 UTC-текст
(`YYYY-MM-DDTHH:MM:SS.ffffffZ`), поэтому строковое сравнение совпадает
с хронологическим. Внешние ключи включаются на каждом соединении
(`PRAGMA foreign_keys = ON`).

### Миграция `0001` (базовая схема)

- `devices` — автоматы: `device_id` (PK), `name`, `location`,
  `machine_state` (по умолчанию `IDLE`), `active_error` (по умолчанию `NONE`),
  `last_seen`.
- `products` — справочник товаров: `product_id` (PK), `name`.
- `events` — журнал событий устройств: `event_id` (PK, ключ идемпотентности),
  `device_id` (FK), `event_type`, `session_id`, `created_at`,
  `data` (JSON, проверка `json_valid`).
- `inventory` — остатки: PK `(device_id, product_id)`,
  `quantity` с `CHECK (quantity >= 0)`.
- `sales` — продажи: `sale_id` (autoincrement), `session_id` (UNIQUE),
  FK на устройство, товар и событие (`event_id` UNIQUE), `price > 0`,
  `payment`, `dispense`, `sold_at`. Индекс `sales_device_time (device_id, sold_at DESC)`.

### Миграция `0002` (таблица ошибок)

- `device_errors` — интервалы неисправностей: `error_code`, `occurred_at`,
  `cleared_at`, `set_event_id` и `clear_event_id` (UNIQUE, FK на `events`),
  `details` (JSON).
- Ограничения: `cleared_at >= occurred_at`; `cleared_at` и `clear_event_id`
  либо оба NULL, либо оба заданы.
- Частичный уникальный индекс `(device_id, error_code) WHERE cleared_at IS NULL`:
  не более одной активной ошибки каждого кода на устройство.
- Индекс `device_errors_device_timeline (device_id, occurred_at DESC)`.

Схемой управляет Alembic; путь к базе берётся из `MTA_DB_PATH`
(по умолчанию `mta.db`):

```
uv run alembic upgrade head       # создать / обновить схему
uv run alembic downgrade -1       # откатить последнюю миграцию
uv run alembic revision --autogenerate -m "описание"   # новая миграция по models.py
```

Миграции `0001` и `0002` написаны на `CREATE ... IF NOT EXISTS`, поэтому
базу, созданную прежними скриптами `mta_schema.sql` / `mta_errors.sql`
(например, текущий `mta.db`), можно просто привести к `head` командой
`upgrade head`. Автогенерация не умеет сравнивать индексы с `DESC` и
nullable у `INTEGER PRIMARY KEY` в SQLite, поэтому `alembic check` и
сгенерированные миграции нужно просматривать вручную.

## 4. Серверное приложение (`mta_server.py`)

FastAPI-приложение; Swagger, ReDoc и OpenAPI отключены. Соединение с БД
открывается на каждый запрос (`get_db_connection()`): WAL-режим,
`row_factory=Row`, commit или rollback, затем закрытие.

### 4.1. Аутентификация устройств

Заголовок `X-MTA-Key` проверяется по JSON-словарю `MTA_DEVICE_KEYS`
(`{device_id: ключ}`):

- ключ не короче 32 символов;
- сравнение через `hmac.compare_digest`;
- при отсутствии или неверной конфигурации — 503 (fail closed), при неверном ключе — 401.

### 4.2. Аутентификация оператора

- Единственный пользователь — `admin`. Пароль проверяется по хешу
  `pbkdf2_sha256$раунды$соль_hex$хеш_hex` из `MTA_ADMIN_PASSWORD_HASH`
  (раундов не менее 200 000).
- Сессия — подписанная cookie `mta_operator_session`: содержимое
  `admin:<expiry>:<nonce>`, подпись HMAC-SHA256 ключом `MTA_SESSION_SECRET`
  (не короче 40 символов), срок жизни 8 часов. Флаги: `HttpOnly`,
  `SameSite=Strict`, `Secure` при `MTA_HTTPS=1`.
- Защита от CSRF — проверка `Origin` / `Referer`.
- Защита от перебора: не более 5 неудачных попыток за 300 с с одного IP
  (счётчик в памяти процесса).

### 4.3. HTTP-интерфейс

| Метод и путь | Доступ | Назначение |
|---|---|---|
| `GET/POST /login`, `POST /logout` | публичный, с проверкой origin | вход и выход оператора |
| `POST /api/v1/device/heartbeat` | ключ устройства | обновляет `machine_state`, `last_seen`, `active_error`; при незакрытых записях в `device_errors` берётся самая свежая, иначе переданная ошибка (только при `state='ERROR'`); 404, если устройства нет |
| `POST /api/v1/device/events` | ключ устройства | приём событий (п. 4.4) |
| `GET /api/v1/devices/{id}` | оператор | краткий статус; `ONLINE`, если heartbeat не старше 30 с |
| `GET /api/v1/monitor/{id}` | оператор | устройство, остатки, 10 последних продаж и ошибок |
| `GET /monitor` | оператор (иначе редирект на `/login`) | HTML-дашборд, обновление каждые 10 с, привязан к `MTA-001` |

### 4.4. Обработка событий

Допустимые `event_type`: `SALE`, `LOAD`, `UNLOAD`, `ERROR_SET`, `ERROR_CLEAR`.
`created_at` обязан содержать часовой пояс. Для `SALE` обязательны
`session_id`, `data.product_id`, положительная `data.price`,
`payment=SUCCESS` и `dispense=SUCCESS`.

Обработка идемпотентна и выполняется в одной транзакции:

1. `INSERT ... ON CONFLICT (event_id) DO NOTHING`; повтор возвращает `already_accepted`.
2. Действие по типу:
   - `SALE` — запись в `sales` и атомарное списание единицы остатка;
   - `LOAD` — увеличение остатка;
   - `UNLOAD` — уменьшение остатка с проверкой достаточности;
   - `ERROR_SET` — открытие записи в `device_errors`, установка `devices.active_error`;
   - `ERROR_CLEAR` — закрытие активной ошибки и пересчёт `active_error`
     по оставшимся открытым (иначе `NONE`).
3. Бизнес-ошибки и нарушения целостности дают 400 со структурой
   `{status, event_id, reason}`; прочие ошибки БД — 503.

## 5. Запуск

Конфигурация хранится в файле `.env` (в `.gitignore`); шаблон — `.env.example`.

1. `cp .env.example .env`
2. Вписать ключ устройства в `MTA_DEVICE_KEYS`, сгенерировать
   `MTA_SESSION_SECRET` (команда в шаблоне) и хеш пароля администратора
   (пароль ≥ 20 символов, вводится без эха):

   ```
   python -c "import hashlib,secrets,getpass; p=getpass.getpass().encode(); s=secrets.token_bytes(16); print('pbkdf2_sha256\$310000\$%s\$%s' % (s.hex(), hashlib.pbkdf2_hmac('sha256',p,s,310000).hex()))"
   ```

   Результат вписать в `MTA_ADMIN_PASSWORD_HASH` **в одинарных кавычках**:
   без них `$` трактуется как подстановка переменных и хеш портится.
3. `uv run server` (читает `.env`; другой файл: `uv run server --env-file other.env`; уже заданные переменные окружения имеют приоритет; по умолчанию `127.0.0.1:8001`)
   — сервер на `http://127.0.0.1:8001/login` (хост и порт — `UVICORN_HOST`,
   `UVICORN_PORT`). Для интернета не предназначен без HTTPS (`MTA_HTTPS=1`).

Переменные окружения (задаются в `.env`):

| Переменная | Значение |
|---|---|
| `MTA_DEVICE_KEYS` | JSON `{device_id: ключ}` |
| `MTA_ADMIN_PASSWORD_HASH` | `pbkdf2_sha256$раунды$соль$хеш` |
| `MTA_SESSION_SECRET` | секрет подписи сессий, ≥ 40 символов |
| `MTA_DB_PATH` | путь к SQLite-файлу (по умолчанию `mta.db`) |
| `MTA_HTTPS` | `1` — включить флаг `Secure` для cookie |
| `UVICORN_HOST`, `UVICORN_PORT` | адрес и порт для `uv run server` (по умолчанию `127.0.0.1` и `8001`) |

## 6. Эмулятор торгового автомата (`tests/virtualmta.py`)

Консольный клиент для ручной проверки сервера: скрипт `virtualmta.py`
(тот же класс `VirtualMTA`, что и в тестах) отправляет heartbeat и события по нажатию клавиш. Автоматические проверки
API — в `tests/test_api.py` (`uv run --group test pytest`).

```
uv run python tests/virtualmta.py seed    # один раз
export MTA_DEVICE_KEY=<ключ из MTA_DEVICE_KEYS>
uv run python tests/virtualmta.py run
```

| Подкоманда | Назначение |
|---|---|
| `seed` | добавляет в SQLite автомат, товар `WATER_05` и нулевые остатки; остатки обязательны, иначе сервер отвечает на `LOAD` 400 «Inventory record not found» |
| `run` | интерактивный режим: сам шлёт только heartbeat (по умолчанию каждые 10 с), события — по клавишам 1–9 |

Клавиши `run` (Enter не нужен; `h` — справка, `q` или Ctrl+C — выход):

| Клавиша | Действие |
|---|---|
| `1` | `SALE`: `WATER_05` (единственный товар прошивки) |
| `5` | `LOAD`: +10 шт. каждого товара (после `seed` остатки нулевые, без загрузки продажи отклоняются) |
| `6` | повтор последней продажи с тем же `event_id` (ожидается `already_accepted`) |
| `7` | `ERROR_SET`: следующий неактивный код из `COIN_JAM`, `DISPENSER_STUCK`, `COOLING_FAIL` |
| `8` | `ERROR_CLEAR`: снять последнюю ошибку |
| `9` | `UNLOAD`: −1 шт. каждого товара |
| `d` | сервисная дверь: открыть/закрыть (`SERVICE` ↔ проверка готовности) |
| `s` | сервисная выдача при открытой двери (без `SALE`) |
| `e` | датчик следующей банки нет/есть (`EMPTY`) |

Состояние в heartbeat — состояние `VirtualMTA` (`IDLE`, `WAIT_PAYMENT`,
`DISPENSING`, `SUCCESS`, `EMPTY`, `SERVICE`, `SERVICE_DISPENSING`,
`WAIT_CLEAR_DISPENSER`, `WAIT_SHUTTER_CLOSE`, `ERROR`; как в прошивке v0.3.10
вход в `ERROR` сам шлёт `ERROR_SET`, выход — `ERROR_CLEAR`); при открытой ошибке продажа невозможна.
Параметр `run`: `--heartbeat` (период, с). Без терминала (pipe) клавиши
читаются из stdin, на EOF эмулятор завершается.
