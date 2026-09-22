"""Aliases: the JSON is stored with the models' aliases, and loads from aliases or field names."""

import json
from collections.abc import Callable
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import AliasChoices, AliasPath, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel

MakeEngine = Callable[[sa.MetaData], sa.Engine]


class CamelModel(EmbeddedPydanticModel):
    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True)


class HomeAddress(CamelModel):
    zip_code: str = "00100"
    street_lines: list[str] = []


class Profile(CamelModel):
    display_name: str = "anon"
    # an explicit alias wins over the generator; `default=` as a keyword keeps it optional for
    # type checkers, which know this field only by its alias
    tax_id: str | None = Field(default=None, alias="TIN")
    home_address: HomeAddress = HomeAddress()
    past_addresses: list[HomeAddress] = []


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "alias_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    profile: Mapped[Profile] = mapped_column(Profile.column(), default=Profile)


def stored_json(session: Session, user_id: int = 1) -> Any:
    raw = session.execute(
        sa.text("select profile from alias_users where id = :id"), {"id": user_id}
    ).scalar()
    return json.loads(raw) if isinstance(raw, str) else raw  # MariaDB returns a string


def test_stored_with_aliases_and_loaded_back(
    make_engine: MakeEngine, expire_on_commit: bool
) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=expire_on_commit) as s:
        s.add(User(id=1, profile=Profile(display_name="Jocke", TIN="123")))
        s.commit()
        assert stored_json(s) == {
            "displayName": "Jocke",
            "TIN": "123",
            "homeAddress": {"zipCode": "00100", "streetLines": []},
            "pastAddresses": [],
        }

        user = s.get_one(User, 1)
        assert user.profile.display_name == "Jocke"
        assert user.profile.tax_id == "123"

        # tracking works as usual, and nested models are stored with their aliases too
        user.profile.home_address.street_lines.append("Main street 1")
        user.profile.past_addresses.append(HomeAddress(zip_code="02100"))
        assert user in s.dirty
        s.commit()
        stored = stored_json(s)
        assert stored["homeAddress"]["streetLines"] == ["Main street 1"]
        assert stored["pastAddresses"] == [{"zipCode": "02100", "streetLines": []}]


def test_rows_stored_with_field_names_still_load(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    old = {"display_name": "Old", "tax_id": "9", "home_address": {"zip_code": "33100"}}
    with Session(engine) as s:
        s.execute(
            sa.text("insert into alias_users (id, profile) values (1, :p)"), {"p": json.dumps(old)}
        )
        s.commit()
        user = s.get_one(User, 1)
        assert user.profile == Profile(
            display_name="Old", TIN="9", home_address=HomeAddress(zip_code="33100")
        )
        # the next save stores the aliases
        user.profile.display_name = "New"
        s.commit()
        assert set(stored_json(s)) == {"displayName", "TIN", "homeAddress", "pastAddresses"}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({"displayName": "D", "TIN": "1"}, id="aliases"),
        pytest.param({"display_name": "D", "tax_id": "1"}, id="field-names"),
    ],
)
def test_assigned_dict_accepts_aliases_and_field_names(
    make_engine: MakeEngine, value: dict[str, str]
) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(User(id=1))
        s.commit()
        user = s.get_one(User, 1)
        user.profile = value  # type: ignore[assignment]
        assert (user.profile.display_name, user.profile.tax_id) == ("D", "1")
        s.commit()
        assert stored_json(s)["displayName"] == "D"


def test_query_uses_the_stored_names(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add_all([User(id=1, profile=Profile(display_name="Jocke")), User(id=2)])
        s.commit()
        by_alias = s.scalars(
            sa.select(User.id).where(User.profile["displayName"].as_string() == "Jocke")
        ).all()
        by_field_name = s.scalars(
            sa.select(User.id).where(User.profile["display_name"].as_string() == "Jocke")
        ).all()
        assert (by_alias, by_field_name) == ([1], [])


# --- aliases that can't round-trip ----------------------------------------------------------------


def test_different_validation_and_serialization_alias_is_rejected() -> None:
    with pytest.raises(TypeError, match=r"Broken\.value is stored as 'out'"):

        class Broken(EmbeddedPydanticModel):
            value: str = Field("x", validation_alias="in", serialization_alias="out")


@pytest.mark.parametrize(
    "validation_alias",
    [
        pytest.param(AliasChoices("in", "out"), id="choices"),
        pytest.param(AliasPath("out"), id="path"),
        pytest.param(AliasChoices("in", AliasPath("out")), id="choices-with-path"),
    ],
)
def test_validation_alias_accepting_the_stored_name_is_fine(validation_alias: Any) -> None:
    class Fine(EmbeddedPydanticModel):
        value: str = Field("x", validation_alias=validation_alias, serialization_alias="out")

    stored = Fine.model_validate({"value": "v"}, by_name=True).model_dump(
        mode="json", by_alias=True
    )
    assert stored == {"out": "v"}
    assert Fine.model_validate(stored, by_alias=True, by_name=True).value == "v"


def test_serialization_alias_alone_is_rejected() -> None:
    # stored as "out", but only loadable as "value"
    with pytest.raises(TypeError, match=r"stored as 'out'"):

        class Broken(EmbeddedPydanticModel):
            value: str = Field("x", serialization_alias="out")
