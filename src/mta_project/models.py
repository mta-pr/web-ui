import enum
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class MachineState(enum.StrEnum):
    IDLE = "IDLE"
    WAIT_PAYMENT = "WAIT_PAYMENT"
    DISPENSING = "DISPENSING"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"


class EventType(enum.StrEnum):
    SALE = "SALE"
    LOAD = "LOAD"
    UNLOAD = "UNLOAD"
    ERROR_SET = "ERROR_SET"
    ERROR_CLEAR = "ERROR_CLEAR"


NO_ERROR = "NONE"


class UtcDateTime(sa.TypeDecorator):
    """Aware datetime stored as fixed-width UTC ISO 8601 text.

    Same format as to_db_time / from_db_time in mta_server.py, so string
    order equals time order.
    """

    impl = sa.String
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; pass a timezone-aware value")
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def process_result_value(self, value: str | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed


def _str_enum(enum_cls: type[enum.StrEnum]) -> sa.Enum:
    """Plain TEXT column holding enum values, without a CHECK constraint."""
    return sa.Enum(
        enum_cls,
        values_callable=lambda cls: [m.value for m in cls],
        native_enum=False,
        create_constraint=False,
        length=32,
    )


def _utc_column(nullable: bool = False) -> sa.Column:
    return sa.Column(UtcDateTime, nullable=nullable)


def _json_column() -> sa.Column:
    return sa.Column(sa.JSON, nullable=False, server_default=sa.text("'{}'"))


class Device(SQLModel, table=True):
    __tablename__ = "devices"  # pyright: ignore[reportAssignmentType]

    device_id: str = Field(primary_key=True)
    name: str | None = None
    location: str | None = None
    machine_state: MachineState = Field(
        default=MachineState.IDLE,
        sa_column=sa.Column(
            _str_enum(MachineState),
            nullable=False,
            server_default=MachineState.IDLE.value,
        ),
    )
    active_error: str = Field(
        default=NO_ERROR,
        sa_column=sa.Column(sa.String, nullable=False, server_default=NO_ERROR),
    )
    last_seen: datetime | None = Field(default=None, sa_column=_utc_column(True))


class Product(SQLModel, table=True):
    __tablename__ = "products"  # pyright: ignore[reportAssignmentType]

    product_id: str = Field(primary_key=True)
    name: str | None = None


class Event(SQLModel, table=True):
    __tablename__ = "events"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (sa.CheckConstraint("json_valid(data)"),)

    event_id: str = Field(primary_key=True)
    device_id: str = Field(foreign_key="devices.device_id")
    event_type: EventType = Field(sa_column=sa.Column(_str_enum(EventType), nullable=False))
    session_id: str | None = None
    created_at: datetime = Field(sa_column=_utc_column())
    data: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())


class InventoryItem(SQLModel, table=True):
    __tablename__ = "inventory"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (sa.CheckConstraint("quantity >= 0"),)

    device_id: str = Field(primary_key=True, foreign_key="devices.device_id")
    product_id: str = Field(primary_key=True, foreign_key="products.product_id")
    quantity: int = Field(
        default=0,
        sa_column=sa.Column(sa.Integer, nullable=False, server_default="0"),
    )


class Sale(SQLModel, table=True):
    __tablename__ = "sales"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        sa.CheckConstraint("price > 0"),
        sa.Index("sales_device_time", "device_id", sa.text("sold_at DESC")),
        {"sqlite_autoincrement": True},
    )

    sale_id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(sa_column=sa.Column(sa.String, nullable=False, unique=True))
    device_id: str = Field(foreign_key="devices.device_id")
    product_id: str = Field(foreign_key="products.product_id")
    price: int
    payment: str
    dispense: str
    sold_at: datetime = Field(sa_column=_utc_column())
    event_id: str = Field(
        sa_column=sa.Column(
            sa.String, sa.ForeignKey("events.event_id"), nullable=False, unique=True
        )
    )


class DeviceError(SQLModel, table=True):
    __tablename__ = "device_errors"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        sa.CheckConstraint(
            "cleared_at IS NULL OR cleared_at >= occurred_at",
            name="device_errors_times_valid",
        ),
        sa.CheckConstraint(
            "(cleared_at IS NULL) = (clear_event_id IS NULL)",
            name="device_errors_clear_consistency",
        ),
        sa.CheckConstraint("json_valid(details)"),
        sa.Index(
            "device_errors_one_active_per_code",
            "device_id",
            "error_code",
            unique=True,
            sqlite_where=sa.text("cleared_at IS NULL"),
        ),
        sa.Index(
            "device_errors_device_timeline",
            "device_id",
            sa.text("occurred_at DESC"),
        ),
        {"sqlite_autoincrement": True},
    )

    error_id: int | None = Field(default=None, primary_key=True)
    device_id: str = Field(foreign_key="devices.device_id")
    error_code: str
    occurred_at: datetime = Field(sa_column=_utc_column())
    cleared_at: datetime | None = Field(default=None, sa_column=_utc_column(True))
    set_event_id: str = Field(
        sa_column=sa.Column(
            sa.String, sa.ForeignKey("events.event_id"), nullable=False, unique=True
        )
    )
    clear_event_id: str | None = Field(
        default=None,
        sa_column=sa.Column(sa.String, sa.ForeignKey("events.event_id"), unique=True),
    )
    details: dict[str, Any] = Field(default_factory=dict, sa_column=_json_column())
