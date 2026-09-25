"""Dataclass-style mapping (MappedAsDataclass): the column works as a dataclass field."""

import dataclasses
from collections.abc import Callable

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedAsDataclass, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel

MakeEngine = Callable[[sa.MetaData], sa.Engine]


class Address(EmbeddedPydanticModel):
    city: str = "Helsinki"


class Settings(EmbeddedPydanticModel):
    tags: list[str] = []
    address: Address = Address()


class Base(MappedAsDataclass, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "dataclass_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[Settings] = mapped_column(Settings.column(), default_factory=Settings)
    extra: Mapped[Settings | None] = mapped_column(Settings.column(), default=None)


def test_default_factory_gives_each_row_its_own_tracked_model(
    make_engine: MakeEngine, expire_on_commit: bool
) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=expire_on_commit) as s:
        a, b = User(id=1), User(id=2)
        assert a.settings is not b.settings
        s.add_all([a, b])
        s.commit()
        a.settings.tags.append("t")
        a.settings.address.city = "Oulu"
        assert a in s.dirty
        assert b not in s.dirty
        s.commit()
    with Session(engine) as s:
        assert s.get_one(User, 1).settings == Settings(tags=["t"], address=Address(city="Oulu"))
        assert s.get_one(User, 2).settings == Settings()
        assert s.get_one(User, 2).extra is None


def test_constructor_takes_a_model_or_a_dict(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(User(id=1, settings=Settings(tags=["m"]), extra={"tags": ["d"]}))  # type: ignore[arg-type]
        s.commit()
        user = s.get_one(User, 1)
        assert user.settings.tags == ["m"]
        assert isinstance(user.extra, Settings)
        assert user.extra.tags == ["d"]


def test_repr_and_equality() -> None:
    assert repr(User(id=1)) == (
        "User(id=1, settings=Settings(tags=[], address=Address(city='Helsinki')), extra=None)"
    )
    assert User(id=1) == User(id=1)
    assert User(id=1) != User(id=1, settings=Settings(tags=["x"]))


def test_replace_shares_the_model_with_the_original(make_engine: MakeEngine) -> None:
    """Like assigning one model to two rows: a change marks both."""
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=False) as s:
        original = User(id=1)
        s.add(original)
        s.commit()
        copy = dataclasses.replace(original, id=2)
        assert copy.settings is original.settings
        s.add(copy)
        s.commit()
        copy.settings.tags.append("shared")
        assert original in s.dirty
        assert copy in s.dirty


def test_asdict_copies_the_model(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=False) as s:
        user = User(id=1)
        s.add(user)
        s.commit()
        settings = dataclasses.asdict(user)["settings"]
        assert settings == user.settings
        assert settings is not user.settings
        settings.tags.append("copy")
        assert user not in s.dirty
