"""Computed and excluded fields: derived from other fields, so they aren't stored."""

import json
from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from pydantic import ConfigDict, Field, computed_field
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel

MakeEngine = Callable[[sa.MetaData], sa.Engine]


class Strict(EmbeddedPydanticModel):
    model_config = ConfigDict(extra="forbid")


class Item(Strict):
    price: int = 1
    quantity: int = 1

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> int:
        return self.price * self.quantity


class Order(Strict):
    items: list[Item] = []
    main: Item = Item()
    note: str = Field(default="", exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def item_count(self) -> int:
        return len(self.items)


class Base(DeclarativeBase):
    pass


class Row(Base):
    __tablename__ = "computed_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    data: Mapped[Order] = mapped_column(Order.column(), default=Order)


class Lenient(EmbeddedPydanticModel):
    price: int = 1

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> int:
        return self.price * 2


class LenientRow(Base):
    __tablename__ = "computed_lenient_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    data: Mapped[Lenient] = mapped_column(Lenient.column())


def stored_json(session: Session) -> Any:
    raw = session.execute(sa.text("select data from computed_rows where id = 1")).scalar()
    return json.loads(raw) if isinstance(raw, str) else raw  # MariaDB returns a string


def test_computed_and_excluded_fields_are_not_stored(
    make_engine: MakeEngine, expire_on_commit: bool
) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=expire_on_commit) as s:
        s.add(Row(id=1, data=Order(items=[Item(price=2, quantity=3)], note="private")))
        s.commit()
        assert stored_json(s) == {
            "items": [{"price": 2, "quantity": 3}],
            "main": {"price": 1, "quantity": 1},
        }

    # extra="forbid" at every level: the stored JSON must load back
    with Session(engine) as s:
        order = s.get_one(Row, 1).data
        assert order.item_count == 1
        assert order.items[0].total == 6
        assert order.note == ""


def test_changing_an_input_updates_the_computed_value(
    make_engine: MakeEngine, expire_on_commit: bool
) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1))
        s.commit()

    with Session(engine, expire_on_commit=expire_on_commit) as s:
        row = s.get_one(Row, 1)
        row.data.items.append(Item(price=5))
        row.data.main.quantity = 4
        assert row in s.dirty
        s.commit()

    with Session(engine) as s:
        order = s.get_one(Row, 1).data
        assert order.item_count == 1
        assert order.items[0].total == 5
        assert order.main.total == 4


def test_computed_values_in_stored_json_are_ignored(make_engine: MakeEngine) -> None:
    """Stored computed values are ignored on load (unless the model forbids extra keys)."""
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.execute(
            sa.text("insert into computed_lenient_rows (id, data) values (1, :data)"),
            {"data": '{"price": 3, "total": 999}'},
        )
        s.commit()
        assert s.get_one(LenientRow, 1).data.total == 6
