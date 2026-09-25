"""
The README's Python examples run, in order, in one namespace.

A block preceded by a ``<!-- readme-test: skip -->`` line is a fragment that can't run on its own.
One preceded by ``<!-- readme-test: needs <module> -->`` runs only when that module is installed.
"""

import importlib.util
import re
from pathlib import Path

from sqlalchemy import Engine

README = Path(__file__).parent.parent / "README.md"
BLOCK = re.compile(r"(?P<before>[^\n]*)\n```python\n(?P<code>.*?)\n```", re.DOTALL)
SKIP = "<!-- readme-test: skip -->"
NEEDS = re.compile(r"<!-- readme-test: needs (?P<module>[\w.]+) -->")


def runs(before: str) -> bool:
    """Whether the block after the line `before` runs."""
    needs = NEEDS.fullmatch(before.strip())
    return before.strip() != SKIP and (not needs or bool(importlib.util.find_spec(needs["module"])))


def test_readme_examples_run() -> None:
    text = README.read_text()
    blocks = [m for m in BLOCK.finditer(text) if runs(m["before"])]
    assert len(blocks) >= 3  # the pattern still finds the examples
    # like a module: without `__name__`, classes get the module "builtins", and Pydantic then
    # fails to validate a discriminated union of them
    namespace: dict[str, object] = {"__name__": "readme"}
    try:
        for match in blocks:
            line = text.count("\n", 0, match.start("code")) + 1
            code = compile(match["code"], f"{README}:{line}", "exec")
            exec(code, namespace)
    finally:
        # close the examples' engines; a leaked connection would warn during a later test
        for value in namespace.values():
            if isinstance(value, Engine):
                value.dispose()
