"""Small helpers shared by the views."""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable
from pathlib import PurePath

import flet as ft
import httpx

from api.errors import APIError, StreamError
from views import theme

logger = logging.getLogger(__name__)


# One instance of a Flet service (clipboard, URL launcher) for every page. Flet
# drops a service nobody holds after the event that created it, so they are kept.
_services: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def is_mounted(control: ft.BaseControl) -> bool:
    """True while ``control`` is part of the page that is on screen.

    A control that never reached a page has no parent and ``control.page`` raises.
    One that was taken out of the page keeps its parent, but the session has
    forgotten its id: a patch for it would address a control the client threw away.
    """
    if isinstance(control, ft.Page):
        return True
    try:
        return control.page.get_control(control._i) is control
    except RuntimeError:
        # No page yet, or a page whose session is gone (the window was closed).
        return False


def safe_update(owner: ft.Control, *controls: ft.Control) -> None:
    """Update ``controls`` (or ``owner`` when none are given) if they are mounted.

    ``Control.update()`` raises for a control that is not on a page, so calling it
    from a background task after the view was disposed would end the task with an
    error. Updating one leaf control instead of the whole view also keeps the
    patch that is computed and sent small.
    """
    if not is_mounted(owner):
        return
    for control in controls or (owner,):
        if is_mounted(control):
            control.update()


# The dialogs and bottom sheets ``open_dialog`` showed on a page, to ask whether one is
# still open. Flet keeps its own stack in a private attribute, and a snackbar is not
# in the way of anything (it goes by itself), so it is not counted.
_shown: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def open_dialog(page: ft.Page | None, dialog: ft.DialogControl) -> None:
    """Show a dialog, a bottom sheet or a snackbar on the page."""
    if page is None:
        return
    try:
        page.show_dialog(dialog)
    except RuntimeError as ex:
        # Still on its way out: the client has not reported the last close yet.
        logger.warning("The dialog was not opened: %s", ex)
        return
    if not isinstance(dialog, ft.SnackBar):
        _shown.setdefault(page, []).append(weakref.ref(dialog))


def has_open_dialog(page: ft.Page | None) -> bool:
    """Whether a dialog or a bottom sheet shown by ``open_dialog`` is still open.

    A key pressed then belongs to the dialog (``Esc`` closes it), not to the chat
    behind it.
    """
    if page is None:
        return False
    refs = _shown.get(page)
    if not refs:
        return False
    # The ones that were closed or thrown away are forgotten here.
    refs[:] = [ref for ref in refs if (d := ref()) is not None and d.open]
    return bool(refs)


def close_dialog(dialog: ft.DialogControl) -> None:
    """Close this very dialog.

    ``page.pop_dialog()`` closes whatever is on top, and a snackbar shown while a
    dialog is open is on top of it.
    """
    dialog.open = False
    if is_mounted(dialog):
        dialog.update()


def _service(page: ft.Page, service_type: type):
    per_page = _services.setdefault(page, {})
    service = per_page.get(service_type)
    if service is None:
        # Created inside an event handler or a page task, where Flet registers a
        # new service with the current page by itself.
        service = per_page[service_type] = service_type()
    return service


async def copy_text(page: ft.Page | None, text: str) -> bool:
    """Put ``text`` on the clipboard; ``False`` (and a line in the log) if it failed."""
    if page is None:
        return False
    try:
        await _service(page, ft.Clipboard).set(text)
    except Exception as ex:  # noqa: BLE001
        logger.warning("Could not copy to the clipboard: %s", ex)
        return False
    return True


async def open_url(page: ft.Page | None, url: str) -> bool:
    """Open ``url`` in the system browser; ``False`` (and a line in the log) if it failed."""
    if page is None:
        return False
    try:
        await _service(page, ft.UrlLauncher).launch_url(url)
    except Exception as ex:  # noqa: BLE001
        logger.warning("Could not open the link: %s", ex)
        return False
    return True


# The backend answers 413 in English ("file too large") to a body over its limit.
TOO_LARGE_TEXT = "над позволения от бекенда размер"

# What a snackbar says when ``save_settings`` returned ``False`` (main and the chat).
SETTINGS_NOT_SAVED_TEXT = "Неуспешно запазване на настройките."


def is_unexpected(error: BaseException) -> bool:
    """Whether ``error`` is a bug rather than something that happens to a client.

    A dropped connection, an answer with an error status, an ``event: error``
    frame and a file or a disk that is not there (``OSError``, a timeout among
    them) are told to the user and written to the log as one line; a traceback
    would only fill ``app.log`` while the backend is down, and the message of an
    ``OSError`` carries the path of a file. Anything else (``AttributeError``, a
    malformed answer, ...) has no explanation but its place in the code, so the
    handler logs it with ``exc_info=is_unexpected(ex)``.
    """
    return not isinstance(error, (httpx.TransportError, APIError, StreamError, OSError))


def file_kind(name: str) -> str:
    """What the log says of a file of the user instead of its name: its type.

    The name is the user's own text (``диагноза_Иван.pdf``), like a message or a
    memory, so it stays on the screen and out of ``app.log``.
    """
    suffix = PurePath(name).suffix.lower()[:12]
    return f"a {suffix} file" if suffix else "a file without an extension"


def error_for_log(error: BaseException) -> str:
    """The text of ``error`` for the log: without the path an ``OSError`` carries.

    ``str(PermissionError)`` is ``[Errno 13] Permission denied: 'C:\\...\\name.pdf'``;
    the log keeps the kind and the number, which is what finds the cause.
    """
    if isinstance(error, OSError):
        return f"{type(error).__name__} (errno {error.errno})"
    return str(error) or type(error).__name__


def error_detail(error: BaseException) -> str:
    """Short text for the tail of "Грешка при ...: <detail>" messages.

    ``str()`` of a connection error is English jargon, and of a timeout it is
    empty - which left the snackbar ending in a bare colon.
    """
    if isinstance(error, httpx.TransportError):
        return "бекендът не е достъпен"
    if isinstance(error, APIError) and error.status == 413:
        return TOO_LARGE_TEXT
    return str(error) or type(error).__name__


def describe_error(error: BaseException) -> str:
    """User-facing text that tells apart the ways a chat request can fail."""
    if isinstance(error, httpx.TransportError):
        # Connection refused, DNS, timeout or a connection dropped mid-stream.
        return "Бекендът не е достъпен. Провери връзката и опитай отново."
    if isinstance(error, APIError):
        if error.status >= 500:
            return f"Грешка в бекенда (HTTP {error.status}). Опитай отново по-късно."
        if error.status == 413:
            return f"Заявката е отхвърлена: {TOO_LARGE_TEXT}"
        return f"Заявката е отхвърлена: {error.message}"
    if isinstance(error, StreamError):
        return "Нещо се обърка. Опитай отново."
    return "Грешка при получаване на отговора."


def show_snackbar(page: ft.Page | None, message: str, success: bool = False) -> None:
    """Show a red (error) or green (success) snackbar on the given page."""
    open_dialog(
        page,
        ft.SnackBar(
            content=ft.Text(message, color=ft.Colors.WHITE),
            bgcolor=theme.TOAST_OK if success else theme.TOAST_PROBLEM,
        ),
    )


async def confirm_dialog(
    page: ft.Page | None,
    title: str,
    message: str,
    confirm_text: str = "Изтрий",
    cancel_text: str = "Отказ",
    destructive: bool = True,
) -> bool:
    """Awaitable modal confirmation; returns True only when the user confirms.

    The confirm button is red unless ``destructive`` is false (a question that is
    not about losing something).
    """
    if page is None:
        return False

    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    def close(result: bool) -> None:
        if not future.done():
            future.set_result(result)
        close_dialog(dialog)

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text(title),
        content=ft.Text(message),
        actions=[
            ft.TextButton(cancel_text, on_click=lambda e: close(False)),
            ft.TextButton(
                confirm_text,
                on_click=lambda e: close(True),
                style=ft.ButtonStyle(color=theme.PROBLEM) if destructive else None,
            ),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    open_dialog(page, dialog)
    try:
        return await future
    finally:
        if not future.done():
            future.cancel()


async def delete_on_backend(
    page: ft.Page | None,
    delete: Callable[[], Awaitable[object]],
    what: str,
    log_what: str,
) -> bool:
    """Run ``delete``; ``True`` when the thing is gone, also when it already was.

    A 404 means it was deleted already (from another client): what the user wanted
    is true, so it counts as done. Any other failure is logged, shown as
    "Грешка при изтриване на <what>: ..." and gives ``False``. ``log_what`` names
    the thing in the log (an id at most, never a name the user gave it).
    """
    try:
        await delete()
    except APIError as ex:
        if ex.status != 404:
            logger.error("Failed to delete %s: %s", log_what, ex)
            show_snackbar(page, f"Грешка при изтриване на {what}: {error_detail(ex)}")
            return False
        logger.info("Already deleted on the backend: %s", log_what)
    except Exception as ex:
        logger.error(
            "Failed to delete %s: %s", log_what, ex, exc_info=is_unexpected(ex)
        )
        show_snackbar(page, f"Грешка при изтриване на {what}: {error_detail(ex)}")
        return False
    return True


class PollingTab:
    """The lifecycle of a tab that loads once it is mounted and polls while it is seen.

    The view that mixes this in calls :meth:`_init_polling` in its ``__init__``,
    keeps ``page_ref`` and names its two coroutines in :meth:`_load_job` and
    :meth:`_poll_job`; the poll loop runs ``while self._polling``. ``main`` calls
    :meth:`set_active` when the tabs change and ``will_unmount`` when the window
    closes.
    """

    page_ref: ft.Page | None

    def _init_polling(self) -> None:
        self._polling = False
        self._active = False
        self._load_task = None
        self._poll_task = None

    def _load_job(self) -> Callable[..., Awaitable[object]]:
        """The coroutine that fills the tab once it is on the page."""
        raise NotImplementedError

    def _poll_job(self) -> Callable[..., Awaitable[object]]:
        """The loop that refreshes the tab while it is the visible one."""
        raise NotImplementedError

    def did_mount(self):
        if self._load_task is None:
            self._load_task = self.page_ref.run_task(self._load_job())
        if self._active:
            self.set_active(True)

    def set_active(self, active: bool):
        """Poll only while this tab is the visible one."""
        self._active = active
        if active:
            if self._poll_task is None and self.page_ref is not None:
                self._polling = True
                self._poll_task = self.page_ref.run_task(self._poll_job())
        else:
            self._stop_polling()

    def will_unmount(self):
        self._active = False
        self._stop_polling()
        if self._load_task is not None:
            self._load_task.cancel()
            self._load_task = None

    def _stop_polling(self) -> None:
        self._polling = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
