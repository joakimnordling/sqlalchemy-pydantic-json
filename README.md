# sqlalchemy-pydantic-json

Pydantic v2 models stored in SQLAlchemy 2.0 JSON columns, with automatic change tracking at any
depth. Changes to fields, lists, dicts, sets and nested models mark the row dirty, with no manual
`flag_modified` calls.

> Work in progress; not yet released.

```python
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy_pydantic_json import EmbeddedPydanticModel


class Address(EmbeddedPydanticModel):
    city: str = "Helsinki"


class Settings(EmbeddedPydanticModel):  # the column's model AND every submodel use the base
    tags: set[str] = set()
    address: Address = Address()


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)


user.settings.address.city = "Espoo"  # user is now dirty; commit() persists it
```

Not affiliated with or endorsed by the SQLAlchemy or Pydantic projects.
