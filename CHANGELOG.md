# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, minor versions
may contain breaking changes.

## [Unreleased]

### Added

- `EmbeddedPydanticModel`: base class for Pydantic models stored in JSON columns. Changes to
  fields, lists, dicts, sets and nested models, at any depth, mark the row as changed.
- `Model.column()`: the column type for `mapped_column()`, with change tracking. Its `json_type`
  option chooses the underlying type, e.g. PostgreSQL's `JSONB`.
- `sqlalchemy_pydantic_json.alembic.make_render_item()`: makes Alembic's autogenerate write plain
  JSON types into migrations.
- Type-checker support: mypy, pyright and ty.
- Works with SQLModel, via `Field(sa_column=Column(Model.column()))`.
- Tested with SQLite, PostgreSQL and MariaDB, on Python 3.11 to 3.14.

[Unreleased]: https://github.com/joakimnordling/sqlalchemy-pydantic-json/commits/main
