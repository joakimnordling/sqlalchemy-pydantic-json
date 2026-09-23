# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, minor versions
may contain breaking changes.

## [Unreleased]

### Added

- `EmbeddedPydanticRootModel`: Pydantic's `RootModel` for columns whose value is a list or a
  union of models, e.g. `class Payment(EmbeddedPydanticRootModel[Card | Invoice])`. The list or
  model is its `root` attribute, and changes to it are tracked. Assigning a plain value (a list,
  one of the union's models, ...) to such a column validates it into the model.
- Tests for generic models, discriminated unions, and validators and serializers.

### Changed

- Requires Pydantic 2.12 or later (was 2.11).

### Fixed

- Models with `extra="allow"`: assigning, changing or deleting an extra value now marks the row
  as changed, at any depth. Before, assigning one was lost unless something else in the row
  changed too.
- Computed fields (`@computed_field`) are no longer stored in the JSON. A model with
  `extra="forbid"` and a computed field, at any depth, couldn't load its own rows. Rows already
  stored with computed values still load, unless the model forbids extra keys: resave them, or
  remove the keys with a data migration.

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
