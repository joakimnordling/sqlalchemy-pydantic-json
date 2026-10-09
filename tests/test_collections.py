"""Other collection types: defaultdict, OrderedDict, Counter and deque."""

import copy
import pickle
import sys
from collections import Counter, OrderedDict, defaultdict, deque
from collections.abc import Callable
from typing import Annotated, Any

import pytest
import sqlalchemy as sa
from pydantic import AfterValidator, Field
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlalchemy_pydantic_json import EmbeddedPydanticModel

MakeEngine = Callable[[sa.MetaData], sa.Engine]


class Item(EmbeddedPydanticModel):
    n: int = 0


def last_three(queue: deque[int]) -> deque[int]:
    return deque(queue, maxlen=3)


class Settings(EmbeddedPydanticModel):
    lists: defaultdict[str, list[int]] = defaultdict(list)
    # Pydantic infers a default factory only for built-in types (list here); a model needs its own
    items: defaultdict[str, Annotated[Item, Field(default_factory=Item)]] = defaultdict(Item)
    ordered: OrderedDict[str, list[int]] = OrderedDict()
    queue: deque[list[int]] = deque()
    tally: Counter[str] = Counter()
    # JSON has no deque, so Pydantic loads one without a maxlen; a validator can set it
    recent: Annotated[deque[int], AfterValidator(last_three)] = deque(maxlen=3)


class Base(DeclarativeBase):
    pass


class Row(Base):
    __tablename__ = "collection_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    settings: Mapped[Settings] = mapped_column(Settings.column(), default=Settings)


Step = tuple[Callable[[Settings], object], Callable[[Settings], bool]]


def run_steps(engine: sa.Engine, expire_on_commit: bool, steps: list[Step]) -> None:
    """Apply each change in its own commit; check it marked the row dirty and was saved."""
    with Session(engine) as s:
        s.add(Row(id=1))
        s.commit()
    for change, saved in steps:
        with Session(engine, expire_on_commit=expire_on_commit) as s:
            row = s.get_one(Row, 1)
            change(row.settings)
            assert row in s.dirty
            s.commit()
        with Session(engine) as s:
            assert saved(s.get_one(Row, 1).settings)


def test_defaultdict(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            # a missing key inserts its default: the row changes, and the default is tracked
            (lambda st: st.lists["a"], lambda st: st.lists == {"a": []}),
            (lambda st: st.lists["a"].append(1), lambda st: st.lists == {"a": [1]}),
            (lambda st: st.lists["b"].append(2), lambda st: st.lists == {"a": [1], "b": [2]}),
            (lambda st: setattr(st.items["x"], "n", 1), lambda st: st.items["x"].n == 1),
            (lambda st: setattr(st.items["x"], "n", 2), lambda st: st.items["x"].n == 2),
            (
                lambda st: setattr(st, "lists", defaultdict(list, {"c": [3]})),
                lambda st: st.lists == {"c": [3]},
            ),
        ],
    )


def test_defaultdict_keeps_its_default_factory(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1))
        s.commit()
        settings = s.get_one(Row, 1).settings
        assert isinstance(settings.lists, defaultdict)
        assert settings.lists.default_factory is list
        settings.lists = defaultdict(list)
        assert isinstance(settings.lists, defaultdict)
        assert settings.lists.default_factory is list


def test_defaultdict_without_a_default_factory(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=False) as s:  # (a reload would validate a new one)
        row = Row(id=1)
        s.add(row)
        s.commit()
        row.settings.lists = defaultdict(None)  # no validate_assignment: kept as is
        s.commit()
        with pytest.raises(KeyError, match="missing"):
            row.settings.lists["missing"]
        assert row not in s.dirty


def test_reading_should_not_mark_dirty(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1, settings=Settings(lists=defaultdict(list, {"a": [1]}))))
        s.commit()
        row = s.get_one(Row, 1)
        assert row.settings.lists["a"] == [1]  # an existing key
        assert row.settings.lists.get("missing") is None  # get() doesn't insert
        assert "missing" not in row.settings.lists
        assert row not in s.dirty


def test_ordered_dict(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    def keys(st: Settings) -> list[str]:
        return list(st.ordered)

    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            (lambda st: st.ordered.update(b=[], a=[]), lambda st: keys(st) == ["b", "a"]),
            (lambda st: st.ordered.__setitem__("c", [1]), lambda st: st.ordered["c"] == [1]),
            (lambda st: st.ordered["b"].append(2), lambda st: st.ordered["b"] == [2]),
            (lambda st: st.ordered.move_to_end("b"), lambda st: keys(st) == ["a", "c", "b"]),
            (lambda st: st.ordered.move_to_end("b", last=False), lambda st: keys(st)[0] == "b"),
            (lambda st: st.ordered.setdefault("d", [4]), lambda st: st.ordered["d"] == [4]),
            (lambda st: st.ordered["d"].append(5), lambda st: st.ordered["d"] == [4, 5]),
            (lambda st: st.ordered.__ior__({"e": []}), lambda st: "e" in st.ordered),
            (lambda st: st.ordered.pop("e"), lambda st: "e" not in st.ordered),
            (lambda st: st.ordered.popitem(), lambda st: "d" not in st.ordered),
            (lambda st: st.ordered.__delitem__("c"), lambda st: keys(st) == ["b", "a"]),
            (lambda st: st.ordered.clear(), lambda st: keys(st) == []),
        ],
    )


def test_ordered_dict_type_is_kept(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1))
        s.commit()
        settings = s.get_one(Row, 1).settings
        assert isinstance(settings.ordered, OrderedDict)
        settings.ordered = OrderedDict(a=[])
        assert isinstance(settings.ordered, OrderedDict)


def test_ordered_dict_should_not_mark_dirty(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1, settings=Settings(ordered=OrderedDict(a=[1]))))
        s.commit()
        row = s.get_one(Row, 1)
        assert row.settings.ordered.pop("missing", None) is None
        assert row.settings.ordered.setdefault("a", [2]) == [1]
        assert row.settings.ordered.get("a") == [1]
        assert row not in s.dirty
        # a value removed from it is no longer its
        popped = row.settings.ordered.pop("a")
        s.commit()
        popped.append(2)
        assert row not in s.dirty


def test_counter(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    def tally(st: Settings) -> dict[str, int]:
        return dict(st.tally)

    def add_one(st: Settings) -> None:
        st.tally["a"] += 1

    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            # an empty Counter's update() skips __setitem__
            (lambda st: st.tally.update({"a": 1}), lambda st: tally(st) == {"a": 1}),
            (add_one, lambda st: tally(st) == {"a": 2}),
            (lambda st: st.tally.update("bb"), lambda st: tally(st) == {"a": 2, "b": 2}),
            (lambda st: st.tally.subtract({"b": 1}), lambda st: tally(st) == {"a": 2, "b": 1}),
            (lambda st: st.tally.__iadd__(Counter(c=3)), lambda st: tally(st)["c"] == 3),
            (lambda st: st.tally.__isub__(Counter(c=3)), lambda st: "c" not in tally(st)),
            (lambda st: st.tally.__ior__(Counter(d=4)), lambda st: tally(st)["d"] == 4),
            (
                lambda st: st.tally.__iand__(Counter(a=1, d=1)),
                lambda st: tally(st) == {"a": 1, "d": 1},
            ),
            (lambda st: st.tally.setdefault("e", 5), lambda st: tally(st)["e"] == 5),
            (lambda st: st.tally.pop("e"), lambda st: "e" not in tally(st)),
            (lambda st: st.tally.__delitem__("d"), lambda st: tally(st) == {"a": 1}),
            (lambda st: st.tally.popitem(), lambda st: tally(st) == {}),
            (lambda st: st.tally.update(x=1), lambda st: tally(st) == {"x": 1}),
            (lambda st: st.tally.clear(), lambda st: tally(st) == {}),
        ],
    )


def test_counter_type_is_kept(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1, settings=Settings(tally=Counter(a=2, b=1))))
        s.commit()
        settings = s.get_one(Row, 1).settings
        assert isinstance(settings.tally, Counter)
        assert settings.tally.most_common(1) == [("a", 2)]
        settings.tally = Counter("xyy")
        assert isinstance(settings.tally, Counter)
        assert settings.tally.most_common(1) == [("y", 2)]


def test_counter_should_not_mark_dirty(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1, settings=Settings(tally=Counter(a=1))))
        s.commit()
        row = s.get_one(Row, 1)
        assert row.settings.tally["missing"] == 0  # a Counter doesn't insert a missing key
        assert row.settings.tally.pop("missing", None) is None
        assert row.settings.tally.setdefault("a", 5) == 1
        del row.settings.tally["missing"]  # a Counter ignores it
        assert row not in s.dirty
        assert dict(row.settings.tally) == {"a": 1}


def test_deque(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    def lists(st: Settings) -> list[list[int]]:
        return list(st.queue)

    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            (lambda st: st.queue.append([1]), lambda st: lists(st) == [[1]]),
            (lambda st: st.queue.appendleft([0]), lambda st: lists(st) == [[0], [1]]),
            (lambda st: st.queue[1].append(2), lambda st: lists(st) == [[0], [1, 2]]),
            (lambda st: st.queue.extend([[3]]), lambda st: lists(st) == [[0], [1, 2], [3]]),
            (
                lambda st: st.queue.extendleft([[-2], [-1]]),
                lambda st: lists(st)[:3] == [[-1], [-2], [0]],
            ),
            (lambda st: st.queue[0].append(-3), lambda st: lists(st)[0] == [-1, -3]),
            (lambda st: st.queue[1].append(-4), lambda st: lists(st)[1] == [-2, -4]),
            (lambda st: st.queue[4].append(4), lambda st: lists(st)[4] == [3, 4]),
            (lambda st: st.queue.rotate(1), lambda st: lists(st)[0] == [3, 4]),
            (lambda st: st.queue[0].append(5), lambda st: lists(st)[0] == [3, 4, 5]),
            (lambda st: st.queue.reverse(), lambda st: lists(st)[-1] == [3, 4, 5]),
            (lambda st: st.queue[-1].append(6), lambda st: lists(st)[-1] == [3, 4, 5, 6]),
            (lambda st: st.queue.insert(1, [7]), lambda st: lists(st)[1] == [7]),
            (lambda st: st.queue[1].append(8), lambda st: lists(st)[1] == [7, 8]),
            (lambda st: st.queue.__setitem__(0, [9]), lambda st: lists(st)[0] == [9]),
            (lambda st: st.queue[0].append(10), lambda st: lists(st)[0] == [9, 10]),
            (lambda st: st.queue.__delitem__(0), lambda st: lists(st)[0] == [7, 8]),
            (lambda st: st.queue.remove([7, 8]), lambda st: [7, 8] not in lists(st)),
            (lambda st: st.queue.pop(), lambda st: [3, 4, 5, 6] not in lists(st)),
            (lambda st: st.queue.popleft(), lambda st: lists(st) == [[-2, -4], [-1, -3]]),
            (lambda st: st.queue.__iadd__([[11]]), lambda st: lists(st)[-1] == [11]),
            (lambda st: st.queue[-1].append(12), lambda st: lists(st)[-1] == [11, 12]),
            (lambda st: st.queue.__imul__(2), lambda st: len(st.queue) == 6),
            (lambda st: st.queue.clear(), lambda st: lists(st) == []),
            (lambda st: setattr(st, "queue", deque([[13]])), lambda st: lists(st) == [[13]]),
            (lambda st: st.queue[0].append(14), lambda st: lists(st) == [[13, 14]]),
        ],
    )


def test_deque_type_and_maxlen_are_kept(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=False) as s:
        row = Row(id=1)
        s.add(row)
        s.commit()
        assert isinstance(row.settings.queue, deque)
        row.settings.queue = deque([[1], [2]], maxlen=2)
        assert isinstance(row.settings.queue, deque)
        assert row.settings.queue.maxlen == 2
        s.commit()
        row.settings.queue.append([3])  # drops [1]
        assert row in s.dirty
        s.commit()
        row.settings.queue[-1].append(4)
        assert row in s.dirty
        assert list(row.settings.queue) == [[2], [3, 4]]


def test_deque_maxlen_set_by_a_validator(make_engine: MakeEngine, expire_on_commit: bool) -> None:
    run_steps(
        make_engine(Base.metadata),
        expire_on_commit,
        [
            (lambda st: st.recent.extend([1, 2, 3]), lambda st: list(st.recent) == [1, 2, 3]),
            (lambda st: st.recent.append(4), lambda st: list(st.recent) == [2, 3, 4]),
            (lambda st: st.recent.appendleft(0), lambda st: list(st.recent) == [0, 2, 3]),
            (
                lambda st: setattr(st, "recent", deque([5, 6], maxlen=3)),
                lambda st: list(st.recent) == [5, 6] and st.recent.maxlen == 3,
            ),
        ],
    )


def test_deque_index_must_be_an_integer(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1, settings=Settings(queue=deque([[1]]))))
        s.commit()
        row = s.get_one(Row, 1)
        with pytest.raises(TypeError, match="slice"):
            row.settings.queue[0:1] = [[2]]  # type: ignore[index, list-item]
        with pytest.raises(TypeError, match="slice"):
            del row.settings.queue[0:1]  # type: ignore[arg-type]
        assert row not in s.dirty


def test_deque_should_not_mark_dirty(make_engine: MakeEngine) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine) as s:
        s.add(Row(id=1, settings=Settings(queue=deque([[1], [2]]))))
        s.commit()
        row = s.get_one(Row, 1)
        queue = row.settings.queue
        assert queue[0] == [1]
        assert queue.index([2]) == 1
        assert queue.count([1]) == 1
        assert [2] in queue
        queue.rotate(0)  # the same order, but a change as far as the deque knows
        s.commit()
        with pytest.raises(ValueError, match="not in deque"):
            queue.remove([3])
        with pytest.raises(IndexError):
            queue[2] = [3]
        with pytest.raises(IndexError):
            del queue[2]
        queue.copy().append([3])  # a copy isn't stored
        assert row not in s.dirty
        # a value removed from it is no longer its
        left = queue.popleft()
        s.commit()
        left.append(2)
        assert row not in s.dirty


@pytest.mark.skipif(sys.version_info < (3, 15), reason="frozendict is new in Python 3.15")
def test_frozendict_contents_are_not_tracked(make_engine: MakeEngine) -> None:
    """A known limit (README, rules and gotchas): only assigning a new frozendict is tracked."""

    class Frozen(EmbeddedPydanticModel):
        # ruff and mypy check for Python 3.11, which has no frozendict
        limits: frozendict[str, list[int]] = frozendict()  # type: ignore[name-defined, unused-ignore]  # noqa: F821

    class FrozenBase(DeclarativeBase):
        pass

    class FrozenRow(FrozenBase):
        __tablename__ = "frozendict_rows"
        id: Mapped[int] = mapped_column(primary_key=True)
        settings: Mapped[Frozen] = mapped_column(Frozen.column(), default=Frozen)

    engine = make_engine(FrozenBase.metadata)
    with Session(engine, expire_on_commit=False) as s:
        row = FrozenRow(id=1, settings=Frozen.model_validate({"limits": {"a": [1]}}))
        s.add(row)
        s.commit()
        row.settings.limits["a"].append(2)
        assert row not in s.dirty

        row.settings.limits |= {"b": [3]}  # a new frozendict
        assert row in s.dirty
        s.commit()
    with Session(engine) as s:
        limits = s.get_one(FrozenRow, 1).settings.limits
        assert type(limits).__name__ == "frozendict"
        assert dict(limits) == {"a": [1, 2], "b": [3]}


COPIES: list[Any] = [
    pytest.param(copy.copy, id="copy.copy"),
    pytest.param(copy.deepcopy, id="copy.deepcopy"),
    pytest.param(lambda m: pickle.loads(pickle.dumps(m)), id="pickle"),
]


@pytest.mark.parametrize("make_copy", COPIES)
def test_copies(make_engine: MakeEngine, make_copy: Callable[[Settings], Settings]) -> None:
    engine = make_engine(Base.metadata)
    with Session(engine, expire_on_commit=False) as s:
        a, b = Row(id=1, settings=Settings(items=defaultdict(Item, {"x": Item()}))), Row(id=2)
        s.add_all([a, b])
        s.commit()
        cp = make_copy(a.settings)
        assert cp.lists.default_factory is list
        cp.lists["new"].append(1)  # the copy's own dict
        assert a not in s.dirty
        b.settings = cp
        s.commit()
        cp.items["x"].n = 1  # a model already in the dict
        assert b in s.dirty
        s.commit()
        cp.lists["new"].append(2)
        assert b in s.dirty
        s.commit()
        cp.ordered["x"] = []
        s.commit()
        cp.ordered["x"].append(1)
        assert b in s.dirty
        assert isinstance(cp.ordered, OrderedDict)
        s.commit()
        cp.tally["x"] += 1
        assert b in s.dirty
        assert isinstance(cp.tally, Counter)
        s.commit()
        cp.queue.append([])
        s.commit()
        cp.queue[0].append(1)
        assert b in s.dirty
        assert isinstance(cp.queue, deque)
