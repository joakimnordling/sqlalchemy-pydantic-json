from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, event


@contextmanager
def capture_updates(engine: Engine) -> Iterator[list[str]]:
    """Collect the UPDATE statements executed on `engine` inside the block."""
    updates: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("UPDATE"):
            updates.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield updates
    finally:
        event.remove(engine, "before_cursor_execute", listener)
