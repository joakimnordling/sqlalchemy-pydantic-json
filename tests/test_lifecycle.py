"""The session lifecycle: merge, refresh, expire, expunge, make_transient, and memory."""

import gc
import pickle
import weakref
from collections.abc import Callable, Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, make_transient, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel
from tests.helpers import capture_updates

MakeEngine = Callable[[sa.MetaData], sa.Engine]


class Item(EmbeddedPydanticModel):
    n: int = 0
    tags: list[str] = []


class Settings(EmbeddedPydanticModel):
    items: list[Item] = []
    by_key: dict[str, Item] = {}
    item: Item = Item()


class Base(DeclarativeBase):
    pass


class Row(Base):
    __tablename__ = "lifecycle_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)


@pytest.fixture
def engine(make_engine: MakeEngine) -> sa.Engine:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1, settings=Settings(items=[Item()], by_key={"a": Item()})))
        s.commit()
    return engine


def load(engine: sa.Engine, row_id: int = 1) -> Settings:
    with Session(engine) as s:
        return s.get_one(Row, row_id).settings


def detached_row(engine: sa.Engine) -> Row:
    with Session(engine, expire_on_commit=False) as s:
        return s.get_one(Row, 1)


# --- merge ---------------------------------------------------------------------------------------


def test_merge_of_a_row_changed_while_detached(engine: sa.Engine) -> None:
    row = detached_row(engine)
    row.settings.items[0].tags.append("detached")
    with Session(engine) as s:
        merged = s.merge(row)
        assert merged in s.dirty
        s.commit()
        # the merged row is tracked, as any loaded one
        merged.settings.item.n = 1
        assert merged in s.dirty
        s.commit()
    settings = load(engine)
    assert settings.items[0].tags == ["detached"]
    assert settings.item.n == 1


@pytest.mark.parametrize("changed_before_merge", [False, True])
def test_merge_of_a_pickled_row(engine: sa.Engine, changed_before_merge: bool) -> None:
    """As with a cache (e.g. dogpile.cache): a row pickled, loaded back, and merged."""
    row = pickle.loads(pickle.dumps(detached_row(engine)))
    if changed_before_merge:
        row.settings.by_key["a"].n = 1
    with Session(engine) as s:
        # merge() assigns every attribute, so the row is always in session.dirty; the commit
        # compares the values, and writes only a changed one
        merged = s.merge(row)
        with capture_updates(engine) as updates:
            s.commit()
        assert len(updates) == (1 if changed_before_merge else 0)
        merged.settings.by_key["a"].tags.append("merged")
        assert merged in s.dirty
        s.commit()
    settings = load(engine)
    assert settings.by_key["a"].n == (1 if changed_before_merge else 0)
    assert settings.by_key["a"].tags == ["merged"]


# --- refresh and expire --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reload",
    [
        pytest.param(lambda s, row: s.refresh(row), id="refresh"),
        pytest.param(lambda s, row: s.expire(row), id="expire"),
        pytest.param(lambda s, row: s.expire(row, ["settings"]), id="expire-attribute"),
    ],
)
def test_reloaded_value_is_tracked_and_the_old_one_is_not(
    engine: sa.Engine, reload: Callable[[Session, Row], None]
) -> None:
    with Session(engine) as s:
        row = s.get_one(Row, 1)
        old = row.settings
        reload(s, row)
        assert row.settings is not old
        old.items[0].tags.append("old")
        assert row not in s.dirty
        row.settings.items[0].tags.append("new")
        assert row in s.dirty
        s.commit()
    assert load(engine).items[0].tags == ["new"]


# --- expunge and make_transient ------------------------------------------------------------------


def test_changes_while_expunged_are_saved_when_added_back(engine: sa.Engine) -> None:
    with Session(engine) as s:
        row = s.get_one(Row, 1)
        s.expunge(row)
        row.settings.item.n = 1
        s.add(row)
        assert row in s.dirty
        s.commit()
    assert load(engine).item.n == 1


def test_make_transient_saves_the_value_with_the_new_row(engine: sa.Engine) -> None:
    with Session(engine) as s:
        row = s.get_one(Row, 1)
        s.expunge(row)
        make_transient(row)
        row.id = 2
        row.settings.item.n = 2
        s.add(row)
        s.commit()
        row.settings.item.n = 3  # tracked on the new row
        assert row in s.dirty
        s.commit()
    assert load(engine, 2).item.n == 3
    assert load(engine, 1).item.n == 0


# --- memory --------------------------------------------------------------------------------------


@pytest.fixture
def gc_disabled() -> Iterator[None]:
    """Only reference counting frees objects: anything in a reference cycle stays alive."""
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


@pytest.mark.usefixtures("gc_disabled")
def test_rows_and_their_values_are_freed_without_the_garbage_collector(
    engine: sa.Engine, expire_on_commit: bool
) -> None:
    refs: list[weakref.ref[object]] = []
    with Session(engine, expire_on_commit=expire_on_commit) as s:
        s.add_all(
            Row(id=i, settings=Settings(items=[Item()], by_key={"a": Item()})) for i in (2, 3)
        )
        s.commit()
        rows = s.scalars(sa.select(Row)).all()
        for row in rows:
            row.settings.items[0].tags.append("changed")  # links and flags everything
            row.settings.by_key["a"].n = 1
            settings = row.settings
            refs += [weakref.ref(x) for x in (row, settings, settings.items, settings.items[0])]
            refs += [weakref.ref(x) for x in (settings.by_key, settings.by_key["a"])]
        s.commit()
        del rows, row, settings
    assert [ref() for ref in refs if ref() is not None] == []
