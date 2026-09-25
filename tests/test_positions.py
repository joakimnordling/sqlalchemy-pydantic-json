"""
Position hints: each link remembers where its parent holds the child.

A change then checks that position instead of searching the parent, so changing every item of a
long list takes linear time. These tests count the searches, and check that moved, removed and
duplicated values are still handled correctly.
"""

import random
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import ConfigDict
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel, _model

MakeEngine = Callable[[sa.MetaData], sa.Engine]


class Item(EmbeddedPydanticModel):
    n: int = 0


class Settings(EmbeddedPydanticModel):
    model_config = ConfigDict(extra="allow")
    items: list[Item] = []
    labels: list[str] = []
    by_key: dict[str, Item] = {}
    first: Item = Item()
    second: Item = Item()


class Base(DeclarativeBase):
    pass


class Row(Base):
    __tablename__ = "position_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)


@pytest.fixture
def searches(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """The parents searched so far (a position hint that was wrong)."""
    searched: list[object] = []
    for link_type in (_model._IndexLink, _model._KeyLink, _model._FieldLink):
        find = link_type._find_and_update_hint

        def counting(link: Any, parent: Any, child: object, find: Any = find) -> bool:
            searched.append(parent)
            return bool(find(link, parent, child))

        monkeypatch.setattr(link_type, "_find_and_update_hint", counting)
    return searched


@pytest.fixture
def session(make_engine: MakeEngine) -> Iterator[Session]:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        items = [Item(n=i) for i in range(20)]
        settings = Settings.model_validate({"items": items, "by_key": {"a": Item()}, "x": {"n": 0}})
        s.add(Row(id=1, settings=settings))
        s.commit()
    with Session(engine, expire_on_commit=False) as s:
        yield s


def change_all(session: Session, row: Row, items: list[Item]) -> None:
    """Change every item, each in its own flush; each must mark the row as changed."""
    for item in items:
        item.n += 100
        assert row in session.dirty
        session.flush()


def test_loaded_values_need_no_search(session: Session, searches: list[object]) -> None:
    row = session.get_one(Row, 1)  # keep it: the session holds unchanged rows weakly
    settings = row.settings
    change_all(session, row, settings.items)
    settings.by_key["a"].n = 1
    settings.first.n = 1
    settings.items.append(Item())
    settings.items[-1].n = 1
    session.flush()
    assert searches == []


LIST_CHANGES: dict[str, Callable[[list[Item]], object]] = {
    "sort": lambda items: items.sort(key=lambda item: -item.n),
    "reverse": lambda items: items.reverse(),
    "slice": lambda items: items.__setitem__(slice(2, 5), [items[4], items[3], items[2]]),
    "extended slice": lambda items: items.__setitem__(slice(None, None, -1), list(items)),
    "shuffle": random.shuffle,
}


@pytest.mark.parametrize("reorder", LIST_CHANGES.values(), ids=LIST_CHANGES.keys())
def test_reordering_keeps_positions(
    session: Session, searches: list[object], reorder: Callable[[list[Item]], object]
) -> None:
    row = session.get_one(Row, 1)
    items = row.settings.items
    reorder(items)
    change_all(session, row, list(items))
    assert searches == []


SHIFTS: dict[str, Callable[[list[Item]], object]] = {
    "insert at front": lambda items: items.insert(0, Item()),
    "pop at front": lambda items: items.pop(0),
    "remove": lambda items: items.remove(items[3]),
    "delete a big slice": lambda items: items.__delitem__(slice(0, 15)),
}


@pytest.mark.parametrize("shift", SHIFTS.values(), ids=SHIFTS.keys())
def test_moved_items_are_searched_once(
    session: Session, searches: list[object], shift: Callable[[list[Item]], object]
) -> None:
    row = session.get_one(Row, 1)
    items = row.settings.items
    shift(items)
    change_all(session, row, list(items))
    assert len(searches) <= len(items)
    searches.clear()
    change_all(session, row, list(items))  # the positions were updated
    assert searches == []


def test_removed_item_is_not_found_at_its_old_position(session: Session) -> None:
    row = session.get_one(Row, 1)
    items = row.settings.items
    popped = items.pop(3)
    items.insert(3, Item())  # another item at the popped item's position
    session.flush()
    popped.n = -1
    assert row not in session.dirty


def test_item_in_a_list_twice(session: Session) -> None:
    row = session.get_one(Row, 1)
    items = row.settings.items
    twice = items[0]
    items.append(twice)  # its position is now the last index
    items.pop()
    session.flush()
    twice.n = -1  # still at index 0
    assert row in session.dirty


def test_item_under_two_keys(session: Session, searches: list[object]) -> None:
    row = session.get_one(Row, 1)
    by_key = row.settings.by_key
    twice = Item()
    by_key["c"] = twice  # after "a", which holds another item
    by_key["b"] = twice  # its position is now "b"
    del by_key["b"]
    session.flush()
    twice.n = -1  # still under "c"
    assert row in session.dirty
    assert searches == [by_key]


def test_value_moved_to_another_key_or_field(session: Session, searches: list[object]) -> None:
    row = session.get_one(Row, 1)
    settings = row.settings
    settings.by_key["b"] = settings.by_key.pop("a")
    item = settings.by_key["b"]
    session.flush()
    item.n = 1
    assert row in session.dirty
    session.flush()

    first = settings.first
    # swap the fields without assignment (which would record the new positions)
    vars(settings)["first"], vars(settings)["second"] = settings.second, first
    searches.clear()
    first.n = 1
    assert row in session.dirty
    assert searches == [settings]


def test_extra_value(session: Session, searches: list[object]) -> None:
    row = session.get_one(Row, 1)
    extra = row.settings.model_extra
    assert extra is not None
    extra["x"]["n"] = 1  # extra values aren't validated: a dict
    assert row in session.dirty
    assert searches == []


def test_sorting_plain_values(session: Session) -> None:
    row = session.get_one(Row, 1)
    row.settings.labels.extend(["b", "a"])
    session.flush()
    row.settings.labels.sort()
    assert row in session.dirty
