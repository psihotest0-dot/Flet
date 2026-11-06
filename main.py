import asyncio
import concurrent.futures
import logging
import os
import sys
import threading
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

import flet as ft

from api.client import APIClient
from state.settings import DEFAULT_DISPLAY_NAME, load_settings, save_settings
from views.chat import ChatView
from views.files import FilesView
from views.memory import MemoryView
from views.oracle import OracleView
from views.settings_dialog import SettingsDialog
from views.ui_utils import show_snackbar

logger = logging.getLogger(__name__)

LOG_FILE = Path(__file__).with_name("app.log")
HEALTH_CHECK_INTERVAL = 10
# Tab index -> the view method that re-reads its data when the tab is opened again.
RELOADED_ON_VISIT = {1: "load_memories", 3: "refresh"}
# Libraries that log every single request on INFO.
NOISY_LOGGERS = ("httpx", "httpcore")


class SafeTaskWrapper:
    def __init__(self, task_future):
        self._task_future = task_future

    def __getattr__(self, name):
        return getattr(self._task_future, name)

    def cancel(self):
        orig_exc = self._task_future.exception
        def safe_exc(*args, **kwargs):
            try:
                return orig_exc(*args, **kwargs)
            except concurrent.futures.CancelledError:
                return None
        self._task_future.exception = safe_exc
        return self._task_future.cancel()


def wrap_run_task(page):
    if hasattr(page, "_is_wrapped"):
        return
    page._is_wrapped = True
    original_run_task = page.run_task

    def safe_run_task(*args, **kwargs):
        task_future = original_run_task(*args, **kwargs)
        return SafeTaskWrapper(task_future)

    page.run_task = safe_run_task


def short_id(value: str) -> str:
    """First 8 characters of an identifier - enough for the log, not the key."""
    return value[:8]


def log_file_is_attached() -> bool:
    """Whether the root logger already writes to ``app.log``."""
    target = os.path.abspath(LOG_FILE)
    return any(
        getattr(handler, "baseFilename", None) == target
        for handler in logging.getLogger().handlers
    )


def setup_logging():
    # ``main`` runs once per window session. A second call used to open ``app.log``
    # again just to throw the new handler away (basicConfig ignores it when the
    # root logger is configured), so the file is attached once.
    if not log_file_is_attached():
        _attach_log_file()
    # httpx logs every request on INFO - the health poll alone is a line every
    # 10 seconds, which drowned the real messages in app.log.
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    install_exception_hooks()


def _attach_log_file():
    logging.basicConfig(
        handlers=[RotatingFileHandler(LOG_FILE, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")],
        level=logging.INFO,
        # The logger name says which module wrote the line (api.client, views.chat, ...).
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    )


def install_exception_hooks():
    """Write exceptions nobody caught to app.log.

    The window has no console, so an exception that ends a thread (or the whole
    start-up) used to leave no trace at all.
    """
    def log_unhandled(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = log_unhandled
    threading.excepthook = lambda args: log_unhandled(args.exc_type, args.exc_value, args.exc_traceback)


async def run_health_checks(probe, on_change, interval: float = HEALTH_CHECK_INTERVAL):
    """Poll ``probe`` forever and call ``on_change(healthy)`` only on a flip.

    Repainting the status indicator on every poll marked the whole appbar dirty
    every few seconds for no reason.
    """
    healthy = None
    while True:
        try:
            is_healthy = await probe()
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.warning(f"Health check failed: {ex}")
            is_healthy = False

        if is_healthy != healthy:
            healthy = is_healthy
            on_change(healthy)

        await asyncio.sleep(interval)



def main(page: ft.Page):
    setup_logging()
    wrap_run_task(page)
    page.title = "AI Agent Client"
    page.theme_mode = ft.ThemeMode.DARK
    page.theme = ft.Theme(color_scheme_seed=ft.colors.BLUE)
    page.window.width = 1100
    page.window.height = 800
    page.window.min_width = 720
    page.window.min_height = 520

    settings = load_settings()

    # The stored user_id is the identity that owns the chat, memory and file
    # history on the backend, so it is reused verbatim. Minting a new one (or
    # "fixing" it) silently orphans everything that belongs to the old id;
    # only a settings file without any user_id gets a fresh one.
    if not settings.user_id:
        settings.user_id = str(uuid.uuid4())
        save_settings(settings)
        logger.info("No user_id in settings, generated one: %s", short_id(settings.user_id))
    else:
        logger.info("Using the stored user_id: %s", short_id(settings.user_id))
        try:
            uuid.UUID(settings.user_id)
        except ValueError:
            logger.warning(
                "settings.user_id %s... is not a canonical UUID; keeping the stored "
                "value so the existing history stays reachable.",
                short_id(settings.user_id),
            )

    logger.info("Starting the client for the backend at %s", settings.base_url)
    api_client = APIClient(base_url=settings.base_url, user_id=settings.user_id)
    # index -> view; tabs are created on first activation (see ensure_view below).
    views: dict[int, ft.Control] = {}

    def apply_settings(new_url: str, display_name: str = DEFAULT_DISPLAY_NAME):
        """Persist the validated Base URL and chat name; point every view at a new client."""
        nonlocal api_client

        # Re-read the file before writing: the chat view stores its session_id
        # and chat_sessions there, and saving the snapshot loaded at startup
        # would silently revert them to an older session.
        current = load_settings()
        current.base_url = new_url
        current.display_name = display_name
        if not current.user_id:
            current.user_id = settings.user_id
        success = save_settings(current)

        if not success:
            # save_settings has logged the reason; this line says what was lost.
            logger.error("The new settings (Base URL %s) were not saved.", new_url)
            show_snackbar(page, "Неуспешно запазване на настройките.")
            return
        logger.info("Settings saved: Base URL %s.", new_url)

        settings.base_url = current.base_url
        settings.user_id = current.user_id
        settings.session_id = current.session_id
        settings.chat_sessions = current.chat_sessions
        settings.display_name = current.display_name

        # The old client keeps pointing at the previous URL - replace it and
        # hand the new one to every view that was already created.
        previous_client = api_client
        api_client = APIClient(base_url=settings.base_url, user_id=settings.user_id)
        for view in views.values():
            view.api_client = api_client
            if isinstance(view, ChatView):
                view.set_display_name(settings.display_name)
        if previous_client is not None:
            page.run_task(previous_client.aclose)

        show_snackbar(page, "Настройките са запазени успешно!", success=True)

    settings_dialog = SettingsDialog(
        page,
        user_id=settings.user_id,
        base_url=settings.base_url,
        on_save=apply_settings,
        display_name=settings.display_name,
    )

    def open_settings(e):
        settings_dialog.open(settings.base_url, settings.display_name)

    status_icon = ft.Icon(name=ft.icons.CIRCLE, color=ft.colors.RED, size=14, tooltip="Връзката е прекъсната")

    def set_status(healthy: bool):
        status_icon.color = ft.colors.GREEN if healthy else ft.colors.RED
        status_icon.tooltip = "Свързан" if healthy else "Връзката е прекъсната"
        if status_icon.uid is not None:
            status_icon.update()
        else:
            page.update()

    page.appbar = ft.AppBar(
        title=ft.Row([ft.Text("AI Agent"), status_icon], alignment=ft.MainAxisAlignment.START),
        actions=[
            ft.IconButton(ft.icons.SETTINGS, on_click=open_settings)
        ]
    )

    async def probe_backend() -> bool:
        # Looked up on every poll: saving a new Base URL replaces ``api_client``
        # and closes the old one, so a bound ``api_client.check_health`` would
        # keep the indicator red for good.
        return await api_client.check_health()

    task_handle = page.run_task(run_health_checks, probe_backend, set_status)

    def on_disconnect(e):
        # ``api_client`` is read when the session ends: after a saved Base URL it
        # is the replacement client, the old one is already closed.
        logger.info("The window was closed, the HTTP client is shut down.")
        task_handle.cancel()
        try:
            page.run_task(api_client.aclose)
        except Exception as ex:  # noqa: BLE001
            logger.warning("Could not close the HTTP client cleanly: %s", ex)

    page.on_disconnect = on_disconnect

    # Views are mounted lazily: creating all of them up front issued an API call
    # per tab and started the pollers before the user opened a single tab.
    def on_chat_session_changed(session_id: str) -> None:
        """React to another chat being opened.

        Memories belong to the user, not to a chat, so the Memory tab is not
        reloaded here - it refreshes when its tab is opened.
        """
        logger.info("Active chat session changed to %s", session_id)

    def create_chat() -> ChatView:
        return ChatView(page, api_client, on_session_changed=on_chat_session_changed)

    def create_memory() -> MemoryView:
        return MemoryView(page, api_client)

    def create_files() -> FilesView:
        return FilesView(page, api_client)

    def create_oracle() -> OracleView:
        return OracleView(page, api_client)

    factories = {0: create_chat, 1: create_memory, 2: create_files, 3: create_oracle}

    chat_view = ChatView(page, api_client, on_session_changed=on_chat_session_changed)
    views[0] = chat_view

    tabs = ft.Tabs(
        selected_index=0,
        animation_duration=300,
        tabs=[
            ft.Tab(text="Чат", content=chat_view),
            ft.Tab(text="Памет"),
            ft.Tab(text="Файлове"),
            ft.Tab(text="Бот"),
        ],
        expand=1,
    )

    def ensure_view(index: int) -> ft.Control:
        view = views.get(index)
        if view is None:
            view = factories[index]()
            views[index] = view
            tabs.tabs[index].content = view
            tabs.update()
        return view

    def on_tab_change(e):
        index = e.control.selected_index or 0
        if index not in factories:
            return
        existed = index in views
        view = ensure_view(index)
        if existed and index in RELOADED_ON_VISIT:
            # A new view loads itself when it is mounted. An existing one is
            # re-read on every visit: memories also appear in the background
            # (the consolidation job proposes candidates after a chat) and so
            # do the bot's decisions.
            page.run_task(getattr(view, RELOADED_ON_VISIT[index]))
        for polled_index, polled_view in views.items():
            if isinstance(polled_view, (FilesView, OracleView)):
                # Poll file statuses and the bot's journal only while their tab
                # is the visible one.
                polled_view.set_active(polled_index == index)

    tabs.on_change = on_tab_change

    page.add(tabs)


if __name__ == "__main__":
    ft.app(target=main)

