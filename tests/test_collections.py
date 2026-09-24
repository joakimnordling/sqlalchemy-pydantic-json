"""Other collection types: defaultdict keeps its default factory, and its values are tracked."""

import copy
import pickle
from collections import defaultdict
from collections.abc import Callable
from typing import Annotated, Any

import pytest
import sqlalchemy as sa
from pydantic import Field
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel

MakeEngine = Callable[[sa.MetaData], sa.Engine]


class Item(EmbeddedPydanticModel):
    n: int = 0


class Settings(EmbeddedPydanticModel):
    lists: defaultdict[str, list[int]] = defaultdict(list)
    # Pydantic infers a default factory only for built-in types (list here); a model needs its own
    items: defaultdict[str, Annotated[Item, Field(default_factory=Item)]] = defaultdict(Item)


class Base(DeclarativeBase):
    pass


class Row(Base):
    __tablename__ = "collection_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)


Step = tuple[Callable[[Settings], object], Callable[[Settings], bool]]


def run_steps(engine: sa.Engine, expire_on_commit: bool, steps: list[Step]) -> None:
    """Apply each change in its own commit; check it marked the row dirty and was saved."""
    with Session(engine) as s:
        s.add(Row(id=1))
        s.commit()
    for change, saved in steps:
        with Session(engine, expire_on_commit=expire_on_commit) as s:
            row = s.get_one(Row, 1)
            change(row.settings)
            assert row in s.dirty
            s.commit()
        with Session(engine) as s:
            assert saved(s.get_one(Row, 1).settings)


def test_defaultdict(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            # a missing key inserts its default: the row changes, and the default is tracked
            (lambda st: st.lists["a"], lambda st: st.lists == {"a": []}),
            (lambda st: st.lists["a"].append(1), lambda st: st.lists == {"a": [1]}),
            (lambda st: st.lists["b"].append(2), lambda st: st.lists == {"a": [1], "b": [2]}),
            (lambda st: setattr(st.items["x"], "n", 1), lambda st: st.items["x"].n == 1),
            (lambda st: setattr(st.items["x"], "n", 2), lambda st: st.items["x"].n == 2),
            (
                lambda st: setattr(st, "lists", defaultdict(list, {"c": [3]})),
                lambda st: st.lists == {"c": [3]},
            ),
        ],
    )


def test_defaultdict_keeps_its_default_factory(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1))
        s.commit()
        settings = s.get_one(Row, 1).settings
        assert isinstance(settings.lists, defaultdict)
        assert settings.lists.default_factory is list
        settings.lists = defaultdict(list)
        assert isinstance(settings.lists, defaultdict)
        assert settings.lists.default_factory is list


def test_defaultdict_without_a_default_factory(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=False) as s:  # (a reload would validate a new one)
        row = Row(id=1)
        s.add(row)
        s.commit()
        row.settings.lists = defaultdict(None)  # no validate_assignment: kept as is
        s.commit()
        with pytest.raises(KeyError, match="missing"):
            row.settings.lists["missing"]
        assert row not in s.dirty


def test_reading_should_not_mark_dirty(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1, settings=Settings(lists=defaultdict(list, {"a": [1]}))))
        s.commit()
        row = s.get_one(Row, 1)
        assert row.settings.lists["a"] == [1]  # an existing key
        assert row.settings.lists.get("missing") is None  # get() doesn't insert
        assert "missing" not in row.settings.lists
        assert row not in s.dirty


COPIES: list[Any] = [
    pytest.param(copy.copy, id="copy.copy"),
    pytest.param(copy.deepcopy, id="copy.deepcopy"),
    pytest.param(lambda m: pickle.loads(pickle.dumps(m)), id="pickle"),
]


@pytest.mark.parametrize("make_copy", COPIES)
def test_copies(make_engine: MakeEngine, make_copy: Callable[[Settings], Settings]) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=False) as s:
        a, b = Row(id=1, settings=Settings(items=defaultdict(Item, {"x": Item()}))), Row(id=2)
        s.add_all([a, b])
        s.commit()
        cp = make_copy(a.settings)
        assert cp.lists.default_factory is list
        cp.lists["new"].append(1)  # the copy's own dict
        assert a not in s.dirty
        b.settings = cp
        s.commit()
        cp.items["x"].n = 1  # a model already in the dict
        assert b in s.dirty
        s.commit()
        cp.lists["new"].append(2)
        assert b in s.dirty
