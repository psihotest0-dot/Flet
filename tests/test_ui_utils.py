from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import flet as ft
import httpx
import pytest
from helpers import mounted, real_session

from api.client import APIError, StreamError
from views.ui_utils import (
    close_dialog,
    confirm_dialog,
    copy_text,
    describe_error,
    error_detail,
    is_mounted,
    open_dialog,
    open_url,
    show_snackbar,
)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout(""),
        httpx.RemoteProtocolError("dropped"),
    ],
)
def test_error_detail_hides_the_connection_jargon(error):
    assert error_detail(error) == "бекендът не е достъпен"


def test_error_detail_keeps_the_text_of_the_backend():
    assert error_detail(APIError(400, "importance must be between 1 and 10")) == (
        "importance must be between 1 and 10"
    )


def test_error_detail_never_ends_in_an_empty_text():
    assert error_detail(ValueError("file too large")) == "file too large"
    assert error_detail(RuntimeError()) == "RuntimeError"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ConnectError("refused"), "Бекендът не е достъпен"),
        (APIError(503, "down"), "Грешка в бекенда (HTTP 503)"),
        (APIError(400, "bad"), "Заявката е отхвърлена: bad"),
        (StreamError("x"), "Нещо се обърка"),
        (RuntimeError("boom"), "Грешка при получаване на отговора"),
    ],
)
def test_describe_error_tells_the_failures_apart(error, expected):
    assert expected in describe_error(error)


# ----------------------------------------------------------------- is_mounted
def test_a_control_that_never_reached_a_page_is_not_mounted():
    assert is_mounted(ft.Text("x")) is False


def test_the_page_itself_is_mounted(mock_page):
    assert is_mounted(mock_page) is True


def test_a_control_the_page_knows_is_mounted():
    with mounted(ft.Text):
        assert is_mounted(ft.Text("x")) is True


def test_a_control_taken_out_of_the_page_is_not_mounted():
    # It keeps its parent, so ``control.page`` still answers - but the session
    # has dropped its id, and a patch for it would address a control the client
    # no longer has.
    forgetful = SimpleNamespace(get_control=lambda control_id: None)
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            ft.Text, "page", property(lambda self: forgetful), raising=False
        )
        assert is_mounted(ft.Text("x")) is False


def test_a_control_of_a_closed_window_is_not_mounted():
    def gone(control_id):
        raise RuntimeError("An attempt to fetch destroyed session.")

    closed = SimpleNamespace(get_control=gone)
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(ft.Text, "page", property(lambda self: closed), raising=False)
        assert is_mounted(ft.Text("x")) is False


# The same three answers from Flet itself, not from a stand-in for its page.
@pytest.mark.asyncio
async def test_flet_agrees_about_a_control_that_was_added_and_then_removed():
    session, _connection = real_session()
    page = session.page
    row = ft.Row([ft.Text("x")])
    text = row.controls[0]
    assert (is_mounted(row), is_mounted(text)) == (False, False)

    page.add(row)
    assert (is_mounted(row), is_mounted(text)) == (True, True)

    page.remove(row)
    # Both still reach the page through their parents ...
    assert row.page is page
    assert text.page is page
    # ... but it has forgotten them.
    assert (is_mounted(row), is_mounted(text)) == (False, False)


@pytest.mark.asyncio
async def test_safe_update_sends_a_patch_only_for_what_is_on_the_page():
    from views.ui_utils import safe_update

    session, connection = real_session()
    page = session.page
    shown, removed, never = ft.Text("a"), ft.Text("b"), ft.Text("c")
    page.add(shown, removed)
    page.remove(removed)
    connection.sent.clear()

    for control in (shown, removed, never):
        control.value = "changed"
    safe_update(page, shown, removed, never)

    patched = [message.body.id for message in connection.sent]
    assert patched == [shown._i]


@pytest.mark.asyncio
async def test_a_dialog_is_mounted_while_it_is_shown_and_closing_it_reaches_the_client():
    session, connection = real_session()
    page = session.page
    dialog = ft.AlertDialog(title=ft.Text("x"))

    open_dialog(page, dialog)
    assert is_mounted(dialog) is True
    connection.sent.clear()

    close_dialog(dialog)

    assert dialog.open is False
    assert [message.body.id for message in connection.sent] == [dialog._i]


# -------------------------------------------------------------------- dialogs
def test_open_dialog_shows_it_on_the_page(mock_page):
    dialog = ft.AlertDialog(title=ft.Text("x"))

    open_dialog(mock_page, dialog)

    mock_page.show_dialog.assert_called_once_with(dialog)
    assert dialog.open is True


def test_open_dialog_without_a_page_does_nothing():
    open_dialog(None, ft.AlertDialog(title=ft.Text("x")))


def test_a_dialog_that_is_still_closing_is_logged_not_raised(mock_page, caplog):
    # Flet refuses to show a dialog whose last close the client has not
    # reported yet; raised out of a click handler it would end the session.
    mock_page.show_dialog.side_effect = RuntimeError("Dialog is already opened")

    open_dialog(mock_page, ft.AlertDialog(title=ft.Text("x")))

    assert "The dialog was not opened: Dialog is already opened" in caplog.text


def test_close_dialog_closes_that_dialog_and_tells_the_client():
    with mounted(ft.AlertDialog):
        dialog = ft.AlertDialog(title=ft.Text("x"), open=True)
        dialog.update = MagicMock()

        close_dialog(dialog)

    assert dialog.open is False
    dialog.update.assert_called_once_with()


def test_close_dialog_never_closes_whatever_is_on_top(mock_page):
    # page.pop_dialog() would take the snackbar that was shown over the dialog.
    dialog = ft.AlertDialog(title=ft.Text("x"))
    open_dialog(mock_page, dialog)
    show_snackbar(mock_page, "над диалога")

    close_dialog(dialog)

    mock_page.pop_dialog.assert_not_called()
    assert dialog.open is False


def test_close_dialog_of_a_dialog_that_is_not_on_the_page_sends_nothing():
    dialog = ft.AlertDialog(title=ft.Text("x"), open=True)
    dialog.update = MagicMock()

    close_dialog(dialog)

    assert dialog.open is False
    dialog.update.assert_not_called()


@pytest.mark.parametrize(
    ("success", "color"),
    [(False, ft.Colors.RED), (True, ft.Colors.GREEN)],
    ids=["error", "success"],
)
def test_snackbar_is_red_for_an_error_and_green_for_a_success(
    mock_page, success, color
):
    show_snackbar(mock_page, "текст", success=success)

    bar = mock_page.show_dialog.call_args.args[0]
    assert isinstance(bar, ft.SnackBar)
    assert bar.content.value == "текст"
    assert bar.bgcolor == color


def test_snackbar_without_a_page_does_nothing():
    show_snackbar(None, "текст")


async def _answer(
    mock_page, button_index: int, **kwargs
) -> tuple[bool, ft.AlertDialog]:
    """Ask, press the button with that index, give the answer and the dialog."""
    question = asyncio.ensure_future(
        confirm_dialog(mock_page, "Заглавие", "Въпрос?", **kwargs)
    )
    await asyncio.sleep(0)
    dialog = mock_page.show_dialog.call_args.args[0]
    assert dialog.open is True
    dialog.actions[button_index].on_click(None)
    return await question, dialog


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("button", "answer"), [(0, False), (1, True)], ids=["cancel", "confirm"]
)
async def test_confirm_dialog_answers_with_the_button_that_was_pressed(
    mock_page, button, answer
):
    result, dialog = await _answer(mock_page, button)

    assert result is answer
    assert dialog.open is False
    assert dialog.modal is True
    assert [action.content for action in dialog.actions] == ["Отказ", "Изтрий"]


@pytest.mark.asyncio
async def test_confirm_dialog_marks_only_a_destructive_answer_in_red(mock_page):
    _result, destructive = await _answer(mock_page, 0)
    _result, harmless = await _answer(
        mock_page, 0, confirm_text="Прикачи", destructive=False
    )

    assert destructive.actions[1].style.color == ft.Colors.RED
    assert harmless.actions[1].style is None


@pytest.mark.asyncio
async def test_confirm_dialog_without_a_page_is_a_no():
    assert await confirm_dialog(None, "Заглавие", "Въпрос?") is False


# ------------------------------------------------------- clipboard and links
@pytest.mark.asyncio
async def test_copy_text_puts_the_text_on_the_clipboard(mock_page, clipboard):
    assert await copy_text(mock_page, "първо") is True
    assert await copy_text(mock_page, "второ") is True

    assert clipboard == ["първо", "второ"]


@pytest.mark.asyncio
async def test_one_clipboard_service_serves_a_page(mock_page, monkeypatch):
    # Flet registers every new service with the client; one per copy would be
    # a registration and a removal for each click.
    made = []

    class CountedClipboard:
        def __init__(self):
            made.append(self)

        async def set(self, value):
            pass

    monkeypatch.setattr(ft, "Clipboard", CountedClipboard)

    await copy_text(mock_page, "a")
    await copy_text(mock_page, "b")

    assert len(made) == 1


@pytest.mark.asyncio
async def test_a_copy_that_failed_is_reported_as_false_and_logged(
    mock_page, monkeypatch, caplog
):
    class BrokenClipboard:
        async def set(self, value):
            raise RuntimeError("Session closed")

    monkeypatch.setattr(ft, "Clipboard", BrokenClipboard)

    assert await copy_text(mock_page, "текст") is False
    assert "Could not copy to the clipboard: Session closed" in caplog.text
    # What was to be copied is the user's own text: it is not in the log.
    assert "текст" not in caplog.text


@pytest.mark.asyncio
async def test_copy_text_without_a_page_is_false():
    assert await copy_text(None, "текст") is False


class FakeLauncher:
    opened: list[str] = []  # noqa: RUF012

    async def launch_url(self, url):
        FakeLauncher.opened.append(url)


@pytest.fixture
def launcher(monkeypatch):
    FakeLauncher.opened = []
    monkeypatch.setattr(ft, "UrlLauncher", FakeLauncher)
    return FakeLauncher.opened


@pytest.mark.asyncio
async def test_open_url_hands_the_address_to_the_system(mock_page, launcher):
    assert await open_url(mock_page, "https://example.com/a?b=1") is True

    assert launcher == ["https://example.com/a?b=1"]


@pytest.mark.asyncio
async def test_a_link_that_did_not_open_is_false_and_logged(
    mock_page, monkeypatch, caplog
):
    class BrokenLauncher:
        async def launch_url(self, url):
            raise RuntimeError("no browser")

    monkeypatch.setattr(ft, "UrlLauncher", BrokenLauncher)

    assert await open_url(mock_page, "https://example.com") is False
    assert "Could not open the link: no browser" in caplog.text


@pytest.mark.asyncio
async def test_open_url_without_a_page_is_false(launcher):
    assert await open_url(None, "https://example.com") is False
    assert launcher == []


# ------------------------------------------------------- links in a message
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/page",
        "http://example.com",
        "mailto:someone@example.com",
        "  HTTPS://Example.com  ",
    ],
    ids=["https", "http", "mailto", "padded-upper-case"],
)
async def test_a_web_or_mail_link_in_an_answer_is_opened(mock_page, launcher, url):
    from views.chat_message import ChatMessage

    message = ChatMessage("assistant", "текст", page=mock_page)

    await message.md.on_tap_link(SimpleNamespace(data=url))

    assert launcher == [url.strip()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///C:/Windows/system32/calc.exe",
        "ms-settings:",
        "",
        None,
    ],
    ids=["javascript", "file", "app-scheme", "empty", "missing"],
)
async def test_any_other_link_in_an_answer_is_refused(mock_page, launcher, caplog, url):
    # What the model writes may come from a web page or a book: a link must
    # never start a program or open a local file.
    from views.chat_message import ChatMessage

    message = ChatMessage("assistant", "текст", page=mock_page)

    await message.md.on_tap_link(SimpleNamespace(data=url))

    assert launcher == []
    assert "Blocked non-http link" in caplog.text
