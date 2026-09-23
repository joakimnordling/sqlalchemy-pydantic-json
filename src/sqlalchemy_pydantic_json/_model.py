"""
Pydantic models stored in SQLAlchemy JSON columns, with full change tracking.

Usage:

    class Address(EmbeddedPydanticModel):
        city: str = "Helsinki"

    class Settings(EmbeddedPydanticModel):
        tags: set[str] = set()
        address: Address = Address()

    class User(Base):
        ...
        settings: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)
        extra: Mapped[Settings | None] = mapped_column(Settings.column())

Any change anywhere inside `user.settings` (attributes, list/dict/set
mutations, nested models) marks `user` dirty. Use EmbeddedPydanticModel as the base
for the column's model *and* for all of its submodels.
"""

from __future__ import annotations

import functools
import weakref
from collections.abc import Callable, Iterable
from typing import Any, Protocol, Self, SupportsIndex, TypeVar, cast, overload

from pydantic import AliasChoices, AliasPath, BaseModel, PrivateAttr
from pydantic.fields import FieldInfo
from sqlalchemy import JSON, Dialect
from sqlalchemy.ext.mutable import Mutable, MutableDict, MutableList, MutableSet
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.types import TypeDecorator, TypeEngine

__all__ = ["EmbeddedPydanticModel", "PydanticJSON"]

_M = TypeVar("_M", bound=BaseModel)
_T = TypeVar("_T")
_KT = TypeVar("_KT")
_VT = TypeVar("_VT")


# --------------------------------------------------------------------------
# Column type
# --------------------------------------------------------------------------
class PydanticJSON(TypeDecorator[_M]):
    """
    JSON column type converting to/from a Pydantic model.

    Don't use it directly; use ``Model.column()``, which also enables change
    tracking (a bare PydanticJSON column would not notice in-place changes).
    """

    impl: TypeEngine[Any] | type[TypeEngine[Any]] = JSON
    cache_ok = True

    def __init__(self, model: type[_M], json_type: type[JSON] | JSON = JSON) -> None:
        # Replaces TypeDecorator.__init__, which would build `impl` from the class attribute.
        self.model = model
        self.json_type = json_type(none_as_null=True) if isinstance(json_type, type) else json_type
        self.impl = self.json_type

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        # Computed fields are derived again on load; stored, they'd fail models with extra="forbid".
        return _validate(self.model, value).model_dump(
            mode="json", by_alias=True, exclude_computed_fields=True
        )

    def process_result_value(self, value: Any, dialect: Dialect) -> _M | None:
        return None if value is None else _validate(self.model, value)


# The JSON is stored with the models' aliases (like Pydantic's own `by_alias=True`). Loading also
# accepts field names, e.g. in rows stored before an alias was added.
def _validate(model: type[_M], value: Any) -> _M:
    return model.model_validate(value, by_alias=True, by_name=True)


# Marks a model_post_init that sets up the tracking. Pydantic wraps model_post_init in every
# subclass (because of the private attribute) with functools.wraps, which copies the mark along.
_LINKS_FIELDS = "_sqlalchemy_pydantic_json_links_fields"
_F = TypeVar("_F", bound=Callable[..., Any])


def _marked_as_linking(post_init: _F) -> _F:
    setattr(post_init, _LINKS_FIELDS, True)
    return post_init


def _links_fields(post_init: object) -> bool:
    """Whether `post_init`, or a function it wraps (functools.wraps), sets up the tracking."""
    while post_init is not None:
        if getattr(post_init, _LINKS_FIELDS, False):
            return True
        post_init = getattr(post_init, "__wrapped__", None)
    return False


def _linking_post_init(
    post_init: Callable[[EmbeddedPydanticModel, Any], None],
) -> Callable[[EmbeddedPydanticModel, Any], None]:
    @functools.wraps(post_init)
    def model_post_init(self: EmbeddedPydanticModel, context: Any, /) -> None:
        post_init(self, context)
        self._link_fields()  # linking again (if it also called super()) is harmless

    return _marked_as_linking(model_post_init)


def _check_aliases_round_trip(model: type[BaseModel]) -> None:
    """Raise if a field is stored under a name it can't be loaded from again."""
    for name, field in model.model_fields.items():
        stored = field.serialization_alias or name
        if stored not in _loadable_names(name, field):
            raise TypeError(
                f"{model.__name__}.{name} is stored as {stored!r} (its serialization alias), "
                f"but can't be loaded from that name (validation alias: "
                f"{field.validation_alias!r}). Use the same alias for both, or include "
                f"{stored!r} in the validation alias with AliasChoices."
            )


def _loadable_names(name: str, field: FieldInfo) -> set[str]:
    names = {name}
    aliases = field.validation_alias
    choices = aliases.choices if isinstance(aliases, AliasChoices) else [aliases]
    for choice in choices:
        if isinstance(choice, str):
            names.add(choice)
        elif isinstance(choice, AliasPath) and len(choice.path) == 1:
            names.add(str(choice.path[0]))
    return names


# --------------------------------------------------------------------------
# Parent links (supports several parents; stale links are pruned lazily)
# --------------------------------------------------------------------------
class _Parent(Protocol):
    """A model or container that can hold tracked values."""

    def _notify(self) -> None: ...


class _ParentLinks:
    __slots__ = ("_refs",)

    def __init__(self) -> None:
        self._refs: dict[int, weakref.ref[_Parent]] = {}

    def add(self, parent: _Parent) -> None:
        self._refs[id(parent)] = weakref.ref(parent)

    def notify(self, child: object) -> None:
        for key, ref in list(self._refs.items()):
            parent = ref()
            # a parent that was garbage collected, or no longer holds the
            # child (popped, replaced, ...), is dropped instead of notified
            if parent is None or not _holds(parent, child):
                self._refs.pop(key, None)
                continue
            parent._notify()

    # Links aren't part of a model's value; Pydantic's `==` compares private attributes too, so
    # without this no two models would ever be equal.
    def __eq__(self, other: object) -> bool:
        return isinstance(other, _ParentLinks) or NotImplemented

    __hash__ = None  # type: ignore[assignment]

    # copies (copy.copy and copy.deepcopy use this too) and pickles start out unlinked
    def __reduce__(self) -> tuple[type[_ParentLinks], tuple[()]]:
        return (_ParentLinks, ())


def _holds(parent: _Parent, child: object) -> bool:
    if isinstance(parent, EmbeddedPydanticModel):
        d = vars(parent)
        return any(d.get(name) is child for name in type(parent).model_fields)
    if isinstance(parent, _TrackedDict):
        return any(v is child for v in cast("_TrackedDict[Any, Any]", parent).values())
    # the only other parents are lists (set items are never linked)
    return any(v is child for v in cast("_TrackedList[Any]", parent))


def _link(value: Any, parent: _Parent) -> Any:
    """Return `value` with tracking enabled, linked to `parent`."""
    tracked: _TrackedList[Any] | _TrackedDict[Any, Any] | _TrackedSet[Any]
    if isinstance(value, EmbeddedPydanticModel):
        value._links.add(parent)  # add, not replace: the same instance may live in several places
        return value
    if isinstance(value, _TrackedList | _TrackedDict | _TrackedSet):
        tracked = cast("_TrackedList[Any] | _TrackedDict[Any, Any] | _TrackedSet[Any]", value)
    elif isinstance(value, list):
        # a new container has no parents yet, so filling it (which links each item to it)
        # notifies nobody
        tracked = _TrackedList()
        tracked.extend(cast("list[Any]", value))
    elif isinstance(value, dict):
        tracked = _TrackedDict()
        tracked.update(cast("dict[Any, Any]", value))
    elif isinstance(value, set):  # set items are hashable, so never models/lists/dicts
        tracked = _TrackedSet(cast("set[Any]", value))
    else:
        return value
    tracked._links.add(parent)
    return tracked


# --------------------------------------------------------------------------
# Tracked containers (SQLAlchemy's Mutable* already hook every mutating method)
# --------------------------------------------------------------------------
class _TrackedContainer:
    @property
    def _links(self) -> _ParentLinks:
        try:
            links: _ParentLinks = self.__dict__["_links_"]
        except KeyError:
            links = self.__dict__["_links_"] = _ParentLinks()
        return links

    def changed(self) -> None:
        self._links.notify(self)

    def _notify(self) -> None:  # a child inside this container changed
        self.changed()


# MutableList's and MutableSet's in-place operators (__iadd__, __ior__, ...) don't match list's and
# set's; that comes from SQLAlchemy.
class _TrackedList(_TrackedContainer, MutableList[_T]):  # ty: ignore[invalid-method-override]
    def __setitem__(self, index: SupportsIndex | slice, value: _T | Iterable[_T]) -> None:
        if isinstance(index, slice):
            value = [_link(x, self) for x in cast("Iterable[_T]", value)]
        else:
            value = _link(value, self)
        super().__setitem__(index, value)

    def append(self, x: _T) -> None:
        super().append(_link(x, self))

    def extend(self, x: Iterable[_T]) -> None:
        super().extend([_link(v, self) for v in x])

    def insert(self, i: SupportsIndex, x: _T) -> None:
        super().insert(i, _link(x, self))


class _TrackedDict(_TrackedContainer, MutableDict[_KT, _VT]):
    def __setitem__(self, key: _KT, value: _VT) -> None:
        super().__setitem__(key, _link(value, self))

    # same overloads as MutableDict.setdefault
    @overload
    def setdefault(
        self: _TrackedDict[_KT, _T | None], key: _KT, value: None = None
    ) -> _T | None: ...

    @overload
    def setdefault(self, key: _KT, value: _VT) -> _VT: ...

    def setdefault(self, key: _KT, value: object = None) -> object:
        return super().setdefault(key, _link(value, self))

    def update(self, *a: Any, **kw: _VT) -> None:
        super().update({k: _link(v, self) for k, v in dict(*a, **kw).items()})


class _TrackedSet(_TrackedContainer, MutableSet[_T]):  # ty: ignore[invalid-method-override]
    pass


# --------------------------------------------------------------------------
# The base model
# --------------------------------------------------------------------------
class EmbeddedPydanticModel(Mutable, BaseModel):
    """Base class for models stored in a JSON column, and for their submodels."""

    _links: _ParentLinks = PrivateAttr(default_factory=_ParentLinks)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        _check_aliases_round_trip(cls)
        # A model_post_init of the subclass's own also sets up tracking, whether or not it calls
        # super().model_post_init().
        own_post_init = cls.__dict__.get("model_post_init")
        if own_post_init is not None and not _links_fields(own_post_init):
            setattr(cls, "model_post_init", _linking_post_init(own_post_init))  # noqa: B010

    @_marked_as_linking
    def model_post_init(self, context: Any, /) -> None:
        self._link_fields()

    def _link_fields(self) -> None:
        d = vars(self)
        for name in type(self).model_fields:
            d[name] = _link(d[name], self)

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name in type(self).model_fields:
            d = vars(self)
            d[name] = _link(d[name], self)
            self._notify()

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        self._notify()

    def _notify(self) -> None:
        self.changed()
        self._links.notify(self)

    def changed(self) -> None:
        """
        Flag every ORM row that currently holds this instance as modified.

        Like Mutable.changed(), but skips (and forgets) rows that no longer hold
        this exact instance, e.g. expired after a commit or since reassigned.
        """
        for state, key in list(self._parents.items()):
            obj = state.obj()
            if obj is None or state.dict.get(key) is not self:
                self._parents.pop(state, None)
                continue
            flag_modified(obj, key)

    # --- copies are independent: not linked to the original's parents/rows ---
    def __copy__(self) -> Self:
        new = super().__copy__()
        d = vars(new)
        for name in type(new).model_fields:  # a shallow copy shares containers; give it its own
            v = d[name]
            if isinstance(v, list | dict | set):
                d[name] = cast("list[Any] | dict[Any, Any] | set[Any]", v).copy()
        return new._detached()

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> Self:
        return super().__deepcopy__(memo)._detached()

    def _detached(self) -> Self:
        vars(self).pop("_parents", None)
        self._links = _ParentLinks()
        self._link_fields()
        return self

    def __getstate__(self) -> dict[Any, Any]:
        state = super().__getstate__()
        state["__dict__"] = {k: v for k, v in state["__dict__"].items() if k != "_parents"}
        return state

    def __setstate__(self, state: dict[Any, Any]) -> None:
        super().__setstate__(state)
        self._link_fields()

    # --- SQLAlchemy Mutable hooks ---
    @classmethod
    def coerce(cls, key: str, value: Any) -> Self | None:
        if value is None or isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return _validate(cls, value)
        return cast("Self | None", super().coerce(key, value))

    @classmethod
    def column(cls, json_type: type[JSON] | JSON = JSON) -> PydanticJSON[Self]:
        """
        Column type for ``mapped_column()``, with change tracking enabled.

        `json_type` is the underlying column type: the generic ``JSON`` by default, or e.g.
        PostgreSQL's ``JSONB``. A class is created with ``none_as_null=True``, so that ``None`` is
        stored as SQL ``NULL``. An instance is used as is, e.g. JSONB on PostgreSQL only::

            JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
        """
        return cast("PydanticJSON[Self]", cls.as_mutable(PydanticJSON(cls, json_type)))
