"""A link in an answer names its host before it is opened (P20)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from helpers import mounted

from views.chat_message import ChatMessage
from views.links import SHOWN_URL_CHARS, ascii_host, link_host, link_question, shown_url


# ------------------------------------------------------------------ the host
@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://example.com/page?x=1#top", "example.com"),
        ("HTTPS://Example.COM:8443/a", "example.com"),
        ("http://127.0.0.1:8080/", "127.0.0.1"),
        ("http://[::1]:8080/", "::1"),
        ("  https://example.com  ", "example.com"),
        # Text before the "@" is a user name, not the site.
        ("https://bank.example@evil.example/login", "evil.example"),
        ("https://bank.example:secret@evil.example:99/", "evil.example"),
    ],
)
def test_the_host_is_the_one_the_browser_reaches(url, host):
    assert link_host(url) == host


@pytest.mark.parametrize("url", ["https://", "http:///path", "http://[::1", "", "x"])
def test_an_address_without_a_readable_host_has_none(url):
    assert link_host(url) is None


def test_a_name_with_other_letters_is_shown_as_the_network_knows_it():
    # The first letter is Cyrillic "а", not Latin "a".
    assert ascii_host("аpple.com") == "xn--pple-43d.com"
    assert ascii_host("example.com") == "example.com"


def test_a_name_that_cannot_be_encoded_is_left_as_it_is():
    too_long_label = "а" * 100 + ".com"
    assert ascii_host(too_long_label) == too_long_label


# ------------------------------------------------------------ the question
def test_the_question_names_the_host_and_shows_the_address():
    question = link_question("https://example.com/a/b?c=d")

    assert question.startswith("Връзката води към example.com.")
    assert "https://example.com/a/b?c=d" in question
    assert "@" not in question
    assert "xn--" not in question


def test_a_user_name_in_front_of_the_host_is_pointed_out():
    question = link_question("https://bank.example@evil.example/login")

    assert question.startswith("Връзката води към evil.example.")
    assert "https://bank.example@evil.example/login" in question
    assert "Сайтът е evil.example" in question


def test_letters_that_look_like_latin_ones_are_pointed_out():
    question = link_question("https://аpple.com/id")

    assert "xn--pple-43d.com" in question
    assert "необичайни букви" in question


def test_a_very_long_address_is_cut_but_the_host_is_still_named_first():
    url = "https://example.com/" + "a" * 1000

    question = link_question(url)

    assert question.startswith("Връзката води към example.com.")
    assert shown_url(url).endswith("…")
    assert len(shown_url(url)) == SHOWN_URL_CHARS
    assert url not in question


def test_a_short_address_is_shown_whole():
    assert shown_url("  https://example.com/x  ") == "https://example.com/x"


def test_there_is_no_question_for_a_link_without_a_host():
    with pytest.raises(ValueError, match="no host|without a host"):
        link_question("https://")


# -------------------------------------------------- what a click does
@pytest.fixture
def opened():
    """The addresses ``ChatMessage`` handed to the system."""
    urls: list[str] = []

    async def record(_page, url):
        urls.append(url)
        return True

    with patch("views.chat_message.open_url", new=record):
        yield urls


def tap(message: ChatMessage, url: str):
    return message.md.on_tap_link(SimpleNamespace(data=url))


@pytest.mark.asyncio
async def test_a_web_link_asks_first_and_opens_on_yes(mock_page, opened):
    message = ChatMessage("assistant", "текст", page=mock_page)
    confirm = AsyncMock(return_value=True)

    with patch("views.chat_message.confirm_dialog", new=confirm):
        await tap(message, "https://bank.example@evil.example/login")

    confirm.assert_awaited_once()
    args, kwargs = confirm.await_args
    assert args[0] is mock_page
    assert args[1] == "Отваряне на връзка"
    assert "Връзката води към evil.example." in args[2]
    # Not a question about losing something: the button is not red.
    assert kwargs == {"confirm_text": "Отвори", "destructive": False}
    assert opened == ["https://bank.example@evil.example/login"]


@pytest.mark.asyncio
async def test_a_web_link_is_not_opened_on_no(mock_page, opened):
    message = ChatMessage("assistant", "текст", page=mock_page)

    with patch("views.chat_message.confirm_dialog", new=AsyncMock(return_value=False)):
        await tap(message, "https://example.com/")

    assert opened == []


@pytest.mark.asyncio
async def test_a_mail_link_opens_without_a_question(mock_page, opened):
    message = ChatMessage("assistant", "текст", page=mock_page)
    confirm = AsyncMock(return_value=False)

    with patch("views.chat_message.confirm_dialog", new=confirm):
        await tap(message, "mailto:someone@example.com")

    confirm.assert_not_called()
    assert opened == ["mailto:someone@example.com"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://", "http://[::1", "http:///x"])
async def test_a_web_link_without_a_host_is_refused_quietly(
    mock_page, opened, caplog, url
):
    message = ChatMessage("assistant", "текст", page=mock_page)
    confirm = AsyncMock(return_value=True)

    with patch("views.chat_message.confirm_dialog", new=confirm):
        await tap(message, url)

    confirm.assert_not_called()
    assert opened == []
    assert "no host" in caplog.text


@pytest.mark.asyncio
async def test_the_question_really_opens_a_dialog_and_waits_for_the_answer(
    mock_page, opened
):
    message = ChatMessage("assistant", "текст", page=mock_page)

    with mounted(ChatMessage):
        clicking = asyncio.ensure_future(tap(message, "https://example.com/"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # The dialog is up and nothing was opened yet.
        (dialog,) = [c.args[0] for c in mock_page.show_dialog.call_args_list]
        assert dialog.modal is True
        assert opened == []

        confirm_button = dialog.actions[1]
        assert confirm_button.content == "Отвори"
        confirm_button.on_click(None)
        await clicking

    assert opened == ["https://example.com/"]
