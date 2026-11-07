from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import flet as ft
import httpx
import pytest

from api.client import (
    SUPPORTED_EXTENSIONS,
    APIError,
    IngestedFile,
    Memory,
    Message,
    StreamError,
    UploadResult,
)
from state.settings import Settings, load_settings, save_settings
from views.chat import (
    HISTORY_PAGE_SIZE,
    HISTORY_REQUEST_LIMIT,
    ChatView,
    _normalize_chat_sessions,
)
from views.files import FilesView, embedding_state
from views.memory import MemoryView


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
    mock_page.run_task.assert_any_call(view.load_memories)
    mock_page.run_task.assert_any_call(view.load_proposals_and_rules)


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
            Message(role="user" if i % 2 == 0 else "assistant", content=f"msg {i}")
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
            Message(role="user", content="x") for _ in range(HISTORY_PAGE_SIZE + 20)
        ]
    )

    with mounted(ChatView):
        await chat_view.load_history()

    # The "showing last N messages" hint plus the capped history.
    assert len(chat_view.chat_list.controls) == HISTORY_PAGE_SIZE + 1


@pytest.mark.asyncio
async def test_load_history_ignores_stale_session(chat_view, mock_api_client):
    async def get_messages(session_id, limit=None):
        # The user switched sessions while the request was in flight.
        chat_view.session_id = "another-session"
        return [Message(role="user", content="stale")]

    mock_api_client.get_messages = get_messages

    with mounted(ChatView):
        await chat_view.load_history()

    assert chat_view.chat_list.controls == []


@pytest.mark.asyncio
async def test_load_history_failure_is_logged_and_shown(
    chat_view, mock_api_client, caplog
):
    mock_api_client.get_messages = AsyncMock(side_effect=httpx.ConnectError("refused"))

    with patch("views.chat.show_snackbar") as snackbar, caplog.at_level("ERROR"):
        await chat_view.load_history()

    assert "Failed to load the history of chat" in caplog.text
    snackbar.assert_called_once()
    assert "Историята на чата не се зареди" in snackbar.call_args.args[1]
    assert "Бекендът не е достъпен" in snackbar.call_args.args[1]


@pytest.mark.asyncio
async def test_load_history_failure_of_a_left_chat_is_not_shown(
    chat_view, mock_api_client
):
    async def get_messages(session_id, limit=None):
        chat_view.session_id = "another-session"
        raise httpx.ConnectError("refused")

    mock_api_client.get_messages = get_messages

    with patch("views.chat.show_snackbar") as snackbar:
        await chat_view.load_history()

    snackbar.assert_not_called()


@pytest.mark.asyncio
async def test_new_chat_warns_when_the_settings_cannot_be_saved(chat_view):
    with patch("views.chat.save_settings", return_value=False), patch(
        "views.chat.show_snackbar"
    ) as snackbar:
        await chat_view.on_new_chat_click(None)

    snackbar.assert_called_once()
    assert "Неуспешно запазване" in snackbar.call_args.args[1]


@pytest.mark.asyncio
async def test_load_history_requests_one_message_more_than_it_shows(
    chat_view, mock_api_client
):
    with mounted(ChatView):
        await chat_view.load_history()

    # The backend returns only 50 messages unless it is asked for more; the extra
    # one is how the view finds out that older messages exist.
    assert HISTORY_REQUEST_LIMIT == HISTORY_PAGE_SIZE + 1
    mock_api_client.get_messages.assert_awaited_once_with(
        session_id=chat_view.session_id, limit=HISTORY_REQUEST_LIMIT
    )


def history_page(count: int) -> list[Message]:
    """``count`` messages, oldest first, like the backend answers."""
    return [Message(role="user", content=f"m{i}") for i in range(1, count + 1)]


@pytest.mark.asyncio
async def test_load_history_says_when_older_messages_are_hidden(
    chat_view, mock_api_client
):
    mock_api_client.get_messages = AsyncMock(
        return_value=history_page(HISTORY_REQUEST_LIMIT)
    )

    with mounted(ChatView):
        await chat_view.load_history()

    hint, *rows = chat_view.chat_list.controls
    assert isinstance(hint, ft.Text)
    assert str(HISTORY_PAGE_SIZE) in hint.value
    # The newest messages stay; the oldest one - the extra - is dropped.
    shown = [row.controls[0].content.md.value for row in rows]
    assert len(shown) == HISTORY_PAGE_SIZE
    assert shown[0] == "m2"
    assert shown[-1] == f"m{HISTORY_REQUEST_LIMIT}"


@pytest.mark.asyncio
async def test_load_history_has_no_hint_when_everything_fits(
    chat_view, mock_api_client
):
    mock_api_client.get_messages = AsyncMock(
        return_value=history_page(HISTORY_PAGE_SIZE)
    )

    with mounted(ChatView):
        await chat_view.load_history()

    assert len(chat_view.chat_list.controls) == HISTORY_PAGE_SIZE
    assert not any(isinstance(c, ft.Text) for c in chat_view.chat_list.controls)


STORED_SOURCES = [
    {
        "chunk_id": "c1",
        "file_name": "book.pdf",
        "page_number": 3,
        "content_snippet": "Река Дунав е граница.",
        "score": 87.5,
        "score_type": "cosine",
        "source_type": "text",
    }
]


@pytest.mark.asyncio
async def test_load_history_keeps_the_sources_of_stored_answers(
    chat_view, mock_api_client
):
    mock_api_client.get_messages = AsyncMock(
        return_value=[
            Message(role="user", content="question"),
            Message(role="assistant", content="answer", sources=STORED_SOURCES),
        ]
    )

    with mounted(ChatView):
        await chat_view.load_history()

    question, answer = [row.controls[0].content for row in chat_view.chat_list.controls]
    assert question.sources == []
    assert answer.sources == STORED_SOURCES

    answer.did_mount()
    assert answer.sources_row.visible is True
    chips = [c for c in answer.sources_row.controls if isinstance(c, ft.Chip)]
    assert [chip.label.value for chip in chips] == ["book.pdf"]
    # A message without references keeps its source row hidden.
    question.did_mount()
    assert question.sources_row.visible is False


@pytest.mark.asyncio
async def test_sources_of_a_second_frame_join_the_first_ones(chat_view, mock_api_client):
    # The backend sends the sources of the prompt first, and the passages the model
    # found with a tool while it answered in a second "sources" frame, with only the
    # new ones (the same contract as docs/openapi.yaml, /chat/stream).
    summary = {
        "chunk_id": "s1", "file_name": "summary.pdf", "content_snippet": "Резюме на книгата.",
        "score": 0.12, "score_type": "distance", "source_type": "concept",
    }
    passage = {
        "chunk_id": "p1", "file_name": "book.pdf", "content_snippet": "Откъс от книгата.",
        "score": 0, "source_type": "text",
    }

    async def fake_stream(**_kwargs):
        yield {"type": "sources", "data": [summary]}
        yield {"type": "sources", "data": [passage]}
        yield {"type": "sources", "data": [passage]}  # a repeated frame adds nothing
        yield {"content": "Отговор"}

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: fake_stream(**kw))
    chat_view.input_field.value = "какво пише в книгата"

    with mounted(ChatView):
        await chat_view.send_message_click(None)

    answer = chat_view.chat_list.controls[-1].controls[0].content
    assert answer.sources == [summary, passage]
    chips = [c for c in answer.sources_row.controls if isinstance(c, ft.Chip)]
    assert [chip.label.value for chip in chips] == ["summary.pdf", "book.pdf"]


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


# ----------------------------------------------------------------- chat errors
def failing_stream(error, *chunks):
    """A chat/regenerate stream that yields ``chunks`` and then raises ``error``."""

    async def stream(**_kwargs):
        for chunk in chunks:
            yield chunk
        raise error

    return MagicMock(side_effect=lambda **kwargs: stream(**kwargs))


def chat_messages(view: ChatView):
    """The ChatMessage controls of the list, oldest first."""
    return [row.controls[0].content for row in view.chat_list.controls]


@pytest.mark.asyncio
async def test_stream_error_is_shown_as_red_text_not_html(chat_view, mock_api_client):
    mock_api_client.stream_chat = failing_stream(
        StreamError("Failed to generate a response (request_id: 1)"),
        {"content": "partial"},
    )
    chat_view.input_field.value = "hi"

    await chat_view.send_message_click(None)

    _question, answer = chat_messages(chat_view)
    # The body stays plain Markdown - no HTML tags that would be shown verbatim.
    assert answer.md.value == "partial"
    assert "<" not in answer.md.value
    assert answer.error_text in answer.controls
    assert answer.error_text.visible is True
    assert answer.error_text.value == "Нещо се обърка. Опитай отново."
    assert answer.error_text.color == ft.colors.RED
    # The backend already stored the user's turn: it is not put back in the input.
    assert chat_view.input_field.value == ""


@pytest.mark.parametrize(
    ("error", "expected", "input_is_restored"),
    [
        (httpx.ConnectError("refused"), "Бекендът не е достъпен", True),
        (httpx.ReadTimeout("slow"), "Бекендът не е достъпен", True),
        (APIError(503, "llm unavailable"), "Грешка в бекенда (HTTP 503)", True),
        (
            APIError(400, "content must be at most 32000 characters"),
            "Заявката е отхвърлена: content must be at most 32000 characters",
            True,
        ),
        (StreamError("boom"), "Нещо се обърка", False),
        (RuntimeError("boom"), "Грешка при получаване на отговора", True),
    ],
    ids=["connect-error", "read-timeout", "5xx", "4xx", "stream-error", "unexpected"],
)
@pytest.mark.asyncio
async def test_chat_tells_the_kinds_of_failure_apart(
    chat_view, mock_api_client, error, expected, input_is_restored
):
    mock_api_client.stream_chat = failing_stream(error)
    chat_view.input_field.value = "hi"

    await chat_view.send_message_click(None)

    answer = chat_messages(chat_view)[-1]
    assert answer.error_text.visible is True
    assert expected in answer.error_text.value
    assert answer.error_text.color == ft.colors.RED
    assert chat_view.input_field.value == ("hi" if input_is_restored else "")


async def load_answered_chat(view: ChatView, api_client):
    api_client.get_messages = AsyncMock(
        return_value=[
            Message(role="user", content="question"),
            Message(role="assistant", content="old answer"),
        ]
    )
    with mounted(ChatView):
        await view.load_history()


@pytest.mark.asyncio
async def test_regeneration_replaces_the_previous_answer(chat_view, mock_api_client):
    await load_answered_chat(chat_view, mock_api_client)
    question_row, old_row = chat_view.chat_list.controls
    mock_api_client.regenerate_chat = MagicMock(
        side_effect=lambda **kw: _chunks({"content": "new answer"})
    )

    await chat_view.regenerate_last()

    assert len(chat_view.chat_list.controls) == 2
    assert chat_view.chat_list.controls[0] is question_row
    new_answer = chat_messages(chat_view)[1]
    assert new_answer.md.value == "new answer"
    assert chat_view.chat_list.controls[1] is not old_row
    assert chat_view._last_assistant_msg is new_answer


async def _chunks(*chunks):
    for chunk in chunks:
        yield chunk


@pytest.mark.parametrize(
    ("error", "chunks", "expected"),
    [
        (APIError(500, "boom"), (), "HTTP 500"),
        (httpx.ConnectError("refused"), (), "Бекендът не е достъпен"),
        # Fails after part of the new answer was already painted.
        (StreamError("boom"), ({"content": "half of a new answ"},), "Нещо се обърка"),
    ],
    ids=["api-error", "connect-error", "stream-error-after-text"],
)
@pytest.mark.asyncio
async def test_failed_regeneration_puts_the_previous_answer_back(
    chat_view, mock_api_client, error, chunks, expected
):
    await load_answered_chat(chat_view, mock_api_client)
    question_row, old_row = chat_view.chat_list.controls
    old_answer = chat_view._last_assistant_msg
    mock_api_client.regenerate_chat = failing_stream(error, *chunks)

    with patch("views.chat.show_snackbar") as snackbar:
        await chat_view.regenerate_last()

    # The backend keeps the old answer when regeneration fails, so the screen
    # must show it again instead of an empty gap or a half-written reply.
    assert len(chat_view.chat_list.controls) == 2
    assert chat_view.chat_list.controls[0] is question_row
    assert chat_view.chat_list.controls[1] is old_row
    assert chat_view._last_assistant_msg is old_answer
    assert chat_view._last_assistant_row is old_row
    assert old_answer.md.value == "old answer"
    assert old_answer.regenerate_btn.visible is True
    snackbar.assert_called_once()
    assert expected in snackbar.call_args.args[1]
    assert chat_view._is_streaming is False
    assert chat_view.send_btn.visible is True


@pytest.mark.asyncio
async def test_stopped_regeneration_puts_the_previous_answer_back(
    chat_view, mock_api_client
):
    await load_answered_chat(chat_view, mock_api_client)
    question_row, old_row = chat_view.chat_list.controls
    old_answer = chat_view._last_assistant_msg

    async def stream(**_kwargs):
        yield {"content": "half of a new answ"}
        # The user presses "Стоп" while the new answer is being written.
        await chat_view.stop_generation_click(None)
        yield {"content": "er that was never finished"}

    mock_api_client.regenerate_chat = MagicMock(side_effect=lambda **kw: stream(**kw))

    with patch("views.chat.show_snackbar") as snackbar:
        await chat_view.regenerate_last()

    # The backend swaps the stored answer only after a complete new one, so the
    # interrupted attempt leaves the old answer in the history - and on screen.
    assert len(chat_view.chat_list.controls) == 2
    assert chat_view.chat_list.controls[0] is question_row
    assert chat_view.chat_list.controls[1] is old_row
    assert chat_view._last_assistant_msg is old_answer
    assert old_answer.md.value == "old answer"
    assert old_answer.regenerate_btn.visible is True
    # Not an error: the user asked for it, the message only explains the swap.
    snackbar.assert_called_once()
    assert "спряна" in snackbar.call_args.args[1]
    assert snackbar.call_args.kwargs == {"success": True}
    assert chat_view._stop_generation is False
    assert chat_view._is_streaming is False
    assert chat_view.send_btn.visible is True


# ---------------------------------------------------------------- chat sessions
CHAT_1 = "11111111-1111-1111-1111-111111111111"
CHAT_2 = "22222222-2222-2222-2222-222222222222"


def seed_chat_sessions(session_id: str | None = CHAT_1) -> None:
    save_settings(
        Settings(
            user_id="user-1",
            session_id=session_id,
            chat_sessions=[
                {"id": CHAT_1, "name": "Чат 1"},
                {"id": CHAT_2, "name": "Чат 2"},
            ],
        )
    )


def make_chat_view(mock_page, mock_api_client, **kwargs) -> ChatView:
    view = ChatView(mock_page, mock_api_client, **kwargs)
    view.update = MagicMock()
    view.scroll_to_bottom = MagicMock()
    return view


def dropdown_keys(view: ChatView) -> list[str]:
    return [option.key for option in view.session_dropdown.options]


def test_chat_view_opens_the_saved_session(mock_page, mock_api_client):
    seed_chat_sessions(session_id=CHAT_2)

    view = make_chat_view(mock_page, mock_api_client)

    assert view.session_id == CHAT_2
    assert dropdown_keys(view) == [CHAT_1, CHAT_2]
    assert view.session_dropdown.value == CHAT_2
    assert load_settings().session_id == CHAT_2


def test_chat_view_reuses_stored_chat_when_session_id_is_missing(mock_page, mock_api_client):
    # A settings.json without the active id must not mint a brand new session.
    seed_chat_sessions(session_id=None)

    view = make_chat_view(mock_page, mock_api_client)

    assert view.session_id == CHAT_1
    saved = load_settings()
    assert saved.session_id == CHAT_1
    assert saved.chat_sessions == [
        {"id": CHAT_1, "name": "Чат 1"},
        {"id": CHAT_2, "name": "Чат 2"},
    ]


def test_chat_view_keeps_a_session_missing_from_chat_sessions(mock_page, mock_api_client):
    save_settings(
        Settings(
            user_id="user-1",
            session_id="legacy-session",
            chat_sessions=[{"id": CHAT_1, "name": "Чат 1"}],
        )
    )

    view = make_chat_view(mock_page, mock_api_client)

    assert view.session_id == "legacy-session"
    assert "legacy-session" in dropdown_keys(view)
    assert len(load_settings().chat_sessions) == 2


def test_chat_entry_without_an_id_is_rebound_to_the_active_session(mock_page, mock_api_client):
    # A broken write leaves the active chat without an id; it must be kept and
    # rebound to the saved session_id instead of being replaced by a new one.
    save_settings(
        Settings(user_id="user-1", session_id="legacy", chat_sessions=[{"name": "Чат 1"}])
    )

    view = make_chat_view(mock_page, mock_api_client)

    assert view.session_id == "legacy"
    assert load_settings().chat_sessions == [{"id": "legacy", "name": "Чат 1"}]


def test_chat_view_reconnects_to_a_hand_edited_settings_file(
    isolated_settings, mock_page, mock_api_client
):
    # The reported bug: a trailing comma made load_settings() discard the file,
    # so main() minted a new user_id and ChatView a new session_id on every run.
    isolated_settings.write_text(
        "{\n"
        '    "base_url": "http://localhost:8080",\n'
        '    "user_id": "123e4567-e89b-12d3-a456-426614174000",\n'
        '    "session_id": "763ebf47-618b-4410-bb08-7cce59fdff02",\n'
        '    "chat_sessions": [\n'
        '        {"id": "763ebf47-618b-4410-bb08-7cce59fdff02", "name": "Чат 1"},\n'
        "    ]\n"
        "}\n",
        encoding="utf-8",
    )

    view = make_chat_view(mock_page, mock_api_client)

    assert view.session_id == "763ebf47-618b-4410-bb08-7cce59fdff02"
    assert dropdown_keys(view) == ["763ebf47-618b-4410-bb08-7cce59fdff02"]
    saved = load_settings()
    assert saved.user_id == "123e4567-e89b-12d3-a456-426614174000"
    assert saved.session_id == "763ebf47-618b-4410-bb08-7cce59fdff02"
    # No backup: the file was repaired, not thrown away.
    assert list(isolated_settings.parent.glob("settings.json.broken.*")) == []


@pytest.mark.asyncio
async def test_chat_selection_and_new_chat_keep_the_stored_user_id(
    mock_page, mock_api_client
):
    seed_chat_sessions(session_id=CHAT_1)
    view = make_chat_view(mock_page, mock_api_client)

    view.session_dropdown.value = CHAT_2
    await view.on_session_change(None)
    await view.on_new_chat_click(None)

    saved = load_settings()
    assert saved.user_id == "user-1"
    assert saved.session_id == view.session_id


def test_normalize_chat_sessions_tolerates_legacy_and_duplicate_entries():
    settings = Settings.model_construct(
        base_url="http://localhost:8080",
        user_id="user-1",
        session_id=CHAT_2,
        chat_sessions=[
            "Чат 1",  # bare name, as written by an early build
            {"id": CHAT_2, "name": ""},
            {"session_id": CHAT_2},  # duplicate id
            "Чат 2",
        ],
    )

    assert _normalize_chat_sessions(settings) == [{"id": CHAT_2, "name": "Чат 1"}]


@pytest.mark.asyncio
async def test_session_change_uses_the_saved_id(mock_page, mock_api_client):
    seed_chat_sessions(session_id=CHAT_1)
    view = make_chat_view(mock_page, mock_api_client)
    mock_page.run_task.reset_mock()

    view.session_dropdown.value = CHAT_2
    await view.on_session_change(None)

    assert view.session_id == CHAT_2
    saved = load_settings()
    assert saved.session_id == CHAT_2
    # Selecting a chat must reuse its id instead of adding a new session.
    assert [chat["id"] for chat in saved.chat_sessions] == [CHAT_1, CHAT_2]
    mock_page.run_task.assert_called_with(view.load_history)


@pytest.mark.asyncio
async def test_session_change_prefers_the_event_value(mock_page, mock_api_client):
    seed_chat_sessions(session_id=CHAT_1)
    view = make_chat_view(mock_page, mock_api_client)

    await view.on_session_change(SimpleNamespace(control=SimpleNamespace(value=CHAT_2)))

    assert view.session_id == CHAT_2
    assert load_settings().session_id == CHAT_2


@pytest.mark.asyncio
async def test_session_change_maps_a_chat_name_to_its_saved_id(mock_page, mock_api_client):
    # Legacy option keyed by the label instead of the session id.
    seed_chat_sessions(session_id=CHAT_1)
    view = make_chat_view(mock_page, mock_api_client)

    view.session_dropdown.value = "Чат 2"
    await view.on_session_change(None)

    assert view.session_id == CHAT_2
    assert view.session_dropdown.value == CHAT_2
    assert load_settings().session_id == CHAT_2
    assert len(load_settings().chat_sessions) == 2


@pytest.mark.asyncio
async def test_session_change_creates_id_only_for_an_unknown_chat(mock_page, mock_api_client):
    seed_chat_sessions(session_id=CHAT_1)
    view = make_chat_view(mock_page, mock_api_client)

    await view.on_session_change(SimpleNamespace(control=SimpleNamespace(value="Нов чат")))

    saved = load_settings()
    created = [chat for chat in saved.chat_sessions if chat["name"] == "Нов чат"]
    assert len(created) == 1
    assert created[0]["id"] == view.session_id == saved.session_id
    assert uuid.UUID(view.session_id)
    assert len(saved.chat_sessions) == 3


@pytest.mark.asyncio
async def test_session_change_notifies_the_app_and_same_chat_is_a_noop(mock_page, mock_api_client):
    seed_chat_sessions(session_id=CHAT_1)
    changes: list[str] = []
    view = make_chat_view(mock_page, mock_api_client, on_session_changed=changes.append)
    mock_page.run_task.reset_mock()

    view.session_dropdown.value = CHAT_1  # already open
    await view.on_session_change(None)

    assert changes == []
    mock_page.run_task.assert_not_called()

    view.session_dropdown.value = CHAT_2
    await view.on_session_change(None)

    assert changes == [CHAT_2]
    mock_page.run_task.assert_called_with(view.load_history)


@pytest.mark.asyncio
async def test_new_chat_keeps_the_existing_sessions(mock_page, mock_api_client):
    seed_chat_sessions(session_id=CHAT_1)
    view = make_chat_view(mock_page, mock_api_client)
    mock_page.run_task.reset_mock()

    await view.on_new_chat_click(None)

    saved = load_settings()
    assert [chat["name"] for chat in saved.chat_sessions] == ["Чат 1", "Чат 2", "Чат 3"]
    assert saved.session_id == view.session_id
    assert view.session_id not in (CHAT_1, CHAT_2)
    assert dropdown_keys(view)[-1] == view.session_id
    # A brand new chat has no history to load.
    mock_page.run_task.assert_not_called()


# ----------------------------------------------------------------- files view
def _files_view(mock_page, mock_api_client, files):
    view = FilesView(mock_page, mock_api_client)
    view.files = files
    view.update_table()
    return view


@pytest.mark.asyncio
async def test_poll_updates_status_and_embedding_cells(mock_page, mock_api_client):
    view = _files_view(
        mock_page,
        mock_api_client,
        [
            IngestedFile(
                id="f1",
                file_name="a.txt",
                status="processing",
                category="general",
                embedding_status="processing",
            )
        ],
    )
    status_cell = view._status_cells["f1"]
    embedding_cell = view._embedding_cells["f1"]
    rows_before = view.data_table.rows

    updated = IngestedFile(
        id="f1",
        file_name="a.txt",
        status="completed",
        category="general",
        chunk_count=7,
        has_embeddings=True,
        embedding_status="completed",
    )
    with patch("views.files.safe_update") as safe_update_mock:
        view._apply_status("f1", updated)

    # Both changed cells travel in one update - the table is not rebuilt.
    safe_update_mock.assert_called_once_with(view, status_cell, embedding_cell)
    assert status_cell.value == "completed"
    assert embedding_cell.value == "Готово · 7 части"
    assert embedding_cell.color == ft.colors.GREEN
    assert view.data_table.rows is rows_before
    assert view.files[0].status == "completed"
    assert view.files[0].chunk_count == 7


@pytest.mark.asyncio
async def test_poll_skips_unchanged_cells(mock_page, mock_api_client):
    file = IngestedFile(
        id="f1",
        file_name="a.txt",
        status="completed",
        chunk_count=3,
        has_embeddings=True,
        embedding_status="completed",
    )
    view = _files_view(mock_page, mock_api_client, [file])

    with patch("views.files.safe_update") as safe_update_mock:
        view._apply_status("f1", file.model_copy())

    # Nothing changed, so nothing is re-sent to the client.
    safe_update_mock.assert_not_called()


def _category_event(value):
    return SimpleNamespace(control=SimpleNamespace(value=value))


@pytest.mark.asyncio
async def test_category_change_is_sent_to_the_backend_and_kept(mock_page, mock_api_client):
    file = IngestedFile(id="f1", file_name="a.txt", status="completed", category="general")
    view = _files_view(mock_page, mock_api_client, [file])
    mock_api_client.update_file_category = AsyncMock(
        return_value=file.model_copy(update={"category": "philosophy", "manual_override": True})
    )
    event = _category_event("philosophy")

    with patch("views.files.show_snackbar") as snackbar:
        await view.create_category_change_handler("f1")(event)

    mock_api_client.update_file_category.assert_awaited_once_with("f1", "philosophy")
    assert view.files[0].category == "philosophy"
    assert view.files[0].manual_override is True
    assert event.control.value == "philosophy"
    assert snackbar.call_args.kwargs.get("success") is True


@pytest.mark.asyncio
async def test_refused_category_change_puts_the_saved_category_back(mock_page, mock_api_client):
    file = IngestedFile(id="f1", file_name="a.txt", status="completed", category="general")
    view = _files_view(mock_page, mock_api_client, [file])
    mock_api_client.update_file_category = AsyncMock(side_effect=APIError(400, "category must be one of: general"))
    event = _category_event("philosophy")

    with patch("views.files.show_snackbar") as snackbar, patch("views.files.safe_update") as safe_update_mock:
        await view.create_category_change_handler("f1")(event)

    # The database was not changed, so the dropdown must not claim it was.
    assert event.control.value == "general"
    assert view.files[0].category == "general"
    safe_update_mock.assert_called_once_with(view, event.control)
    assert "Грешка при обновяване на категорията" in snackbar.call_args.args[1]


def test_update_table_renders_embedding_column(mock_page, mock_api_client):
    view = _files_view(
        mock_page,
        mock_api_client,
        [
            IngestedFile(
                id="f1",
                file_name="indexed.pdf",
                status="completed",
                chunk_count=12,
                has_embeddings=True,
                embedding_status="completed",
            ),
            IngestedFile(
                id="f2",
                file_name="scan.pdf",
                status="completed",
                chunk_count=0,
                has_embeddings=False,
                embedding_status="empty",
            ),
        ],
    )

    assert [column.label.value for column in view.data_table.columns] == [
        "Име на файл",
        "Статус",
        "Вектори",
        "Категория",
        "Действия",
    ]
    assert view._embedding_cells["f1"].value == "Готово · 12 части"
    assert view._embedding_cells["f2"].value == "Няма текст"
    # The second row keeps its cells aligned with the headers.
    assert len(view.data_table.rows[1].cells) == len(view.data_table.columns)


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        (IngestedFile(status="pending"), ("Изчаква", ft.colors.AMBER)),
        (IngestedFile(status="processing"), ("Обработва се", ft.colors.BLUE)),
        (
            IngestedFile(
                status="completed",
                chunk_count=2,
                has_embeddings=True,
                embedding_status="completed",
            ),
            ("Готово · 2 части", ft.colors.GREEN),
        ),
        (
            IngestedFile(
                status="completed",
                chunk_count=1,
                has_embeddings=True,
                embedding_status="completed",
            ),
            ("Готово · 1 част", ft.colors.GREEN),
        ),
        (
            IngestedFile(
                status="completed",
                chunk_count=0,
                has_embeddings=False,
                embedding_status="empty",
            ),
            ("Няма текст", ft.colors.ORANGE),
        ),
        (IngestedFile(status="failed"), ("Грешка", ft.colors.RED)),
        # Payload from a backend without embedding metadata: fall back to status.
        (IngestedFile(status="completed"), ("Готово", ft.colors.GREEN)),
    ],
)
def test_embedding_state_maps_backend_statuses(file, expected):
    label, color, _tooltip = embedding_state(file)
    assert (label, color) == expected


@pytest.mark.asyncio
async def test_failed_upload_shows_the_backend_text(
    mock_page, mock_api_client, tmp_path
):
    picked = tmp_path / "big.pdf"
    picked.write_bytes(b"x")
    mock_api_client.upload_file = AsyncMock(side_effect=APIError(413, "file too large"))
    view = FilesView(mock_page, mock_api_client)

    with patch("views.files.safe_update"), patch(
        "views.files.show_snackbar"
    ) as snackbar:
        await view.on_file_picker_result(
            SimpleNamespace(files=[SimpleNamespace(name="big.pdf", path=str(picked))])
        )

    snackbar.assert_called_once()
    assert "big.pdf: file too large" in snackbar.call_args.args[1]


def _picked(tmp_path, *names):
    """Files as the picker reports them: a name and a path on disk."""
    files = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"x")
        files.append(SimpleNamespace(name=name, path=str(path)))
    return SimpleNamespace(files=files)


@pytest.mark.asyncio
async def test_unsupported_file_is_not_uploaded(mock_page, mock_api_client, tmp_path):
    mock_api_client.upload_file = AsyncMock(
        return_value=UploadResult(IngestedFile(id="1", file_name="ok.txt"))
    )
    view = FilesView(mock_page, mock_api_client)

    with patch("views.files.safe_update"), patch(
        "views.files.show_snackbar"
    ) as snackbar:
        await view.on_file_picker_result(_picked(tmp_path, "letter.doc", "slides.pptx", "ok.txt"))

    # Only the supported file reaches the backend.
    assert [c.args[1] for c in mock_api_client.upload_file.call_args_list] == ["ok.txt"]
    message = snackbar.call_args.args[1]
    assert "letter.doc: форматът не се поддържа" in message
    assert "slides.pptx: форматът не се поддържа" in message
    assert snackbar.call_args.kwargs.get("success") is False


@pytest.mark.asyncio
async def test_duplicate_upload_is_announced(mock_page, mock_api_client, tmp_path):
    existing = IngestedFile(id="1", file_name="book.pdf", status="completed")

    # Uploads run concurrently, so the answer must depend on the file, not on
    # the order of the calls.
    async def upload(_content, name, *_args):
        return UploadResult(existing, duplicate=True) if name == "book.pdf" else UploadResult(
            IngestedFile(id="2", file_name=name)
        )

    mock_api_client.upload_file = AsyncMock(side_effect=upload)
    view = FilesView(mock_page, mock_api_client)

    with patch("views.files.safe_update"), patch(
        "views.files.show_snackbar"
    ) as snackbar:
        await view.on_file_picker_result(_picked(tmp_path, "book.pdf", "new.txt"))

    snackbar.assert_called_once()
    assert snackbar.call_args.args[1] == "Файлът вече е качен: book.pdf"
    # Not an error: a duplicate is shown as information, not in red.
    assert snackbar.call_args.kwargs.get("success") is True


@pytest.mark.asyncio
async def test_new_file_and_duplicate_are_reported_together_with_errors(
    mock_page, mock_api_client, tmp_path
):
    async def upload(_content, name, *_args):
        if name == "b.pdf":
            raise APIError(413, "file too large")
        return UploadResult(IngestedFile(id="1", file_name=name), duplicate=True)

    mock_api_client.upload_file = AsyncMock(side_effect=upload)
    view = FilesView(mock_page, mock_api_client)

    with patch("views.files.safe_update"), patch(
        "views.files.show_snackbar"
    ) as snackbar:
        await view.on_file_picker_result(_picked(tmp_path, "a.pdf", "b.pdf"))

    message = snackbar.call_args.args[1]
    assert "Неуспешно качване: b.pdf: file too large" in message
    assert "Файлът вече е качен: a.pdf" in message
    assert snackbar.call_args.kwargs.get("success") is False


@pytest.mark.asyncio
async def test_a_plain_upload_shows_no_message(mock_page, mock_api_client, tmp_path):
    mock_api_client.upload_file = AsyncMock(
        return_value=UploadResult(IngestedFile(id="1", file_name="a.txt"))
    )
    view = FilesView(mock_page, mock_api_client)

    with patch("views.files.safe_update"), patch(
        "views.files.show_snackbar"
    ) as snackbar:
        await view.on_file_picker_result(_picked(tmp_path, "a.txt"))

    snackbar.assert_not_called()


def test_file_picker_offers_only_supported_extensions(mock_page, mock_api_client):
    view = FilesView(mock_page, mock_api_client)
    view.file_picker.pick_files = MagicMock()

    view.upload_btn.on_click(None)

    kwargs = view.file_picker.pick_files.call_args.kwargs
    assert kwargs["file_type"] == ft.FilePickerFileType.CUSTOM
    assert kwargs["allowed_extensions"] == list(SUPPORTED_EXTENSIONS)
    assert kwargs["allow_multiple"] is True
    for ext in ("pdf", "docx", "epub", "txt", "png"):
        assert ext in kwargs["allowed_extensions"]
    for ext in ("doc", "rtf", "zip"):
        assert ext not in kwargs["allowed_extensions"]


def test_embedding_state_explains_an_empty_file():
    label, _color, tooltip = embedding_state(
        IngestedFile(
            status="completed",
            chunk_count=0,
            has_embeddings=False,
            embedding_status="empty",
        )
    )
    assert label == "Няма текст"
    assert "няма да бъде намерен" in tooltip


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
async def test_memory_row_save_error_shows_the_backend_text(mock_page, mock_api_client):
    _view, row = _memory_row(
        mock_page, mock_api_client, Memory(id="m1", content="old", status="active")
    )
    mock_api_client.update_memory = AsyncMock(
        side_effect=APIError(400, "content must be at most 32000 characters")
    )

    with patch("views.memory.safe_update"), patch(
        "views.memory.show_snackbar"
    ) as snackbar:
        row._edit(None)
        row.content_field.value = "new"
        await row._save(None)

    snackbar.assert_called_once()
    assert "content must be at most 32000 characters" in snackbar.call_args.args[1]
    assert "Client error" not in snackbar.call_args.args[1]


@pytest.mark.asyncio
async def test_memory_row_save_over_the_byte_limit_is_not_sent(
    mock_page, mock_api_client
):
    _view, row = _memory_row(
        mock_page, mock_api_client, Memory(id="m1", content="old", status="active")
    )
    mock_api_client.update_memory = AsyncMock()

    with patch("views.memory.safe_update"), patch(
        "views.memory.show_snackbar"
    ) as snackbar:
        row._edit(None)
        # 20000 letters is under "32000 characters", but 40000 bytes in UTF-8.
        row.content_field.value = "я" * 20000
        await row._save(None)

    mock_api_client.update_memory.assert_not_called()
    snackbar.assert_called_once()
    assert "40000" in snackbar.call_args.args[1]
    # Still editing, so the text can be shortened and saved again.
    assert row.content_field_container.visible is True
    assert row._saving is False
    assert row.save_btn.disabled is False


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
async def test_memory_row_delete_of_a_memory_that_is_gone_removes_the_row(mock_page, mock_api_client):
    # The backend answers 404 for a memory that does not exist any more.
    view, row = _memory_row(
        mock_page, mock_api_client, Memory(id="m1", content="a", status="active")
    )
    mock_api_client.delete_memory = AsyncMock(side_effect=APIError(404, "memory not found"))

    with patch("views.memory.safe_update"), patch(
        "views.memory.confirm_dialog", new=AsyncMock(return_value=True)
    ):
        await row._delete(None)

    assert view.data_table.rows == []
    assert view.memories == []
    mock_page.open.assert_not_called()  # no error snackbar: nothing went wrong


@pytest.mark.asyncio
async def test_memory_row_delete_error_keeps_row_and_says_so(mock_page, mock_api_client):
    view, row = _memory_row(
        mock_page, mock_api_client, Memory(id="m1", content="a", status="active")
    )
    mock_api_client.delete_memory = AsyncMock(side_effect=APIError(500, "internal server error"))

    with patch("views.memory.safe_update"), patch(
        "views.memory.confirm_dialog", new=AsyncMock(return_value=True)
    ):
        await row._delete(None)

    assert view.data_table.rows == [row]
    assert "Грешка при изтриване" in mock_page.open.call_args.args[0].content.value


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


# ------------------------------------------------------- memory: importance
@pytest.mark.parametrize("stored", [None, 0], ids=["missing", "legacy-zero"])
@pytest.mark.asyncio
async def test_memory_row_save_never_sends_zero_importance(
    mock_page, mock_api_client, stored
):
    # The backend answers 400 to importance 0 (valid: 1-10).
    _view, row = _memory_row(
        mock_page,
        mock_api_client,
        Memory(id="m1", content="old", status="active", importance=stored),
    )
    mock_api_client.update_memory = AsyncMock(
        return_value=Memory(id="m1", content="new", status="active", importance=5)
    )

    with patch("views.memory.safe_update"):
        row.content_field.value = "new"
        await row._save(None)

    mock_api_client.update_memory.assert_awaited_once_with(
        memory_id="m1", content="new", status="active", importance=5
    )


@pytest.mark.asyncio
async def test_memory_row_activate_without_importance_sends_a_valid_one(
    mock_page, mock_api_client
):
    _view, row = _memory_row(
        mock_page, mock_api_client, Memory(id="m1", content="a", status="candidate")
    )
    mock_api_client.update_memory = AsyncMock(
        return_value=Memory(id="m1", content="a", status="active", importance=5)
    )

    with patch("views.memory.safe_update"):
        await row._activate(None)

    mock_api_client.update_memory.assert_awaited_once_with(
        memory_id="m1", content="a", status="active", importance=5
    )


def test_memory_table_shows_importance_next_to_the_content(mock_page, mock_api_client):
    view, row = _memory_row(
        mock_page,
        mock_api_client,
        Memory(id="m1", content="a", status="active", importance=3),
    )

    assert [column.label.value for column in view.data_table.columns] == [
        "Съдържание",
        "Важност",
        "Статус",
        "Действия",
    ]
    assert len(row.cells) == len(view.data_table.columns)
    assert row.importance_text.value == "3"
    assert [option.key for option in row.importance_dropdown.options] == [
        str(value) for value in range(1, 11)
    ]


@pytest.mark.asyncio
async def test_memory_row_importance_can_be_edited_on_its_own(
    mock_page, mock_api_client
):
    view, row = _memory_row(
        mock_page,
        mock_api_client,
        Memory(id="m1", content="same", status="active", importance=3),
    )
    mock_api_client.update_memory = AsyncMock(
        return_value=Memory(id="m1", content="same", status="active", importance=8)
    )

    with patch("views.memory.safe_update"):
        row._edit(None)
        assert row.importance_dropdown.visible is True
        assert row.importance_text.visible is False
        assert row.importance_dropdown.value == "3"

        row.importance_dropdown.value = "8"
        await row._save(None)

    mock_api_client.update_memory.assert_awaited_once_with(
        memory_id="m1", content="same", status="active", importance=8
    )
    assert row.importance_text.value == "8"
    assert row.importance_text.visible is True
    assert row.importance_dropdown.visible is False
    assert view.memories[0].importance == 8


@pytest.mark.asyncio
async def test_memory_row_save_without_any_change_is_a_noop(
    mock_page, mock_api_client
):
    _view, row = _memory_row(
        mock_page,
        mock_api_client,
        Memory(id="m1", content="same", status="active", importance=3),
    )

    with patch("views.memory.safe_update"):
        row._edit(None)
        await row._save(None)

    mock_api_client.update_memory.assert_not_called()
    assert row.content_text_container.visible is True


@pytest.mark.asyncio
async def test_memory_row_cancel_restores_the_importance(mock_page, mock_api_client):
    _view, row = _memory_row(
        mock_page,
        mock_api_client,
        Memory(id="m1", content="same", status="active", importance=3),
    )

    with patch("views.memory.safe_update"):
        row._edit(None)
        row.importance_dropdown.value = "9"
        row._cancel(None)

    assert row.importance_dropdown.value == "3"
    assert row.importance_text.value == "3"


def test_memory_content_tooltip_names_the_fact_key(mock_page, mock_api_client):
    _view, with_key = _memory_row(
        mock_page,
        mock_api_client,
        Memory(id="m1", content="a", metadata={"fact_key": "user.city"}),
    )
    _view, without_key = _memory_row(
        mock_page, mock_api_client, Memory(id="m2", content="b", metadata=None)
    )

    assert with_key.content_text.tooltip == "fact_key: user.city"
    assert not without_key.content_text.tooltip


# ----------------------------------------------------------- memory: new memory
def _memory_view_with_dialog(mock_page, mock_api_client) -> MemoryView:
    view = MemoryView(mock_page, mock_api_client)
    view.on_new_click(None)
    return view


@pytest.mark.asyncio
async def test_new_memory_dialog_creates_an_active_memory_and_reloads(
    mock_page, mock_api_client
):
    view = _memory_view_with_dialog(mock_page, mock_api_client)
    mock_page.open.assert_called_once_with(view.new_dialog)
    mock_api_client.create_memory = AsyncMock(
        return_value=Memory(id="m9", content="x", status="active", importance=7)
    )
    view.new_content_field.value = "  Предпочитам кратки отговори.  "
    view.new_importance.value = "7"

    with patch("views.memory.show_snackbar") as snackbar:
        await view.on_new_save(None)

    mock_api_client.create_memory.assert_awaited_once_with(
        content="Предпочитам кратки отговори.", status="active", importance=7
    )
    mock_page.close.assert_called_once_with(view.new_dialog)
    # The table is re-read, so the status filter decides what is shown.
    mock_api_client.list_memories.assert_awaited_once()
    snackbar.assert_called_once()
    assert view._creating is False


def test_new_memory_dialog_starts_empty_with_the_default_importance(
    mock_page, mock_api_client
):
    view = MemoryView(mock_page, mock_api_client)
    view.new_content_field.value = "left over"
    view.new_importance.value = "9"

    view.on_new_click(None)

    assert view.new_content_field.value == ""
    assert view.new_importance.value == "5"


@pytest.mark.asyncio
async def test_new_memory_needs_some_text(mock_page, mock_api_client):
    view = _memory_view_with_dialog(mock_page, mock_api_client)
    view.new_content_field.value = "   "

    await view.on_new_save(None)

    mock_api_client.create_memory.assert_not_called()
    assert view.new_content_field.error_text
    mock_page.close.assert_not_called()


@pytest.mark.parametrize(
    ("text", "is_sent"),
    [
        ("я" * 16000, True),  # 32000 bytes: exactly the limit
        ("a" * 32000, True),
        ("я" * 16000 + "a", False),  # one byte over
        ("я" * 20000, False),  # 20000 letters, but 40000 bytes
        ("a" * 32001, False),
    ],
    ids=["cyrillic-at-limit", "latin-at-limit", "one-byte-over", "cyrillic-over", "latin-over"],
)
@pytest.mark.asyncio
async def test_new_memory_is_checked_against_the_backend_byte_limit(
    mock_page, mock_api_client, text, is_sent
):
    view = _memory_view_with_dialog(mock_page, mock_api_client)
    mock_api_client.create_memory = AsyncMock(
        return_value=Memory(id="m9", content="x", status="active", importance=5)
    )
    view.new_content_field.value = text

    with patch("views.memory.show_snackbar"):
        await view.on_new_save(None)

    if is_sent:
        mock_api_client.create_memory.assert_awaited_once()
        mock_page.close.assert_called_once_with(view.new_dialog)
    else:
        # The dialog says what is wrong instead of sending a doomed request.
        mock_api_client.create_memory.assert_not_called()
        mock_page.close.assert_not_called()
        assert "байта" in view.new_content_field.error_text
        assert str(len(text.encode("utf-8"))) in view.new_content_field.error_text
    assert view._creating is False
    assert view.new_save_btn.disabled is False


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (APIError(409, '{"id":"m1"}'), "Такъв спомен вече съществува."),
        (
            APIError(400, "content must be at most 32000 characters"),
            "content must be at most 32000 characters",
        ),
        (httpx.ConnectError("refused"), "Бекендът не е достъпен."),
        (RuntimeError("boom"), "Неуспешно запазване на спомена."),
    ],
    ids=["duplicate", "rejected", "unreachable", "unexpected"],
)
@pytest.mark.asyncio
async def test_new_memory_failure_keeps_the_dialog_open(
    mock_page, mock_api_client, error, expected
):
    view = _memory_view_with_dialog(mock_page, mock_api_client)
    mock_api_client.create_memory = AsyncMock(side_effect=error)
    view.new_content_field.value = "a fact"

    await view.on_new_save(None)

    assert view.new_content_field.error_text == expected
    mock_page.close.assert_not_called()
    mock_api_client.list_memories.assert_not_called()
    # The user can fix the text and try again.
    assert view._creating is False
    assert view.new_save_btn.disabled is False


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




