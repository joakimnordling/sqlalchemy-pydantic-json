"""
Alembic support: render `PydanticJSON` columns in migrations as their plain JSON type.

Without this, autogenerate writes
``sqlalchemy_pydantic_json._model.PydanticJSON(none_as_null=True)`` into the migration, which fails
when it runs. With it, the migration gets ``sa.JSON(none_as_null=True)``, and depends on neither
this package nor your models.

In ``env.py``, pass the result of `make_render_item()` to ``context.configure()`` (in both the
offline and the online function)::

    from sqlalchemy_pydantic_json.alembic import make_render_item

    context.configure(..., render_item=make_render_item())

If you already have a ``render_item`` function of your own, let it handle everything else::

    context.configure(..., render_item=make_render_item(wrap=my_render_item))

This module doesn't import Alembic at runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, Never

from sqlalchemy_pydantic_json._model import PydanticJSON

if TYPE_CHECKING:
    from alembic.autogenerate.api import AutogenContext

__all__ = ["make_render_item"]

# the signature Alembic expects for ``render_item``
_RenderItem = Callable[[str, Any, "AutogenContext"], "str | Literal[False]"]


def _render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    """
    Render `PydanticJSON` column types as their underlying JSON type.

    Returns ``False`` for everything else, which tells Alembic to use its default rendering.
    """
    if type_ != "type" or not isinstance(obj, PydanticJSON):
        return False
    impl = obj.impl_instance
    module = type(impl).__module__
    if module.startswith("sqlalchemy.dialects."):
        # e.g. sqlalchemy.dialects.postgresql.json -> postgresql.JSONB(...)
        dialect = module.split(".")[2]
        autogen_context.imports.add(f"from sqlalchemy.dialects import {dialect}")
        return f"{dialect}.{impl!r}"
    prefix = autogen_context.opts.get("sqlalchemy_module_prefix", "sa.") or ""
    return f"{prefix}{impl!r}"


def make_render_item(*args: Never, wrap: _RenderItem | None = None) -> _RenderItem:
    """
    Return a ``render_item`` function for Alembic's ``context.configure()``.

    It renders `PydanticJSON` column types as their underlying JSON type. Everything else is passed
    on to `wrap` if given, or otherwise left to Alembic's default rendering.
    """
    # `*args` only exists to give a clear error when the function itself is passed to Alembic
    # (``render_item=make_render_item``), which then calls it with three positional arguments.
    if args:  # ty: ignore[redundant-condition]  # only reachable when called untyped, by Alembic
        raise TypeError(
            "make_render_item() takes no positional arguments. Pass its result to Alembic: "
            "context.configure(render_item=make_render_item()), or "
            "make_render_item(wrap=my_render_item) to combine it with your own function."
        )

    def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
        rendered = _render_item(type_, obj, autogen_context)
        if rendered is not False or wrap is None:
            return rendered
        return wrap(type_, obj, autogen_context)

    return render_item
