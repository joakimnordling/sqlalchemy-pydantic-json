"""
Each kind of in-place change marks the row dirty, issues an UPDATE and is persisted.

Every case runs in a session that has already committed once, both with and without
`expire_on_commit`. The `plain-sub` variant uses a plain `BaseModel` submodel, which is a known,
documented limit: changes inside it are not tracked.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from sqlalchemy_pydantic_json import EmbeddedPydanticModel
from tests.helpers import capture_updates


@dataclass
class Models:
    base: type[DeclarativeBase]
    user: Any
    address: Any


def make_models(sub_base: type[BaseModel]) -> Models:
    class Address(sub_base):  # type: ignore[misc, valid-type]
        city: str = "Helsinki"
        lines: list[str] = []

    class Settings(EmbeddedPydanticModel):
        theme: str = "light"
        tags: list[str] = []
        meta: dict[str, int] = {}
        address: Address = Address()
        history: list[Address] = []

    class Base(DeclarativeBase):
        pass

    class User(Base):
        __tablename__ = "u"
        id: Mapped[int] = mapped_column(primary_key=True)
        settings: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)
        opt: Mapped[Settings | None] = mapped_column(Settings.column(), nullable=True)

    return Models(Base, User, Address)


MODELS = {"embedded-sub": make_models(EmbeddedPydanticModel), "plain-sub": make_models(BaseModel)}


@dataclass
class Case:
    mutate: Callable[[Any, Models], object]
    persisted: Callable[[Any], bool]
    setup: Callable[[Any, Models], object] | None = None  # applied and committed first
    changes: bool = True
    # False when the change happens inside a plain-BaseModel submodel (untracked)
    tracked_with_plain_sub: bool = True


def _append_and_edit(st: Any, m: Models) -> None:
    st.history.append(m.address())
    st.history[-1].city = "Vaasa"


CASES = {
    "top-level attr": Case(
        lambda st, m: setattr(st, "theme", "dark"),
        lambda st: st.theme == "dark",
    ),
    "list append": Case(
        lambda st, m: st.tags.append("a"),
        lambda st: st.tags == ["a"],
    ),
    "list second append": Case(
        lambda st, m: st.tags.append("b"),
        lambda st: st.tags == ["a", "b"],
        setup=lambda st, m: st.tags.append("a"),
    ),
    "dict set": Case(
        lambda st, m: st.meta.__setitem__("x", 1),
        lambda st: st.meta == {"x": 1},
    ),
    "submodel attr": Case(
        lambda st, m: setattr(st.address, "city", "Turku"),
        lambda st: st.address.city == "Turku",
        tracked_with_plain_sub=False,
    ),
    "submodel list": Case(
        lambda st, m: st.address.lines.append("L1"),
        lambda st: st.address.lines == ["L1"],
        tracked_with_plain_sub=False,
    ),
    "list-of-models item": Case(
        lambda st, m: setattr(st.history[0], "city", "Oulu"),
        lambda st: st.history[0].city == "Oulu",
        tracked_with_plain_sub=False,
    ),
    # the append alone marks the row dirty, so this works even with plain submodels
    "appended model, then edit": Case(
        _append_and_edit,
        lambda st: st.history[-1].city == "Vaasa",
    ),
    "no change": Case(
        lambda st, m: None,
        lambda st: True,
        changes=False,
    ),
}


@pytest.fixture(params=list(MODELS))
def models(request: pytest.FixtureRequest) -> Models:
    return MODELS[request.param]


@pytest.fixture
def engine(models: Models, make_engine: Callable[[sa.MetaData], Engine]) -> Engine:
    engine = make_engine(models.base.metadata)
    with sessionmaker(engine)() as s:
        s.add(models.user(id=1, settings={"history": [{"city": "Espoo"}]}))
        s.commit()
    return engine


@pytest.mark.parametrize("case", CASES.values(), ids=list(CASES))
def test_change(engine: Engine, models: Models, expire_on_commit: bool, case: Case) -> None:
    make_session = sessionmaker(engine, expire_on_commit=expire_on_commit)
    tracked = case.changes and (
        case.tracked_with_plain_sub or issubclass(models.address, EmbeddedPydanticModel)
    )

    with make_session() as s:  # one long-lived session, with commits in between
        user = s.get(models.user, 1)
        if case.setup:
            case.setup(user.settings, models)
        s.commit()

        case.mutate(user.settings, models)
        assert (user in s.dirty) is tracked
        with capture_updates(engine) as updates:
            s.commit()
        assert bool(updates) is tracked

    if case.changes:  # an untracked change is lost
        with make_session() as s:
            assert case.persisted(s.get(models.user, 1).settings) is tracked
