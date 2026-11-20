"""An empty chat says how to begin; the header of a message stands over its bubble."""

from __future__ import annotations

from unittest.mock import MagicMock

import flet as ft
import pytest

from api.models import Message
from state.settings import Settings, save_settings
from views.chat import EMPTY_CHAT_SUGGESTIONS, ChatView
from views.chat_message import ChatMessage

CHAT_1 = "11111111-1111-1111-1111-111111111111"
CHAT_2 = "22222222-2222-2222-2222-222222222222"


def make_view(mock_page, mock_api_client) -> ChatView:
    save_settings(
        Settings(
            user_id="user-1",
            session_id=CHAT_1,
            chat_sessions=[
                {"id": CHAT_1, "name": "Чат 1"},
                {"id": CHAT_2, "name": "Чат 2"},
            ],
        )
    )
    view = ChatView(mock_page, mock_api_client)
    view.update = MagicMock()
    view.scroll_to_bottom = MagicMock()
    mock_page.run_task.reset_mock()
    return view


def chips(view: ChatView) -> list[ft.Chip]:
    column = view.empty_state.content
    (row,) = [c for c in column.controls if isinstance(c, ft.Row)]
    return row.controls


def test_the_empty_chat_is_not_shown_before_the_history_is_known(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client)

    # Else a chat that has a history would flash "begin a conversation" while it loads.
    assert view.empty_state.visible is False


@pytest.mark.asyncio
async def test_a_history_without_messages_shows_how_to_begin(
    mock_page, mock_api_client
):
    mock_api_client.get_messages.return_value = []
    view = make_view(mock_page, mock_api_client)

    await view.load_history()

    assert view.empty_state.visible is True


@pytest.mark.asyncio
async def test_a_history_with_messages_does_not(mock_page, mock_api_client):
    mock_api_client.get_messages.return_value = [Message(role="user", content="x")]
    view = make_view(mock_page, mock_api_client)

    await view.load_history()

    assert view.empty_state.visible is False


@pytest.mark.asyncio
async def test_a_failed_history_does_not_claim_that_the_chat_is_empty(
    mock_page, mock_api_client
):
    mock_api_client.get_messages.side_effect = RuntimeError("boom")
    view = make_view(mock_page, mock_api_client)

    await view.load_history()

    assert view.empty_state.visible is False


@pytest.mark.asyncio
async def test_the_first_message_takes_it_away(mock_page, mock_api_client):
    mock_api_client.get_messages.return_value = []
    view = make_view(mock_page, mock_api_client)
    await view.load_history()

    view.add_message("user", "Здравей", notify=False)

    assert view.empty_state.visible is False


@pytest.mark.asyncio
async def test_a_new_chat_has_nothing_to_wait_for_and_shows_it_at_once(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client)
    view.add_message("user", "Здравей", notify=False)

    await view.on_new_chat_click(None)

    assert view.empty_state.visible is True
    assert view.chat_list.controls == []


@pytest.mark.asyncio
async def test_opening_a_chat_waits_for_its_history_before_saying_it_is_empty(
    mock_page, mock_api_client
):
    mock_api_client.get_messages.return_value = []
    view = make_view(mock_page, mock_api_client)
    await view.load_history()
    assert view.empty_state.visible is True

    view._activate_session(CHAT_2)

    assert view.empty_state.visible is False


def test_there_is_an_example_for_each_suggestion(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)

    assert [chip.label.value for chip in chips(view)] == [
        text.strip() for text in EMPTY_CHAT_SUGGESTIONS
    ]


@pytest.mark.parametrize("index", range(len(EMPTY_CHAT_SUGGESTIONS)))
def test_an_example_goes_into_the_field_and_is_not_sent(
    mock_page, mock_api_client, index
):
    view = make_view(mock_page, mock_api_client)

    chips(view)[index].on_click(None)

    assert view.input_field.value == EMPTY_CHAT_SUGGESTIONS[index]
    assert view._input_text == EMPTY_CHAT_SUGGESTIONS[index]
    mock_api_client.stream_chat.assert_not_called()
    assert view._is_streaming is False


# ---------------------------------------------- the header of a message
def test_the_header_of_my_message_stands_over_my_bubble():
    mine = ChatMessage("user", "x")
    hers = ChatMessage("assistant", "x")

    assert mine.top_row.alignment == ft.MainAxisAlignment.END
    assert hers.top_row.alignment == ft.MainAxisAlignment.START
