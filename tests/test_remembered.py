"""The line "Лили си спомни" under an answer planned from the person's memories.

The backend tells which memories a planned answer used: ``event: plan`` in the stream
(only when the request asks with ``plan_events``) and ``remembered`` on the message of
the history. The app shows them quietly under the answer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.models import Message
from api.stream import Remembered, TextChunk, parse_frame
from state.settings import Settings, save_settings
from tests.helpers import mounted
from views.chat import ChatView
from views.chat_message import REMEMBERED_PREFIX, ChatMessage

CHAT = "11111111-1111-1111-1111-111111111111"


# ------------------------------------------------------------------ the frame
def test_the_plan_frame_gives_the_memories():
    frame = '{"remembered": ["почива на дивана", "  ", 5, "работи на смени"]}'
    assert parse_frame("plan", frame) == [
        Remembered(["почива на дивана", "работи на смени"])
    ]


@pytest.mark.parametrize(
    "data", ["not json", "[]", '{"remembered": "диванът"}', '{"remembered": []}', "{}"]
)
def test_a_plan_frame_this_client_cannot_read_is_skipped(data):
    assert parse_frame("plan", data) == []


# ---------------------------------------------------------------- the message
def test_an_answer_with_memories_shows_the_line():
    message = ChatMessage("assistant", "Почини си.", remembered=["почива на дивана"])
    assert message.remembered_label.visible
    assert message.remembered_label.value == REMEMBERED_PREFIX + "почива на дивана"
    assert message.remembered_label.italic


def test_the_line_is_hidden_without_memories_and_for_the_user():
    assert not ChatMessage("assistant", "Здравей.").remembered_label.visible
    assert not ChatMessage(
        "assistant", "x", remembered=[" ", ""]
    ).remembered_label.visible
    user = ChatMessage("user", "уморен съм", remembered=["почива на дивана"])
    assert not user.remembered_label.visible


def test_the_stream_can_fill_the_line_later():
    message = ChatMessage("assistant", "")
    message.set_remembered(["почива на дивана", "работи на смени"])
    assert message.remembered_label.value == (
        REMEMBERED_PREFIX + "почива на дивана · работи на смени"
    )


# ------------------------------------------------------------------- the chat
def make_view(mock_page, mock_api_client) -> ChatView:
    save_settings(
        Settings(
            user_id="user-1",
            session_id=CHAT,
            chat_sessions=[{"id": CHAT, "name": "Чат"}],
        )
    )
    view = ChatView(mock_page, mock_api_client)
    view.update = MagicMock()
    view.scroll_to_bottom = MagicMock()
    return view


@pytest.mark.asyncio
async def test_the_stream_shows_the_memories_under_the_answer(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client)

    async def stream(**_kwargs):
        yield TextChunk("Почини си.")
        yield Remembered(["почива на дивана"])

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: stream(**kw))
    view.input_field.value = "уморен съм"
    await view.send_message_click(None)

    answer = view._last_assistant_msg
    assert answer.text_value == "Почини си.", "the frame is not shown as text"
    assert answer.remembered_label.value == REMEMBERED_PREFIX + "почива на дивана"


@pytest.mark.asyncio
async def test_the_history_keeps_the_line(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)
    mock_api_client.get_messages = AsyncMock(
        return_value=[
            Message(role="user", content="уморен съм"),
            Message(
                role="assistant", content="Почини си.", remembered=["почива на дивана"]
            ),
        ]
    )

    with mounted(ChatView):
        await view.load_history()

    question, answer = [row.controls[0].content for row in view.chat_list.controls]
    assert not question.remembered_label.visible
    assert answer.remembered_label.visible
    assert answer.remembered_label.value == REMEMBERED_PREFIX + "почива на дивана"
