"""``function.md`` names every function, method and class of the app.

AGENTS.md rule 8 asks for it, and by hand it slipped: names added with the last
tabs were missing for days. A name counts as described when it stands in the
file anywhere; a group written with a star (``Settings._validate_*``) covers
every name that fits it. Helpers nested inside a function are left out on
purpose: they are described in the row of the function that owns them.
"""

from __future__ import annotations

import ast
import re
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "function.md").read_text(encoding="utf-8")
SOURCES = sorted(
    [
        ROOT / "main.py",
        *(
            path
            for folder in ("api", "state", "views")
            for path in (ROOT / folder).glob("*.py")
            if path.name != "__init__.py"
        ),
    ]
)
# ``Settings._validate_*`` names a whole group of methods at once.
GROUPS = re.findall(r"[A-Za-z_]\w*\*", DOC)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def top_level_names(path: Path) -> list[str]:
    """Classes, functions and methods of a module; not what is nested in a function."""
    names: list[str] = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (*FUNCTIONS, ast.ClassDef)):
            names.append(node.name)
        if isinstance(node, ast.ClassDef):
            names.extend(
                member.name for member in node.body if isinstance(member, FUNCTIONS)
            )
    return names


def is_described(name: str) -> bool:
    if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", DOC):
        return True
    return any(fnmatchcase(name, group) for group in GROUPS)


def test_the_sources_were_found():
    assert len(SOURCES) >= 14
    assert "keep_the_window_alive" in top_level_names(ROOT / "main.py")


@pytest.mark.parametrize(
    "path", SOURCES, ids=lambda path: path.relative_to(ROOT).as_posix()
)
def test_function_md_describes_every_name(path):
    missing = [name for name in top_level_names(path) if not is_described(name)]

    assert not missing, (
        f"function.md does not mention {', '.join(missing)} "
        f"({path.name}); describe them there (AGENTS.md rule 8)"
    )


def test_a_name_that_is_not_written_down_is_not_described():
    assert not is_described("a_function_nobody_wrote_down")
    assert is_described("keep_the_window_alive")
    # Covered by the group ``Settings._validate_*``.
    assert is_described("_validate_base_url")
