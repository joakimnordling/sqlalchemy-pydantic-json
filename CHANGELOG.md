# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, minor versions
may contain breaking changes.

## [Unreleased]

## [0.1.0] - 2026-09-22

First release.

### Added

- `EmbeddedPydanticModel`: base class for Pydantic models stored in JSON columns. Changes to
  fields, lists, dicts, sets and nested models, at any depth, mark the row as changed.
- `Model.column()`: the column type for `mapped_column()`, with change tracking. Its `json_type`
  option chooses the underlying type, e.g. PostgreSQL's `JSONB`.
- `sqlalchemy_pydantic_json.alembic.make_render_item()`: makes Alembic's autogenerate write plain
  JSON types into migrations.
- Pydantic aliases (e.g. camelCase via an alias generator) decide the stored JSON keys; loading
  accepts both the aliases and the field names.
- Type-checker support: mypy, pyright and ty.
- Works with SQLModel, via `Field(sa_column=Column(Model.column()))`.
- Works with `AsyncSession`.
- Tested with SQLite, PostgreSQL and MariaDB, on Python 3.11 to 3.14.

[Unreleased]: https://github.com/joakimnordling/sqlalchemy-pydantic-json/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/joakimnordling/sqlalchemy-pydantic-json/releases/tag/v0.1.0
