"""Models inside dicts and lists, replacing items, deleting fields, and invalid assignments."""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel

MakeEngine = Callable[[sa.MetaData], sa.Engine]


class Item(EmbeddedPydanticModel):
    name: str = "item"


class Settings(EmbeddedPydanticModel):
    theme: str = "light"
    by_key: dict[str, Item] = {}
    items: list[Item] = []
    groups: dict[str, list[Item]] = {}


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "container_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)


@pytest.fixture
def engine(make_engine: MakeEngine) -> sa.Engine:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(
            User(
                id=1,
                settings=Settings(
                    by_key={"a": Item(name="a")},
                    items=[Item(name="first")],
                    groups={"g": [Item(name="g1")]},
                ),
            )
        )
        s.commit()
    return engine


@pytest.fixture
def session(engine: sa.Engine, expire_on_commit: bool) -> Iterator[Session]:
    with Session(engine, expire_on_commit=expire_on_commit) as s:
        yield s


def get_user(session: Session) -> User:
    return session.get_one(User, 1)


def reload(engine: sa.Engine) -> Settings:
    with Session(engine) as s:
        return get_user(s).settings


def changes(session: Session, change: Callable[[Settings], object]) -> bool:
    """Apply `change` to user 1's settings; return whether the row became dirty, and commit."""
    user = get_user(session)
    assert user not in session.dirty
    change(user.settings)
    dirty = user in session.dirty
    session.commit()
    return dirty


# --- models inside dicts -------------------------------------------------------------------------


def test_model_in_dict_is_tracked(engine: sa.Engine, session: Session) -> None:
    assert changes(session, lambda st: setattr(st.by_key["a"], "name", "changed"))
    assert reload(engine).by_key["a"].name == "changed"


@pytest.mark.parametrize(
    ("add", "key"),
    [
        pytest.param(lambda d, item: d.__setitem__("b", item), "b", id="setitem"),
        pytest.param(lambda d, item: d.setdefault("b", item), "b", id="setdefault"),
        pytest.param(lambda d, item: d.update({"b": item}), "b", id="update"),
        pytest.param(lambda d, item: d.__setitem__("a", item), "a", id="replace"),
    ],
)
def test_model_added_to_dict_is_tracked(
    engine: sa.Engine,
    session: Session,
    add: Callable[[dict[str, Item], Item], object],
    key: str,
) -> None:
    assert changes(session, lambda st: add(st.by_key, Item(name="new")))
    # the added model is tracked in turn (read it back: an expiring commit reloads it)
    assert changes(session, lambda st: setattr(st.by_key[key], "name", "edited"))
    assert reload(engine).by_key[key].name == "edited"


def test_model_popped_from_dict_no_longer_flags_the_row(engine: sa.Engine) -> None:
    with Session(engine, expire_on_commit=False) as s:
        user = get_user(s)
        item = user.settings.by_key.pop("a")
        s.commit()
        item.name = "orphan"
        assert user not in s.dirty
    assert reload(engine).by_key == {}


def test_list_inside_dict_is_tracked(engine: sa.Engine, session: Session) -> None:
    assert changes(session, lambda st: st.groups["g"].append(Item(name="g2")))
    assert changes(session, lambda st: setattr(st.groups["g"][1], "name", "edited"))
    assert [i.name for i in reload(engine).groups["g"]] == ["g1", "edited"]


# --- replacing a list item -----------------------------------------------------------------------


def test_list_item_replaced_by_index_is_tracked(engine: sa.Engine, session: Session) -> None:
    # a model is iterable: SQLAlchemy before 2.0.44 dropped iterable values assigned by index
    assert changes(session, lambda st: st.items.__setitem__(0, Item(name="replaced")))
    assert reload(engine).items[0].name == "replaced"
    assert changes(session, lambda st: setattr(st.items[0], "name", "edited"))
    assert reload(engine).items[0].name == "edited"
    assert changes(session, lambda st: st.groups["g"].__setitem__(0, Item(name="new")))
    assert reload(engine).groups["g"][0].name == "new"


# --- deleting a field ----------------------------------------------------------------------------


def test_deleted_field_is_saved_and_reloads_as_its_default(
    engine: sa.Engine, session: Session
) -> None:
    assert changes(session, lambda st: setattr(st, "theme", "dark"))
    assert changes(session, lambda st: delattr(st, "theme"))
    assert reload(engine).theme == "light"


def test_other_fields_stay_tracked_after_a_field_is_deleted(session: Session) -> None:
    settings = get_user(session).settings
    items = settings.items
    delattr(settings, "items")
    session.commit()
    items.append(Item())  # no longer in the model
    assert get_user(session) not in session.dirty
    assert changes(session, lambda st: setattr(st.by_key["a"], "name", "edited"))


# --- invalid assignments -------------------------------------------------------------------------


@pytest.mark.parametrize("value", [42, "text", ["a", "list"]])
def test_assigning_something_else_raises(session: Session, value: Any) -> None:
    user = get_user(session)
    with pytest.raises(ValueError, match="does not accept objects of type"):
        user.settings = value


def test_assigning_an_invalid_dict_raises(session: Session) -> None:
    user = get_user(session)
    with pytest.raises(ValueError, match="theme"):  # Pydantic's ValidationError
        user.settings = {"theme": ["not", "a", "string"]}  # type: ignore[assignment]
