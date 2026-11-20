"""Nothing in the app stops a cancellation by accident (AGENTS.md rule 7).

A task is cancelled by raising ``asyncio.CancelledError`` in it. That is a
``BaseException``, not an ``Exception``, so every ``except Exception`` lets it through
and none of them needs a clause of its own for it. What would swallow it is a bare
``except:`` or an ``except BaseException``, and a handler that names
``CancelledError`` and does not raise it again. The few handlers that do so on
purpose are listed here, each with the reason.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
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
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)

# (file, function) -> why that handler may end a cancellation.
STOPS_CANCELLATION_ON_PURPOSE = {
    ("views/chat_turns.py", "show_activity_after_delay"): (
        "the task is only a timer for the '...' indicator: cancelling it is how it ends"
    ),
    ("views/memory.py", "debounced_load"): (
        "a new keystroke cancels the load that was waiting for it"
    ),
    ("views/chat_turns.py", "_stream_response"): (
        "'Спри' cancels the task that reads the answer; a cancel of the handler "
        "itself is told apart by ``cancelling()`` and raised again"
    ),
}


def _handlers(path: Path):
    """Every ``except`` clause of a module with the name of the function around it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            owner = node
            while owner in parents and not isinstance(owner, FUNCTIONS):
                owner = parents[owner]
            yield node, owner.name if isinstance(owner, FUNCTIONS) else "<module>"


def _names(handler: ast.ExceptHandler) -> set[str]:
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {
        t.attr if isinstance(t, ast.Attribute) else t.id
        for t in types
        if isinstance(t, (ast.Attribute, ast.Name))
    }


def _raises(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(node, ast.Raise) for node in ast.walk(handler))


def test_a_cancellation_is_not_an_exception():
    # The reason no handler of ``Exception`` needs a clause for it.
    assert not issubclass(asyncio.CancelledError, Exception)


@pytest.mark.parametrize(
    "path", SOURCES, ids=lambda path: path.relative_to(ROOT).as_posix()
)
def test_no_handler_swallows_everything(path):
    swallowing = [
        handler.lineno
        for handler, _owner in _handlers(path)
        if (handler.type is None or "BaseException" in _names(handler))
        and not _raises(handler)
    ]

    assert not swallowing, (
        f"{path.name}:{swallowing} catches BaseException and stops a cancellation; "
        "catch Exception (the cancellation passes it) or raise again"
    )


@pytest.mark.parametrize(
    "path", SOURCES, ids=lambda path: path.relative_to(ROOT).as_posix()
)
def test_a_handler_of_the_cancellation_raises_it_again_or_says_why_not(path):
    relative = path.relative_to(ROOT).as_posix()
    unexplained = [
        f"{owner} (line {handler.lineno})"
        for handler, owner in _handlers(path)
        if "CancelledError" in _names(handler)
        and not _raises(handler)
        and (relative, owner) not in STOPS_CANCELLATION_ON_PURPOSE
    ]

    assert not unexplained, (
        f"{relative}: {', '.join(unexplained)} ends a cancellation; "
        "raise it again, or list the handler in STOPS_CANCELLATION_ON_PURPOSE with the reason"
    )


def test_the_listed_handlers_still_exist():
    # A handler that was removed or renamed must leave the list too.
    found = {
        (path.relative_to(ROOT).as_posix(), owner)
        for path in SOURCES
        for handler, owner in _handlers(path)
        if "CancelledError" in _names(handler)
    }

    assert set(STOPS_CANCELLATION_ON_PURPOSE) <= found
