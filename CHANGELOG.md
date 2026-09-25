# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, minor versions
may contain breaking changes.

## [Unreleased]

### Fixed

- A value selected from inside the JSON, such as `select(User.settings["theme"])`, was validated
  as the column's model. That failed, or, for an object such as `User.settings["address"]`,
  silently returned the column's model filled with its defaults. It's now the raw JSON value (a
  dict, list, string, ...), not validated by Pydantic, as from a JSON column. Comparing such a
  value without an accessor (`User.settings["theme"] == "blue"`) failed the same way, and now
  works as on a JSON column. Filtering with a typed accessor such as `.as_string()` wasn't
  affected.
- `JSONB` operators on the column: `has_key()` failed, and `contains({"theme": "dark"})`
  compared against the whole model with its defaults filled in, so it missed rows whose other
  fields weren't the defaults. A part of a document, or a key, is now passed as is. Comparing a
  whole document with `==` or `!=` still validates a dict into the model.

## [0.2.0] - 2026-09-25

### Added

- `EmbeddedPydanticRootModel`: Pydantic's `RootModel` for columns whose value is a list or a
  union of models, e.g. `class Payment(EmbeddedPydanticRootModel[Card | Invoice])`. The list or
  model is its `root` attribute, and changes to it are tracked. Assigning a plain value (a list,
  one of the union's models, ...) to such a column validates it into the model.
- Tested with SQLAlchemy 2.1, as well as 2.0.
- Tested and documented: FastAPI request and response models, dataclass-style
  (`MappedAsDataclass`) and imperative mapping, generic models, discriminated unions, validators
  and serializers, and `merge()` of a pickled row (as with a cache).
- README: a list of what's tracked, and what isn't.

### Changed

- Requires Pydantic 2.12 or later (was 2.11).
- Requires SQLAlchemy 2.0.44 or later (was 2.0.22). Before 2.0.44, assigning a model, tuple or
  list to a list index (`settings.items[0] = Item()`) was silently dropped (strings too, before
  2.0.24): SQLAlchemy's `MutableList` ignored iterable values.
- Faster tracking in long lists: a change to an item no longer searches the whole list. Changing
  every model in a list of 10,000 went from about 1.2 s to 50 ms.

### Fixed

- `Counter` fields were turned into plain dicts, so their own methods (`most_common()`, ...) were
  gone. They stay `Counter`s now, and are tracked.
- `OrderedDict` fields were turned into plain dicts, so their own methods (`move_to_end()`, ...)
  were gone. They stay `OrderedDict`s now, and are tracked. `move_to_end()` also changes the
  stored order, which Pydantic alone doesn't do.
- `defaultdict` fields were turned into plain dicts and lost their default factory, so reading a
  missing key raised `KeyError`. They stay `defaultdict`s now, and the default that a missing key
  inserts is tracked.
- Changes to lists, dicts and models inside tuples (also named tuples, and tuples inside lists
  and dicts) weren't tracked, and were lost unless something else in the row changed too.
- Models inside lists weren't tracked after `copy.deepcopy()`, `model_copy(deep=True)` or
  pickling (e.g. when caching rows): changes to them were lost. Items in dicts were fine.
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

[Unreleased]: https://github.com/joakimnordling/sqlalchemy-pydantic-json/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/joakimnordling/sqlalchemy-pydantic-json/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/joakimnordling/sqlalchemy-pydantic-json/releases/tag/v0.1.0
