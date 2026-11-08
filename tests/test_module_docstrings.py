"""Each module of the app keeps its description as a real docstring.

``from __future__ import annotations`` above the description turns it into a
plain string that nobody can read through ``module.__doc__`` (it happened in 8
files, and ``ruff`` with the rules in use does not see it).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULES = sorted(
    path
    for folder in ("api", "state", "views")
    for path in (ROOT / folder).glob("*.py")
    if path.name != "__init__.py"
)


def test_the_modules_were_found():
    assert len(MODULES) >= 10


@pytest.mark.parametrize("path", MODULES, ids=lambda path: f"{path.parent.name}/{path.name}")
def test_the_module_description_is_the_docstring(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))

    assert ast.get_docstring(tree), f"{path.name}: the description must be the first statement"
