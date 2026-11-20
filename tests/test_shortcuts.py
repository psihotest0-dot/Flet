"""The keyboard shortcuts of the chat: the key table, the chat's reaction, the wiring."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import flet as ft
import pytest
from helpers import tab_view

import main as main_module
from state.settings import Settings, save_settings
from views.chat import ChatView
from views.shortcuts import NEW_CHAT, RENAME, STOP, shortcut_action
from views.ui_utils import close_dialog, has_open_dialog, open_dialog


@pytest.fixture
def start_app(monkeypatch, mock_page):
    """Run ``main.main`` against a mock page and return the Tabs it mounts."""
    monkeypatch.setattr(main_module, "setup_logging", lambda: None)

    def start() -> ft.Tabs:
        main_module.main(mock_page)
        return mock_page.add.call_args.args[0]

    return start


def key(name, ctrl=False, shift=False, alt=False, meta=False):
    """A Flet ``KeyboardEvent``, as far as the chat reads it."""
    return SimpleNamespace(key=name, ctrl=ctrl, shift=shift, alt=alt, meta=meta)


# ----------------------------------------------------------------- the table
@pytest.mark.parametrize(
    ("name", "ctrl", "action"),
    [
        ("Escape", False, STOP),
        ("F2", False, RENAME),
        ("N", True, NEW_CHAT),
        ("n", True, NEW_CHAT),
        # The key of the Latin N is the Cyrillic letter on a Bulgarian layout.
        ("Н", True, NEW_CHAT),
        ("н", True, NEW_CHAT),
    ],
)
def test_the_keys_of_the_chat(name, ctrl, action):
    assert shortcut_action(name, ctrl=ctrl) == action


@pytest.mark.parametrize(
    "event",
    [
        key("N"),  # typing the letter n
        key("Escape", ctrl=True),
        key("F2", shift=True),
        key("N", ctrl=True, shift=True),
        key("N", ctrl=True, alt=True),
        key("N", ctrl=True, meta=True),
        key("C", ctrl=True),  # copies the selection: not ours
        key("S", ctrl=True),
        key("Enter"),
        key(""),
    ],
)
def test_other_keys_and_other_modifiers_mean_nothing(event):
    assert (
        shortcut_action(event.key, event.ctrl, event.shift, event.alt, event.meta)
        is None
    )


# ------------------------------------------------------------------ the chat
def make_view(mock_page, mock_api_client) -> ChatView:
    save_settings(
        Settings(
            user_id="user-1",
            session_id="s1",
            chat_sessions=[{"id": "s1", "name": "Чат 1"}],
        )
    )
    view = ChatView(mock_page, mock_api_client)
    view.update = MagicMock()
    view.scroll_to_bottom = MagicMock()
    mock_page.run_task.reset_mock()
    return view


def test_escape_stops_an_answer_that_is_being_written(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)
    view._is_streaming = True

    assert view.handle_key(key("Escape")) is True

    mock_page.run_task.assert_called_once_with(view.stop_generation_click, None)


def test_escape_with_nothing_to_stop_is_not_taken(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)

    assert view.handle_key(key("Escape")) is False

    mock_page.run_task.assert_not_called()


def test_ctrl_n_starts_a_new_chat(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)

    assert view.handle_key(key("N", ctrl=True)) is True

    mock_page.run_task.assert_called_once_with(view.on_new_chat_click, None)


def test_f2_asks_for_a_new_name(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)

    assert view.handle_key(key("F2")) is True

    dialog = mock_page.show_dialog.call_args.args[0]
    assert isinstance(dialog, ft.AlertDialog)
    assert dialog.content.content.value == "Чат 1"


def test_an_ordinary_key_is_left_to_the_field(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)

    assert view.handle_key(key("A")) is False

    mock_page.run_task.assert_not_called()
    mock_page.show_dialog.assert_not_called()


def test_the_keys_are_in_the_tooltips(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)

    assert "Ctrl+N" in view.new_chat_btn.tooltip
    assert "Esc" in view.stop_btn.tooltip
    assert "F2" in view.rename_item.content


# -------------------------------------------------------------- open dialogs
def test_a_dialog_that_is_open_is_known(mock_page):
    dialog = ft.AlertDialog(title=ft.Text("x"))

    assert has_open_dialog(mock_page) is False
    open_dialog(mock_page, dialog)
    assert has_open_dialog(mock_page) is True

    close_dialog(dialog)
    assert has_open_dialog(mock_page) is False


def test_a_snackbar_is_not_a_dialog_in_the_way(mock_page):
    open_dialog(mock_page, ft.SnackBar(ft.Text("Готово")))

    assert has_open_dialog(mock_page) is False


def test_a_bottom_sheet_is_in_the_way(mock_page):
    sheet = ft.BottomSheet(content=ft.Text("x"))

    open_dialog(mock_page, sheet)

    assert has_open_dialog(mock_page) is True


def test_a_dialog_that_was_refused_is_not_counted(mock_page):
    mock_page.show_dialog.side_effect = RuntimeError("Dialog is already opened")

    open_dialog(mock_page, ft.AlertDialog(title=ft.Text("x")))

    assert has_open_dialog(mock_page) is False


def test_no_page_has_no_dialog():
    assert has_open_dialog(None) is False


# ------------------------------------------------------------------- wiring
def test_the_app_listens_to_the_keyboard_and_gives_the_chat_its_keys(
    start_app, mock_page
):
    save_settings(
        Settings(
            user_id="user-1",
            session_id="s1",
            chat_sessions=[{"id": "s1", "name": "Чат 1"}],
        )
    )
    tabs = start_app()
    assert isinstance(tab_view(tabs, 0), ChatView)
    tabs.selected_index = 0

    with patch.object(ChatView, "handle_key") as handle:
        event = key("F2")
        mock_page.on_keyboard_event(event)

    handle.assert_called_once_with(event)


def test_the_keys_are_ignored_on_another_tab(start_app, mock_page):
    tabs = start_app()
    tabs.selected_index = 2

    with patch.object(ChatView, "handle_key") as handle:
        mock_page.on_keyboard_event(key("F2"))

    handle.assert_not_called()


def test_the_keys_are_ignored_while_a_dialog_is_open(start_app, mock_page):
    tabs = start_app()
    tabs.selected_index = 0
    open_dialog(mock_page, ft.AlertDialog(title=ft.Text("Изтриване")))

    with patch.object(ChatView, "handle_key") as handle:
        mock_page.on_keyboard_event(key("Escape"))

    handle.assert_not_called()
