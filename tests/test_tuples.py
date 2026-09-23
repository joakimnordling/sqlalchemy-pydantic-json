"""
Tuples: a tuple never changes, but the lists, dicts and models inside it can.

Their changes mark the row as changed, like everywhere else: the items inside a tuple are linked
to whatever holds the tuple.
"""

import copy
import pickle
from collections.abc import Callable
from typing import Any, NamedTuple

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel

MakeEngine = Callable[[sa.MetaData], sa.Engine]


class Item(EmbeddedPydanticModel):
    n: int = 0


class Pair(NamedTuple):
    labels: list[str]
    item: Item


class Settings(EmbeddedPydanticModel):
    pair: tuple[list[int], Item] = ([], Item())
    nested: tuple[tuple[list[int], int], str] = (([], 0), "")
    named: Pair = Pair([], Item())
    in_list: list[tuple[str, list[int]]] = []
    in_dict: dict[str, tuple[Item, dict[str, int]]] = {}
    scalars: tuple[int, str] = (0, "")


class Base(DeclarativeBase):
    pass


class Row(Base):
    __tablename__ = "tuple_rows"
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


def test_changes_inside_tuples(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            (lambda st: st.pair[0].append(1), lambda st: st.pair[0] == [1]),
            (lambda st: setattr(st.pair[1], "n", 1), lambda st: st.pair[1].n == 1),
            (lambda st: st.nested[0][0].append(2), lambda st: st.nested[0][0] == [2]),
            (lambda st: st.named.labels.append("a"), lambda st: st.named.labels == ["a"]),
            (lambda st: setattr(st.named.item, "n", 2), lambda st: st.named.item.n == 2),
        ],
    )


def test_tuples_inside_containers(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    def fill(st: Settings) -> None:
        st.in_list.extend([("a", []), ("b", [])])
        st.in_dict["k"] = (Item(), {})

    def insert_then_change(st: Settings) -> None:
        st.in_list.insert(0, ("new", []))
        st.in_list[2][1].append(2)  # "b", moved from index 1

    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            (fill, lambda st: len(st.in_list) == 2),
            (lambda st: st.in_list[1][1].append(1), lambda st: st.in_list[1] == ("b", [1])),
            (insert_then_change, lambda st: st.in_list[2] == ("b", [1, 2])),
            (lambda st: st.in_list.reverse(), lambda st: st.in_list[0] == ("b", [1, 2])),
            (lambda st: st.in_list[0][1].append(3), lambda st: st.in_list[0] == ("b", [1, 2, 3])),
            (lambda st: st.in_dict["k"][1].update(x=1), lambda st: st.in_dict["k"][1] == {"x": 1}),
            (lambda st: setattr(st.in_dict["k"][0], "n", 1), lambda st: st.in_dict["k"][0].n == 1),
        ],
    )


def test_tuple_types_are_kept(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1))
        s.commit()
        settings = s.get_one(Row, 1).settings
        assert type(settings.named) is Pair
        settings.named = Pair(["x"], Item())
        assert type(settings.named) is Pair
        assert settings.named.labels == ["x"]
        # a tuple with nothing to track inside is kept as is
        scalars = (1, "a")
        settings.scalars = scalars
        assert settings.scalars is scalars


def test_replaced_tuple_should_not_mark_dirty(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=False) as s:
        row = Row(id=1)
        s.add(row)
        s.commit()
        old_list, old_item = row.settings.pair
        row.settings.pair = ([], Item())
        s.commit()
        old_list.append(1)
        old_item.n = 1
        assert row not in s.dirty

        # a tuple removed from a list: nothing, or something else, at its old index
        row.settings.in_list.extend([("a", []), ("b", [])])
        s.commit()
        _, last = row.settings.in_list.pop()
        _, first = row.settings.in_list[0]
        row.settings.in_list[0] = ("c", [])
        s.commit()
        last.append(1)
        first.append(1)
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
        a, b = Row(id=1), Row(id=2)
        s.add_all([a, b])
        s.commit()
        cp = make_copy(a.settings)
        # a copy has its own lists inside tuples, and isn't attached to any row
        cp.pair[0].append(1)
        assert a not in s.dirty
        assert a.settings.pair[0] == []
        # once assigned, the copy's tuples are tracked
        b.settings = cp
        s.commit()
        cp.nested[0][0].append(1)
        assert b in s.dirty
