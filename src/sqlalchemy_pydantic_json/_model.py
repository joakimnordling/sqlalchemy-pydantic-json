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

import weakref
from typing import Any

from pydantic import BaseModel, PrivateAttr
from sqlalchemy import JSON
from sqlalchemy.ext.mutable import Mutable, MutableDict, MutableList, MutableSet
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.types import TypeDecorator

__all__ = ["EmbeddedPydanticModel", "PydanticJSON"]


# --------------------------------------------------------------------------
# Column type
# --------------------------------------------------------------------------
class PydanticJSON(TypeDecorator):
    """
    JSON column type converting to/from a Pydantic model.

    Don't use it directly; use ``Model.column()``, which also enables change
    tracking (a bare PydanticJSON column would not notice in-place changes).
    """

    impl = JSON
    cache_ok = True

    def __init__(self, model: type[BaseModel]):
        super().__init__(none_as_null=True)
        self.model = model

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return self.model.model_validate(value).model_dump(mode="json")

    def process_result_value(self, value, dialect):
        return None if value is None else self.model.model_validate(value)


# --------------------------------------------------------------------------
# Parent links (supports several parents; stale links are pruned lazily)
# --------------------------------------------------------------------------
class _ParentLinks:
    __slots__ = ("_refs",)

    def __init__(self):
        self._refs: dict[int, weakref.ref] = {}

    def set_only(self, parent):
        self._refs = {id(parent): weakref.ref(parent)}

    def add(self, parent):
        self._refs[id(parent)] = weakref.ref(parent)

    def notify(self, child):
        for key, ref in list(self._refs.items()):
            parent = ref()
            # a parent that was garbage collected, or no longer holds the
            # child (popped, replaced, ...), is dropped instead of notified
            if parent is None or not _holds(parent, child):
                self._refs.pop(key, None)
                continue
            parent._notify()

    # copies and pickles start out unlinked
    def __copy__(self):
        return _ParentLinks()

    def __deepcopy__(self, memo):
        return _ParentLinks()

    def __reduce__(self):
        return (_ParentLinks, ())


def _holds(parent, child) -> bool:
    if isinstance(parent, EmbeddedPydanticModel):
        d = parent.__dict__
        return any(d.get(name) is child for name in type(parent).model_fields)
    if isinstance(parent, dict):
        return any(v is child for v in parent.values())
    if isinstance(parent, list):
        return any(v is child for v in parent)
    return False


def _link(value, parent):
    """Return `value` with tracking enabled, linked to `parent`."""
    if isinstance(value, EmbeddedPydanticModel):
        value._links.add(parent)  # add, not replace: the same instance may live in several places
        return value
    if isinstance(value, list):
        if not isinstance(value, _TrackedList):
            value = _TrackedList(value)
            for i, v in enumerate(value):
                list.__setitem__(value, i, _link(v, value))
        value._links.add(parent)
        return value
    if isinstance(value, dict):
        if not isinstance(value, _TrackedDict):
            value = _TrackedDict(value)
            for k, v in value.items():
                dict.__setitem__(value, k, _link(v, value))
        value._links.add(parent)
        return value
    if isinstance(value, set):  # set items are hashable, so never models/lists/dicts
        if not isinstance(value, _TrackedSet):
            value = _TrackedSet(value)
        value._links.add(parent)
        return value
    return value


# --------------------------------------------------------------------------
# Tracked containers (SQLAlchemy's Mutable* already hook every mutating method)
# --------------------------------------------------------------------------
class _TrackedContainer:
    @property
    def _links(self) -> _ParentLinks:
        try:
            return self.__dict__["_links_"]
        except KeyError:
            links = self.__dict__["_links_"] = _ParentLinks()
            return links

    def changed(self):
        self._links.notify(self)

    _notify = changed  # a child inside this container changed


class _TrackedList(_TrackedContainer, MutableList):
    def __setitem__(self, i, v):
        v = [_link(x, self) for x in v] if isinstance(i, slice) else _link(v, self)
        super().__setitem__(i, v)

    def append(self, v):
        super().append(_link(v, self))

    def extend(self, vs):
        super().extend([_link(v, self) for v in vs])

    def __iadd__(self, vs):
        self.extend(vs)
        return self

    def insert(self, i, v):
        super().insert(i, _link(v, self))


class _TrackedDict(_TrackedContainer, MutableDict):
    def __setitem__(self, k, v):
        super().__setitem__(k, _link(v, self))

    def setdefault(self, k, v=None):
        return super().setdefault(k, _link(v, self))

    def update(self, *a, **kw):
        super().update({k: _link(v, self) for k, v in dict(*a, **kw).items()})


class _TrackedSet(_TrackedContainer, MutableSet):
    pass


# --------------------------------------------------------------------------
# The base model
# --------------------------------------------------------------------------
class EmbeddedPydanticModel(Mutable, BaseModel):
    """Base class for models stored in a JSON column, and for their submodels."""

    _links: _ParentLinks = PrivateAttr(default_factory=_ParentLinks)

    def model_post_init(self, context: Any, /) -> None:
        self._link_fields()

    def _link_fields(self):
        for name in type(self).model_fields:
            self.__dict__[name] = _link(self.__dict__[name], self)

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name in type(self).model_fields:
            self.__dict__[name] = _link(self.__dict__[name], self)
            self._notify()

    def __delattr__(self, name):
        super().__delattr__(name)
        self._notify()

    def _notify(self):
        self.changed()
        self._links.notify(self)

    def changed(self):
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
    def __copy__(self):
        new = super().__copy__()
        for name in type(new).model_fields:  # a shallow copy shares containers; give it its own
            v = new.__dict__[name]
            for plain in (list, dict, set):
                if isinstance(v, plain):
                    new.__dict__[name] = plain(v)
        return new._detached()

    def __deepcopy__(self, memo=None):
        return super().__deepcopy__(memo)._detached()

    def _detached(self):
        self.__dict__.pop("_parents", None)
        self.__pydantic_private__["_links"] = _ParentLinks()
        self._link_fields()
        return self

    def __getstate__(self):
        state = super().__getstate__()
        state["__dict__"] = {k: v for k, v in state["__dict__"].items() if k != "_parents"}
        return state

    def __setstate__(self, state):
        super().__setstate__(state)
        self._link_fields()

    # --- SQLAlchemy Mutable hooks ---
    @classmethod
    def coerce(cls, key, value):
        if value is None or isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls.model_validate(value)
        return super().coerce(key, value)

    @classmethod
    def column(cls) -> PydanticJSON:
        """Column type for ``mapped_column()``, with change tracking enabled."""
        return cls.as_mutable(PydanticJSON(cls))
