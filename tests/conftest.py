import os
from collections.abc import Callable, Iterator

import pytest
import sqlalchemy as sa

# SQLite always runs; the others run when their URL is set (see compose.yaml).
BACKENDS = {
    "sqlite": "sqlite://",
    "postgresql": os.environ.get("TEST_POSTGRES_URL"),
    "mariadb": os.environ.get("TEST_MARIADB_URL"),
}
ENABLED = {name: url for name, url in BACKENDS.items() if url}


@pytest.fixture(params=[True, False], ids=["expire", "no-expire"])
def expire_on_commit(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


@pytest.fixture(params=list(ENABLED))
def backend(request: pytest.FixtureRequest) -> str:
    """Name of the database backend under test."""
    return str(request.param)


@pytest.fixture
def db_url(backend: str) -> str:
    return ENABLED[backend]


@pytest.fixture
def make_engine(db_url: str) -> Iterator[Callable[[sa.MetaData], sa.Engine]]:
    """Return a function creating an engine with the tables of `metadata` created (fresh)."""
    created: list[tuple[sa.Engine, sa.MetaData]] = []

    def make(metadata: sa.MetaData) -> sa.Engine:
        engine = sa.create_engine(db_url)
        metadata.drop_all(engine)
        metadata.create_all(engine)
        created.append((engine, metadata))
        return engine

    yield make
    for engine, metadata in created:
        metadata.drop_all(engine)
        engine.dispose()
