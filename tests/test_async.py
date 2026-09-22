"""AsyncSession: tracking works the same (SQLite via aiosqlite, PostgreSQL via psycopg)."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel

ASYNC_DRIVERS = {"sqlite": "sqlite+aiosqlite", "postgresql": "postgresql+psycopg"}


class Address(EmbeddedPydanticModel):
    city: str = "Helsinki"


class Settings(EmbeddedPydanticModel):
    tags: list[str] = []
    address: Address = Address()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "async_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)


@pytest.fixture
def async_url(backend: str, db_url: str) -> str:
    if backend not in ASYNC_DRIVERS:
        pytest.skip(f"no async driver installed for {backend}")
    return ASYNC_DRIVERS[backend] + db_url[db_url.index("://") :]


def test_async_session(async_url: str, expire_on_commit: bool) -> None:
    async def run() -> None:
        engine = create_async_engine(async_url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)

            async with AsyncSession(engine, expire_on_commit=expire_on_commit) as s:
                s.add(User(id=1))
                await s.commit()
                user = await s.get_one(User, 1)
                user.settings.tags.append("x")
                user.settings.address.city = "Espoo"
                assert user in s.dirty
                await s.commit()

            async with AsyncSession(engine) as s:
                user = await s.get_one(User, 1)
                assert user.settings == Settings(tags=["x"], address=Address(city="Espoo"))

            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
        finally:
            await engine.dispose()

    asyncio.run(run())
