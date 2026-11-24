"""The buttons of the modes (2026-11-24): the menu in the place of the paperclip, the chip, the request."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from api.client import APIClient
from api.stream import TextChunk
from state.settings import Settings, save_settings
from views.chat import ChatView
from views.chat_modes import DEFAULT_HINT, MODE_HINTS, MODES

CHAT_1 = "11111111-1111-1111-1111-111111111111"
CHAT_2 = "22222222-2222-2222-2222-222222222222"
BACKEND_MODES = {"books", "web", "library", "files", "memory", "style"}


def seed() -> None:
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


def make_view(mock_page, mock_api_client) -> ChatView:
    view = ChatView(mock_page, mock_api_client)
    view.update = MagicMock()
    view.scroll_to_bottom = MagicMock()
    return view


async def send(view: ChatView, mock_api_client, text: str = "здравей") -> None:
    async def stream(**_kwargs):
        yield TextChunk("здрасти")

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: stream(**kw))
    view.input_field.value = text
    await view.send_message_click(None)


def test_the_modes_are_the_ones_the_backend_knows():
    assert {key for key, *_ in MODES} == BACKEND_MODES


def test_the_menu_has_the_paperclip_first_and_then_the_modes(
    mock_page, mock_api_client
):
    seed()
    view = make_view(mock_page, mock_api_client)
    labels = [item.content for item in view.mode_menu.items]
    assert labels == ["Прикачи файл"] + [label for _key, label, *_ in MODES]
    assert view.mode_menu.items[0].on_click == view.on_attach_click
    input_row = view.content.controls[-1]
    assert view.mode_menu in input_row.controls
    assert view.attach_btn not in input_row.controls, "the paperclip is in the menu"


def test_choosing_a_mode_shows_it_and_the_cross_takes_it_away(
    mock_page, mock_api_client
):
    seed()
    view = make_view(mock_page, mock_api_client)
    assert not view.mode_chip.visible
    assert view.input_field.hint_text == DEFAULT_HINT

    view.mode_menu.items[2].on_click(None)  # "Интернет"
    assert view.chat_mode == "web"
    assert view.mode_chip.visible
    assert "Интернет" in view.mode_chip_text.value
    assert view.input_field.hint_text == MODE_HINTS["web"]

    view.clear_mode()
    assert view.chat_mode is None
    assert not view.mode_chip.visible
    assert view.input_field.hint_text == DEFAULT_HINT


@pytest.mark.asyncio
async def test_the_mode_goes_with_one_message_only(mock_page, mock_api_client):
    seed()
    view = make_view(mock_page, mock_api_client)

    view.choose_mode("files")
    await send(view, mock_api_client, "запиши в бележки.md: купи мляко")
    assert mock_api_client.stream_chat.call_args.kwargs["mode"] == "files"
    assert view.chat_mode is None, "cleared by the send"
    assert not view.mode_chip.visible

    await send(view, mock_api_client)
    assert "mode" not in mock_api_client.stream_chat.call_args.kwargs


@pytest.mark.asyncio
async def test_another_conversation_has_no_mode(mock_page, mock_api_client):
    seed()
    view = make_view(mock_page, mock_api_client)
    view.choose_mode("books")
    view._reset_conversation()
    assert view.chat_mode is None
    assert not view.mode_chip.visible


@respx.mock
@pytest.mark.asyncio
async def test_the_client_sends_the_mode_only_when_there_is_one():
    client = APIClient(base_url="http://localhost:8080", user_id="u1")
    route = respx.post("http://localhost:8080/chat/stream").mock(
        return_value=httpx.Response(
            200,
            content=b"data: [DONE]\n\n",
            headers={"Content-Type": "text/event-stream"},
        )
    )
    _ = [x async for x in client.stream_chat("s", "c")]
    assert "mode" not in json.loads(route.calls.last.request.content)
    _ = [x async for x in client.stream_chat("s", "c", mode="memory")]
    assert json.loads(route.calls.last.request.content)["mode"] == "memory"
