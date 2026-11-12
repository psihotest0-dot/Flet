import dataclasses
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import flet as ft
import pytest

import state.settings as settings_module
from api.client import APIClient, OracleDecisionPage


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Never touch the real settings.json while views are constructed.

    ``ChatView.__init__`` reads and writes the settings file, so without this the
    tests would depend on (and mutate) whatever is in the working directory.
    """
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(settings_module, "SETTINGS_FILE", settings_file)
    return settings_file


@pytest.fixture(autouse=True)
def no_unknown_flet_properties(monkeypatch):
    """Fail on ``control.some_name = value`` when Flet's control has no such property.

    Flet 1.x controls are dataclasses without slots: a property that was renamed
    (``error_text`` became ``error`` on a text field) or misspelled is accepted
    and silently never reaches the screen. Only Flet's own classes are checked;
    the views are free to keep their own attributes.
    """

    def guarded(self, name, value):
        cls = type(self)
        if (
            cls.__module__.startswith("flet.")
            and not name.startswith("_")
            and not hasattr(cls, name)
            and name not in {field.name for field in dataclasses.fields(cls)}
        ):
            raise AttributeError(
                f"{cls.__name__} has no property {name!r}: Flet would ignore it silently"
            )
        object.__setattr__(self, name, value)

    monkeypatch.setattr(ft.BaseControl, "__setattr__", guarded, raising=False)


@pytest.fixture
def mock_page():
    page = MagicMock(spec=ft.Page)
    # ``window`` is a field with a factory, so the spec does not know it.
    page.window = MagicMock(spec=ft.Window)
    page.window.destroy = AsyncMock()
    # Like the real page: a dialog that was shown is open.
    page.show_dialog.side_effect = lambda dialog: setattr(dialog, "open", True)
    return page


class FakeClipboard:
    """Stands in for ``ft.Clipboard``: remembers what was copied."""

    copied: ClassVar[list[str]] = []

    async def set(self, value: str) -> None:
        FakeClipboard.copied.append(value)


@pytest.fixture
def clipboard(monkeypatch):
    """What the app put on the clipboard, in order."""
    FakeClipboard.copied = []
    monkeypatch.setattr(ft, "Clipboard", FakeClipboard)
    return FakeClipboard.copied


@pytest.fixture
def mock_api_client():
    client = MagicMock(spec=APIClient)
    client.get_messages = AsyncMock(return_value=[])
    client.list_memories = AsyncMock(return_value=[])
    client.list_files = AsyncMock(return_value=[])
    client.list_proposals = AsyncMock(return_value=[])
    client.list_rules = AsyncMock(return_value=[])
    client.list_oracle_decisions = AsyncMock(return_value=OracleDecisionPage())
    return client
