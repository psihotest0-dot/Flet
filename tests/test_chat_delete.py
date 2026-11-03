from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from state.settings import Settings, load_settings, save_settings
from views.chat import ChatView, _remove_chat_session

CHAT_1 = "11111111-1111-1111-1111-111111111111"
CHAT_2 = "22222222-2222-2222-2222-222222222222"
CHAT_3 = "33333333-3333-3333-3333-333333333333"


def seed(session_id: str, chats: tuple[str, ...] = (CHAT_1, CHAT_2)) -> None:
    save_settings(
        Settings(
            user_id="user-1",
            session_id=session_id,
            chat_sessions=[
                {"id": chat, "name": f"Чат {number}"}
                for number, chat in enumerate(chats, start=1)
            ],
        )
    )


def make_view(mock_page, mock_api_client, **kwargs) -> ChatView:
    view = ChatView(mock_page, mock_api_client, **kwargs)
    view.update = MagicMock()
    view.scroll_to_bottom = MagicMock()
    mock_page.run_task.reset_mock()
    mock_api_client.reset_mock()
    return view


def confirming(answer: bool):
    return patch("views.chat.confirm_dialog", new=AsyncMock(return_value=answer))


def dropdown_keys(view: ChatView) -> list[str]:
    return [option.key for option in view.session_dropdown.options]


# ------------------------------------------------------------- the button
def test_chat_has_a_delete_button_next_to_new_chat(mock_page, mock_api_client):
    seed(CHAT_1)
    view = make_view(mock_page, mock_api_client)
    buttons = view.top_row.controls[-1].controls
    assert buttons == [view.delete_chat_btn, view.new_chat_btn]


def test_delete_button_is_disabled_while_an_answer_streams(mock_page, mock_api_client):
    seed(CHAT_1)
    view = make_view(mock_page, mock_api_client)

    view._set_streaming_ui(True)
    assert view.delete_chat_btn.disabled is True
    view._set_streaming_ui(False)
    assert view.delete_chat_btn.disabled is False


# -------------------------------------------------------------- deleting
@pytest.mark.asyncio
async def test_delete_opens_the_neighbouring_chat(mock_page, mock_api_client):
    seed(CHAT_1)
    changes: list[str] = []
    view = make_view(mock_page, mock_api_client, on_session_changed=changes.append)

    with confirming(True) as dialog:
        await view.on_delete_chat_click(None)

    assert dialog.await_count == 1
    assert view.session_id == CHAT_2
    assert dropdown_keys(view) == [CHAT_2]
    assert view.session_dropdown.value == CHAT_2
    assert changes == [CHAT_2]
    mock_page.run_task.assert_called_with(view.load_history)

    saved = load_settings()
    assert saved.session_id == CHAT_2
    assert saved.chat_sessions == [{"id": CHAT_2, "name": "Чат 2"}]


@pytest.mark.asyncio
async def test_deleting_the_last_chat_of_the_list_opens_the_previous_one(
    mock_page, mock_api_client
):
    seed(CHAT_3, chats=(CHAT_1, CHAT_2, CHAT_3))
    view = make_view(mock_page, mock_api_client)

    with confirming(True):
        await view.on_delete_chat_click(None)

    assert view.session_id == CHAT_2
    assert dropdown_keys(view) == [CHAT_1, CHAT_2]


@pytest.mark.asyncio
async def test_deleting_a_middle_chat_opens_the_one_that_took_its_place(
    mock_page, mock_api_client
):
    seed(CHAT_2, chats=(CHAT_1, CHAT_2, CHAT_3))
    view = make_view(mock_page, mock_api_client)

    with confirming(True):
        await view.on_delete_chat_click(None)

    assert view.session_id == CHAT_3
    assert dropdown_keys(view) == [CHAT_1, CHAT_3]


@pytest.mark.asyncio
async def test_deleting_the_only_chat_starts_a_new_one(mock_page, mock_api_client):
    seed(CHAT_1, chats=(CHAT_1,))
    view = make_view(mock_page, mock_api_client)

    with confirming(True):
        await view.on_delete_chat_click(None)

    assert view.session_id != CHAT_1
    assert uuid.UUID(view.session_id)
    saved = load_settings()
    assert saved.session_id == view.session_id
    assert saved.chat_sessions == [{"id": view.session_id, "name": "Чат 1"}]
    assert dropdown_keys(view) == [view.session_id]
    # The new chat has no history to fetch.
    mock_page.run_task.assert_not_called()


@pytest.mark.asyncio
async def test_the_removed_chat_is_archived_so_its_id_is_not_lost(
    mock_page, mock_api_client
):
    seed(CHAT_1)
    view = make_view(mock_page, mock_api_client)

    with confirming(True):
        await view.on_delete_chat_click(None)

    assert load_settings().archived_chats == [{"id": CHAT_1, "name": "Чат 1"}]


@pytest.mark.asyncio
async def test_the_backend_is_never_asked_to_delete_anything(mock_page, mock_api_client):
    seed(CHAT_1)
    view = make_view(mock_page, mock_api_client)

    with confirming(True):
        await view.on_delete_chat_click(None)

    # cancel_stream only stops the local request that was still running.
    called = {name for name, _args, _kwargs in mock_api_client.method_calls}
    assert called <= {"cancel_stream"}


@pytest.mark.asyncio
async def test_a_deleted_chat_does_not_come_back_on_the_next_start(
    mock_page, mock_api_client
):
    seed(CHAT_1)
    view = make_view(mock_page, mock_api_client)
    with confirming(True):
        await view.on_delete_chat_click(None)

    reopened = make_view(mock_page, mock_api_client)

    assert reopened.session_id == CHAT_2
    assert dropdown_keys(reopened) == [CHAT_2]
    assert load_settings().archived_chats == [{"id": CHAT_1, "name": "Чат 1"}]


# ----------------------------------------------------------- not deleting
@pytest.mark.asyncio
async def test_declining_the_dialog_changes_nothing(mock_page, mock_api_client):
    seed(CHAT_1)
    before = load_settings()
    view = make_view(mock_page, mock_api_client)

    with confirming(False):
        await view.on_delete_chat_click(None)

    assert view.session_id == CHAT_1
    assert dropdown_keys(view) == [CHAT_1, CHAT_2]
    assert load_settings() == before
    mock_page.run_task.assert_not_called()


@pytest.mark.asyncio
async def test_a_chat_that_is_still_answering_is_not_deleted(mock_page, mock_api_client):
    seed(CHAT_1)
    before = load_settings()
    view = make_view(mock_page, mock_api_client)
    view._is_streaming = True

    with confirming(True) as dialog, patch("views.chat.show_snackbar") as snackbar:
        await view.on_delete_chat_click(None)

    dialog.assert_not_awaited()
    snackbar.assert_called_once()
    assert view.session_id == CHAT_1
    assert load_settings() == before


@pytest.mark.asyncio
async def test_failed_save_leaves_the_screen_as_it_was(mock_page, mock_api_client):
    seed(CHAT_1)
    before = load_settings()
    view = make_view(mock_page, mock_api_client)

    with (
        confirming(True),
        patch("views.chat.save_settings", return_value=False),
        patch("views.chat.show_snackbar") as snackbar,
    ):
        await view.on_delete_chat_click(None)

    snackbar.assert_called_once()
    assert view.session_id == CHAT_1
    assert dropdown_keys(view) == [CHAT_1, CHAT_2]
    assert load_settings() == before


# ------------------------------------------------------- the settings side
def test_removing_a_chat_that_is_not_listed_changes_nothing():
    settings = Settings(
        session_id=CHAT_1, chat_sessions=[{"id": CHAT_1, "name": "Чат 1"}]
    )

    assert _remove_chat_session(settings, CHAT_2) is None
    assert settings.chat_sessions == [{"id": CHAT_1, "name": "Чат 1"}]
    assert settings.archived_chats == []


def test_archiving_the_same_chat_twice_keeps_one_entry():
    settings = Settings(
        session_id=CHAT_1,
        chat_sessions=[{"id": CHAT_1, "name": "Чат 1"}, {"id": CHAT_2, "name": "Чат 2"}],
        archived_chats=[{"id": CHAT_1, "name": "Стар чат"}],
    )

    assert _remove_chat_session(settings, CHAT_1) == (CHAT_2, False)
    assert settings.archived_chats == [{"id": CHAT_1, "name": "Чат 1"}]


def test_archived_chats_survive_saving(mock_page):
    save_settings(
        Settings(
            user_id="user-1",
            session_id=CHAT_1,
            chat_sessions=[{"id": CHAT_1, "name": "Чат 1"}],
            archived_chats=[{"id": CHAT_2, "name": "Чат 2"}],
        )
    )
    assert load_settings().archived_chats == [{"id": CHAT_2, "name": "Чат 2"}]


def test_a_settings_file_from_before_archiving_still_loads(isolated_settings):
    isolated_settings.write_text(
        json.dumps({"user_id": "user-1", "session_id": CHAT_1}), encoding="utf-8"
    )

    settings = load_settings()

    assert settings.archived_chats == []
    assert (settings.user_id, settings.session_id) == ("user-1", CHAT_1)
