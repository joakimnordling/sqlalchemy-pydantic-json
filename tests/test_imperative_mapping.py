"""Imperative mapping (registry.map_imperatively): a plain class mapped to a Table."""

from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, registry

from sqlalchemy_pydantic_json import EmbeddedPydanticModel

MakeEngine = Callable[[sa.MetaData], sa.Engine]


class Address(EmbeddedPydanticModel):
    city: str = "Helsinki"


class Settings(EmbeddedPydanticModel):
    tags: list[str] = []
    address: Address = Address()


mapper_registry = registry()

users = sa.Table(
    "imperative_users",
    mapper_registry.metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("settings", Settings.column(), default=Settings),
    sa.Column("extra", Settings.column()),
)


class User:
    id: int
    settings: Settings
    extra: Settings | None

    def __init__(self, **values: Any) -> None:
        for name, value in values.items():
            setattr(self, name, value)


mapper_registry.map_imperatively(User, users)


def test_tracking(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    engine = make_engine(mapper_registry.metadata)
    with Session(engine, expire_on_commit=expire_on_commit) as s:
        user = User(id=1)
        s.add(user)
        s.commit()
        assert user.settings == Settings()
        assert user.extra is None
        user.settings.tags.append("t")
        user.settings.address.city = "Oulu"
        assert user in s.dirty
        s.commit()
        user.extra = {"tags": ["d"]}  # type: ignore[assignment]  # coerced by the column
        assert isinstance(user.extra, Settings)
        s.commit()
    with Session(engine) as s:
        user = s.get_one(User, 1)
        assert user.settings == Settings(tags=["t"], address=Address(city="Oulu"))
        assert user.extra == Settings(tags=["d"])


def test_reading_should_not_mark_dirty(make_engine: MakeEngine) -> None:
    engine = make_engine(mapper_registry.metadata)
    with Session(engine) as s:
        s.add(User(id=1, settings=Settings(tags=["t"])))
        s.commit()
        user = s.get_one(User, 1)
        assert user.settings.tags == ["t"]
        assert user.settings.address.city == "Helsinki"
        assert user not in s.dirty


def test_querying_inside_the_json(make_engine: MakeEngine) -> None:
    engine = make_engine(mapper_registry.metadata)
    with Session(engine) as s:
        s.add_all([User(id=1, settings=Settings(address=Address(city="Oulu"))), User(id=2)])
        s.commit()
        city = users.c.settings[("address", "city")].as_string()
        assert [u.id for u in s.scalars(sa.select(User).where(city == "Oulu"))] == [1]
