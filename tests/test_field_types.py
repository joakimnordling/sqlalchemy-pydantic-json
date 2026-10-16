"""Non-JSON types (datetimes, UUIDs, decimals, enums...), stored as strings or numbers."""

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum
from ipaddress import IPv4Address
from typing import Annotated, Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from pydantic import ConfigDict, HttpUrl, Strict, ValidationError
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel

from .helpers import capture_updates

MakeEngine = Callable[[sa.MetaData], sa.Engine]

PLUS_3 = timezone(timedelta(hours=3))


class Color(StrEnum):
    RED = "red"
    BLUE = "blue"


class Level(IntEnum):
    LOW = 1
    HIGH = 2


class Shape(Enum):
    CIRCLE = "circle"
    SQUARE = "square"


class Event(EmbeddedPydanticModel):
    at: datetime = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=PLUS_3)
    id: UUID = UUID(int=1)


class Values(EmbeddedPydanticModel):
    naive: datetime = datetime(2026, 1, 2, 3, 4, 5, 678901)
    aware: datetime = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=PLUS_3)
    utc: datetime = datetime(2026, 1, 2, tzinfo=UTC)
    day: date = date(2026, 1, 2)
    clock: time = time(3, 4, 5, 678901)
    duration: timedelta = timedelta(days=1, seconds=2, microseconds=3)
    uid: UUID = UUID("12345678-1234-5678-1234-567812345678")
    amount: Decimal = Decimal("12345678901234567890.123456789")
    color: Color = Color.RED
    level: Level = Level.LOW
    shape: Shape = Shape.CIRCLE
    raw: bytes = b"abc"
    url: HttpUrl = HttpUrl("https://example.com/path?q=1")
    ip: IPv4Address = IPv4Address("192.0.2.1")
    times: list[datetime] = [datetime(2026, 1, 2, tzinfo=PLUS_3)]
    ids: set[UUID] = {UUID(int=1)}
    colors: set[Color] = {Color.RED}
    prices: dict[str, Decimal] = {"a": Decimal("0.10")}
    by_day: dict[date, int] = {date(2026, 1, 2): 1}
    by_id: dict[UUID, str] = {UUID(int=1): "one"}
    by_level: dict[Level, int] = {Level.LOW: 1}
    by_shape: dict[Shape, int] = {Shape.CIRCLE: 1}
    event: Event = Event()
    events: list[Event] = [Event()]


class Base(DeclarativeBase):
    pass


class Row(Base):
    __tablename__ = "field_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    # JSONB on PostgreSQL, where the json type can't be compared with =
    values: Mapped[Values] = mapped_column(
        Values.column(
            json_type=sa.JSON(none_as_null=True).with_variant(
                JSONB(none_as_null=True), "postgresql"
            )
        ),
        default=Values,
    )


@pytest.fixture
def engine(make_engine: MakeEngine) -> sa.Engine:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1))
        s.commit()
    return engine


def first_key(d: dict[Any, Any]) -> Any:
    return next(iter(d))


def test_round_trip(engine: sa.Engine) -> None:
    with Session(engine) as s:
        values = s.get_one(Row, 1).values
    assert values == Values()
    assert values.aware.utcoffset() == timedelta(hours=3)
    assert values.utc.utcoffset() == timedelta(0)
    assert values.naive.tzinfo is None
    assert str(values.amount) == "12345678901234567890.123456789"
    assert str(values.prices["a"]) == "0.10"  # the precision is kept
    assert [type(x) for x in (values.color, values.level, values.shape)] == [Color, Level, Shape]
    assert type(first_key(values.by_day)) is date
    assert type(first_key(values.by_id)) is UUID
    assert type(first_key(values.by_level)) is Level
    assert type(first_key(values.by_shape)) is Shape
    assert type(next(iter(values.ids))) is UUID
    assert type(next(iter(values.colors))) is Color


def test_loading_does_not_mark_dirty(engine: sa.Engine) -> None:
    """Values that change form when stored (a UUID to a string...) don't make a loaded row dirty."""
    with Session(engine) as s, capture_updates(engine) as updates:
        row = s.get_one(Row, 1)
        assert row.values == Values()
        assert row not in s.dirty
        s.commit()
    assert updates == []


@pytest.mark.parametrize(
    ("change", "saved"),
    [
        pytest.param(
            lambda v: setattr(v, "aware", datetime(2027, 1, 1, tzinfo=PLUS_3)),
            lambda v: v.aware == datetime(2027, 1, 1, tzinfo=PLUS_3),
            id="datetime",
        ),
        pytest.param(
            lambda v: setattr(v, "day", date(2027, 1, 1)),
            lambda v: v.day == date(2027, 1, 1),
            id="date",
        ),
        pytest.param(
            lambda v: setattr(v, "duration", timedelta(hours=5)),
            lambda v: v.duration == timedelta(hours=5),
            id="timedelta",
        ),
        pytest.param(
            lambda v: setattr(v, "uid", UUID(int=7)),
            lambda v: v.uid == UUID(int=7),
            id="uuid",
        ),
        pytest.param(
            lambda v: setattr(v, "amount", Decimal("1.50")),
            lambda v: str(v.amount) == "1.50",
            id="decimal",
        ),
        pytest.param(
            lambda v: setattr(v, "color", Color.BLUE),
            lambda v: v.color is Color.BLUE,
            id="enum",
        ),
        pytest.param(
            lambda v: setattr(v, "raw", b"xyz"),
            lambda v: v.raw == b"xyz",
            id="bytes",
        ),
        pytest.param(
            lambda v: v.times.append(datetime(2027, 1, 1, tzinfo=UTC)),
            lambda v: v.times[-1] == datetime(2027, 1, 1, tzinfo=UTC),
            id="list-append",
        ),
        pytest.param(
            lambda v: v.ids.add(UUID(int=2)),
            lambda v: v.ids == {UUID(int=1), UUID(int=2)},
            id="set-add",
        ),
        pytest.param(
            lambda v: v.colors.discard(Color.RED),
            lambda v: v.colors == set(),
            id="set-discard",
        ),
        pytest.param(
            lambda v: v.prices.__setitem__("b", Decimal("2.000")),
            lambda v: str(v.prices["b"]) == "2.000",
            id="dict-value",
        ),
        pytest.param(
            lambda v: v.by_day.__setitem__(date(2027, 1, 1), 2),
            lambda v: v.by_day == {date(2026, 1, 2): 1, date(2027, 1, 1): 2},
            id="date-key",
        ),
        pytest.param(
            lambda v: v.by_id.pop(UUID(int=1)),
            lambda v: v.by_id == {},
            id="uuid-key-pop",
        ),
        pytest.param(
            lambda v: v.by_level.update({Level.HIGH: 2}),
            lambda v: v.by_level == {Level.LOW: 1, Level.HIGH: 2},
            id="enum-key",
        ),
        pytest.param(
            lambda v: setattr(v.event, "at", datetime(2027, 1, 1, tzinfo=UTC)),
            lambda v: v.event.at == datetime(2027, 1, 1, tzinfo=UTC),
            id="submodel",
        ),
        pytest.param(
            lambda v: setattr(v.events[0], "id", UUID(int=9)),
            lambda v: v.events[0].id == UUID(int=9),
            id="submodel-in-list",
        ),
    ],
)
def test_change_is_saved(
    engine: sa.Engine,
    expire_on_commit: bool,
    change: Callable[[Values], object],
    saved: Callable[[Values], bool],
) -> None:
    with Session(engine, expire_on_commit=expire_on_commit) as s:
        row = s.get_one(Row, 1)
        change(row.values)
        assert row in s.dirty
        s.commit()
    with Session(engine) as s:
        assert saved(s.get_one(Row, 1).values)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(Values(), id="model"),
        pytest.param(
            {"day": date(2026, 1, 2), "amount": "12345678901234567890.123456789"}, id="dict"
        ),
    ],
)
def test_compare_whole_document(engine: sa.Engine, value: Any) -> None:
    with Session(engine) as s:
        s.add(Row(id=2, values=Values(day=date(2027, 1, 1))))
        s.commit()
        assert s.scalars(sa.select(Row.id).where(Row.values == value)).all() == [1]
        assert s.scalars(sa.select(Row.id).where(Row.values != value)).all() == [2]


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        pytest.param("aware", "2026-01-02T03:04:05.678901+03:00", id="datetime"),
        pytest.param("utc", "2026-01-02T00:00:00Z", id="utc"),
        pytest.param("day", "2026-01-02", id="date"),
        pytest.param("clock", "03:04:05.678901", id="time"),
        pytest.param("duration", "P1DT2.000003S", id="timedelta"),
        pytest.param("uid", "12345678-1234-5678-1234-567812345678", id="uuid"),
        pytest.param("amount", "12345678901234567890.123456789", id="decimal"),
        pytest.param("color", "red", id="str-enum"),
        pytest.param("level", 1, id="int-enum"),
        pytest.param("shape", "circle", id="enum"),
        pytest.param("ids", ["00000000-0000-0000-0000-000000000001"], id="set"),
        pytest.param("by_day", {"2026-01-02": 1}, id="date-key"),
        pytest.param("by_level", {"1": 1}, id="int-enum-key"),
    ],
)
def test_select_from_json(engine: sa.Engine, key: str, expected: Any) -> None:
    """A value selected from inside the JSON is what's stored: a string or a number."""
    with Session(engine) as s:
        assert s.scalar(sa.select(Row.values[key])) == expected


def test_query_by_stored_string(engine: sa.Engine) -> None:
    """A value can be compared with its stored form, e.g. a date with its ISO string."""
    stored = Values().model_dump(mode="json")
    with Session(engine) as s:
        query = sa.select(Row.id).where(Row.values["day"].as_string() == stored["day"])
        assert s.scalars(query).all() == [1]
        query = sa.select(Row.id).where(Row.values["uid"].as_string() == str(UUID(int=3)))
        assert s.scalars(query).all() == []


class Binary(EmbeddedPydanticModel):
    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")

    raw: bytes = b"\x00\xff"


class BinaryRow(Base):
    __tablename__ = "field_types_binary"

    id: Mapped[int] = mapped_column(primary_key=True)
    data: Mapped[Binary] = mapped_column(Binary.column(), default=Binary)


def test_bytes_that_are_not_utf8(make_engine: MakeEngine) -> None:
    """Bytes are stored as UTF-8 text by default; other bytes need base64 (`ser_json_bytes`)."""
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(BinaryRow(id=1))
        s.commit()
        assert s.scalar(sa.select(BinaryRow.data["raw"])) == "AP8="
    with Session(engine) as s:
        row = s.get_one(BinaryRow, 1)
        assert row.data.raw == b"\x00\xff"
        row.data.raw += b"\x01"
        s.commit()
    with Session(engine) as s:
        assert s.get_one(BinaryRow, 1).data.raw == b"\x00\xff\x01"
    with Session(engine) as s:
        s.add(Row(id=1, values=Values(raw=b"\xff")))
        with pytest.raises(sa.exc.StatementError, match="utf-8"):
            s.flush()


class StrictEvent(EmbeddedPydanticModel):
    model_config = ConfigDict(strict=True)

    at: datetime = datetime(2026, 1, 2, tzinfo=UTC)


class StrictValues(EmbeddedPydanticModel):
    model_config = ConfigDict(strict=True)

    at: datetime = datetime(2026, 1, 2, tzinfo=UTC)
    uid: UUID = UUID(int=1)
    amount: Decimal = Decimal("0.10")
    color: Color = Color.RED
    ids: set[UUID] = {UUID(int=1)}
    pair: tuple[int, date] = (1, date(2026, 1, 2))
    by_day: dict[date, int] = {date(2026, 1, 2): 1}
    event: StrictEvent = StrictEvent()


class PartlyStrict(EmbeddedPydanticModel):
    at: Annotated[datetime, Strict()] = datetime(2026, 1, 2, tzinfo=UTC)
    event: StrictEvent = StrictEvent()


class StrictRow(Base):
    __tablename__ = "field_types_strict"

    id: Mapped[int] = mapped_column(primary_key=True)
    strict: Mapped[StrictValues] = mapped_column(StrictValues.column(), default=StrictValues)
    partly: Mapped[PartlyStrict] = mapped_column(PartlyStrict.column(), default=PartlyStrict)


def test_strict_models_load(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    """Strict models load the strings and lists they're stored as (as from JSON text)."""
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=expire_on_commit) as s:
        s.add(StrictRow(id=1))
        s.commit()
        row = s.get_one(StrictRow, 1)
        assert row.strict == StrictValues()
        assert row.partly == PartlyStrict()
        row.strict.ids.add(UUID(int=2))
        row.partly.event.at = datetime(2027, 1, 1, tzinfo=UTC)
        s.commit()
    with Session(engine) as s:
        row = s.get_one(StrictRow, 1)
        assert row.strict.ids == {UUID(int=1), UUID(int=2)}
        assert row.partly.event.at == datetime(2027, 1, 1, tzinfo=UTC)


def test_strict_models_validate_assigned_dicts_strictly(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(StrictRow(id=1))
        s.commit()
        row = s.get_one(StrictRow, 1)
        with pytest.raises(ValidationError, match="valid datetime"):
            row.strict = {"at": "2027-01-01T00:00:00Z"}  # type: ignore[assignment]
        assert row not in s.dirty
