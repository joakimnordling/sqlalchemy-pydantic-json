"""Column defaults: the forms the README recommends, and the one it warns about."""

from collections.abc import Callable

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel

MakeEngine = Callable[[sa.MetaData], sa.Engine]


class Settings(EmbeddedPydanticModel):
    theme: str = "light"
    tags: list[str] = []


SHARED = Settings(theme="shared")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "defaults"
    id: Mapped[int] = mapped_column(primary_key=True)
    from_class: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)
    from_dict: Mapped[Settings] = mapped_column(Settings.column(), default={"theme": "dark"})
    from_server: Mapped[Settings] = mapped_column(Settings.column(), server_default=sa.text("'{}'"))
    from_instance: Mapped[Settings] = mapped_column(Settings.column(), default=SHARED)


def add_two(engine: sa.Engine, session: Session) -> tuple[User, User]:
    a, b = User(id=1), User(id=2)
    session.add_all([a, b])
    session.commit()
    return a, b


def test_recommended_defaults(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=expire_on_commit) as s:
        a, b = add_two(engine, s)
        assert a.from_class == Settings()
        assert a.from_dict == Settings(theme="dark")
        assert a.from_server == Settings()
        # each row gets its own model, and it's tracked
        assert a.from_class is not b.from_class
        assert a.from_dict is not b.from_dict
        a.from_class.tags.append("x")
        a.from_dict.tags.append("y")
        assert a in s.dirty
        assert b not in s.dirty
        s.commit()
    with Session(engine) as s:
        a, b = s.get_one(User, 1), s.get_one(User, 2)
        assert (a.from_class.tags, a.from_dict.tags) == (["x"], ["y"])
        assert (b.from_class.tags, b.from_dict.tags) == ([], [])


def test_instance_default_is_shared_between_rows(make_engine: MakeEngine) -> None:
    # documented trap: the same object becomes every new row's value
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=False) as s:
        a, b = add_two(engine, s)
        assert a.from_instance is b.from_instance is SHARED
