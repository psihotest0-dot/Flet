from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import flet as ft
import httpx
import pytest
import respx

import main as main_module
from api.client import APIClient
from state.settings import Settings, load_settings, save_settings

USER_ID = "123e4567-e89b-12d3-a456-426614174000"


@pytest.fixture
def restore_logger_levels():
    saved = {name: logging.getLogger(name).level for name in main_module.NOISY_LOGGERS}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture
def start_app(monkeypatch, mock_page):
    """Run ``main.main`` against a mock page and return the Tabs it mounts."""
    monkeypatch.setattr(main_module, "setup_logging", lambda: None)
    # Keep ``page.run_task`` a plain mock so the calls can be asserted.
    monkeypatch.setattr(main_module, "wrap_run_task", lambda page: None)
    # Control.update() needs a real page, which a mock cannot provide.
    monkeypatch.setattr(ft.Tabs, "update", lambda self: None)

    def start() -> ft.Tabs:
        main_module.main(mock_page)
        return mock_page.add.call_args.args[0]

    return start


# --------------------------------------------------------------------- logging
@respx.mock
@pytest.mark.asyncio
async def test_health_poll_leaves_no_httpx_info_records(
    caplog, monkeypatch, restore_logger_levels
):
    # Under pytest the root logger already has a handler, so basicConfig would be
    # a no-op anyway; patching it also keeps the real app.log untouched.
    monkeypatch.setattr(main_module, "RotatingFileHandler", MagicMock())
    monkeypatch.setattr(main_module.logging, "basicConfig", MagicMock())
    main_module.setup_logging()
    caplog.set_level(logging.INFO)

    respx.get("http://localhost:8080/healthz").mock(return_value=httpx.Response(200))
    async with APIClient("http://localhost:8080", USER_ID) as client:
        assert await client.check_health() is True

    noisy = [
        record.name
        for record in caplog.records
        if record.name.split(".")[0] in main_module.NOISY_LOGGERS
    ]
    assert noisy == []


@pytest.mark.parametrize("stored", [True, False], ids=["stored", "generated"])
def test_main_never_logs_the_full_user_id(caplog, start_app, stored):
    if stored:
        save_settings(Settings(user_id=USER_ID))
    caplog.set_level(logging.INFO)

    start_app()

    user_id = load_settings().user_id
    assert user_id == USER_ID or not stored
    assert user_id not in caplog.text
    assert user_id[:8] in caplog.text


def test_main_does_not_log_a_non_canonical_user_id_in_full(caplog, start_app):
    save_settings(Settings(user_id="legacy-user-identifier"))
    caplog.set_level(logging.INFO)

    start_app()

    assert "legacy-user-identifier" not in caplog.text
    assert "legacy-u" in caplog.text


# ------------------------------------------------------------- settings dialog
@pytest.fixture
def settings_dialog_spy(monkeypatch):
    """Replace the dialog with a spy that records how ``main`` builds it."""
    captured: dict = {}

    class Spy:
        def __init__(self, page, user_id, base_url, on_save, display_name="Ти"):
            captured.update(
                user_id=user_id,
                base_url=base_url,
                on_save=on_save,
                display_name=display_name,
                dialog=self,
            )
            self.opened_with = []

        def open(self, base_url, display_name="Ти"):
            self.opened_with.append((base_url, display_name))

    monkeypatch.setattr(main_module, "SettingsDialog", Spy)
    return captured


def test_saving_settings_keeps_the_identity_and_swaps_the_client(
    start_app, settings_dialog_spy
):
    save_settings(
        Settings(
            user_id=USER_ID,
            base_url="http://localhost:8080",
            session_id="session-1",
            chat_sessions=[{"id": "session-1", "name": "Чат 1"}],
        )
    )
    tabs = start_app()
    chat_view = tabs.tabs[0].content
    previous_client = chat_view.api_client
    assert settings_dialog_spy["user_id"] == USER_ID
    assert settings_dialog_spy["base_url"] == "http://localhost:8080"

    settings_dialog_spy["on_save"]("http://127.0.0.1:9000", "Ти")

    saved = load_settings()
    assert saved.base_url == "http://127.0.0.1:9000"
    # Only the address changed: the identity and the open chat are untouched.
    assert saved.user_id == USER_ID
    assert saved.session_id == "session-1"
    assert saved.chat_sessions == [{"id": "session-1", "name": "Чат 1"}]
    assert chat_view.api_client is not previous_client
    assert chat_view.api_client.base_url == "http://127.0.0.1:9000"
    assert chat_view.api_client.user_id == USER_ID


def test_dialog_is_built_with_the_saved_chat_name_and_reopens_with_it(
    start_app, settings_dialog_spy, mock_page
):
    save_settings(Settings(user_id=USER_ID, display_name="Иван"))
    start_app()
    assert settings_dialog_spy["display_name"] == "Иван"

    open_settings = mock_page.appbar.actions[0].on_click
    open_settings(None)
    settings_dialog_spy["on_save"]("http://localhost:8080", "Мария")
    open_settings(None)

    assert settings_dialog_spy["dialog"].opened_with == [
        ("http://localhost:8080", "Иван"),
        ("http://localhost:8080", "Мария"),
    ]


def test_saving_a_chat_name_persists_it_and_relabels_the_open_chat(
    start_app, settings_dialog_spy
):
    save_settings(
        Settings(
            user_id=USER_ID,
            session_id="session-1",
            chat_sessions=[{"id": "session-1", "name": "Чат 1"}],
        )
    )
    tabs = start_app()
    chat_view = tabs.tabs[0].content
    mine = chat_view.add_message("user", "здравей", notify=False)
    answer = chat_view.add_message("assistant", "здрасти", notify=False)
    assert mine.sender_label.value == "Ти"

    settings_dialog_spy["on_save"]("http://localhost:8080", "Иван")

    saved = load_settings()
    assert saved.display_name == "Иван"
    # The identity and the open chat are untouched by a rename.
    assert saved.user_id == USER_ID
    assert saved.session_id == "session-1"
    # The messages already on screen and the next ones carry the new name; the
    # assistant keeps its own.
    assert mine.sender_label.value == "Иван"
    assert chat_view.add_message("user", "пак", notify=False).sender_label.value == "Иван"
    assert answer.sender_label.value == "Лили"


def test_closing_the_session_stops_the_health_poll_and_closes_the_client(
    start_app, mock_page
):
    tabs = start_app()
    client = tabs.tabs[0].content.api_client
    health_task = mock_page.run_task.return_value
    mock_page.run_task.reset_mock()

    # Flet calls this when the window or the browser tab goes away.
    mock_page.on_disconnect(None)

    health_task.cancel.assert_called_once()
    mock_page.run_task.assert_called_once_with(client.aclose)


def test_closing_the_session_after_a_save_closes_the_new_client(
    start_app, settings_dialog_spy, mock_page
):
    tabs = start_app()
    settings_dialog_spy["on_save"]("http://127.0.0.1:9000", "Ти")
    current = tabs.tabs[0].content.api_client
    mock_page.run_task.reset_mock()

    mock_page.on_disconnect(None)

    # The previous client was closed by the save; only the current one remains.
    mock_page.run_task.assert_called_once_with(current.aclose)


@respx.mock
@pytest.mark.asyncio
async def test_connection_indicator_follows_the_saved_base_url(
    start_app, settings_dialog_spy, mock_page
):
    save_settings(Settings(user_id=USER_ID, base_url="http://localhost:8080"))
    start_app()
    # run_task(run_health_checks, probe, on_change) - the app's first task.
    probe = mock_page.run_task.call_args_list[0].args[1]

    settings_dialog_spy["on_save"]("http://127.0.0.1:9000")

    # The old client is closed after a save; the indicator must probe the new one.
    respx.get("http://127.0.0.1:9000/healthz").mock(return_value=httpx.Response(200))
    assert await probe() is True


# ------------------------------------------------------------------ memory tab
def select_tab(tabs: ft.Tabs, index: int) -> None:
    tabs.on_change(SimpleNamespace(control=SimpleNamespace(selected_index=index)))


def test_switching_chats_does_not_reload_the_memories(start_app, mock_page):
    # Memories belong to the user, not to a chat. Reloading goes through
    # page.run_task(memory_view.load_memories), so no task means no request.
    tabs = start_app()
    select_tab(tabs, 1)
    chat_view = tabs.tabs[0].content
    mock_page.run_task.reset_mock()

    chat_view.on_session_changed("another-session")

    mock_page.run_task.assert_not_called()


def test_memory_tab_reloads_each_time_it_is_opened_again(start_app, mock_page):
    tabs = start_app()
    mock_page.run_task.reset_mock()

    select_tab(tabs, 1)
    memory_view = tabs.tabs[1].content
    # The first visit builds the view, which loads itself when it is mounted.
    mock_page.run_task.assert_not_called()

    select_tab(tabs, 0)
    select_tab(tabs, 1)
    mock_page.run_task.assert_called_once_with(memory_view.load_memories)
    assert tabs.tabs[1].content is memory_view
