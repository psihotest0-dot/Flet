"""The time of a message: how it is written, and that the chat shows it."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from api.models import Message
from api.stream import TextChunk
from state.settings import Settings, save_settings
from views.chat import ChatView
from views.chat_message import ChatMessage
from views.timeline import full_label, now, when_label


def local(*args) -> datetime:
    """A moment of this computer's own time, so the tests do not depend on its zone."""
    return datetime(*args).astimezone()


NOW = local(2026, 11, 20, 14, 5)


# ---------------------------------------------------------------- the labels
def test_a_message_of_today_shows_the_clock_only():
    assert when_label(local(2026, 11, 20, 9, 7), NOW) == "09:07"


def test_yesterday_is_named():
    assert when_label(local(2026, 11, 19, 21, 30), NOW) == "вчера 21:30"


def test_the_midnight_between_today_and_yesterday():
    assert when_label(local(2026, 11, 19, 23, 59), NOW) == "вчера 23:59"
    assert when_label(local(2026, 11, 20, 0, 1), NOW) == "00:01"


def test_an_older_message_of_this_year_has_its_day_and_month():
    assert when_label(local(2026, 11, 14, 9, 12), NOW) == "14 ноември, 09:12"
    assert when_label(local(2026, 1, 3, 18, 0), NOW) == "3 януари, 18:00"


def test_a_message_of_another_year_has_the_whole_date():
    assert when_label(local(2025, 12, 31, 23, 50), NOW) == "31.12.2025 23:50"


def test_the_time_of_the_backend_is_shown_in_the_time_of_this_computer():
    sent = datetime(2026, 11, 20, 12, 5, tzinfo=UTC)

    assert when_label(sent, sent) == f"{sent.astimezone():%H:%M}"


def test_the_full_label_has_the_whole_date_for_a_file():
    assert full_label(local(2026, 11, 20, 14, 5)) == "20.11.2026 14:05"


def test_now_knows_its_zone():
    assert now().tzinfo is not None


# ------------------------------------------------------- the message on screen
def test_a_message_with_a_time_shows_it_next_to_the_name(mock_page):
    with patch("views.timeline.now", return_value=NOW):
        message = ChatMessage("user", "здравей", created_at=local(2026, 11, 20, 9, 7))

    assert message.time_label.value == "09:07"
    assert message.time_label.visible is True
    top = message.top_row.controls
    assert top[0] is message.sender_label and top[1] is message.time_label


def test_a_message_without_a_time_does_not_guess_one():
    message = ChatMessage("assistant", "x")

    assert message.created_at is None
    assert message.time_label.visible is False
    assert message.time_label.value == ""


# ------------------------------------------------------------ in the chat
def make_view(mock_page, mock_api_client) -> ChatView:
    save_settings(Settings(user_id="user-1", session_id="s1"))
    view = ChatView(mock_page, mock_api_client)
    view.update = MagicMock()
    view.scroll_to_bottom = MagicMock()
    return view


def messages_of(view: ChatView) -> list[ChatMessage]:
    return [
        row.controls[0].content
        for row in view.chat_list.controls
        if isinstance(getattr(row.controls[0], "content", None), ChatMessage)
    ]


@pytest.mark.asyncio
async def test_the_history_shows_when_each_message_was_written(
    mock_page, mock_api_client
):
    mock_api_client.get_messages.return_value = [
        Message(role="user", content="a", created_at=local(2026, 11, 19, 21, 30)),
        Message(role="assistant", content="b", created_at=local(2026, 11, 20, 9, 7)),
        Message(role="user", content="c"),
    ]
    view = make_view(mock_page, mock_api_client)

    with patch("views.timeline.now", return_value=NOW):
        # The label is made when the message is built, so patch while it loads.
        await view.load_history()

    first, second, third = messages_of(view)
    assert first.time_label.value == "вчера 21:30"
    assert second.time_label.value == "09:07"
    assert third.time_label.visible is False


@pytest.mark.asyncio
async def test_a_message_sent_now_and_its_answer_get_the_time_they_began(
    mock_page, mock_api_client
):
    async def stream(**_kwargs):
        yield TextChunk("здрасти")

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: stream(**kw))
    view = make_view(mock_page, mock_api_client)
    view.input_field.value = "здравей"

    with (
        patch("views.chat_turns.now", return_value=local(2026, 11, 20, 14, 5)),
        patch("views.timeline.now", return_value=NOW),
    ):
        await view.send_message_click(None)

    question, answer = messages_of(view)
    assert question.time_label.value == "14:05"
    assert answer.time_label.value == "14:05"
