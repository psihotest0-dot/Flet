import asyncio
import logging
import os
import sys
import threading
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

import flet as ft

from api.client import APIClient
from state.settings import (
    DEFAULT_DISPLAY_NAME,
    THEME_DARK,
    THEME_LIGHT,
    load_settings,
    save_settings,
)
from views import theme
from views.chat import ChatView
from views.files import FilesView
from views.memory import MemoryView
from views.oracle import OracleView
from views.settings_dialog import SettingsDialog, is_plain_http_to_another_computer
from views.ui_utils import (
    SETTINGS_NOT_SAVED_TEXT,
    has_open_dialog,
    is_unexpected,
    safe_update,
    show_snackbar,
)
from views.user_files import UserFilesView

logger = logging.getLogger(__name__)

LOG_FILE = Path(__file__).with_name("app.log")
HEALTH_CHECK_INTERVAL = 10
# How long closing the window waits for the HTTP client; then the window goes anyway.
CLOSE_TIMEOUT = 5.0
TAB_LABELS = ("Чат", "Памет", "Файлове", "Бот", "Файлове на Лили")
# Tab index -> the view method that re-reads its data when the tab is opened again.
RELOADED_ON_VISIT = {1: "load_memories", 3: "refresh", 4: "refresh"}
# Libraries that log every single request on INFO.
NOISY_LOGGERS = ("httpx", "httpcore")
UNEXPECTED_ERROR_TEXT = "Възникна неочаквана грешка. Подробностите са в app.log."


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
        handlers=[
            RotatingFileHandler(
                LOG_FILE, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
            )
        ],
        level=logging.INFO,
        # The logger name says which module wrote the line (api.client, views.chat, ...).
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
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
    threading.excepthook = lambda args: log_unhandled(
        args.exc_type, args.exc_value, args.exc_traceback
    )


def keep_the_window_alive(page: ft.Page) -> None:
    """Show a message instead of Flet's error screen when an event handler raises.

    Flet 1.x answers an exception that escaped a handler by telling the client
    that the session crashed: the whole window becomes "Error running app" and
    stays so until the app is restarted - the answer being streamed and the text
    being typed go with it. By the time Flet calls ``session.error`` it has
    written the traceback to the log (logger ``flet``); from here on that call
    only tells the user, and the window stays as it was.

    Called at the end of ``main``: a failure while the window is being built
    still gets Flet's screen - there is nothing to keep alive yet.
    """

    def report(message: str) -> None:
        try:
            show_snackbar(page, UNEXPECTED_ERROR_TEXT)
        except Exception as ex:  # noqa: BLE001
            # Raised here it would escape Flet's own handler of the first error.
            logger.debug("The error message was not shown: %s", ex)

    page.session.error = report


async def run_health_checks(probe, on_change, interval: float = HEALTH_CHECK_INTERVAL):
    """Poll ``probe`` forever and call ``on_change(healthy)`` only on a flip.

    Repainting the status indicator on every poll marked the whole appbar dirty
    every few seconds for no reason.
    """
    healthy = None
    while True:
        try:
            is_healthy = await probe()
        except Exception as ex:
            logger.warning("Health check failed: %s", ex, exc_info=is_unexpected(ex))
            is_healthy = False

        if is_healthy != healthy:
            healthy = is_healthy
            on_change(healthy)

        await asyncio.sleep(interval)


def main(page: ft.Page):
    setup_logging()
    page.title = "AI Agent Client"
    page.window.width = 1100
    page.window.height = 800
    page.window.min_width = 720
    page.window.min_height = 520

    settings = load_settings()
    theme.apply_theme(page, settings.theme)

    # The stored user_id is the identity that owns the chat, memory and file
    # history on the backend, so it is reused verbatim. Minting a new one (or
    # "fixing" it) silently orphans everything that belongs to the old id;
    # only a settings file without any user_id gets a fresh one.
    if not settings.user_id:
        settings.user_id = str(uuid.uuid4())
        save_settings(settings)
        logger.info(
            "No user_id in settings, generated one: %s", short_id(settings.user_id)
        )
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
    if is_plain_http_to_another_computer(settings.base_url):
        logger.warning(
            "The backend at %s is reached over plain http: the user_id and the "
            "chats are not encrypted on the way.",
            settings.base_url,
        )
    api_client = APIClient(base_url=settings.base_url, user_id=settings.user_id)
    # index -> view; tabs are created on first activation (see ensure_view below).
    views: dict[int, ft.Control] = {}

    def apply_settings(new_url: str, display_name: str = DEFAULT_DISPLAY_NAME):
        """Persist the validated Base URL and chat name; point the client at the URL."""
        # The chat view works on this very object (it keeps its session_id and
        # chat_sessions in it), so nothing is read back from the file: what is
        # saved is what every part of the app has seen, user_id included.
        snapshot = settings.snapshot()
        settings.base_url = new_url
        settings.display_name = display_name

        if not save_settings(settings):
            settings.restore(snapshot)
            # save_settings has logged the reason; this line says what was lost.
            logger.error("The new settings (Base URL %s) were not saved.", new_url)
            show_snackbar(page, SETTINGS_NOT_SAVED_TEXT)
            return
        logger.info("Settings saved: Base URL %s.", new_url)

        # The one client is kept and only pointed at the new address. Replacing
        # and closing it cut off an answer that was being streamed, broke the
        # proposal cards and the chat messages (they hold the client) and left
        # "Спри" cancelling on a client that was not streaming.
        if api_client.base_url != settings.base_url:
            api_client.set_base_url(settings.base_url)
            # The tooltip and the banner name the address.
            show_connection()
        for view in views.values():
            if isinstance(view, ChatView):
                view.set_display_name(settings.display_name)

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

    # What the last probe found: ``None`` until the first one has answered, then
    # whether the backend is there. The indicator, its tooltip and the banner are
    # all drawn from it by ``show_connection``.
    healthy: bool | None = None

    status_icon = ft.Icon(ft.Icons.CIRCLE, size=16)
    status_label = ft.Text(size=13)
    status_pill = ft.Container(
        content=ft.Row([status_icon, status_label], spacing=6, tight=True),
        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
        border_radius=14,
    )
    offline_text = ft.Text(color=theme.ON_PROBLEM_BACKGROUND, expand=True)
    offline_banner = ft.Container(
        content=ft.Row(
            [
                ft.Icon(ft.Icons.CLOUD_OFF, color=theme.ON_PROBLEM_BACKGROUND),
                offline_text,
                ft.TextButton("Настройки", on_click=open_settings),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor=theme.PROBLEM_BACKGROUND,
        padding=ft.Padding.symmetric(horizontal=16, vertical=4),
        border_radius=8,
        visible=False,
    )

    def show_connection() -> None:
        """Draw the indicator and the banner from ``healthy`` and the address now set."""
        url = api_client.base_url
        if healthy is None:
            icon_color, label, tip = theme.MUTED, "Връзка…", "Проверявам връзката."
            label_color, background = theme.MUTED, None
        elif healthy:
            icon_color, label, tip = theme.OK, "Свързан", f"Свързан с {url}"
            label_color, background = theme.MUTED, None
        else:
            icon_color, label, tip = (
                theme.PROBLEM,
                "Няма връзка",
                f"Няма връзка с {url}",
            )
            label_color, background = (
                theme.ON_PROBLEM_BACKGROUND,
                theme.PROBLEM_BACKGROUND,
            )
        status_icon.color = icon_color
        status_label.value = label
        status_label.color = label_color
        status_pill.bgcolor = background
        status_pill.tooltip = tip
        offline_banner.visible = healthy is False
        offline_text.value = (
            f"Няма връзка с бекенда на {url}. Опитвам отново на всеки "
            f"{HEALTH_CHECK_INTERVAL} секунди."
        )
        # Before the first page update there is nothing to send: the controls go
        # out with the page, already drawn this way.
        safe_update(page, status_pill, offline_banner)

    def set_status(is_healthy: bool):
        nonlocal healthy
        was_offline = healthy is False
        healthy = is_healthy
        show_connection()
        if is_healthy and was_offline:
            logger.info("The connection to the backend is back.")
            show_snackbar(page, "Връзката е възстановена.", success=True)
        elif not is_healthy:
            logger.warning("The backend does not answer.")

    show_connection()

    def theme_button_look(name: str) -> tuple[str, str]:
        """``(icon, tooltip)`` of the button that goes to the other theme."""
        if name == THEME_DARK:
            return ft.Icons.LIGHT_MODE, "Светла тема"
        return ft.Icons.DARK_MODE, "Тъмна тема"

    theme_button = ft.IconButton(
        icon=theme_button_look(settings.theme)[0],
        tooltip=theme_button_look(settings.theme)[1],
    )

    def toggle_theme(e) -> None:
        """Go to the other theme, and keep the choice for the next start."""
        snapshot = settings.snapshot()
        wanted = THEME_LIGHT if settings.theme == THEME_DARK else THEME_DARK
        settings.theme = wanted
        if not save_settings(settings):
            settings.restore(snapshot)
            # save_settings has logged the reason; this line says what was lost (the
            # theme that was asked for: ``restore`` has put the old one back).
            logger.error("The choice of the %s theme was not saved.", wanted)
            show_snackbar(page, SETTINGS_NOT_SAVED_TEXT)
            return
        logger.info("Theme: %s.", settings.theme)
        theme.apply_theme(page, settings.theme)
        theme_button.icon, theme_button.tooltip = theme_button_look(settings.theme)
        safe_update(page)
        # The one thing that does not follow the theme by itself.
        chat_view.restyle_messages()

    theme_button.on_click = toggle_theme

    page.appbar = ft.AppBar(
        title=ft.Row(
            [ft.Text("AI Agent"), status_pill], alignment=ft.MainAxisAlignment.START
        ),
        actions=[
            theme_button,
            ft.IconButton(ft.Icons.SETTINGS, on_click=open_settings),
        ],
    )

    async def probe_backend() -> bool:
        # Saving a new Base URL points the same client at it, so the indicator
        # follows the new address on its next poll.
        return await api_client.check_health()

    task_handle = page.run_task(run_health_checks, probe_backend, set_status)

    closing = False

    async def on_window_event(e: ft.WindowEvent):
        """Stop what the session runs and let the window go.

        ``page.on_disconnect`` is for a browser tab; a desktop window never
        raised it, so nothing was stopped on exit. With ``prevent_close`` the
        window stays until ``destroy()``. Whatever happens before that call - a
        clean-up that fails, or one that hangs - must not leave a window that
        cannot be closed, so it is in ``finally`` and the wait has a limit.
        """
        nonlocal closing
        if e.type != ft.WindowEventType.CLOSE or closing:
            # A second press of the X while the first is still being served.
            return
        closing = True
        logger.info("The window was closed, the HTTP client is shut down.")
        try:
            task_handle.cancel()
            for view in views.values():
                # Their own clean-up: the pollers, the loads, the stream.
                view.will_unmount()
            # The session has one client for its whole life; it is closed here.
            await asyncio.wait_for(api_client.aclose(), CLOSE_TIMEOUT)
        except Exception as ex:
            logger.warning(
                "Could not shut down cleanly: %s",
                str(ex) or type(ex).__name__,
                exc_info=is_unexpected(ex),
            )
        finally:
            try:
                await page.window.destroy()
            except Exception as ex:
                # Raised out of the handler it would be reported as a crash of
                # the session. The window is still there: let the X work again.
                closing = False
                logger.warning(
                    "The window was not destroyed: %s", ex, exc_info=is_unexpected(ex)
                )

    page.window.prevent_close = True
    page.window.on_event = on_window_event

    # Views are mounted lazily: creating all of them up front issued an API call
    # per tab and started the pollers before the user opened a single tab.
    def on_chat_session_changed(session_id: str) -> None:
        """React to another chat being opened.

        Memories belong to the user, not to a chat, so the Memory tab is not
        reloaded here - it refreshes when its tab is opened.
        """
        logger.info("Active chat session changed to %s", short_id(session_id))

    def create_memory() -> MemoryView:
        return MemoryView(page, api_client)

    def create_files() -> FilesView:
        return FilesView(page, api_client)

    def create_oracle() -> OracleView:
        return OracleView(page, api_client)

    def create_user_files() -> UserFilesView:
        return UserFilesView(page, api_client)

    # The chat tab is built at once (below); the others on their first visit.
    factories = {
        1: create_memory,
        2: create_files,
        3: create_oracle,
        4: create_user_files,
    }

    chat_view = ChatView(
        page, api_client, on_session_changed=on_chat_session_changed, settings=settings
    )
    views[0] = chat_view

    # One slot for every tab; an empty container stands in for a view that has
    # not been opened yet.
    tab_contents = ft.TabBarView(
        expand=True,
        controls=[chat_view, *(ft.Container() for _ in TAB_LABELS[1:])],
    )
    tabs = ft.Tabs(
        length=len(TAB_LABELS),
        selected_index=0,
        animation_duration=300,
        content=ft.Column(
            expand=True,
            controls=[
                ft.TabBar(tabs=[ft.Tab(label=label) for label in TAB_LABELS]),
                tab_contents,
            ],
        ),
        expand=1,
    )

    def ensure_view(index: int) -> ft.Control:
        view = views.get(index)
        if view is None:
            view = factories[index]()
            views[index] = view
            tab_contents.controls[index] = view
            safe_update(page, tab_contents)
        return view

    def on_tab_change(e):
        index = e.control.selected_index or 0
        if index not in views and index not in factories:
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
            if isinstance(polled_view, (ChatView, FilesView, OracleView)):
                # Poll file statuses, the bot's journal and an open Telegram
                # conversation only while their tab is the visible one.
                polled_view.set_active(polled_index == index)

    tabs.on_change = on_tab_change

    def on_key(e: ft.KeyboardEvent):
        """The shortcuts of the chat, while its tab is the open one and no dialog is."""
        chat = views.get(0)
        if isinstance(chat, ChatView) and not tabs.selected_index:
            if not has_open_dialog(page):
                chat.handle_key(e)

    page.on_keyboard_event = on_key

    # The banner first and the tabs last: the tabs take the rest of the height.
    page.add(offline_banner)
    page.add(tabs)
    keep_the_window_alive(page)


if __name__ == "__main__":
    # The views send their own, small updates. Left on, Flet would also diff the
    # whole page after every event whose handler sent none - each scroll tick of
    # the chat and each move of the window among them.
    ft.context.disable_auto_update()
    ft.run(main)
