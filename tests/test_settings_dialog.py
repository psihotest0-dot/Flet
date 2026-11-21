from __future__ import annotations

import flet as ft
import httpx
import pytest
import respx

from views import theme
from views.settings_dialog import (
    INSECURE_URL_NOTE,
    SettingsDialog,
    clean_display_name,
    is_plain_http_to_another_computer,
    validate_base_url,
)

USER_ID = "123e4567-e89b-12d3-a456-426614174000"
SAVED_URL = "http://127.0.0.1:8080"


@pytest.fixture
def saved():
    """What ``on_save`` received, as ``(url, display_name)`` pairs."""
    return []


@pytest.fixture
def dialog(mock_page, saved):
    return SettingsDialog(
        mock_page,
        USER_ID,
        SAVED_URL,
        on_save=lambda url, name: saved.append((url, name)),
    )


# ----------------------------------------------------------------- validation
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("https://example.com", "https://example.com"),
        ("http://localhost", "http://localhost"),
        ("http://[::1]:8080", "http://[::1]:8080"),
        ("http://host:8080/", "http://host:8080"),
        ("http://host:8080/api//", "http://host:8080/api"),
        ("  http://localhost:8080  ", "http://localhost:8080"),
        ("HTTP://Localhost:8080", "HTTP://Localhost:8080"),
    ],
)
def test_valid_base_urls_are_normalised(raw, expected):
    assert validate_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        None,
        "127.0.0.1:8080",
        "localhost",
        "ftp://host:21",
        "ws://host:8080",
        "http://",
        "http:///path",
        "http://ho st:8080",
        "http://host:8080/a b",
        "http://host:99999",
        "http://host:port",
        "http://host:8080?x=1",
        "http://host:8080/#frag",
    ],
)
def test_invalid_base_urls_are_refused(raw):
    with pytest.raises(ValueError):
        validate_base_url(raw)


# ----------------------------------------------------------------------- save
def test_invalid_url_is_not_saved(dialog, saved):
    dialog.open(SAVED_URL)
    dialog.base_url_field.value = "ftp://host"

    dialog._save(None)

    assert saved == []
    assert dialog.base_url_field.error
    # The dialog stays open so the address can be corrected.
    assert dialog.dialog.open is True


def test_valid_url_is_saved_normalised_and_closes_the_dialog(dialog, saved):
    dialog.open(SAVED_URL)
    dialog.base_url_field.value = " http://127.0.0.1:9000/ "

    dialog._save(None)

    assert saved == [("http://127.0.0.1:9000", "Ти")]
    assert not dialog.base_url_field.error
    assert dialog.dialog.open is False


def test_cancel_closes_without_saving(dialog, saved):
    dialog.open(SAVED_URL)
    dialog.base_url_field.value = "http://127.0.0.1:9000"

    dialog._cancel(None)

    assert saved == []
    assert dialog.dialog.open is False


@pytest.mark.asyncio
async def test_open_resets_what_was_typed_and_shown_before(
    dialog, mock_page, clipboard
):
    dialog.base_url_field.value = "nonsense"
    dialog._save(None)
    dialog._show_result("Няма връзка", theme.PROBLEM)
    await dialog._copy_user_id(None)
    assert dialog.copy_btn.icon == ft.Icons.CHECK

    dialog.display_name_field.value = "typed but not saved"

    dialog.open(SAVED_URL, "Иван")

    assert dialog.base_url_field.value == SAVED_URL
    assert dialog.display_name_field.value == "Иван"
    assert not dialog.base_url_field.error
    assert dialog.check_result.visible is False
    assert dialog.copy_btn.icon == ft.Icons.COPY
    mock_page.show_dialog.assert_called_once_with(dialog.dialog)


# -------------------------------------------------------------------- user_id
def test_user_id_is_shown_read_only(dialog):
    assert dialog.user_id_field.value == USER_ID
    assert dialog.user_id_field.read_only is True


@pytest.mark.asyncio
async def test_copy_button_puts_the_user_id_on_the_clipboard(dialog, clipboard):
    await dialog._copy_user_id(None)

    assert clipboard == [USER_ID]
    assert dialog.copy_btn.icon == ft.Icons.CHECK
    assert dialog.copy_btn.tooltip == "Копирано"


@pytest.mark.asyncio
async def test_copy_button_does_not_claim_a_copy_that_failed(dialog, monkeypatch):
    class BrokenClipboard:
        async def set(self, value):
            raise RuntimeError("Session closed")

    monkeypatch.setattr(ft, "Clipboard", BrokenClipboard)

    await dialog._copy_user_id(None)

    assert dialog.copy_btn.icon == ft.Icons.COPY
    assert dialog.copy_btn.tooltip == "Копирай"


def test_dialog_explains_what_the_user_id_is_for(dialog):
    texts = [
        control.value
        for control in dialog.dialog.content.controls
        if isinstance(control, ft.Text)
    ]
    assert any("settings.json" in text and "Telegram" in text for text in texts)


# ------------------------------------------------------------ check connection
@respx.mock
@pytest.mark.asyncio
async def test_check_connection_reports_a_reachable_backend(dialog):
    route = respx.get("http://10.0.0.5:8080/healthz").mock(
        return_value=httpx.Response(200)
    )
    # The typed (not yet saved) address is the one that is probed.
    dialog.base_url_field.value = "http://10.0.0.5:8080/"

    await dialog._check_connection(None)

    assert route.called
    assert dialog.check_result.visible is True
    assert dialog.check_result.value == "Връзката е успешна."
    assert dialog.check_result.color == theme.OK
    assert dialog.check_btn.disabled is False
    assert dialog.check_progress.visible is False


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("slow"),
        httpx.Response(500),
    ],
    ids=["refused", "timeout", "unhealthy"],
)
async def test_check_connection_reports_an_unreachable_backend(dialog, outcome):
    route = respx.get("http://127.0.0.1:8080/healthz")
    if isinstance(outcome, Exception):
        route.mock(side_effect=outcome)
    else:
        route.mock(return_value=outcome)

    await dialog._check_connection(None)

    assert dialog.check_result.visible is True
    assert dialog.check_result.value == "Няма връзка с http://127.0.0.1:8080"
    assert dialog.check_result.color == theme.PROBLEM
    assert dialog.check_btn.disabled is False
    assert dialog.check_progress.visible is False


@respx.mock
@pytest.mark.asyncio
async def test_check_connection_refuses_an_invalid_address_without_a_request(dialog):
    # respx fails the test on any request that was not mocked.
    dialog.base_url_field.value = "not a url"

    await dialog._check_connection(None)

    assert dialog.base_url_field.error
    assert dialog.check_result.visible is False
    assert dialog.check_btn.disabled is False


# ---------------------------------------------------------------- display name
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Иван", "Иван"),
        ("  Иван Петров  ", "Иван Петров"),
        ("", "Ти"),
        ("   ", "Ти"),
        (None, "Ти"),
        ("я" * 100, "я" * 40),
    ],
)
def test_clean_display_name(raw, expected):
    assert clean_display_name(raw) == expected


def test_chat_name_is_saved_with_the_address(dialog, saved):
    dialog.display_name_field.value = "  Иван  "

    dialog._save(None)

    assert saved == [(SAVED_URL, "Иван")]


def test_blank_chat_name_is_saved_as_the_default(dialog, saved):
    dialog.display_name_field.value = "   "

    dialog._save(None)

    assert saved == [(SAVED_URL, "Ти")]


def test_chat_name_is_not_saved_when_the_address_is_invalid(dialog, saved):
    dialog.display_name_field.value = "Иван"
    dialog.base_url_field.value = "ftp://host"

    dialog._save(None)

    assert saved == []


def test_dialog_shows_the_saved_chat_name_and_limits_its_length(mock_page):
    dialog = SettingsDialog(
        mock_page,
        USER_ID,
        SAVED_URL,
        on_save=lambda url, name: None,
        display_name="Иван",
    )

    assert dialog.display_name_field.value == "Иван"
    assert dialog.display_name_field.max_length == 40
    assert dialog.display_name_field in dialog.dialog.content.controls


# ----------------------------------------------- plain http to another computer
@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.20:8080",
        "http://10.0.0.5",
        "http://example.com",
        "HTTP://Backend.Example.COM:8080",
        "http://my-server:8080",
        "http://[2001:db8::1]:8080",
    ],
)
def test_http_to_another_computer_is_flagged(url):
    assert is_plain_http_to_another_computer(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080",
        "http://LOCALHOST",
        "http://api.localhost:8080",
        "http://127.0.0.1:8080",
        "http://127.1.2.3",
        "http://[::1]:8080",
        "https://example.com",
        "https://192.168.1.20",
        "",
        None,
        "not a url",
        "http://",
        "http://[::1",
    ],
)
def test_this_computer_https_and_nonsense_are_not_flagged(url):
    assert is_plain_http_to_another_computer(url) is False


def test_the_warning_is_hidden_for_a_saved_local_address(dialog):
    dialog.open("http://127.0.0.1:8080")

    assert dialog.base_url_warning.visible is False


def test_the_warning_shows_when_the_dialog_opens_on_a_remote_http_address(dialog):
    dialog.open("http://192.168.1.20:8080")

    assert dialog.base_url_warning.visible is True
    assert dialog.base_url_warning.value == INSECURE_URL_NOTE
    assert "https://" in INSECURE_URL_NOTE


def test_the_warning_follows_the_address_as_it_is_typed(dialog):
    dialog.open(SAVED_URL)

    dialog.base_url_field.value = "http://backend.example"
    dialog._on_url_change(None)
    assert dialog.base_url_warning.visible is True

    dialog.base_url_field.value = "https://backend.example"
    dialog._on_url_change(None)
    assert dialog.base_url_warning.visible is False


def test_the_warning_is_not_an_error_and_the_address_is_saved(dialog, saved):
    dialog.open("http://192.168.1.20:8080")

    dialog._save(None)

    assert saved == [("http://192.168.1.20:8080", "Ти")]
    assert not dialog.base_url_field.error
