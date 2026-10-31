from __future__ import annotations

import flet as ft
import httpx
import pytest
import respx

from views.settings_dialog import SettingsDialog, validate_base_url

USER_ID = "123e4567-e89b-12d3-a456-426614174000"
SAVED_URL = "http://127.0.0.1:8080"


@pytest.fixture
def saved():
    return []


@pytest.fixture
def dialog(mock_page, saved):
    return SettingsDialog(mock_page, USER_ID, SAVED_URL, on_save=saved.append)


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
def test_invalid_url_is_not_saved(dialog, saved, mock_page):
    dialog.base_url_field.value = "ftp://host"

    dialog._save(None)

    assert saved == []
    assert dialog.base_url_field.error_text
    # The dialog stays open so the address can be corrected.
    mock_page.close.assert_not_called()


def test_valid_url_is_saved_normalised_and_closes_the_dialog(dialog, saved, mock_page):
    dialog.base_url_field.value = " http://127.0.0.1:9000/ "

    dialog._save(None)

    assert saved == ["http://127.0.0.1:9000"]
    assert not dialog.base_url_field.error_text
    mock_page.close.assert_called_once_with(dialog.dialog)


def test_cancel_closes_without_saving(dialog, saved, mock_page):
    dialog.base_url_field.value = "http://127.0.0.1:9000"

    dialog._cancel(None)

    assert saved == []
    mock_page.close.assert_called_once_with(dialog.dialog)


def test_open_resets_what_was_typed_and_shown_before(dialog, mock_page):
    dialog.base_url_field.value = "nonsense"
    dialog._save(None)
    dialog._show_result("Няма връзка", ft.colors.RED)
    dialog._copy_user_id(None)

    dialog.open(SAVED_URL)

    assert dialog.base_url_field.value == SAVED_URL
    assert not dialog.base_url_field.error_text
    assert dialog.check_result.visible is False
    assert dialog.copy_btn.icon == ft.icons.COPY
    mock_page.open.assert_called_once_with(dialog.dialog)


# -------------------------------------------------------------------- user_id
def test_user_id_is_shown_read_only(dialog):
    assert dialog.user_id_field.value == USER_ID
    assert dialog.user_id_field.read_only is True


def test_copy_button_puts_the_user_id_on_the_clipboard(dialog, mock_page):
    dialog._copy_user_id(None)

    mock_page.set_clipboard.assert_called_once_with(USER_ID)
    assert dialog.copy_btn.icon == ft.icons.CHECK


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
    assert dialog.check_result.color == ft.colors.GREEN
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
    assert dialog.check_result.color == ft.colors.RED
    assert dialog.check_btn.disabled is False
    assert dialog.check_progress.visible is False


@respx.mock
@pytest.mark.asyncio
async def test_check_connection_refuses_an_invalid_address_without_a_request(dialog):
    # respx fails the test on any request that was not mocked.
    dialog.base_url_field.value = "not a url"

    await dialog._check_connection(None)

    assert dialog.base_url_field.error_text
    assert dialog.check_result.visible is False
    assert dialog.check_btn.disabled is False
