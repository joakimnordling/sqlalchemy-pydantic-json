"""The underlying JSON type (`column(json_type=...)`), NULL handling and JSON path queries."""

from collections.abc import Callable
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel

MakeEngine = Callable[[sa.MetaData], sa.Engine]


class Address(EmbeddedPydanticModel):
    city: str = "Helsinki"


class Settings(EmbeddedPydanticModel):
    theme: str = "light"
    level: int = 1
    tags: list[str] = []
    address: Address = Address()


JSONB_ON_POSTGRES = sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")

# json_type -> the column type reflected from the database, per backend
JSON_TYPES: dict[str, tuple[Any, dict[str, str]]] = {
    "default": (sa.JSON, {"sqlite": "JSON", "postgresql": "JSON", "mariadb": "LONGTEXT"}),
    "variant": (
        JSONB_ON_POSTGRES,
        {"sqlite": "JSON", "postgresql": "JSONB", "mariadb": "LONGTEXT"},
    ),
    "jsonb": (JSONB, {"postgresql": "JSONB"}),
}


def make_models(json_type: Any) -> tuple[sa.MetaData, Any]:
    class Base(DeclarativeBase):
        pass

    class User(Base):
        __tablename__ = "json_types"
        id: Mapped[int] = mapped_column(primary_key=True)
        settings: Mapped[Settings] = mapped_column(
            Settings.column(json_type=json_type), default=Settings
        )
        extra: Mapped[Settings | None] = mapped_column(Settings.column(json_type=json_type))

    return Base.metadata, User


@pytest.fixture(params=list(JSON_TYPES))
def setup(request: pytest.FixtureRequest, backend: str, make_engine: MakeEngine) -> Any:
    """`(engine, User, reflected column type)` for each json_type the backend supports."""
    json_type, reflected = JSON_TYPES[request.param]
    if backend not in reflected:
        pytest.skip(f"{request.param} is not supported on {backend}")
    metadata, user = make_models(json_type)
    engine = make_engine(metadata)
    with Session(engine) as s:
        s.add_all(
            [
                user(id=1, settings={"theme": "dark", "level": 3, "address": {"city": "Espoo"}}),
                user(id=2),
            ]
        )
        s.commit()
    return engine, user, reflected[backend]


def test_column_type_in_database(setup: Any) -> None:
    engine, _, expected = setup
    columns = {c["name"]: c["type"] for c in sa.inspect(engine).get_columns("json_types")}
    assert type(columns["settings"]).__name__ == expected


def test_round_trip_and_tracking(setup: Any) -> None:
    engine, user, _ = setup
    with Session(engine) as s:
        u = s.get(user, 1)
        assert u.settings == Settings(theme="dark", level=3, address=Address(city="Espoo"))
        u.settings.tags.append("x")
        u.settings.address.city = "Turku"
        assert u in s.dirty
        s.commit()
    with Session(engine) as s:
        u = s.get(user, 1)
        assert u.settings.tags == ["x"]
        assert u.settings.address.city == "Turku"


def test_none_is_sql_null(setup: Any) -> None:
    engine, user, _ = setup
    with Session(engine) as s:
        assert s.scalars(sa.select(user.id).where(user.extra.is_(None))).all() == [1, 2]
        s.get(user, 1).extra = Settings()
        s.commit()
        assert s.scalars(sa.select(user.id).where(user.extra.is_not(None))).all() == [1]
        s.get(user, 1).extra = None
        s.commit()
        raw = s.execute(sa.text("select extra from json_types where id = 1")).scalar()
        assert raw is None


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        pytest.param(lambda u: u.settings["theme"].as_string() == "dark", [1], id="string"),
        pytest.param(lambda u: u.settings["level"].as_integer() > 2, [1], id="integer"),
        pytest.param(
            lambda u: u.settings[("address", "city")].as_string() == "Espoo", [1], id="path"
        ),
        pytest.param(
            lambda u: u.settings["address"]["city"].as_string() == "Helsinki", [2], id="nested"
        ),
    ],
)
def test_query_into_json(setup: Any, where: Callable[[Any], Any], expected: list[int]) -> None:
    engine, user, _ = setup
    with Session(engine) as s:
        assert s.scalars(sa.select(user.id).where(where(user)).order_by(user.id)).all() == expected
