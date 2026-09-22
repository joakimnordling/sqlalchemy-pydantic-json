"""Use with SQLModel: `Field(sa_column=Column(Model.column()))`."""

from collections.abc import Callable

import sqlalchemy as sa
from sqlmodel import Field, Session, SQLModel, col, select

from sqlalchemy_pydantic_json import EmbeddedPydanticModel

MakeEngine = Callable[[sa.MetaData], sa.Engine]


class Address(EmbeddedPydanticModel):
    city: str = "Helsinki"


class Settings(EmbeddedPydanticModel):
    theme: str = "light"
    tags: list[str] = []
    address: Address = Address()


class Hero(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=50)  # MariaDB needs a VARCHAR length
    settings: Settings = Field(
        default_factory=Settings, sa_column=sa.Column(Settings.column(), nullable=False)
    )
    extra: Settings | None = Field(default=None, sa_column=sa.Column(Settings.column()))


def test_sqlmodel_round_trip_and_tracking(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    engine = make_engine(SQLModel.metadata)
    with Session(engine, expire_on_commit=expire_on_commit) as s:
        s.add(Hero(id=1, name="Deadpond", extra=Settings(theme="dark")))
        s.commit()

        hero = s.get_one(Hero, 1)
        assert isinstance(hero.settings, Settings)
        assert isinstance(hero.extra, Settings)
        assert hero.extra.theme == "dark"
        assert hero not in s.dirty

        hero.settings.tags.append("fast")
        hero.settings.address.city = "Espoo"
        assert hero in s.dirty
        s.commit()

    with Session(engine) as s:
        hero = s.get_one(Hero, 1)
        assert hero.settings.tags == ["fast"]
        assert hero.settings.address.city == "Espoo"


def test_sqlmodel_query_into_json(make_engine: MakeEngine) -> None:
    engine = make_engine(SQLModel.metadata)
    with Session(engine) as s:
        s.add_all([Hero(id=1, name="a", settings=Settings(theme="dark")), Hero(id=2, name="b")])
        s.commit()
        query = select(Hero.id).where(col(Hero.settings)["theme"].as_string() == "dark")
        assert s.exec(query).all() == [1]
