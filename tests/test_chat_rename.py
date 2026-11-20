"""Renaming a chat: only the name changes, never the id the conversation is kept under."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import flet as ft

from state.chat_sessions import MAX_CHAT_NAME_CHARS, rename_chat_session
from state.settings import Settings, load_settings, save_settings
from views.chat import ChatView

CHAT_1 = "11111111-1111-1111-1111-111111111111"
CHAT_2 = "22222222-2222-2222-2222-222222222222"


def settings_with_two_chats(active: str = CHAT_1) -> Settings:
    return Settings(
        user_id="user-1",
        session_id=active,
        chat_sessions=[
            {"id": CHAT_1, "name": "Чат 1"},
            {"id": CHAT_2, "name": "Чат 2"},
        ],
        cloud_modes={CHAT_1: "cloud"},
    )


# -------------------------------------------------------------- the state
def test_a_rename_changes_the_name_and_nothing_else():
    settings = settings_with_two_chats()

    assert rename_chat_session(settings, CHAT_1, "Пътуване до Рим") == "Пътуване до Рим"

    assert settings.chat_sessions == [
        {"id": CHAT_1, "name": "Пътуване до Рим"},
        {"id": CHAT_2, "name": "Чат 2"},
    ]
    assert settings.session_id == CHAT_1
    assert settings.cloud_modes == {CHAT_1: "cloud"}


def test_renaming_another_chat_keeps_the_open_one_open():
    settings = settings_with_two_chats(active=CHAT_1)

    rename_chat_session(settings, CHAT_2, "Работа")

    assert settings.session_id == CHAT_1
    assert settings.chat_sessions[1] == {"id": CHAT_2, "name": "Работа"}


def test_blanks_are_collapsed_and_the_name_is_cut():
    settings = settings_with_two_chats()

    assert rename_chat_session(settings, CHAT_1, "  две   думи \n") == "две думи"
    long_name = rename_chat_session(settings, CHAT_1, "я" * 100)
    assert long_name == "я" * MAX_CHAT_NAME_CHARS


def test_a_name_that_another_chat_has_gets_a_number():
    settings = settings_with_two_chats()

    assert rename_chat_session(settings, CHAT_1, "Чат 2") == "Чат 2 (2)"


def test_keeping_the_own_name_is_not_a_clash():
    settings = settings_with_two_chats()

    assert rename_chat_session(settings, CHAT_1, "Чат 1") == "Чат 1"


def test_a_blank_name_or_an_unknown_chat_changes_nothing():
    settings = settings_with_two_chats()
    before = settings.snapshot()

    assert rename_chat_session(settings, CHAT_1, "   ") is None
    assert rename_chat_session(settings, "no-such-chat", "Име") is None

    assert settings == before


# ------------------------------------------------------------------ the view
def make_view(mock_page, mock_api_client) -> ChatView:
    save_settings(settings_with_two_chats())
    view = ChatView(mock_page, mock_api_client)
    view.update = MagicMock()
    view.scroll_to_bottom = MagicMock()
    mock_page.run_task.reset_mock()
    return view


def open_rename_dialog(view: ChatView, mock_page):
    view.on_rename_click()
    dialog = mock_page.show_dialog.call_args.args[0]
    assert isinstance(dialog, ft.AlertDialog)
    field = dialog.content.content
    save, cancel = dialog.actions[1].on_click, dialog.actions[0].on_click
    return dialog, field, save, cancel


def names(view: ChatView) -> list[str]:
    return [option.text for option in view.session_dropdown.options]


def test_the_chat_menu_offers_a_rename(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)

    assert view.rename_item in view.more_btn.items
    assert view.rename_item.on_click == view.on_rename_click


def test_the_dialog_starts_with_the_name_of_the_open_chat(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)

    _dialog, field, _save, _cancel = open_rename_dialog(view, mock_page)

    assert field.value == "Чат 1"
    assert field.max_length == MAX_CHAT_NAME_CHARS


def test_saving_renames_the_chat_on_the_screen_and_in_the_file(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client)
    dialog, field, save, _cancel = open_rename_dialog(view, mock_page)

    field.value = "Пътуване до Рим"
    save(None)

    assert names(view) == ["Пътуване до Рим", "Чат 2"]
    assert view.session_dropdown.value == CHAT_1
    assert dialog.open is False
    saved = load_settings()
    assert saved.chat_sessions[0] == {"id": CHAT_1, "name": "Пътуване до Рим"}
    assert saved.session_id == CHAT_1


def test_enter_in_the_field_saves_too(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)
    _dialog, field, save, _cancel = open_rename_dialog(view, mock_page)

    assert field.on_submit == save


def test_a_blank_name_keeps_the_dialog_open_and_says_why(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)
    dialog, field, save, _cancel = open_rename_dialog(view, mock_page)

    field.value = "   "
    save(None)

    assert field.error == "Въведи име на чата."
    assert dialog.open is True
    assert names(view) == ["Чат 1", "Чат 2"]


def test_cancel_changes_nothing(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)
    dialog, field, _save, cancel = open_rename_dialog(view, mock_page)

    field.value = "Друго"
    cancel(None)

    assert dialog.open is False
    assert names(view) == ["Чат 1", "Чат 2"]
    assert load_settings().chat_sessions[0]["name"] == "Чат 1"


def test_a_name_that_is_taken_is_saved_with_a_number_and_the_user_is_told(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client)
    _dialog, field, save, _cancel = open_rename_dialog(view, mock_page)

    field.value = "Чат 2"
    with patch("views.chat.show_snackbar") as snackbar:
        save(None)

    assert names(view) == ["Чат 2 (2)", "Чат 2"]
    assert "Чат 2 (2)" in snackbar.call_args.args[1]


def test_when_the_settings_cannot_be_saved_nothing_changes(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)
    _dialog, field, save, _cancel = open_rename_dialog(view, mock_page)

    field.value = "Ново име"
    with (
        patch("views.chat.save_settings", return_value=False),
        patch("views.chat.show_snackbar") as snackbar,
    ):
        save(None)

    assert names(view) == ["Чат 1", "Чат 2"]
    assert view.settings.chat_sessions[0]["name"] == "Чат 1"
    assert "запазване на настройките" in snackbar.call_args.args[1]


def test_the_log_says_that_a_chat_was_renamed_but_not_its_name(
    mock_page, mock_api_client, caplog
):
    view = make_view(mock_page, mock_api_client)
    _dialog, field, save, _cancel = open_rename_dialog(view, mock_page)

    field.value = "Лична тайна"
    with caplog.at_level("INFO"):
        save(None)

    assert "was renamed" in caplog.text
    assert "Лична тайна" not in caplog.text
    assert CHAT_1 not in caplog.text
