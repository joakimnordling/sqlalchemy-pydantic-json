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
import operator
import weakref
from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable, Iterable, Iterator
from itertools import compress, count, repeat
from typing import Any, Generic, Protocol, Self, SupportsIndex, TypeVar, cast, overload

from pydantic import AliasChoices, AliasPath, BaseModel, PrivateAttr, RootModel
from pydantic.fields import FieldInfo
from sqlalchemy import JSON, Dialect
from sqlalchemy.ext.mutable import Mutable, MutableDict, MutableList, MutableSet
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.types import TypeDecorator, TypeEngine

__all__ = ["EmbeddedPydanticModel", "EmbeddedPydanticRootModel", "PydanticJSON"]

_M = TypeVar("_M", bound=BaseModel)
_T = TypeVar("_T")
_KT = TypeVar("_KT", bound=Hashable)
_VT = TypeVar("_VT")
_RootT = TypeVar("_RootT")


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


_P = TypeVar("_P", bound=_Parent)
_H = TypeVar("_H")


class _Link(ABC, Generic[_P, _H]):
    """
    A weak link from a child to a parent, with a hint of where the parent holds the child.

    Checking the hint is a single lookup. When the child has moved (after an insert, a sort,
    ...), the parent is searched instead, and the hint is updated.
    """

    __slots__ = ("hint", "parent")
    parent: weakref.ref[_P]
    hint: _H

    def __init__(self, parent: _P, hint: _H) -> None:
        self.parent = weakref.ref(parent)
        self.hint = hint

    def notify(self, child: object) -> bool:
        """Notify the parent if it still holds `child`; False if it doesn't (or is gone)."""
        parent = self.parent()
        if parent is None:
            return False
        if not (self._held_at_hint(parent, child) or self._find_and_update_hint(parent, child)):
            return False
        parent._notify()
        return True

    @abstractmethod
    def _held_at_hint(self, parent: _P, child: object) -> bool: ...

    @abstractmethod
    def _find_and_update_hint(self, parent: _P, child: object) -> bool: ...


_NEAR = 8  # how far an insert or delete near an item usually moves it


class _IndexLink(_Link["_TrackedList[Any]", int]):
    """A list holds the child, at an index."""

    __slots__ = ()

    def _held_at_hint(self, parent: _TrackedList[Any], child: object) -> bool:
        return self.hint < len(parent) and parent[self.hint] is child

    def _find_and_update_hint(self, parent: _TrackedList[Any], child: object) -> bool:
        index = self._find_near_hint(parent, child)
        if index is None:
            index = _index_of(parent, child)
        if index is None:
            return False
        self.hint = index
        return True

    def _find_near_hint(self, parent: _TrackedList[Any], child: object) -> int | None:
        """Search around the old index: an insert or delete near the child moves it a little."""
        for i in range(max(self.hint - _NEAR, 0), min(self.hint + _NEAR + 1, len(parent))):
            if parent[i] is child:
                return i
        return None


def _index_of(items: list[Any], value: object) -> int | None:
    """The index of `value` in `items`, compared with `is` (list.index() would use ==), or None."""
    # the same as `next((i for i, item in enumerate(items) if item is value), None)`, looping in C
    return next(compress(count(), map(operator.is_, items, repeat(value))), None)


class _KeyLink(_Link["_TrackedDict[Any, Any]", Hashable]):
    """A dict holds the child, under a key."""

    __slots__ = ()

    def _held_at_hint(self, parent: _TrackedDict[Any, Any], child: object) -> bool:
        # a missing key gives None, which is never the child (always a model or container)
        return parent.get(self.hint) is child

    def _find_and_update_hint(self, parent: _TrackedDict[Any, Any], child: object) -> bool:
        for key, value in parent.items():  # not next(..., None): None can be a key
            if value is child:
                self.hint = key
                return True
        return False


class _FieldLink(_Link["EmbeddedPydanticModel", str]):
    """A model holds the child, in a field or an extra value (extra="allow")."""

    __slots__ = ()

    def _held_at_hint(self, parent: EmbeddedPydanticModel, child: object) -> bool:
        # a missing field gives None, which is never the child (always a model or container)
        extra = parent.__pydantic_extra__
        return vars(parent).get(self.hint) is child or (
            extra is not None and extra.get(self.hint) is child
        )

    def _find_and_update_hint(self, parent: EmbeddedPydanticModel, child: object) -> bool:
        found = (name for values, name in _model_values(parent) if values[name] is child)
        name = next(found, None)
        if name is None:
            return False
        self.hint = name
        return True


class _ParentLinks:
    """The links to every parent of a model or container; stale links are dropped lazily."""

    __slots__ = ("_refs",)

    def __init__(self) -> None:
        self._refs: dict[int, _Link[Any, Any]] = {}

    def add(self, link: _Link[Any, Any]) -> None:
        self._refs[id(link.parent())] = link

    def notify(self, child: object) -> None:
        for key, link in list(self._refs.items()):
            # a parent that was garbage collected, or no longer holds the
            # child (popped, replaced, ...), is dropped instead of notified
            if not link.notify(child):
                self._refs.pop(key, None)

    # Links aren't part of a model's value; Pydantic's `==` compares private attributes too, so
    # without this no two models would ever be equal.
    def __eq__(self, other: object) -> bool:
        return isinstance(other, _ParentLinks) or NotImplemented

    __hash__ = None  # type: ignore[assignment]

    # copies (copy.copy and copy.deepcopy use this too) and pickles start out unlinked
    def __reduce__(self) -> tuple[type[_ParentLinks], tuple[()]]:
        return (_ParentLinks, ())


def _model_values(model: EmbeddedPydanticModel) -> Iterator[tuple[dict[str, Any], str]]:
    """Where `model` keeps each of its values: its fields, and its extra values (extra="allow")."""
    d = vars(model)
    for name in type(model).model_fields:
        if name in d:  # missing after `del model.field`
            yield d, name
    extra = model.__pydantic_extra__
    if extra:
        for name in extra:
            yield extra, name


def _link(value: Any, link_type: type[_Link[_P, _H]], parent: _P, hint: _H) -> Any:
    """Return `value` with tracking enabled, linked to `parent`, which holds it at `hint`."""
    # the link is only created for a value that is linked (not for strings, numbers, ...)
    tracked: _TrackedList[Any] | _TrackedDict[Any, Any] | _TrackedSet[Any]
    if isinstance(value, EmbeddedPydanticModel):
        # add, not replace: the same instance may live in several places
        value._links.add(link_type(parent, hint))
        return value
    if isinstance(value, _TrackedList | _TrackedDict | _TrackedSet):
        tracked = cast("_TrackedList[Any] | _TrackedDict[Any, Any] | _TrackedSet[Any]", value)
    elif isinstance(value, list):
        # a new container has no parents yet, so filling it (which links each item to it)
        # notifies nobody
        tracked = _TrackedList(cast("list[Any]", value))
    elif isinstance(value, dict):
        tracked = _TrackedDict()
        tracked.update(cast("dict[Any, Any]", value))
    elif isinstance(value, set):  # set items are hashable, so never models/lists/dicts
        tracked = _TrackedSet(cast("set[Any]", value))
    else:
        return value
    tracked._links.add(link_type(parent, hint))
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
    # MutableList pickles and deep-copies itself as `cls(items)`: link the items here too.
    def __init__(self, items: Iterable[_T] = (), /) -> None:
        super().__init__()
        self.extend(items)

    def __setitem__(self, index: SupportsIndex | slice, value: _T | Iterable[_T]) -> None:
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            positions = count(start) if step == 1 else range(start, stop, step)
            items = cast("Iterable[_T]", value)
            # not strict: count() never ends; list raises for an extended slice of the wrong size
            value = [_link(x, _IndexLink, self, p) for p, x in zip(positions, items, strict=False)]
        else:
            i = operator.index(index)
            value = _link(value, _IndexLink, self, i + len(self) if i < 0 else i)
        super().__setitem__(index, value)

    def append(self, x: _T) -> None:
        super().append(_link(x, _IndexLink, self, len(self)))

    def extend(self, x: Iterable[_T]) -> None:
        start = len(self)
        super().extend([_link(v, _IndexLink, self, start + i) for i, v in enumerate(x)])

    def insert(self, i: SupportsIndex, x: _T) -> None:
        n, index = len(self), operator.index(i)
        position = max(index + n, 0) if index < 0 else min(index, n)  # as list.insert clamps it
        super().insert(i, _link(x, _IndexLink, self, position))

    # sort() and reverse() move items without __setitem__: update every item's position
    def sort(self, **kw: Any) -> None:
        super().sort(**kw)
        self._update_positions()

    def reverse(self) -> None:
        super().reverse()
        self._update_positions()

    def _update_positions(self) -> None:
        for i, item in enumerate(self):
            if isinstance(item, EmbeddedPydanticModel | _TrackedContainer):
                item._links.add(_IndexLink(self, i))


class _TrackedDict(_TrackedContainer, MutableDict[_KT, _VT]):
    def __setitem__(self, key: _KT, value: _VT) -> None:
        super().__setitem__(key, _link(value, _KeyLink, self, key))

    # same overloads as MutableDict.setdefault
    @overload
    def setdefault(
        self: _TrackedDict[_KT, _T | None], key: _KT, value: None = None
    ) -> _T | None: ...

    @overload
    def setdefault(self, key: _KT, value: _VT) -> _VT: ...

    def setdefault(self, key: _KT, value: object = None) -> object:
        return super().setdefault(key, _link(value, _KeyLink, self, key))

    def update(self, *a: Any, **kw: _VT) -> None:
        super().update({k: _link(v, _KeyLink, self, k) for k, v in dict(*a, **kw).items()})


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
        for values, name in list(_model_values(self)):
            values[name] = _link(values[name], _FieldLink, self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name in type(self).model_fields:
            values = vars(self)
        elif self.__pydantic_extra__ is not None and name in self.__pydantic_extra__:
            values = self.__pydantic_extra__
        else:  # a private attribute
            return
        values[name] = _link(values[name], _FieldLink, self, name)
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
        for values, name in _model_values(new):  # a shallow copy shares containers; give it its own
            v = values[name]
            if isinstance(v, list | dict | set):
                values[name] = cast("list[Any] | dict[Any, Any] | set[Any]", v).copy()
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
        # a RootModel's root can be anything: a list, a submodel, ...
        if isinstance(value, dict) or issubclass(cls, RootModel):
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


class EmbeddedPydanticRootModel(EmbeddedPydanticModel, RootModel[_RootT], Generic[_RootT]):
    """
    Base class for a column whose value is a list, a union of models, ...: Pydantic's RootModel.

    The list or model itself is the ``root`` attribute, e.g. ``items.root.append(item)``::

        class Items(EmbeddedPydanticRootModel[list[Item]]):
            pass
    """

    # EmbeddedPydanticModel comes first: its copying and pickling must win over RootModel's.
