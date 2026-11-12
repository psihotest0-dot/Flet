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
from flet.messaging.protocol import MessageAction
from helpers import real_session, tab_view

import main as main_module
from api.client import APIClient
from state.settings import Settings, load_settings, save_settings
from views.files import FilesView
from views.oracle import OracleView

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
def app_log_in_tmp(
    tmp_path, monkeypatch, restore_logger_levels, restore_exception_hooks
):
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
    chat_view = tab_view(tabs, 0)
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
    "new_url",
    ["http://127.0.0.1:9000", "http://localhost:8080"],
    ids=["changed", "same"],
)
def test_saving_settings_never_closes_the_client(
    start_app, settings_dialog_spy, mock_page, new_url
):
    # "Запази" is also pressed with nothing changed (the dialog was opened only to
    # copy the user_id). Closing the client cut off the answer being streamed.
    save_settings(Settings(user_id=USER_ID, base_url="http://localhost:8080"))
    tabs = start_app()
    client = tab_view(tabs, 0).api_client
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
    held = tab_view(tabs, 0).api_client
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
    chat_view = tab_view(tabs, 0)
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
    chat_view = tab_view(tabs, 0)
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
    assert (
        chat_view.add_message("user", "пак", notify=False).sender_label.value == "Иван"
    )
    assert answer.sender_label.value == "Лили"


def window_event(event_type: ft.WindowEventType) -> SimpleNamespace:
    return SimpleNamespace(type=event_type)


def test_the_window_waits_for_the_app_before_it_closes(start_app, mock_page):
    # Without prevent_close the window is gone before the close event is handled,
    # and nothing the session runs is stopped (P17).
    start_app()

    assert mock_page.window.prevent_close is True
    assert mock_page.window.on_event is not None


@pytest.mark.asyncio
async def test_closing_the_window_stops_the_health_poll_and_closes_the_client(
    start_app, mock_page, caplog
):
    tabs = start_app()
    client = tab_view(tabs, 0).api_client
    health_task = mock_page.run_task.return_value
    caplog.set_level(logging.INFO, logger="main")

    # What Flet sends when the X of the window is pressed.
    await mock_page.window.on_event(window_event(ft.WindowEventType.CLOSE))

    health_task.cancel.assert_called_once()
    assert client.client.is_closed is True
    mock_page.window.destroy.assert_awaited_once()
    assert "The window was closed" in caplog.text


@pytest.mark.asyncio
async def test_closing_the_window_after_a_save_closes_the_one_client(
    start_app, settings_dialog_spy, mock_page
):
    tabs = start_app()
    client = tab_view(tabs, 0).api_client
    settings_dialog_spy["on_save"]("http://127.0.0.1:9000", "Ти")

    await mock_page.window.on_event(window_event(ft.WindowEventType.CLOSE))

    assert client.client.is_closed is True
    mock_page.window.destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_closing_the_window_stops_what_the_open_tabs_run(start_app, mock_page):
    tabs = start_app()
    select_tab(tabs, 2)
    select_tab(tabs, 3)
    files_view, bot_view = tab_view(tabs, 2), tab_view(tabs, 3)
    assert bot_view._polling is True

    await mock_page.window.on_event(window_event(ft.WindowEventType.CLOSE))

    assert (files_view._polling, files_view._active) == (False, False)
    assert (bot_view._polling, bot_view._active) == (False, False)
    assert bot_view._poll_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    [
        ft.WindowEventType.RESIZED,
        ft.WindowEventType.MOVED,
        ft.WindowEventType.FOCUS,
        ft.WindowEventType.BLUR,
        ft.WindowEventType.MINIMIZE,
    ],
    ids=lambda event_type: event_type.name.lower(),
)
async def test_other_window_events_close_nothing(start_app, mock_page, event_type):
    tabs = start_app()
    client = tab_view(tabs, 0).api_client
    health_task = mock_page.run_task.return_value

    await mock_page.window.on_event(window_event(event_type))

    health_task.cancel.assert_not_called()
    assert client.client.is_closed is False
    mock_page.window.destroy.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_clean_up_still_lets_the_window_go(
    start_app, mock_page, monkeypatch, caplog
):
    # prevent_close holds the window until destroy(): if the clean-up raised
    # before it, the X button would do nothing and the app could only be killed.
    tabs = start_app()
    client = tab_view(tabs, 0).api_client

    async def broken_close():
        raise RuntimeError("the pool is broken")

    monkeypatch.setattr(client, "aclose", broken_close)

    await mock_page.window.on_event(window_event(ft.WindowEventType.CLOSE))

    mock_page.window.destroy.assert_awaited_once()
    assert "Could not shut down cleanly: the pool is broken" in caplog.text


@pytest.mark.asyncio
async def test_a_clean_up_that_hangs_does_not_hold_the_window(
    start_app, mock_page, monkeypatch, caplog
):
    tabs = start_app()
    client = tab_view(tabs, 0).api_client
    monkeypatch.setattr(main_module, "CLOSE_TIMEOUT", 0.01)

    async def never_returns():
        await asyncio.sleep(3600)

    monkeypatch.setattr(client, "aclose", never_returns)

    await asyncio.wait_for(
        mock_page.window.on_event(window_event(ft.WindowEventType.CLOSE)), timeout=5
    )

    mock_page.window.destroy.assert_awaited_once()
    assert "Could not shut down cleanly: TimeoutError" in caplog.text


@pytest.mark.asyncio
async def test_a_second_press_of_the_x_is_not_served_twice(
    start_app, mock_page, monkeypatch
):
    tabs = start_app()
    client = tab_view(tabs, 0).api_client
    health_task = mock_page.run_task.return_value
    closes = []
    release = asyncio.Event()

    async def slow_close():
        closes.append(1)
        await release.wait()

    monkeypatch.setattr(client, "aclose", slow_close)

    first = asyncio.ensure_future(
        mock_page.window.on_event(window_event(ft.WindowEventType.CLOSE))
    )
    await asyncio.sleep(0)
    # The X again, while the first press is still closing the client.
    await mock_page.window.on_event(window_event(ft.WindowEventType.CLOSE))
    release.set()
    await first

    assert closes == [1]
    health_task.cancel.assert_called_once()
    mock_page.window.destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_window_that_was_not_destroyed_can_be_closed_again(
    start_app, mock_page, caplog
):
    # destroy() waits for the client's answer; raised out of the handler its
    # failure would be reported as a crash of the session.
    start_app()
    mock_page.window.destroy.side_effect = [RuntimeError("no answer"), None]

    await mock_page.window.on_event(window_event(ft.WindowEventType.CLOSE))
    assert "The window was not destroyed: no answer" in caplog.text

    await mock_page.window.on_event(window_event(ft.WindowEventType.CLOSE))
    assert mock_page.window.destroy.await_count == 2


def test_cancelled_tasks_need_no_wrapper_any_more():
    # Flet 0.24 raised from the done-callback of a cancelled task, and main.py
    # wrapped page.run_task to swallow it. Flet 1.x returns early for a cancelled
    # future; the wrapper is gone and must not come back unnoticed.
    assert not hasattr(main_module, "SafeTaskWrapper")
    assert not hasattr(main_module, "wrap_run_task")


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
    chat_view = tab_view(tabs, 0)
    mock_page.run_task.reset_mock()

    chat_view.on_session_changed("another-session")

    mock_page.run_task.assert_not_called()


def test_switching_chats_logs_only_the_start_of_the_session_id(caplog, start_app):
    session_id = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
    tabs = start_app()
    caplog.set_level(logging.INFO, logger="main")

    tab_view(tabs, 0).on_session_changed(session_id)

    # AGENTS.md rule 7: the full session_id is the key to the conversation.
    assert "Active chat session changed to 77777777" in caplog.text
    assert session_id not in caplog.text


def test_memory_tab_reloads_each_time_it_is_opened_again(start_app, mock_page):
    tabs = start_app()
    mock_page.run_task.reset_mock()

    select_tab(tabs, 1)
    memory_view = tab_view(tabs, 1)
    # The first visit builds the view, which loads itself when it is mounted.
    mock_page.run_task.assert_not_called()

    select_tab(tabs, 0)
    select_tab(tabs, 1)
    mock_page.run_task.assert_called_once_with(memory_view.load_memories)
    assert tab_view(tabs, 1) is memory_view


def test_returning_to_the_chat_tab_stops_the_pollers_of_the_other_tabs(
    start_app, monkeypatch
):
    calls: list[tuple[str, bool]] = []
    for view_class in (FilesView, OracleView):
        monkeypatch.setattr(
            view_class,
            "set_active",
            lambda self, active: calls.append((type(self).__name__, active)),
        )
    tabs = start_app()
    select_tab(tabs, 2)
    select_tab(tabs, 3)
    assert calls[-2:] == [("FilesView", False), ("OracleView", True)]
    calls.clear()

    # The chat view is built at once and is not made by a factory; the pollers
    # must stop all the same, or they would keep asking the backend in the background.
    select_tab(tabs, 0)

    assert sorted(calls) == [("FilesView", False), ("OracleView", False)]


@pytest.fixture
def restore_exception_hooks():
    saved = (sys.excepthook, threading.excepthook)
    yield
    sys.excepthook, threading.excepthook = saved


def test_unhandled_exceptions_of_the_main_thread_reach_the_log(
    caplog, restore_exception_hooks
):
    main_module.install_exception_hooks()

    try:
        raise RuntimeError("start-up broke")
    except RuntimeError:
        with caplog.at_level(logging.CRITICAL):
            sys.excepthook(*sys.exc_info())

    record = next(r for r in caplog.records if r.getMessage() == "Unhandled exception")
    assert record.levelno == logging.CRITICAL
    assert "start-up broke" in "".join(traceback.format_exception(*record.exc_info))


def test_unhandled_exceptions_of_other_threads_reach_the_log(
    caplog, restore_exception_hooks
):
    main_module.install_exception_hooks()

    def boom():
        raise ValueError("worker thread broke")

    with caplog.at_level(logging.CRITICAL):
        worker = threading.Thread(target=boom)
        worker.start()
        worker.join()

    assert any(
        r.getMessage() == "Unhandled exception"
        and "worker thread broke" in str(r.exc_info[1])
        for r in caplog.records
    )


def test_ctrl_c_is_left_to_the_default_hook(
    caplog, restore_exception_hooks, monkeypatch
):
    called = []
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: called.append(a[0]))
    main_module.install_exception_hooks()

    with caplog.at_level(logging.CRITICAL):
        sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)

    assert called == [KeyboardInterrupt]
    assert not caplog.records


# ------------------------------------------------- a handler that raises
def real_session_with_a_broken_button():
    """A real Flet session and page (no client), with a button whose handler raises."""
    session, connection = real_session()

    def broken(e):
        raise ValueError("a bug in a handler")

    button = ft.Button("x", on_click=broken)
    session.page.add(button)
    assert MessageAction.PATCH_CONTROL in connection.actions(), (
        "the button reached the client"
    )
    connection.sent.clear()
    return session, connection, button


@pytest.mark.asyncio
async def test_flet_alone_ends_the_session_when_a_handler_raises():
    # The behaviour keep_the_window_alive exists for. If this fails, Flet has
    # changed how it reports the error and the guard has to be looked at again.
    session, connection, button = real_session_with_a_broken_button()

    await session.dispatch_event(button._i, "click", None)

    assert MessageAction.SESSION_CRASHED in connection.actions()


@pytest.mark.asyncio
async def test_a_handler_that_raises_gets_a_message_and_the_window_stays(caplog):
    session, connection, button = real_session_with_a_broken_button()
    main_module.keep_the_window_alive(session.page)

    await session.dispatch_event(button._i, "click", None)

    assert MessageAction.SESSION_CRASHED not in connection.actions()
    # The snackbar went to the client instead ...
    shown = [d for d in session.page._dialogs.controls if isinstance(d, ft.SnackBar)]
    assert [bar.content.value for bar in shown] == [main_module.UNEXPECTED_ERROR_TEXT]
    assert MessageAction.PATCH_CONTROL in connection.actions()
    # ... and the traceback is in the log, written by Flet itself.
    assert "Unhandled error in 'on_click' handler" in caplog.text
    assert "a bug in a handler" in caplog.text


@pytest.mark.asyncio
async def test_the_window_survives_even_if_the_message_cannot_be_shown(monkeypatch):
    session, connection, button = real_session_with_a_broken_button()
    main_module.keep_the_window_alive(session.page)

    def no_snackbar(page, message, success=False):
        raise RuntimeError("the connection is gone")

    monkeypatch.setattr(main_module, "show_snackbar", no_snackbar)

    await session.dispatch_event(button._i, "click", None)

    assert MessageAction.SESSION_CRASHED not in connection.actions()


def test_the_app_installs_the_guard_once_the_window_is_built(start_app, mock_page):
    start_app()

    # mock_page.session is a mock; the app replaced its ``error`` with a function.
    assert callable(mock_page.session.error)
    assert not isinstance(mock_page.session.error, MagicMock)
