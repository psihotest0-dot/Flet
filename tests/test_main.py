from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import threading
import traceback
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


@contextlib.contextmanager
def unconfigured_root_logger():
    """The root logger as it is at app start: no handlers at all.

    Entered inside the test body, because pytest puts its own capture handlers on
    the root logger when the test starts - with those in place ``basicConfig`` is
    a no-op and the log file would never be attached.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    try:
        yield root
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)


@pytest.fixture
def app_log_in_tmp(tmp_path, monkeypatch, restore_logger_levels, restore_exception_hooks):
    monkeypatch.setattr(main_module, "LOG_FILE", tmp_path / "app.log")
    return tmp_path / "app.log"


def test_setup_logging_attaches_app_log_only_once(app_log_in_tmp, monkeypatch):
    # ``main`` runs once per window session; every run used to open app.log again.
    opened = []
    real_handler = main_module.RotatingFileHandler

    def spy(*args, **kwargs):
        handler = real_handler(*args, **kwargs)
        opened.append(handler)
        return handler

    monkeypatch.setattr(main_module, "RotatingFileHandler", spy)

    with unconfigured_root_logger() as root:
        main_module.setup_logging()
        main_module.setup_logging()

        assert len(opened) == 1
        assert root.handlers == opened


def test_app_log_lines_name_the_module_that_wrote_them(app_log_in_tmp):
    with unconfigured_root_logger() as root:
        main_module.setup_logging()

        logging.getLogger("views.chat").info("hello")
        for handler in root.handlers:
            handler.flush()

    assert " - INFO - views.chat - hello" in app_log_in_tmp.read_text(encoding="utf-8")


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


def test_saving_settings_keeps_the_identity_and_repoints_the_same_client(
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
    # The chat messages and proposal cards hold this very object.
    assert chat_view.api_client is previous_client
    assert previous_client.base_url == "http://127.0.0.1:9000"
    assert previous_client.user_id == USER_ID


@pytest.mark.parametrize(
    "new_url", ["http://127.0.0.1:9000", "http://localhost:8080"], ids=["changed", "same"]
)
def test_saving_settings_never_closes_the_client(
    start_app, settings_dialog_spy, mock_page, new_url
):
    # "Запази" is also pressed with nothing changed (the dialog was opened only to
    # copy the user_id). Closing the client cut off the answer being streamed.
    save_settings(Settings(user_id=USER_ID, base_url="http://localhost:8080"))
    tabs = start_app()
    client = tabs.tabs[0].content.api_client
    mock_page.run_task.reset_mock()

    settings_dialog_spy["on_save"](new_url, "Ти")

    mock_page.run_task.assert_not_called()
    assert client.client.is_closed is False
    assert client.base_url == new_url


@respx.mock
@pytest.mark.asyncio
async def test_a_client_held_from_before_the_save_reaches_the_new_address(
    start_app, settings_dialog_spy
):
    save_settings(Settings(user_id=USER_ID, base_url="http://localhost:8080"))
    tabs = start_app()
    # What a proposal card or a chat message got when it was drawn.
    held = tabs.tabs[0].content.api_client
    old = respx.post("http://localhost:8080/proposals/7/approve")
    new = respx.post("http://127.0.0.1:9000/proposals/7/approve").mock(
        return_value=httpx.Response(200, json={"id": 7, "status": "approved"})
    )

    settings_dialog_spy["on_save"]("http://127.0.0.1:9000", "Ти")
    await held.approve_proposal(7)

    assert new.called
    assert not old.called


@pytest.mark.asyncio
async def test_stop_still_reaches_the_answer_being_streamed_after_a_save(
    start_app, settings_dialog_spy
):
    tabs = start_app()
    chat_view = tabs.tabs[0].content
    # The stream records its task on the client it reads through.
    answer = asyncio.get_running_loop().create_task(asyncio.sleep(10))
    chat_view.api_client._current_stream_task = answer

    settings_dialog_spy["on_save"]("http://127.0.0.1:9000", "Ти")
    # What "Спри" does (ChatView.stop_generation_click).
    chat_view.api_client.cancel_stream()

    with pytest.raises(asyncio.CancelledError):
        await answer


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


def test_closing_the_session_after_a_save_closes_the_one_client(
    start_app, settings_dialog_spy, mock_page
):
    tabs = start_app()
    client = tabs.tabs[0].content.api_client
    settings_dialog_spy["on_save"]("http://127.0.0.1:9000", "Ти")
    mock_page.run_task.reset_mock()

    mock_page.on_disconnect(None)

    mock_page.run_task.assert_called_once_with(client.aclose)


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


def test_switching_chats_logs_only_the_start_of_the_session_id(
    caplog, start_app
):
    session_id = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
    tabs = start_app()
    caplog.set_level(logging.INFO, logger="main")

    tabs.tabs[0].content.on_session_changed(session_id)

    # AGENTS.md rule 7: the full session_id is the key to the conversation.
    assert "Active chat session changed to 77777777" in caplog.text
    assert session_id not in caplog.text


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


@pytest.fixture
def restore_exception_hooks():
    saved = (sys.excepthook, threading.excepthook)
    yield
    sys.excepthook, threading.excepthook = saved


def test_unhandled_exceptions_of_the_main_thread_reach_the_log(caplog, restore_exception_hooks):
    main_module.install_exception_hooks()

    try:
        raise RuntimeError("start-up broke")
    except RuntimeError:
        with caplog.at_level(logging.CRITICAL):
            sys.excepthook(*sys.exc_info())

    record = next(r for r in caplog.records if r.getMessage() == "Unhandled exception")
    assert record.levelno == logging.CRITICAL
    assert "start-up broke" in "".join(traceback.format_exception(*record.exc_info))


def test_unhandled_exceptions_of_other_threads_reach_the_log(caplog, restore_exception_hooks):
    main_module.install_exception_hooks()

    def boom():
        raise ValueError("worker thread broke")

    with caplog.at_level(logging.CRITICAL):
        worker = threading.Thread(target=boom)
        worker.start()
        worker.join()

    assert any(r.getMessage() == "Unhandled exception" and "worker thread broke" in str(r.exc_info[1]) for r in caplog.records)


def test_ctrl_c_is_left_to_the_default_hook(caplog, restore_exception_hooks, monkeypatch):
    called = []
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: called.append(a[0]))
    main_module.install_exception_hooks()

    with caplog.at_level(logging.CRITICAL):
        sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)

    assert called == [KeyboardInterrupt]
    assert not caplog.records
