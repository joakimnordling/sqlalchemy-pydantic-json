"""Type-checking test: the Alembic helpers fit `context.configure()`; this file is never run."""

from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext

from sqlalchemy_pydantic_json.alembic import make_render_item


def my_render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    return False


def configure() -> None:
    context.configure(render_item=make_render_item())
    context.configure(render_item=make_render_item(wrap=my_render_item))
    context.configure(render_item=make_render_item)  # type: ignore  # not called
    make_render_item(my_render_item)  # type: ignore  # `wrap` is keyword-only
    make_render_item(wrap=42)  # type: ignore
