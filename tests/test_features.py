"""Pydantic features: optional submodels, validate_assignment, frozen models, frozensets."""

from collections.abc import Callable

import pytest
import sqlalchemy as sa
from pydantic import ConfigDict, ValidationError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel

MakeEngine = Callable[[sa.MetaData], sa.Engine]


class Address(EmbeddedPydanticModel):
    city: str = "Helsinki"


class Country(EmbeddedPydanticModel):
    model_config = ConfigDict(frozen=True)
    code: str = "FI"


class Settings(EmbeddedPydanticModel):
    model_config = ConfigDict(validate_assignment=True)
    level: int = 1
    address: Address | None = None
    country: Country = Country()
    codes: frozenset[str] = frozenset()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "feature_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)


def run_steps(
    engine: sa.Engine,
    expire_on_commit: bool,
    steps: list[tuple[Callable[[Settings], object], Callable[[Settings], bool]]],
) -> None:
    """Apply each change in its own commit; check it marked the row dirty and was saved."""
    with Session(engine) as s:
        s.add(User(id=1))
        s.commit()
    for change, saved in steps:
        with Session(engine, expire_on_commit=expire_on_commit) as s:
            user = s.get_one(User, 1)
            change(user.settings)
            assert user in s.dirty
            s.commit()
        with Session(engine) as s:
            user = s.get_one(User, 1)
            assert saved(user.settings)


def test_optional_submodel(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            (
                lambda st: setattr(st, "address", Address(city="Oulu")),
                lambda st: st.address is not None and st.address.city == "Oulu",
            ),
            (
                lambda st: setattr(st.address, "city", "Pori"),
                lambda st: st.address is not None and st.address.city == "Pori",
            ),
            (lambda st: setattr(st, "address", None), lambda st: st.address is None),
            (
                lambda st: setattr(st, "address", {"city": "Kemi"}),
                lambda st: st.address is not None and st.address.city == "Kemi",
            ),
            (
                lambda st: setattr(st.address, "city", "Lahti"),
                lambda st: st.address is not None and st.address.city == "Lahti",
            ),
        ],
    )


def test_validate_assignment(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    run_steps(engine, False, [(lambda st: setattr(st, "level", "5"), lambda st: st.level == 5)])
    with Session(engine) as s:
        user = s.get_one(User, 1)
        with pytest.raises(ValidationError):
            user.settings.level = "not a number"  # type: ignore[assignment]
        assert user not in s.dirty
        assert user.settings.level == 5


def test_frozen_submodel_is_replaced_not_changed(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    run_steps(
        engine,
        False,
        [
            (
                lambda st: setattr(st, "country", Country(code="SE")),
                lambda st: st.country.code == "SE",
            )
        ],
    )
    with Session(engine) as s:
        user = s.get_one(User, 1)
        with pytest.raises(ValidationError, match="frozen"):
            user.settings.country.code = "NO"
        assert user not in s.dirty


def test_frozenset_is_replaced(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            (
                lambda st: setattr(st, "codes", frozenset({"a", "b"})),
                lambda st: st.codes == {"a", "b"},
            ),
            (
                lambda st: setattr(st, "codes", st.codes | {"c"}),
                lambda st: st.codes == {"a", "b", "c"},
            ),
        ],
    )
