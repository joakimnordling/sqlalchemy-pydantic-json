"""
FastAPI: the models work as request and response schemas.

The tracking stays out of the OpenAPI schema and the responses, a request body is tracked once it's
assigned to a row, and returning a row's value doesn't change the row. (The endpoints don't use the
database: FastAPI runs them in another thread, and an in-memory SQLite database is per thread.)
"""

import json
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterator
from typing import Annotated, Any, Literal

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel, EmbeddedPydanticRootModel

MakeEngine = Callable[[sa.MetaData], sa.Engine]


# A factory, not a defaultdict as the default: Pydantic warns that a defaultdict isn't JSON
def new_counts() -> defaultdict[str, list[int]]:
    return defaultdict(list)


class Address(EmbeddedPydanticModel):
    city: str = "Helsinki"
    lines: list[str] = []


class Settings(EmbeddedPydanticModel):
    theme: str = "light"
    tags: set[str] = set()
    address: Address = Address()
    history: list[Address] = []
    pair: tuple[list[int], Address] = ([], Address())
    counts: defaultdict[str, list[int]] = Field(default_factory=new_counts)
    ordered: OrderedDict[str, int] = OrderedDict()


class Card(EmbeddedPydanticModel):
    kind: Literal["card"] = "card"


class Invoice(EmbeddedPydanticModel):
    kind: Literal["invoice"] = "invoice"
    emails: list[str] = []


class Payment(EmbeddedPydanticRootModel[Annotated[Card | Invoice, Field(discriminator="kind")]]):
    pass


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "fastapi_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)
    payment: Mapped[Payment] = mapped_column(Payment.column(), default=lambda: Payment(Card()))


@pytest.fixture
def store() -> dict[str, Any]:
    """What the endpoints receive, and what they return."""
    return {}


@pytest.fixture
def client(store: dict[str, Any]) -> Iterator[TestClient]:
    app = FastAPI()

    @app.post("/settings")
    def post_settings(settings: Settings) -> Settings:
        store["received"] = settings
        return settings

    @app.get("/settings")
    def get_settings() -> Settings:
        return store["settings"]  # type: ignore[no-any-return]

    @app.get("/payment")
    def get_payment() -> Payment:
        return store["payment"]  # type: ignore[no-any-return]

    with TestClient(app) as client:
        yield client


def test_openapi_schema_is_that_of_plain_pydantic_models(client: TestClient) -> None:
    class Address(BaseModel):
        city: str = "Helsinki"
        lines: list[str] = []

    class Settings(BaseModel):
        theme: str = "light"
        tags: set[str] = set()
        address: Address = Address()
        history: list[Address] = []
        pair: tuple[list[int], Address] = ([], Address())
        counts: defaultdict[str, list[int]] = Field(default_factory=new_counts)
        ordered: OrderedDict[str, int] = OrderedDict()

    schema = client.get("/openapi.json").json()
    schemas = schema["components"]["schemas"]
    plain = Settings.model_json_schema(ref_template="#/components/schemas/{model}")
    assert schemas["Address"] == plain["$defs"]["Address"]
    assert schemas["Settings"] == {k: v for k, v in plain.items() if k != "$defs"}
    for internal in ("_links", "_parents", "Tracked", "Mutable"):
        assert internal not in json.dumps(schema)


def test_request_body_is_tracked_once_assigned(
    client: TestClient, store: dict[str, Any], make_engine: MakeEngine, expire_on_commit: bool
) -> None:
    body = {"theme": "dark", "history": [{"city": "Espoo"}], "pair": [[1], {"city": "Turku"}]}
    response = client.post("/settings", json=body)
    assert response.status_code == 200
    assert response.json()["history"] == [{"city": "Espoo", "lines": []}]

    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=expire_on_commit) as s:
        user = User(id=1, settings=store["received"])
        s.add(user)
        s.commit()
        user.settings.history[0].lines.append("Mannerheimintie 1")
        user.settings.pair[0].append(2)
        assert user in s.dirty
        s.commit()
    with Session(engine) as s:
        settings = s.get_one(User, 1).settings
        assert settings.history[0].lines == ["Mannerheimintie 1"]
        assert settings.pair[0] == [1, 2]


def test_tracked_values_in_responses(
    client: TestClient, store: dict[str, Any], make_engine: MakeEngine
) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        settings = Settings(tags={"a"}, history=[Address(lines=["x"])], ordered=OrderedDict(b=1))
        settings.counts["n"].append(1)
        s.add(User(id=1, settings=settings, payment=Payment(Invoice(emails=["a@example.com"]))))
        s.commit()

        user = s.get_one(User, 1)
        store["settings"], store["payment"] = user.settings, user.payment
        assert client.get("/settings").json() == user.settings.model_dump(mode="json")
        assert client.get("/payment").json() == {"kind": "invoice", "emails": ["a@example.com"]}
        assert user not in s.dirty  # returning it doesn't change it


def test_invalid_request_body(client: TestClient) -> None:
    response = client.post("/settings", json={"history": [{"city": 1}]})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "history", 0, "city"]
