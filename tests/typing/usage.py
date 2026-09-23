"""
Type-checking tests for code that uses the package; this file is checked, never run.

Every line marked `# type: ignore` must produce an error: the checkers are configured to report
unused ignore comments, so a planted error that stops being detected fails the check.
"""

from typing import Annotated, Literal, assert_type

from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import JSON, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, InstrumentedAttribute, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel, EmbeddedPydanticRootModel, PydanticJSON


class Address(EmbeddedPydanticModel):
    city: str = "Helsinki"
    lines: list[str] = []


class Settings(EmbeddedPydanticModel):
    theme: str = "light"
    tags: set[str] = set()
    address: Address = Address()


class CamelModel(EmbeddedPydanticModel):
    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True)


class Profile(CamelModel):
    display_name: str = "anon"
    tax_id: str | None = Field(default=None, alias="TIN")


class Card(EmbeddedPydanticModel):
    kind: Literal["card"] = "card"


class Invoice(EmbeddedPydanticModel):
    kind: Literal["invoice"] = "invoice"
    emails: list[str] = []


class Payment(EmbeddedPydanticRootModel[Annotated[Card | Invoice, Field(discriminator="kind")]]):
    pass


class Addresses(EmbeddedPydanticRootModel[list[Address]]):
    pass


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)
    extra: Mapped[Settings | None] = mapped_column(Settings.column())
    payment: Mapped[Payment] = mapped_column(Payment.column(), default=lambda: Payment(Card()))
    addresses: Mapped[Addresses] = mapped_column(Addresses.column(), default=lambda: Addresses([]))


def use(session: Session) -> None:
    user = session.get(User, 1)
    assert user is not None
    assert_type(user.settings, Settings)
    assert_type(user.settings.address.city, str)
    assert_type(user.extra, Settings | None)
    user.settings.tags.add("x")
    user.settings.address.lines.append("y")
    user.settings = Settings(theme="dark")
    user.extra = None
    assert_type(User.settings, InstrumentedAttribute[Settings])  # class-level access
    assert_type(Settings.column(), PydanticJSON[Settings])
    assert_type(Settings.column(json_type=JSONB), PydanticJSON[Settings])
    variant = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
    assert_type(Settings.column(json_type=variant), PydanticJSON[Settings])
    assert_type(Address.model_validate({"city": "Espoo"}), Address)
    # aliases: field names for generated aliases, the alias for an explicit one (as the README says)
    profile = Profile(display_name="Jocke", TIN="123")
    assert_type(profile.tax_id, str | None)
    assert_type(Profile.column(), PydanticJSON[Profile])
    # root models
    assert_type(user.payment.root, Card | Invoice)
    assert_type(user.addresses.root, list[Address])
    user.addresses.root.append(Address())
    user.payment = Payment(Invoice(emails=["a@example.com"]))
    assert_type(Payment.column(), PydanticJSON[Payment])


def expected_errors(user: User) -> None:
    user.settings = 123  # type: ignore
    user.settings.theme = 1  # type: ignore
    user.settings.tags.add(1)  # type: ignore
    user.settings.address.lines.append(2)  # type: ignore
    user.settings.address = "Espoo"  # type: ignore
    print(user.extra.theme)  # type: ignore  # may be None
    Settings.column(json_type=Integer)  # type: ignore  # not a JSON type
    user.addresses.root.append("Espoo")  # type: ignore
    Addresses(["Espoo"])  # type: ignore
    print(user.payment.root.emails)  # type: ignore  # may be a Card
