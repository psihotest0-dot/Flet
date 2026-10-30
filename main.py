import asyncio
import concurrent.futures
import logging
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

import flet as ft

from api.client import APIClient
from state.settings import load_settings, save_settings
from views.chat import ChatView
from views.files import FilesView
from views.memory import MemoryView
from views.ui_utils import show_snackbar

logger = logging.getLogger(__name__)

LOG_FILE = Path(__file__).with_name("app.log")
HEALTH_CHECK_INTERVAL = 10


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


def setup_logging():
    logging.basicConfig(
        handlers=[RotatingFileHandler(LOG_FILE, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")],
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )


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
        logger.info("No user_id in settings, generated one: %s", settings.user_id)
    else:
        logger.info("Using the stored user_id: %s", settings.user_id)
        try:
            uuid.UUID(settings.user_id)
        except ValueError:
            logger.warning(
                "settings.user_id %r is not a canonical UUID; keeping the stored "
                "value so the existing history stays reachable.",
                settings.user_id,
            )

    base_url_field = ft.TextField(label="Base URL", value=settings.base_url)

    api_client = APIClient(base_url=settings.base_url, user_id=settings.user_id)
    # index -> view; tabs are created on first activation (see ensure_view below).
    views: dict[int, ft.Control] = {}

    def close_dlg(e):
        page.close(settings_dialog)

    def save_dlg(e):
        nonlocal api_client

        new_url = (base_url_field.value or "").strip()
        # Re-read the file before writing: the chat view stores its session_id
        # and chat_sessions there, and saving the snapshot loaded at startup
        # would silently revert them to an older session.
        current = load_settings()
        if new_url:
            current.base_url = new_url
        if not current.user_id:
            current.user_id = settings.user_id
        success = save_settings(current)
        page.close(settings_dialog)

        if not success:
            show_snackbar(page, "Неуспешно запазване на настройките.")
            return

        settings.base_url = current.base_url
        settings.user_id = current.user_id
        settings.session_id = current.session_id
        settings.chat_sessions = current.chat_sessions

        # The old client keeps pointing at the previous URL - replace it and
        # hand the new one to every view that was already created.
        previous_client = api_client
        api_client = APIClient(base_url=settings.base_url, user_id=settings.user_id)
        for view in views.values():
            view.api_client = api_client
        if previous_client is not None:
            page.run_task(previous_client.aclose)

        show_snackbar(page, "Настройките са запазени успешно!", success=True)

    settings_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Настройки"),
        content=ft.Column([base_url_field], tight=True),
        actions=[
            ft.TextButton("Отказ", on_click=close_dlg),
            ft.TextButton("Запази", on_click=save_dlg),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    def open_settings(e):
        page.open(settings_dialog)

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

    task_handle = page.run_task(run_health_checks, api_client.check_health, set_status)

    def on_disconnect(e):
        task_handle.cancel()
        try:
            page.run_task(api_client.aclose)
        except Exception:  # noqa: BLE001
            logger.debug("Could not close the HTTP client cleanly.")

    # Views are mounted lazily: creating all three up front issued three API
    # calls and started the files poller before the user opened a single tab.
    def on_chat_session_changed(session_id: str) -> None:
        """Reload the already created tabs that belong to the active session."""
        logger.info("Active chat session changed to %s", session_id)
        memory_view = views.get(1)
        if isinstance(memory_view, MemoryView):
            # Memories are re-read for the newly selected chat so the tab never
            # keeps showing data fetched for the previous session.
            page.run_task(memory_view.load_memories)

    def create_chat() -> ChatView:
        return ChatView(page, api_client, on_session_changed=on_chat_session_changed)

    def create_memory() -> MemoryView:
        return MemoryView(page, api_client)

    def create_files() -> FilesView:
        return FilesView(page, api_client)

    factories = {0: create_chat, 1: create_memory, 2: create_files}

    chat_view = ChatView(page, api_client, on_session_changed=on_chat_session_changed)
    views[0] = chat_view

    tabs = ft.Tabs(
        selected_index=0,
        animation_duration=300,
        tabs=[
            ft.Tab(text="Чат", content=chat_view),
            ft.Tab(text="Памет"),
            ft.Tab(text="Файлове"),
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
        ensure_view(index)
        files_view = views.get(2)
        if isinstance(files_view, FilesView):
            # Poll file statuses only while the "Файлове" tab is visible.
            files_view.set_active(index == 2)

    tabs.on_change = on_tab_change

    page.add(tabs)


if __name__ == "__main__":
    ft.app(target=main)

