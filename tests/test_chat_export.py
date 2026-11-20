"""Taking a conversation out of the chat: its Markdown text, the clipboard, a file."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import flet as ft
import pytest

from api.models import Message
from state.settings import Settings, save_settings
from views.chat import HISTORY_PAGE_SIZE, ChatView
from views.conversation_export import (
    ExportedMessage,
    conversation_markdown,
    export_file_name,
    message_count_text,
    sources_line,
)


def local(*args) -> datetime:
    return datetime(*args).astimezone()


EXPORTED_AT = local(2026, 11, 20, 14, 5)
SESSION = "46f4da29-1111-4222-8333-444455556666"


# ------------------------------------------------------------- the text
def test_the_text_has_a_heading_the_time_of_export_and_every_message():
    messages = [
        ExportedMessage("Ти", "Здравей", local(2026, 11, 20, 14, 2)),
        ExportedMessage(
            "Лили",
            "Здравей и на теб!",
            local(2026, 11, 20, 14, 3),
            model="☁ deepseek-flash",
        ),
    ]

    assert conversation_markdown("Чат 1", messages, EXPORTED_AT) == (
        "# Чат 1\n\n"
        "Експортиран на 20.11.2026 14:05 · 2 съобщения\n\n"
        "---\n\n"
        "**Ти** · 20.11.2026 14:02\n\n"
        "Здравей\n\n"
        "---\n\n"
        "**Лили** · 20.11.2026 14:03 · ☁ deepseek-flash\n\n"
        "Здравей и на теб!\n"
    )


def test_a_message_without_a_time_has_only_its_sender():
    text = conversation_markdown("Чат", [ExportedMessage("Ти", "x")], EXPORTED_AT)

    assert "\n**Ти**\n\nx\n" in text


def test_the_sources_are_named_once_with_the_number_of_passages():
    sources = [
        {"title": "a.pdf"},
        {"file_name": "b.pdf"},
        {"title": "a.pdf"},
        "просто текст",
    ]

    assert sources_line(sources) == "a.pdf (2), b.pdf, просто текст"
    text = conversation_markdown(
        "Чат", [ExportedMessage("Лили", "Отговор", sources=tuple(sources))], EXPORTED_AT
    )
    assert "*Източници: a.pdf (2), b.pdf, просто текст*" in text


def test_an_unfinished_answer_says_so():
    text = conversation_markdown(
        "Чат",
        [ExportedMessage("Лили", "Половин отговор", unfinished=True)],
        EXPORTED_AT,
    )

    assert "Половин отговор\n\n*Отговорът не е завършен.*" in text


def test_a_truncated_history_says_so():
    messages = [ExportedMessage("Ти", "x")]

    assert "по-стари съобщения" in conversation_markdown(
        "Чат", messages, EXPORTED_AT, truncated=True
    )
    assert "по-стари съобщения" not in conversation_markdown(
        "Чат", messages, EXPORTED_AT
    )


def test_the_count_is_in_the_right_number():
    assert message_count_text(1) == "1 съобщение"
    assert message_count_text(2) == "2 съобщения"
    assert message_count_text(0) == "0 съобщения"


@pytest.mark.parametrize(
    ("title", "name"),
    [
        ("Чат 1", "Чат 1 2026-11-20.md"),
        ('Рим: "лято" / 2026?', "Рим лято  2026 2026-11-20.md"),
        ("con", "con 2026-11-20.md"),
        ("  ...  ", "Разговор 2026-11-20.md"),
        ("", "Разговор 2026-11-20.md"),
        ("я" * 100, "я" * 60 + " 2026-11-20.md"),
    ],
)
def test_the_file_name_is_safe_on_windows(title, name):
    assert export_file_name(title, EXPORTED_AT) == name


# --------------------------------------------------------------- the view
def make_view(mock_page, mock_api_client) -> ChatView:
    save_settings(
        Settings(
            user_id="user-1",
            session_id=SESSION,
            chat_sessions=[{"id": SESSION, "name": "Пътуване до Рим"}],
        )
    )
    view = ChatView(mock_page, mock_api_client)
    view.update = MagicMock()
    view.scroll_to_bottom = MagicMock()
    return view


def talk(view: ChatView) -> None:
    """A conversation of two messages on the screen."""
    view.add_message(
        "user", "Какво да видя?", notify=False, created_at=local(2026, 11, 20, 14, 2)
    )
    answer = view.add_message(
        "assistant",
        "Колизеума.",
        sources=[{"title": "guide.pdf"}],
        notify=False,
        created_at=local(2026, 11, 20, 14, 3),
    )
    answer.set_model({"provider": "local"})


def test_the_chat_menu_offers_the_copy_and_the_file(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)

    assert view.copy_item in view.more_btn.items
    assert view.export_item in view.more_btn.items
    assert view.copy_item.on_click == view.on_copy_conversation_click
    assert view.export_item.on_click == view.on_export_click


@pytest.mark.asyncio
async def test_copy_puts_the_whole_conversation_on_the_clipboard(
    mock_page, mock_api_client, clipboard
):
    view = make_view(mock_page, mock_api_client)
    talk(view)

    with patch("views.chat_export.show_snackbar") as snackbar:
        await view.on_copy_conversation_click()

    (copied,) = clipboard
    assert copied.startswith("# Пътуване до Рим\n\nЕкспортиран на ")
    assert "· 2 съобщения" in copied
    assert "**Ти** · 20.11.2026 14:02\n\nКакво да видя?" in copied
    assert "**Лили** · 20.11.2026 14:03 · 🏠 на този компютър\n\nКолизеума." in copied
    assert "*Източници: guide.pdf*" in copied
    assert snackbar.call_args.args[1] == "Разговорът е копиран."


@pytest.mark.asyncio
async def test_a_failed_copy_says_so(mock_page, mock_api_client):
    view = make_view(mock_page, mock_api_client)
    talk(view)

    with (
        patch("views.chat_export.copy_text", new=AsyncMock(return_value=False)),
        patch("views.chat_export.show_snackbar") as snackbar,
    ):
        await view.on_copy_conversation_click()

    assert snackbar.call_args.args[1] == "Не успях да копирам."


@pytest.mark.asyncio
async def test_an_empty_chat_has_nothing_to_take_out(
    mock_page, mock_api_client, clipboard
):
    view = make_view(mock_page, mock_api_client)
    view.file_picker.save_file = AsyncMock()

    with patch("views.chat_export.show_snackbar") as snackbar:
        await view.on_copy_conversation_click()
        await view.on_export_click()

    assert clipboard == []
    view.file_picker.save_file.assert_not_called()
    assert snackbar.call_count == 2
    assert "още няма съобщения" in snackbar.call_args.args[1]


@pytest.mark.asyncio
async def test_a_message_that_has_no_text_yet_is_left_out(
    mock_page, mock_api_client, clipboard
):
    view = make_view(mock_page, mock_api_client)
    talk(view)
    view.add_message("assistant", "", notify=False)

    with patch("views.chat_export.show_snackbar"):
        await view.on_copy_conversation_click()

    assert "· 2 съобщения" in clipboard[0]


@pytest.mark.asyncio
async def test_nothing_is_taken_while_an_answer_is_written(
    mock_page, mock_api_client, clipboard
):
    view = make_view(mock_page, mock_api_client)
    talk(view)
    view._is_streaming = True
    view.file_picker.save_file = AsyncMock()

    with patch("views.chat_export.show_snackbar") as snackbar:
        await view.on_copy_conversation_click()
        await view.on_export_click()

    assert clipboard == []
    view.file_picker.save_file.assert_not_called()
    assert "Изчакай края на отговора" in snackbar.call_args.args[1]


def test_the_menu_entries_are_off_while_an_answer_is_written(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client)

    view._set_streaming_ui(True)
    assert view.copy_item.disabled is True and view.export_item.disabled is True
    view._set_streaming_ui(False)
    assert view.copy_item.disabled is False and view.export_item.disabled is False


@pytest.mark.asyncio
async def test_an_answer_that_was_cut_off_is_marked(
    mock_page, mock_api_client, clipboard
):
    view = make_view(mock_page, mock_api_client)
    view.add_message("user", "Въпрос", notify=False)
    cut = view.add_message("assistant", "Половин", notify=False)
    cut.finalize_with_error("Нещо се обърка.")

    with patch("views.chat_export.show_snackbar"):
        await view.on_copy_conversation_click()

    assert "Половин\n\n*Отговорът не е завършен.*" in clipboard[0]


@pytest.mark.asyncio
async def test_a_long_history_says_that_older_messages_are_not_in_it(
    mock_page, mock_api_client, clipboard
):
    mock_api_client.get_messages.return_value = [
        Message(role="user", content=f"m{i}") for i in range(HISTORY_PAGE_SIZE + 1)
    ]
    view = make_view(mock_page, mock_api_client)
    await view.load_history()

    with patch("views.chat_export.show_snackbar"):
        await view.on_copy_conversation_click()

    assert "Има и по-стари съобщения" in clipboard[0]


# ----------------------------------------------------------------- the file
@pytest.mark.asyncio
async def test_export_asks_for_a_markdown_file_and_hands_the_text_over(
    mock_page, mock_api_client
):
    view = make_view(mock_page, mock_api_client)
    talk(view)
    view.file_picker.save_file = AsyncMock(return_value="C:/Users/me/Рим.md")

    with (
        patch("views.chat_export.now", return_value=EXPORTED_AT),
        patch("views.chat_export.show_snackbar") as snackbar,
    ):
        await view.on_export_click()

    kwargs = view.file_picker.save_file.call_args.kwargs
    assert kwargs["file_name"] == "Пътуване до Рим 2026-11-20.md"
    assert kwargs["file_type"] == ft.FilePickerFileType.CUSTOM
    assert kwargs["allowed_extensions"] == ["md"]
    text = kwargs["src_bytes"].decode("utf-8")
    assert text.startswith("# Пътуване до Рим\n\nЕкспортиран на 20.11.2026 14:05")
    assert "Колизеума." in text
    assert snackbar.call_args.args[1] == "Разговорът е запазен."


@pytest.mark.asyncio
async def test_closing_the_dialog_saves_nothing_and_says_nothing(
    mock_page, mock_api_client, caplog
):
    view = make_view(mock_page, mock_api_client)
    talk(view)
    view.file_picker.save_file = AsyncMock(return_value=None)

    with patch("views.chat_export.show_snackbar") as snackbar, caplog.at_level("INFO"):
        await view.on_export_click()

    snackbar.assert_not_called()
    assert "saved to a file" not in caplog.text


@pytest.mark.asyncio
async def test_a_failed_save_is_told_and_logged_without_the_path(
    mock_page, mock_api_client, caplog
):
    view = make_view(mock_page, mock_api_client)
    talk(view)
    view.file_picker.save_file = AsyncMock(
        side_effect=PermissionError(13, "Access denied", "C:\\Users\\me\\Рим.md")
    )

    with patch("views.chat_export.show_snackbar") as snackbar, caplog.at_level("ERROR"):
        await view.on_export_click()

    assert "Разговорът не беше запазен" in snackbar.call_args.args[1]
    assert "PermissionError (errno 13)" in caplog.text
    assert "Рим.md" not in caplog.text
    assert "C:\\Users" not in caplog.text


@pytest.mark.asyncio
async def test_the_log_has_only_the_size_of_what_was_saved(
    mock_page, mock_api_client, caplog
):
    view = make_view(mock_page, mock_api_client)
    talk(view)
    view.file_picker.save_file = AsyncMock(return_value="C:/Users/me/Рим.md")

    with patch("views.chat_export.show_snackbar"), caplog.at_level("INFO"):
        await view.on_export_click()

    # The first 8 characters of the session id, like everywhere in the log.
    assert "Chat 46f4da29 saved to a file (2 messages," in caplog.text
    for private in ("Колизеума", "Какво да видя", "Рим.md", "C:/Users", SESSION):
        assert private not in caplog.text
