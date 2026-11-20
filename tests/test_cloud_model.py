"""The choice of the model for a conversation (P47): the button, the request, who answered."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import flet as ft
import httpx
import pytest
import respx

from api.client import APIClient
from api.stream import ModelUsed, TextChunk
from state.chat_sessions import restart_chat_session
from state.settings import Settings, load_settings, save_settings
from views.chat import ChatView
from views.chat_message import ChatMessage

CHAT_1 = "11111111-1111-1111-1111-111111111111"
CHAT_2 = "22222222-2222-2222-2222-222222222222"


def seed(session_id: str = CHAT_1, modes: dict | None = None) -> None:
    save_settings(
        Settings(
            user_id="user-1",
            session_id=session_id,
            chat_sessions=[
                {"id": CHAT_1, "name": "Чат 1"},
                {"id": CHAT_2, "name": "Чат 2"},
            ],
            cloud_modes=modes or {},
        )
    )


def make_view(mock_page, mock_api_client, **kwargs) -> ChatView:
    view = ChatView(mock_page, mock_api_client, **kwargs)
    view.update = MagicMock()
    view.scroll_to_bottom = MagicMock()
    return view


async def send(view: ChatView, mock_api_client, text: str = "здравей") -> None:
    async def stream(**_kwargs):
        yield TextChunk("здрасти")

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: stream(**kw))
    view.input_field.value = text
    await view.send_message_click(None)


# ------------------------------------------------------------ the button
def test_a_new_conversation_follows_the_backend(mock_page, mock_api_client):
    seed()
    view = make_view(mock_page, mock_api_client)

    assert view._cloud_mode() == "auto"
    assert view._use_paid() is None
    assert view._chat_options() == {}


@pytest.mark.asyncio
async def test_the_button_goes_round_auto_cloud_home_auto(mock_page, mock_api_client):
    seed()
    view = make_view(mock_page, mock_api_client)

    seen = []
    for _ in range(4):
        await view.on_cloud_click(None)
        seen.append((view._cloud_mode(), view._use_paid()))

    assert seen == [("cloud", True), ("home", False), ("auto", None), ("cloud", True)]


@pytest.mark.asyncio
async def test_the_button_shows_the_mode_it_is_in(mock_page, mock_api_client):
    seed()
    view = make_view(mock_page, mock_api_client)
    shown = [(view.cloud_btn.content, view.cloud_btn.icon)]
    auto_tip = view.cloud_btn.tooltip

    await view.on_cloud_click(None)
    shown.append((view.cloud_btn.content, view.cloud_btn.icon))
    cloud_tip = view.cloud_btn.tooltip
    await view.on_cloud_click(None)
    shown.append((view.cloud_btn.content, view.cloud_btn.icon))

    assert len(set(shown)) == 3, "three modes, three pictures"
    assert shown[0] == ("auto", None), "auto says auto, not an empty cloud"
    assert shown[1] == (None, ft.Icons.CLOUD), "the cloud is a cloud"
    assert shown[2] == (None, ft.Icons.HOME)
    assert "автоматичния" not in auto_tip and "както е настроено" in auto_tip
    assert "облака" in cloud_tip
    assert "компютър" in view.cloud_btn.tooltip


@pytest.mark.asyncio
async def test_a_failed_save_leaves_the_mode_as_it_was(mock_page, mock_api_client):
    seed(modes={CHAT_1: "cloud"})
    view = make_view(mock_page, mock_api_client)

    with (
        patch("views.chat.save_settings", return_value=False),
        patch("views.chat.show_snackbar") as snackbar,
    ):
        await view.on_cloud_click(None)

    snackbar.assert_called_once()
    # The button still says "cloud", and so do the settings in memory.
    assert view._cloud_mode() == "cloud"
    assert view.settings.cloud_modes == {CHAT_1: "cloud"}
    assert load_settings().cloud_modes == {CHAT_1: "cloud"}


@pytest.mark.asyncio
async def test_the_view_works_on_the_settings_it_is_given(mock_page, mock_api_client):
    seed()
    shared = load_settings()
    view = make_view(mock_page, mock_api_client, settings=shared)

    await view.on_cloud_click(None)

    assert view.settings is shared
    # What the chat changed is seen by whoever else holds the object (main).
    assert shared.cloud_modes == {CHAT_1: "cloud"}


@pytest.mark.asyncio
async def test_no_handler_reads_the_settings_file_once_the_view_is_built(
    mock_page, mock_api_client, monkeypatch
):
    seed()
    view = make_view(mock_page, mock_api_client)

    def read_the_file():
        raise AssertionError("the settings file was read again")

    monkeypatch.setattr("views.chat.load_settings", read_the_file)

    await view.on_cloud_click(None)
    await view.on_new_chat_click(None)
    view.session_dropdown.value = CHAT_2
    await view.on_session_change(None)

    assert view.session_id == CHAT_2
    assert view._cloud_mode() == "auto"


@pytest.mark.asyncio
async def test_the_mode_is_kept_for_the_conversation_across_restarts(
    mock_page, mock_api_client
):
    seed()
    view = make_view(mock_page, mock_api_client)
    await view.on_cloud_click(None)

    assert load_settings().cloud_modes == {CHAT_1: "cloud"}
    again = make_view(mock_page, mock_api_client)
    assert again._cloud_mode() == "cloud"
    assert again.cloud_btn.icon == view.cloud_btn.icon


@pytest.mark.asyncio
async def test_each_conversation_has_its_own_mode(mock_page, mock_api_client):
    seed(modes={CHAT_2: "home"})
    view = make_view(mock_page, mock_api_client)
    await view.on_cloud_click(None)  # chat 1 -> cloud
    cloud_icon = view.cloud_btn.icon

    view._activate_session(CHAT_2, reload_history=False)

    assert view._cloud_mode() == "home"
    assert view.cloud_btn.icon != cloud_icon, "the button follows the open chat"
    view._activate_session(CHAT_1, reload_history=False)
    assert view._cloud_mode() == "cloud"


@pytest.mark.asyncio
async def test_back_to_auto_leaves_nothing_in_the_settings(mock_page, mock_api_client):
    seed(modes={CHAT_1: "home"})
    view = make_view(mock_page, mock_api_client)

    await view.on_cloud_click(None)  # home -> auto

    assert load_settings().cloud_modes == {}


# -------------------------------------------------------- the request
@pytest.mark.asyncio
async def test_a_message_in_auto_does_not_mention_the_choice(
    mock_page, mock_api_client
):
    seed()
    view = make_view(mock_page, mock_api_client)

    await send(view, mock_api_client)

    kwargs = mock_api_client.stream_chat.call_args.kwargs
    assert "use_paid" not in kwargs, "the request is what it was before the button"


@pytest.mark.parametrize(("mode", "sent"), [("cloud", True), ("home", False)])
@pytest.mark.asyncio
async def test_a_message_carries_the_choice(mock_page, mock_api_client, mode, sent):
    seed(modes={CHAT_1: mode})
    view = make_view(mock_page, mock_api_client)

    await send(view, mock_api_client)

    assert mock_api_client.stream_chat.call_args.kwargs["use_paid"] is sent


@pytest.mark.asyncio
async def test_the_regeneration_carries_the_choice_too(mock_page, mock_api_client):
    seed(modes={CHAT_1: "cloud"})
    view = make_view(mock_page, mock_api_client)
    await send(view, mock_api_client)

    async def stream(**_kwargs):
        yield TextChunk("ново")

    mock_api_client.regenerate_chat = MagicMock(side_effect=lambda **kw: stream(**kw))
    await view.regenerate_last()

    assert mock_api_client.regenerate_chat.call_args.kwargs["use_paid"] is True


@pytest.mark.asyncio
async def test_a_deleted_chat_keeps_its_mode_under_the_new_session():
    settings = Settings(
        user_id="user-1",
        session_id=CHAT_1,
        chat_sessions=[{"id": CHAT_1, "name": "Чат 1"}],
        cloud_modes={CHAT_1: "home"},
    )

    new_id = restart_chat_session(settings, CHAT_1)

    assert settings.cloud_modes == {new_id: "home"}


# ------------------------------------------------------ the settings
def test_unreadable_modes_in_the_settings_are_dropped():
    settings = Settings(cloud_modes={"a": "cloud", "b": "bogus", "": "home", "c": 5})
    assert settings.cloud_modes == {"a": "cloud"}
    assert Settings(cloud_modes="cloud").cloud_modes == {}
    assert Settings().cloud_modes == {}


# ------------------------------------------------ who answered
def test_a_message_shows_who_answered():
    message = ChatMessage("assistant", "")
    assert not message.model_label.visible, (
        "nothing is said while the backend says nothing"
    )

    message.set_model({"provider": "deepseek", "model": "deepseek-flash"})
    assert message.model_label.visible
    assert "deepseek-flash" in message.model_label.value
    assert "облак" in message.model_label.tooltip

    message.set_model({"provider": "local"})
    assert "компютър" in message.model_label.value


def test_an_unreadable_who_is_ignored():
    message = ChatMessage("assistant", "")
    for who in (None, "x", {}, {"provider": ""}, {"model": "m"}):
        message.set_model(who)
    assert not message.model_label.visible


def test_the_message_of_the_user_has_no_badge():
    message = ChatMessage("user", "здравей")
    message.set_model({"provider": "local"})
    assert not message.model_label.visible


@pytest.mark.asyncio
async def test_the_stream_marks_the_message_with_who_answered(
    mock_page, mock_api_client
):
    seed()
    view = make_view(mock_page, mock_api_client)

    async def stream(**_kwargs):
        yield TextChunk("здрасти")
        yield ModelUsed({"provider": "deepseek", "model": "deepseek-flash"})

    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: stream(**kw))
    view.input_field.value = "здравей"
    await view.send_message_click(None)

    assistant = view._last_assistant_msg
    assert assistant.text_value == "здрасти", "the frame is not shown as text"
    assert "deepseek-flash" in assistant.model_label.value


# ------------------------------------------------------ the client
@respx.mock
@pytest.mark.asyncio
async def test_the_client_sends_the_choice_only_when_there_is_one():
    client = APIClient(base_url="http://localhost:8080", user_id="u1")
    route = respx.post("http://localhost:8080/chat/stream").mock(
        return_value=httpx.Response(
            200,
            content=b"data: [DONE]\n\n",
            headers={"Content-Type": "text/event-stream"},
        )
    )
    for args, expected in [
        ({}, {"session_id": "s", "content": "c"}),
        ({"use_paid": True}, {"session_id": "s", "content": "c", "use_paid": True}),
        ({"use_paid": False}, {"session_id": "s", "content": "c", "use_paid": False}),
    ]:
        _ = [x async for x in client.stream_chat("s", "c", **args)]
        assert json.loads(route.calls.last.request.content) == expected

    regen = respx.post("http://localhost:8080/chat/regenerate").mock(
        return_value=httpx.Response(
            200,
            content=b"data: [DONE]\n\n",
            headers={"Content-Type": "text/event-stream"},
        )
    )
    _ = [x async for x in client.regenerate_chat("s", use_paid=False)]
    assert json.loads(regen.calls.last.request.content) == {
        "session_id": "s",
        "use_paid": False,
    }


@respx.mock
@pytest.mark.asyncio
async def test_the_client_hands_on_who_answered_and_ignores_a_bad_frame():
    client = APIClient(base_url="http://localhost:8080", user_id="u1")
    respx.post("http://localhost:8080/chat/stream").mock(
        return_value=httpx.Response(
            200,
            content=(
                b'data: {"content": "Hi"}\n\n'
                b'event: model\ndata: {"provider":"deepseek","model":"deepseek-flash"}\n\n'
                b"event: model\ndata: not json\n\n"
                b'event: model\ndata: ["x"]\n\n'
                b"data: [DONE]\n\n"
            ),
            headers={"Content-Type": "text/event-stream"},
        )
    )

    results = [x async for x in client.stream_chat("s", "c")]

    assert results == [
        TextChunk("Hi"),
        ModelUsed({"provider": "deepseek", "model": "deepseek-flash"}),
    ]
