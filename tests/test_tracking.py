"""Change tracking: sets, shared and moved models, copies, stale values, coercion."""

import copy
import dataclasses
import pickle
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, PrivateAttr
from pydantic.dataclasses import dataclass as pydantic_dataclass
from sqlalchemy import Engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel, PydanticJSON
from sqlalchemy_pydantic_json._model import _TrackedSet


class Address(EmbeddedPydanticModel):
    city: str = "Helsinki"
    lines: list[str] = []


class Settings(EmbeddedPydanticModel):
    theme: str = "light"
    tags: set[str] = set()
    labels: list[str] = []
    address: Address = Address()
    history: list[Address] = []


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)
    extra: Mapped[Settings | None] = mapped_column(Settings.column())


Fresh = Callable[[bool], tuple[Session, User, User]]
MakeEngine = Callable[[sa.MetaData], Engine]


@pytest.fixture
def engine(make_engine: Callable[[sa.MetaData], Engine]) -> Engine:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add_all([User(id=1, settings={"tags": ["x"]}), User(id=2)])
        s.commit()
    return engine


@pytest.fixture
def fresh(engine: Engine) -> Iterator[Fresh]:
    """Open a new session and load users 1 and 2 as `(session, a, b)`."""
    sessions: list[Session] = []

    def open_(expire: bool) -> tuple[Session, User, User]:
        s = Session(engine, expire_on_commit=expire)
        sessions.append(s)
        a, b = s.get_one(User, 1), s.get_one(User, 2)
        return s, a, b

    yield open_
    for s in sessions:
        s.close()


def load(engine: Engine, user_id: int) -> User:
    with Session(engine) as s:
        user = s.get_one(User, user_id)
        return user


# --- column declaration --------------------------------------------------------------------------


def test_column_types() -> None:
    columns = User.__table__.c
    assert isinstance(columns.settings.type, PydanticJSON)
    assert isinstance(columns.extra.type, PydanticJSON)
    assert columns.settings.nullable is False
    assert columns.extra.nullable is True


# --- equality ------------------------------------------------------------------------------------


def test_equality_ignores_tracking_state(engine: Engine, fresh: Fresh) -> None:
    assert Settings() == Settings()
    assert Settings(theme="dark") != Settings()
    _, a, b = fresh(False)
    # loaded, linked to rows and parents, vs. freshly built: equal when the values are
    assert a.settings == Settings(tags={"x"})
    assert b.settings == Settings()
    assert a.settings.address == b.settings.address
    assert a.settings != b.settings


# --- sets ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        pytest.param(lambda t: t.add("y"), {"x", "y"}, id="add"),
        pytest.param(lambda t: t.discard("x"), set(), id="discard"),
        pytest.param(lambda t: t.__ior__({"z"}), {"x", "z"}, id="ior"),
        pytest.param(lambda t: t.update({"q"}), {"x", "q"}, id="update"),
        pytest.param(lambda t: t.clear(), set(), id="clear"),
        pytest.param(lambda t: t.pop(), set(), id="pop"),
    ],
)
def test_set_mutation(
    engine: Engine,
    fresh: Fresh,
    expire_on_commit: bool,
    mutate: Callable[[set[str]], Any],
    expected: set[str],
) -> None:
    s, a, _ = fresh(expire_on_commit)
    mutate(a.settings.tags)
    assert a in s.dirty
    s.commit()
    assert load(engine, 1).settings.tags == expected


def test_set_field_is_tracked_after_load(engine: Engine) -> None:
    assert type(load(engine, 1).settings.tags) is _TrackedSet


# --- shared submodels ----------------------------------------------------------------------------


def test_shared_submodel_flags_every_parent(engine: Engine, fresh: Fresh) -> None:
    # no expiry, so both rows keep holding `shared` across commits
    s, a, b = fresh(False)
    shared = Address(city="Shared")
    a.settings.address = shared
    b.settings.history.append(shared)
    s.commit()

    shared.city = "Changed"
    assert a in s.dirty
    assert b in s.dirty
    s.commit()
    assert load(engine, 1).settings.address.city == "Changed"
    assert load(engine, 2).settings.history[-1].city == "Changed"

    # once removed from B, it no longer flags B
    b.settings.history.pop()
    s.commit()
    shared.city = "Again"
    assert a in s.dirty
    assert b not in s.dirty


def test_same_root_on_two_rows_flags_both(
    engine: Engine, fresh: Fresh, expire_on_commit: bool
) -> None:
    s, a, b = fresh(expire_on_commit)
    b.settings = a.settings
    a.settings.theme = "both"
    assert a in s.dirty
    assert b in s.dirty
    s.commit()
    assert load(engine, 1).settings.theme == "both"
    assert load(engine, 2).settings.theme == "both"


# --- lists of models ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "add",
    [
        pytest.param(lambda lst, item: lst.append(item), id="append"),
        pytest.param(lambda lst, item: lst.extend([item]), id="extend"),
        pytest.param(lambda lst, item: lst.__iadd__([item]), id="iadd"),
        pytest.param(lambda lst, item: lst.insert(0, item), id="insert"),
        pytest.param(lambda lst, item: lst.__setitem__(slice(0, 0), [item]), id="slice"),
    ],
)
def test_model_added_to_list_is_tracked(
    engine: Engine,
    fresh: Fresh,
    expire_on_commit: bool,
    add: Callable[[list[Address], Address], object],
) -> None:
    s, a, _ = fresh(expire_on_commit)
    item = Address()
    add(a.settings.history, item)
    s.commit()

    item = a.settings.history[0]  # the same instance, unless the commit expired it
    assert a not in s.dirty
    item.city = "Tampere"
    assert a in s.dirty
    s.commit()
    assert load(engine, 1).settings.history[0].city == "Tampere"


# --- moving a model between rows -----------------------------------------------------------------


def test_move_between_rows(fresh: Fresh, expire_on_commit: bool) -> None:
    _, a, b = fresh(expire_on_commit)
    old_a = a.settings
    b.settings = a.settings
    a.settings = Settings(theme="new-a")
    assert b.settings is old_a
    assert a.settings is not old_a


def test_moved_model_flags_only_new_row(engine: Engine, fresh: Fresh) -> None:
    # committed first (without expiry) so that A starts out clean
    s, a, b = fresh(False)
    old_a = a.settings
    b.settings = a.settings
    a.settings = Settings(theme="new-a")
    s.commit()

    old_a.theme = "moved"
    assert b in s.dirty
    assert a not in s.dirty
    a.settings.labels.append("x")
    assert a in s.dirty
    s.commit()

    stored_a, stored_b = load(engine, 1), load(engine, 2)
    assert stored_a.settings.theme == "new-a"
    assert stored_a.settings.labels == ["x"]
    assert stored_b.settings.theme == "moved"


# --- copies --------------------------------------------------------------------------------------

COPIES: list[Any] = [
    pytest.param(lambda m: m.model_copy(), False, id="model_copy"),
    pytest.param(lambda m: m.model_copy(deep=True), True, id="model_copy-deep"),
    pytest.param(copy.copy, False, id="copy.copy"),
    pytest.param(copy.deepcopy, True, id="copy.deepcopy"),
    pytest.param(lambda m: pickle.loads(pickle.dumps(m)), True, id="pickle"),
]


@pytest.mark.parametrize(("make_copy", "deep"), COPIES)
def test_copy_is_detached(
    fresh: Fresh,
    expire_on_commit: bool,
    make_copy: Callable[[Settings], Settings],
    deep: bool,
) -> None:
    s, a, _ = fresh(expire_on_commit)
    cp = make_copy(a.settings)

    cp.labels.append("zz")
    cp.theme = "copy"
    cp.tags.add("c")
    assert a not in s.dirty
    assert a.settings.theme != "copy"

    # as in Pydantic, a shallow copy shares its submodels with the original
    cp.address.city = "Copy"
    assert (a in s.dirty) is (not deep)


@pytest.mark.parametrize(("make_copy", "deep"), COPIES)
def test_copy_assigned_to_other_row_tracks_models_in_its_lists(
    fresh: Fresh, make_copy: Callable[[Settings], Settings], deep: bool
) -> None:
    s, a, b = fresh(False)
    a.settings.history.append(Address())
    s.commit()
    cp = make_copy(a.settings)
    b.settings = cp
    s.commit()
    cp.history[0].city = "viaCopy"
    assert b in s.dirty
    assert (a in s.dirty) is (not deep)  # a shallow copy shares the Address


def test_deep_copy_assigned_to_other_row_tracks_that_row(fresh: Fresh) -> None:
    s, a, b = fresh(False)
    cp = a.settings.model_copy(deep=True)
    b.settings = cp
    s.commit()
    cp.address.city = "viaCopy"
    assert b in s.dirty
    assert a not in s.dirty


# --- values kept across commits ------------------------------------------------------------------


def test_stale_value_after_expiring_commit_is_ignored(fresh: Fresh) -> None:
    s, a, _ = fresh(True)
    stale = a.settings
    s.commit()
    stale.theme = "stale"
    stale.labels.append("s")
    assert a not in s.dirty
    assert a.settings is not stale
    assert a.settings.theme != "stale"


def test_kept_value_without_expiry_is_tracked(fresh: Fresh) -> None:
    s, a, _ = fresh(False)
    kept = a.settings
    s.commit()
    kept.theme = "kept"
    assert a in s.dirty


# --- coercion and None ---------------------------------------------------------------------------


def test_dict_is_coerced_and_none_stored_as_null(fresh: Fresh, expire_on_commit: bool) -> None:
    s, a, _ = fresh(expire_on_commit)
    a.extra = {"theme": "d"}  # type: ignore[assignment]
    assert isinstance(a.extra, Settings)
    s.commit()

    assert a.extra is not None
    a.extra.tags.add("t")
    assert a in s.dirty

    a.extra = None
    s.commit()
    assert s.execute(text("select extra from users where id = 1")).scalar() is None


# --- plain BaseModel submodels (documented: work apart from in-place changes) --------------------


class PlainAddress(BaseModel):
    city: str = "Helsinki"


class FrozenCountry(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: str = "FI"


class FrozenPlainLines(BaseModel):
    model_config = ConfigDict(frozen=True)
    lines: list[str] = []


class FrozenLines(EmbeddedPydanticModel):
    model_config = ConfigDict(frozen=True)
    lines: list[str] = []


@dataclasses.dataclass
class Point:
    x: int = 0


@pydantic_dataclass
class PydanticPoint:
    x: int = 0


class MixedSettings(EmbeddedPydanticModel):
    address: PlainAddress = PlainAddress()
    history: list[PlainAddress] = []
    country: FrozenCountry = FrozenCountry()
    frozen_plain: FrozenPlainLines = FrozenPlainLines()
    frozen: FrozenLines = FrozenLines()
    point: Point = Point()
    pydantic_point: PydanticPoint = PydanticPoint()


class MixedBase(DeclarativeBase):
    pass


class MixedUser(MixedBase):
    __tablename__ = "mixed_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[MixedSettings] = mapped_column(MixedSettings.column(), default=MixedSettings)


def test_plain_submodels_work_apart_from_in_place_changes(make_engine: MakeEngine) -> None:
    engine = make_engine(MixedBase.metadata)
    with Session(engine, expire_on_commit=False) as s:
        s.add(MixedUser(id=1))
        s.commit()
        user = s.get_one(MixedUser, 1)

        # replacing or adding a plain model is tracked
        user.settings.address = PlainAddress(city="Espoo")
        assert user in s.dirty
        s.commit()
        user.settings.history.append(PlainAddress(city="Vaasa"))
        assert user in s.dirty
        s.commit()
        user.settings.country = FrozenCountry(code="SE")
        assert user in s.dirty
        s.commit()

        # a change inside a plain model is not
        user.settings.address.city = "Turku"
        assert user not in s.dirty
        s.commit()

    with Session(engine) as s:
        user = s.get_one(MixedUser, 1)
        assert user.settings == MixedSettings(
            address=PlainAddress(city="Espoo"),
            history=[PlainAddress(city="Vaasa")],
            country=FrozenCountry(code="SE"),
        )


def test_dataclasses_and_frozen_models(make_engine: MakeEngine) -> None:
    """Known limits (README, rules and gotchas), and what a frozen EmbeddedPydanticModel keeps."""
    engine = make_engine(MixedBase.metadata)
    with Session(engine, expire_on_commit=False) as s:
        user = MixedUser(id=1)
        s.add(user)
        s.commit()

        # a frozen model can't be assigned to, but a list in it can still change: tracked in an
        # EmbeddedPydanticModel, not in a plain one
        user.settings.frozen.lines.append("tracked")
        assert user in s.dirty
        s.commit()
        user.settings.frozen_plain.lines.append("lost")
        assert user not in s.dirty

        # a change inside a dataclass isn't tracked; replacing it is
        user.settings.point.x = 1
        user.settings.pydantic_point.x = 1
        assert user not in s.dirty
        user.settings.point = Point(x=2)
        assert user in s.dirty
        s.commit()

    with Session(engine) as s:
        settings = s.get_one(MixedUser, 1).settings
        assert settings.frozen.lines == ["tracked"]
        assert settings.point == Point(x=2)


# --- a model_post_init of the user's own ----------------------------------------------------------


class PostInitWithoutSuper(EmbeddedPydanticModel):
    tags: list[str] = []
    ran: bool = False

    def model_post_init(self, context: Any, /) -> None:
        self.ran = True  # no super().model_post_init(context)


class PostInitWithSuper(EmbeddedPydanticModel):
    tags: list[str] = []
    _note: str = PrivateAttr(default="")

    def model_post_init(self, context: Any, /) -> None:
        super().model_post_init(context)
        self._note = "ran"


class PostInitInherited(PostInitWithoutSuper):
    pass


class PostInitBase(DeclarativeBase):
    pass


class PostInitUser(PostInitBase):
    __tablename__ = "post_init_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    without_super: Mapped[PostInitWithoutSuper] = mapped_column(
        PostInitWithoutSuper.column(), default=PostInitWithoutSuper
    )
    with_super: Mapped[PostInitWithSuper] = mapped_column(
        PostInitWithSuper.column(), default=PostInitWithSuper
    )
    inherited: Mapped[PostInitInherited] = mapped_column(
        PostInitInherited.column(), default=PostInitInherited
    )


def test_own_model_post_init_keeps_tracking(
    make_engine: MakeEngine, expire_on_commit: bool
) -> None:
    engine = make_engine(PostInitBase.metadata)
    with Session(engine, expire_on_commit=expire_on_commit) as s:
        s.add(PostInitUser(id=1))
        s.commit()
        user = s.get_one(PostInitUser, 1)
        # the user's own code ran
        assert user.without_super.ran
        assert user.with_super._note == "ran"
        assert user.inherited.ran

        for column in ("without_super", "with_super", "inherited"):
            s.commit()
            assert user not in s.dirty
            getattr(user, column).tags.append("x")  # read after the commit, as it may expire
            assert user in s.dirty
        s.commit()

    with Session(engine) as s:
        user = s.get_one(PostInitUser, 1)
        assert user.without_super.tags == user.with_super.tags == user.inherited.tags == ["x"]


class PostInitChildOfOwn(PostInitWithoutSuper):
    extra: int = 0


class PlainChild(Settings):
    pass


@pytest.mark.parametrize(
    "model",
    [
        Settings,
        PlainChild,
        PostInitWithoutSuper,
        PostInitWithSuper,
        PostInitInherited,
        PostInitChildOfOwn,
    ],
)
def test_fields_are_linked_once_per_instance(
    monkeypatch: pytest.MonkeyPatch, model: type[EmbeddedPydanticModel]
) -> None:
    calls: list[EmbeddedPydanticModel] = []
    link_fields = EmbeddedPydanticModel._link_fields

    def counting(self: EmbeddedPydanticModel) -> None:
        calls.append(self)
        link_fields(self)

    monkeypatch.setattr(EmbeddedPydanticModel, "_link_fields", counting)
    instance = model()
    # (PostInitWithSuper also calls super(), so it links twice, harmlessly)
    assert len([c for c in calls if c is instance]) == (2 if model is PostInitWithSuper else 1)
