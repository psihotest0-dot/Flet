from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import flet as ft
import pytest

from views.chat import ChatView
from views.emoji_picker import (
    DEFAULT_CATEGORY,
    EMOJI_CATEGORIES,
    SELECTED_CATEGORY_BGCOLOR,
    UNSELECTED_CATEGORY_BGCOLOR,
    EmojiPicker,
)


def click(cell):
    cell.on_click(SimpleNamespace(control=cell))


def shown(picker):
    return [cell.data for cell in picker.grid.controls]


@pytest.fixture
def picked():
    return []


@pytest.fixture
def picker(picked):
    return EmojiPicker(on_pick=picked.append)


@pytest.fixture
def chat_view(mock_page, mock_api_client):
    view = ChatView(mock_page, mock_api_client)
    view.update = MagicMock()
    return view


# ------------------------------------------------------------------- data
@pytest.mark.parametrize("name", list(EMOJI_CATEGORIES))
def test_category_has_unique_emoji_and_a_button_emoji(name):
    icon, emojis = EMOJI_CATEGORIES[name]
    assert emojis
    assert len(emojis) == len(set(emojis))
    assert all(isinstance(emoji, str) and emoji.strip() for emoji in emojis)
    assert icon in emojis


# ----------------------------------------------------------------- picker
def test_picker_starts_hidden_on_the_first_category(picker):
    assert picker.visible is False
    assert picker.selected_category == DEFAULT_CATEGORY
    assert shown(picker) == EMOJI_CATEGORIES[DEFAULT_CATEGORY][1]


def test_clicking_an_emoji_reports_it(picker, picked):
    click(picker.grid.controls[0])
    click(picker.grid.controls[1])
    first, second = EMOJI_CATEGORIES[DEFAULT_CATEGORY][1][:2]
    assert picked == [first, second]


def test_selecting_a_category_swaps_the_grid_and_the_highlight(picker):
    picker.select_category("Жестове")

    assert picker.selected_category == "Жестове"
    assert shown(picker) == EMOJI_CATEGORIES["Жестове"][1]
    assert picker.category_buttons["Жестове"].bgcolor == SELECTED_CATEGORY_BGCOLOR
    assert (
        picker.category_buttons[DEFAULT_CATEGORY].bgcolor
        == UNSELECTED_CATEGORY_BGCOLOR
    )


def test_clicking_a_category_button_selects_it(picker):
    click(picker.category_buttons["Жестове"])
    assert picker.selected_category == "Жестове"


def test_an_emoji_of_another_category_can_be_picked(picker, picked):
    picker.select_category("Жестове")
    click(picker.grid.controls[0])
    assert picked == [EMOJI_CATEGORIES["Жестове"][1][0]]


def test_unknown_category_is_ignored(picker):
    picker.select_category("Няма такава")
    assert picker.selected_category == DEFAULT_CATEGORY
    assert shown(picker) == EMOJI_CATEGORIES[DEFAULT_CATEGORY][1]


def test_cells_are_built_once_per_category(picker):
    picker.select_category("Жестове")
    first_cell = picker.grid.controls[0]
    picker.select_category(DEFAULT_CATEGORY)
    picker.select_category("Жестове")
    assert picker.grid.controls[0] is first_cell


# ------------------------------------------------------------------- chat
def test_chat_has_an_emoji_button_next_to_the_input(chat_view):
    input_row = chat_view.content.controls[-1]
    assert chat_view.emoji_btn in input_row.controls
    assert chat_view.emoji_picker in chat_view.content.controls


def test_emoji_button_opens_and_closes_the_picker(chat_view):
    assert chat_view.emoji_picker.visible is False
    closed_icon = chat_view.emoji_btn.icon

    chat_view.toggle_emoji_picker()
    assert chat_view.emoji_picker.visible is True
    assert chat_view.emoji_btn.icon != closed_icon

    chat_view.toggle_emoji_picker()
    assert chat_view.emoji_picker.visible is False
    assert chat_view.emoji_btn.icon == closed_icon


def test_opening_the_picker_keeps_the_end_of_the_chat_in_view(chat_view, mock_page):
    chat_view.toggle_emoji_picker()
    mock_page.run_task.assert_called_once_with(chat_view._scroll_after_layout)


def test_picked_emoji_is_added_to_the_message(chat_view):
    chat_view.input_field.value = "Здравей "
    chat_view.insert_emoji("👋")
    chat_view.insert_emoji("🙂")
    assert chat_view.input_field.value == "Здравей 👋🙂"


def test_emoji_can_start_an_empty_message(chat_view):
    chat_view.input_field.value = None
    chat_view.insert_emoji("👍")
    assert chat_view.input_field.value == "👍"


def test_clicking_in_the_chat_picker_fills_the_input(chat_view):
    cell = chat_view.emoji_picker.grid.controls[0]
    click(cell)
    assert chat_view.input_field.value == cell.data


def test_input_keeps_focus_after_picking_when_mounted(chat_view):
    chat_view.input_field.focus = MagicMock()
    with patch.object(
        ft.TextField, "uid", new_callable=PropertyMock, return_value="uid-1"
    ):
        chat_view.insert_emoji("👍")
    chat_view.input_field.focus.assert_called_once_with()


def test_detached_input_is_not_focused(chat_view):
    chat_view.input_field.focus = MagicMock()
    chat_view.insert_emoji("👍")
    chat_view.input_field.focus.assert_not_called()
    assert chat_view.input_field.value == "👍"
