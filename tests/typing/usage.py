from typing import assert_type

from sqlalchemy.orm import DeclarativeBase, InstrumentedAttribute, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel


class Address(EmbeddedPydanticModel):
    city: str = "Helsinki"
    lines: list[str] = []


class Settings(EmbeddedPydanticModel):
    theme: str = "light"
    tags: set[str] = set()
    address: Address = Address()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)
    extra: Mapped[Settings | None] = mapped_column(Settings.column())


def use(session: Session) -> None:
    user = session.get(User, 1)
    assert user is not None
    assert_type(user.settings, Settings)
    assert_type(user.settings.address.city, str)
    assert_type(user.extra, Settings | None)
    user.settings.tags.add("x")
    user.settings.address.lines.append("y")
    user.settings = Settings(theme="dark")
    user.extra = None
    assert_type(User.settings, InstrumentedAttribute[Settings])  # class-level access


def expected_errors(user: User) -> None:
    user.settings = 123  # should be an error
    user.settings.theme = 1  # should be an error
