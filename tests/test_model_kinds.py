"""Kinds of models: root models, unions, generic models, extra="allow", validators."""

import copy
import json
import pickle
from collections.abc import Callable
from typing import Annotated, Any, Generic, Literal, Self, TypeVar

import pytest
import sqlalchemy as sa
from pydantic import (
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel, EmbeddedPydanticRootModel

MakeEngine = Callable[[sa.MetaData], sa.Engine]
T = TypeVar("T")


class Cat(EmbeddedPydanticModel):
    kind: Literal["cat"] = "cat"
    toys: list[str] = []


class Dog(EmbeddedPydanticModel):
    kind: Literal["dog"] = "dog"
    tricks: list[str] = []


AnyPet = Annotated[Cat | Dog, Field(discriminator="kind")]


class Pets(EmbeddedPydanticRootModel[list[AnyPet]]):
    """A column that is just a list."""


class Pet(EmbeddedPydanticRootModel[AnyPet]):
    """A column that is one of several models."""


class Owner(EmbeddedPydanticModel):
    pet: AnyPet = Cat()


class Box(EmbeddedPydanticModel, Generic[T]):
    item: T
    items: list[T] = []


class Extras(EmbeddedPydanticModel):
    model_config = ConfigDict(extra="allow")
    name: str = ""


class TypedExtras(EmbeddedPydanticModel):
    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, Cat] = Field(init=False)


class Validated(EmbeddedPydanticModel):
    numbers: list[int] = []
    labels: set[str] = set()

    @field_validator("numbers")
    @classmethod
    def sort_numbers(cls, value: list[int]) -> list[int]:
        return sorted(value)

    @model_validator(mode="after")
    def never_empty(self) -> Self:
        if not self.numbers:
            self.numbers.append(0)
        return self

    @field_serializer("labels")
    def sorted_labels(self, value: set[str]) -> list[str]:
        return sorted(value)


class Base(DeclarativeBase):
    pass


class Row(Base):
    __tablename__ = "model_kind_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    pets: Mapped[Pets] = mapped_column(Pets.column(), default=lambda: Pets([]))
    pet: Mapped[Pet] = mapped_column(Pet.column(), default=lambda: Pet(Cat()))
    owner: Mapped[Owner] = mapped_column(Owner.column(), default=Owner)
    box: Mapped[Box[Cat]] = mapped_column(Box[Cat].column(), default=lambda: Box[Cat](item=Cat()))
    extras: Mapped[Extras] = mapped_column(Extras.column(), default=Extras)
    typed_extras: Mapped[TypedExtras] = mapped_column(TypedExtras.column(), default=TypedExtras)
    validated: Mapped[Validated] = mapped_column(Validated.column(), default=Validated)


def stored_json(session: Session, column: str) -> Any:
    raw = session.execute(sa.text(f"select {column} from model_kind_rows where id = 1")).scalar()
    return json.loads(raw) if isinstance(raw, str) else raw  # MariaDB returns a string


Step = tuple[Callable[[Row], object], Callable[[Row], bool]]


def run_steps(engine: sa.Engine, expire_on_commit: bool, steps: list[Step]) -> None:
    """Apply each change in its own commit; check it marked the row dirty and was saved."""
    with Session(engine) as s:
        s.add(Row(id=1))
        s.commit()
    for change, saved in steps:
        with Session(engine, expire_on_commit=expire_on_commit) as s:
            row = s.get_one(Row, 1)
            change(row)
            assert row in s.dirty
            s.commit()
        with Session(engine) as s:
            assert saved(s.get_one(Row, 1))


def test_root_model_list_column(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    def assign_list(row: Row) -> None:
        row.pets = [{"kind": "dog"}, Cat()]  # type: ignore[assignment]  # coerced by the column

    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            (lambda r: r.pets.root.append(Cat()), lambda r: r.pets.root == [Cat()]),
            (
                lambda r: (
                    r.pets.root[0].toys.append("ball") if isinstance(r.pets.root[0], Cat) else None
                ),
                lambda r: r.pets.root == [Cat(toys=["ball"])],
            ),
            (lambda r: setattr(r.pets, "root", [Dog()]), lambda r: r.pets.root == [Dog()]),
            (assign_list, lambda r: r.pets.root == [Dog(), Cat()]),
        ],
    )


@pytest.mark.parametrize(
    "make_copy",
    [copy.copy, copy.deepcopy, lambda m: pickle.loads(pickle.dumps(m))],
    ids=["copy", "deepcopy", "pickle"],
)
def test_root_model_copies_are_not_attached(
    make_engine: MakeEngine, make_copy: Callable[[Pets], Pets]
) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=False) as s:
        s.add(Row(id=1, pets=Pets([Cat()])))
        s.commit()
        row = s.get_one(Row, 1)
        pets = make_copy(row.pets)
        assert pets == row.pets
        pets.root.append(Dog())
        assert row not in s.dirty
        assert row.pets == Pets([Cat()])
        # the copy is tracked once it's assigned
        row.pets = pets
        s.commit()
        pets.root.append(Cat())
        assert row in s.dirty


def test_discriminated_union_column(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    def assign_model(row: Row) -> None:
        row.pet = Dog(tricks=["sit"])  # type: ignore[assignment]  # coerced by the column

    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            (
                lambda r: r.pet.root.toys.append("ball") if isinstance(r.pet.root, Cat) else None,
                lambda r: r.pet.root == Cat(toys=["ball"]),
            ),
            (lambda r: setattr(r.pet, "root", Dog()), lambda r: r.pet.root == Dog()),
            (
                lambda r: r.pet.root.tricks.append("roll") if isinstance(r.pet.root, Dog) else None,
                lambda r: r.pet.root == Dog(tricks=["roll"]),
            ),
            (assign_model, lambda r: r.pet.root == Dog(tricks=["sit"])),
        ],
    )


def test_discriminated_union_field(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            (
                lambda r: r.owner.pet.toys.append("ball") if isinstance(r.owner.pet, Cat) else None,
                lambda r: r.owner.pet == Cat(toys=["ball"]),
            ),
            (lambda r: setattr(r.owner, "pet", Dog()), lambda r: r.owner.pet == Dog()),
        ],
    )


def test_generic_model(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            (lambda r: r.box.item.toys.append("ball"), lambda r: r.box.item.toys == ["ball"]),
            (lambda r: r.box.items.append(Cat()), lambda r: r.box.items == [Cat()]),
            (
                lambda r: r.box.items[0].toys.append("yarn"),
                lambda r: r.box.items[0].toys == ["yarn"],
            ),
        ],
    )


def extra(model: EmbeddedPydanticModel) -> dict[str, Any]:
    """The model's extra values (type checkers don't know them as attributes)."""
    assert model.model_extra is not None
    return model.model_extra


def test_extra_values(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            (lambda r: setattr(r.extras, "count", 1), lambda r: extra(r.extras) == {"count": 1}),
            (lambda r: setattr(r.extras, "count", 2), lambda r: extra(r.extras) == {"count": 2}),
            (
                lambda r: setattr(r.extras, "config", {"tags": ["a"]}),
                lambda r: extra(r.extras)["config"] == {"tags": ["a"]},
            ),
            (
                lambda r: extra(r.extras)["config"]["tags"].append("b"),
                lambda r: extra(r.extras)["config"] == {"tags": ["a", "b"]},
            ),
            (lambda r: delattr(r.extras, "count"), lambda r: "count" not in extra(r.extras)),
        ],
    )


def test_typed_extra_values(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            (
                lambda r: setattr(r.typed_extras, "tom", Cat()),
                lambda r: extra(r.typed_extras) == {"tom": Cat()},
            ),
            (
                lambda r: extra(r.typed_extras)["tom"].toys.append("ball"),
                lambda r: extra(r.typed_extras) == {"tom": Cat(toys=["ball"])},
            ),
        ],
    )


def test_extra_values_should_not_mark_dirty(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1, extras=Extras.model_validate({"tags": ["a"]})))
        s.commit()
        row = s.get_one(Row, 1)
        # a removed extra value is no longer linked to the model
        tags = extra(row.extras)["tags"]
        delattr(row.extras, "tags")
        s.commit()
        tags.append("b")
        assert row not in s.dirty
        # a copy's extra containers are its own
        setattr(row.extras, "tags", ["c"])  # noqa: B010
        s.commit()
        extra(copy.copy(row.extras))["tags"].append("d")
        assert row not in s.dirty
        assert extra(row.extras) == {"tags": ["c"]}
        # private attributes aren't stored
        row.extras._cache = [1]
        assert row not in s.dirty


def test_extra_values_are_stored(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1, extras=Extras.model_validate({"name": "x", "count": 1})))
        s.commit()
        assert stored_json(s, "extras") == {"name": "x", "count": 1}


def test_validators_and_serializers(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    engine = make_engine(Base.metadata)
    run_steps(
        engine,
        expire_on_commit,
        [
            (lambda r: r.validated.numbers.append(5), lambda r: r.validated.numbers == [0, 5]),
            (
                lambda r: setattr(r.validated, "numbers", [3, 1]),
                lambda r: r.validated.numbers == [1, 3],
            ),
            (
                lambda r: r.validated.labels.update({"b", "a"}),
                lambda r: r.validated.labels == {"a", "b"},
            ),
        ],
    )
    with Session(engine) as s:
        assert stored_json(s, "validated") == {"numbers": [1, 3], "labels": ["a", "b"]}


def test_root_model_rejects_invalid_values() -> None:
    row = Row(id=1)
    with pytest.raises(ValidationError):
        row.pets = [{"kind": "fish"}]  # type: ignore[assignment]
