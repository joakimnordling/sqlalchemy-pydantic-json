"""Pydantic v2 models stored in SQLAlchemy JSON columns, with automatic change tracking."""

from sqlalchemy_pydantic_json._model import EmbeddedPydanticModel, PydanticJSON

__all__ = ["EmbeddedPydanticModel", "PydanticJSON"]
