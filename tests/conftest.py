import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.pool import Pool

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


# --- connection leak check ---------------------------------------------------------------------
# A connection left open (an engine that was never disposed) is garbage-collected at some random
# later point, and its ResourceWarning then fails whichever test happens to be running. Catch it in
# the test that leaked it instead.

_open_connections: dict[int, str] = {}  # id of the DBAPI connection -> its driver module


@event.listens_for(Pool, "connect")
def _on_connect(dbapi_connection: Any, record: Any) -> None:
    _open_connections[id(dbapi_connection)] = type(dbapi_connection).__module__


@event.listens_for(Pool, "close")
def _on_close(dbapi_connection: Any, record: Any) -> None:
    _open_connections.pop(id(dbapi_connection), None)


@event.listens_for(Pool, "close_detached")
def _on_close_detached(dbapi_connection: Any) -> None:
    _open_connections.pop(id(dbapi_connection), None)


@pytest.fixture(autouse=True)
def no_leaked_connections() -> Iterator[None]:
    """Fail a test that leaves database connections open (checked after its fixtures' teardown)."""
    before = set(_open_connections)
    yield
    leaked = {key: driver for key, driver in _open_connections.items() if key not in before}
    if leaked:
        for key in leaked:  # report each leak once
            del _open_connections[key]
        drivers = ", ".join(sorted(leaked.values()))
        pytest.fail(
            f"{len(leaked)} database connection(s) left open ({drivers}): dispose the engine"
        )
