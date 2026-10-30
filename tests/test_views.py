import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import flet as ft
import pytest

from api.client import APIClient, IngestedFile, Memory
from views.chat import HISTORY_PAGE_SIZE, ChatView
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


def mounted(view_class):
    """Pretend the view is attached to a page (``Control.uid`` is read-only)."""
    return patch.object(
        view_class, "uid", new_callable=PropertyMock, return_value="uid-1"
    )


@pytest.fixture
def chat_view(mock_page, mock_api_client):
    view = ChatView(mock_page, mock_api_client)
    view.update = MagicMock()
    view.scroll_to_bottom = MagicMock()
    return view


# ------------------------------------------------------------------ mount/start
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


@pytest.mark.asyncio
async def test_files_view_polls_only_when_tab_is_active(mock_page, mock_api_client):
    view = FilesView(mock_page, mock_api_client)
    view.did_mount()

    poll_calls = [
        call for call in mock_page.run_task.call_args_list
        if call.args and call.args[0] == view.poll_statuses
    ]
    assert poll_calls == []

    view.set_active(True)
    mock_page.run_task.assert_any_call(view.poll_statuses)
    assert view._polling is True

    view.set_active(False)
    assert view._polling is False
    assert view._poll_task is None


# ------------------------------------------------------------------- chat view
@pytest.mark.asyncio
async def test_load_history_batches_updates(chat_view, mock_api_client):
    mock_api_client.get_messages = AsyncMock(
        return_value=[
            MagicMock(role="user" if i % 2 == 0 else "assistant", content=f"msg {i}")
            for i in range(50)
        ]
    )

    with mounted(ChatView):
        await chat_view.load_history()

    assert len(chat_view.chat_list.controls) == 50
    # One update and one scroll for the whole history, not one per message.
    assert chat_view.update.call_count == 1
    assert chat_view.scroll_to_bottom.call_count == 1


@pytest.mark.asyncio
async def test_load_history_caps_rendered_messages(chat_view, mock_api_client):
    mock_api_client.get_messages = AsyncMock(
        return_value=[
            MagicMock(role="user", content="x") for _ in range(HISTORY_PAGE_SIZE + 20)
        ]
    )

    with mounted(ChatView):
        await chat_view.load_history()

    # The "showing last N messages" hint plus the capped history.
    assert len(chat_view.chat_list.controls) == HISTORY_PAGE_SIZE + 1


@pytest.mark.asyncio
async def test_load_history_ignores_stale_session(chat_view, mock_api_client):
    async def get_messages(session_id):
        # The user switched sessions while the request was in flight.
        chat_view.session_id = "another-session"
        return [MagicMock(role="user", content="stale")]

    mock_api_client.get_messages = get_messages

    with mounted(ChatView):
        await chat_view.load_history()

    assert chat_view.chat_list.controls == []


@pytest.mark.asyncio
async def test_streaming_does_not_update_view_per_chunk(chat_view, mock_api_client):
    async def fake_stream(**_kwargs):
        for _ in range(100):
            yield "chunk"

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: fake_stream(**kw))
    chat_view.input_field.value = "hi"

    with mounted(ChatView):
        await chat_view.send_message_click(None)

    # One update after adding the two messages, one when the stream ends.
    assert chat_view.update.call_count == 2
    assert chat_view.scroll_to_bottom.call_count == 1
    assert chat_view._is_streaming is False


@pytest.mark.asyncio
async def test_send_message_is_not_reentrant(chat_view, mock_api_client):
    chat_view._is_streaming = True
    chat_view.input_field.value = "hi"

    with mounted(ChatView):
        await chat_view.send_message_click(None)

    mock_api_client.stream_chat.assert_not_called()


@pytest.mark.asyncio
async def test_send_message_ignores_blank_input(chat_view, mock_api_client):
    chat_view.input_field.value = "   "

    with mounted(ChatView):
        await chat_view.send_message_click(None)

    mock_api_client.stream_chat.assert_not_called()
    assert chat_view._is_streaming is False


@pytest.mark.asyncio
async def test_send_message_resets_state_after_stream_error(chat_view, mock_api_client):
    async def failing_stream(**_kwargs):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: failing_stream(**kw))
    chat_view.input_field.value = "hi"

    with mounted(ChatView):
        await chat_view.send_message_click(None)

    assert chat_view._is_streaming is False
    assert chat_view._stop_generation is False
    assert chat_view.send_btn.visible is True
    assert chat_view.stop_btn.visible is False
    # The failed input is restored so the user can retry.
    assert chat_view.input_field.value == "hi"


# ----------------------------------------------------------------- files view
@pytest.mark.asyncio
async def test_poll_updates_single_status_cell(mock_page, mock_api_client):
    view = FilesView(mock_page, mock_api_client)
    view.files = [
        IngestedFile(id="f1", file_name="a.txt", status="processing", category="general"),
    ]
    view.update_table()
    status_cell = view._status_cells["f1"]
    rows_before = view.data_table.rows

    updated = IngestedFile(
        id="f1", file_name="a.txt", status="completed", category="general"
    )
    with patch("views.files.safe_update") as safe_update_mock:
        view._apply_status("f1", updated)

    # Only the status cell is re-sent - the table is not rebuilt.
    safe_update_mock.assert_called_once_with(view, status_cell)
    assert status_cell.value == "completed"
    assert view.data_table.rows is rows_before
    assert view.files[0].status == "completed"


# ---------------------------------------------------------------- memory view
def _memory_row(mock_page, mock_api_client, memory):
    view = MemoryView(mock_page, mock_api_client)
    view.memories = [memory]
    view.update_table()
    return view, view.data_table.rows[0]


@pytest.mark.asyncio
async def test_memory_row_edit_does_not_rebuild_table(mock_page, mock_api_client):
    view, row = _memory_row(
        mock_page, mock_api_client, Memory(id="m1", content="old", status="active")
    )
    rows_before = view.data_table.rows

    with patch("views.memory.safe_update") as safe_update_mock:
        row._edit(None)
        assert row.content_text_container.visible is False
        assert row.content_field_container.visible is True
        assert row.save_btn.visible is True
        assert safe_update_mock.call_count == 1

        row._cancel(None)

    assert row.content_text_container.visible is True
    assert row.content_field.value == "old"
    # The table and its row objects are untouched.
    assert view.data_table.rows is rows_before
    assert view.data_table.rows[0] is row


@pytest.mark.asyncio
async def test_memory_row_save_updates_only_its_cells(mock_page, mock_api_client):
    view, row = _memory_row(
        mock_page, mock_api_client, Memory(id="m1", content="old", status="active", importance=2)
    )
    updated = Memory(id="m1", content="new", status="active", importance=2)
    mock_api_client.update_memory = AsyncMock(return_value=updated)

    with patch("views.memory.safe_update"):
        row.content_field.value = "new"
        await row._save(None)

    mock_api_client.update_memory.assert_awaited_once_with(
        memory_id="m1", content="new", status="active", importance=2
    )
    assert row.content_text.value == "new"
    assert row.content_text_container.visible is True
    assert view.memories[0] is updated
    assert view.data_table.rows[0] is row


@pytest.mark.asyncio
async def test_memory_row_blank_save_is_a_noop(mock_page, mock_api_client):
    _view, row = _memory_row(
        mock_page, mock_api_client, Memory(id="m1", content="old", status="active")
    )

    with patch("views.memory.safe_update"):
        row.content_field.value = "   "
        await row._save(None)

    mock_api_client.update_memory.assert_not_called()
    assert row.content_field.value == "old"
    assert row.content_text_container.visible is True


@pytest.mark.asyncio
async def test_memory_row_save_error_keeps_edit_mode(mock_page, mock_api_client):
    view, row = _memory_row(
        mock_page, mock_api_client, Memory(id="m1", content="old", status="active")
    )
    mock_api_client.update_memory = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("views.memory.safe_update"):
        row._edit(None)
        row.content_field.value = "new"
        await row._save(None)

    # Still in edit mode so the user can retry, and nothing changed locally.
    assert row.content_field_container.visible is True
    assert row.save_btn.disabled is False
    assert row.memory.content == "old"
    assert view.memories[0].content == "old"


@pytest.mark.asyncio
async def test_memory_row_activate_updates_status_in_place(mock_page, mock_api_client):
    view, row = _memory_row(
        mock_page, mock_api_client, Memory(id="m1", content="a", status="candidate", importance=3)
    )
    mock_api_client.update_memory = AsyncMock(
        return_value=Memory(id="m1", content="a", status="active", importance=3)
    )

    with patch("views.memory.safe_update"):
        await row._activate(None)

    mock_api_client.update_memory.assert_awaited_once_with(
        memory_id="m1", content="a", status="active", importance=3
    )
    assert row.status_text.value == "active"
    assert row.activate_btn.visible is False
    assert view.memories[0].status == "active"


@pytest.mark.asyncio
async def test_memory_row_delete_removes_row(mock_page, mock_api_client):
    view, row = _memory_row(
        mock_page, mock_api_client, Memory(id="m1", content="a", status="active")
    )
    mock_api_client.delete_memory = AsyncMock(return_value=None)

    with patch("views.memory.safe_update"), patch(
        "views.memory.confirm_dialog", new=AsyncMock(return_value=True)
    ):
        await row._delete(None)

    mock_api_client.delete_memory.assert_awaited_once_with("m1")
    assert view.data_table.rows == []
    assert view.memories == []
    assert view.empty_hint.visible is True


@pytest.mark.asyncio
async def test_memory_row_delete_cancelled_keeps_row(mock_page, mock_api_client):
    view, row = _memory_row(
        mock_page, mock_api_client, Memory(id="m1", content="a", status="active")
    )

    with patch("views.memory.safe_update"), patch(
        "views.memory.confirm_dialog", new=AsyncMock(return_value=False)
    ):
        await row._delete(None)

    mock_api_client.delete_memory.assert_not_called()
    assert view.data_table.rows == [row]


@pytest.mark.asyncio
async def test_memory_row_ignores_updates_for_detached_view(mock_page, mock_api_client):
    view, row = _memory_row(
        mock_page, mock_api_client, Memory(id="m1", content="old", status="active")
    )
    # Simulate a closed session: Control._dispose() sets page to None.
    view.page = None

    row._edit(None)  # must not raise

    assert row.content_field_container.visible is True


# -------------------------------------------------------------------- helpers
def test_safe_update_skips_detached_and_unmounted_controls(mock_page):
    from views.ui_utils import safe_update

    detached = ft.Container()
    safe_update(detached)  # no page -> silently ignored

    attached = ft.Container()
    attached.page = mock_page
    child = ft.Text("x")
    child.update = MagicMock()

    safe_update(attached, child)  # child never got a uid
    child.update.assert_not_called()

    safe_update(attached)  # attached but not mounted -> no uid either
    assert attached.page is mock_page


@pytest.mark.asyncio
async def test_health_checks_notify_only_on_change(monkeypatch):
    import main as main_module

    results = [True, True, False, False]
    calls = []

    async def probe():
        return results.pop(0)

    async def fake_sleep(_seconds):
        if not results:
            raise asyncio.CancelledError

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main_module.run_health_checks(probe, calls.append, interval=0)

    assert calls == [True, False]




