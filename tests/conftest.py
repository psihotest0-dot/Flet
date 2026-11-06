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


@pytest.fixture
def mock_page():
    page = MagicMock(spec=ft.Page)
    page.overlay = []
    return page


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
