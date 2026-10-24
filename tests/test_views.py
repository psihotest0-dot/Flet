from unittest.mock import AsyncMock, MagicMock

import flet as ft
import pytest

from api.client import APIClient
from views.chat import ChatView
from views.files import FilesView
from views.memory import MemoryView


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
    return client

@pytest.mark.asyncio
async def test_chat_view_did_mount(mock_page, mock_api_client):
    view = ChatView(mock_page, mock_api_client)
    view.did_mount()
    # verify task was run
    mock_page.run_task.assert_called_with(view.load_history)

@pytest.mark.asyncio
async def test_memory_view_did_mount(mock_page, mock_api_client):
    view = MemoryView(mock_page, mock_api_client)
    view.did_mount()
    mock_page.run_task.assert_called_with(view.load_memories)

@pytest.mark.asyncio
async def test_files_view_did_mount(mock_page, mock_api_client):
    view = FilesView(mock_page, mock_api_client)
    view.did_mount()
    # file picker should be in overlay
    assert view.file_picker in mock_page.overlay
    mock_page.run_task.assert_any_call(view.initial_load)
    mock_page.run_task.assert_any_call(view.poll_statuses)
