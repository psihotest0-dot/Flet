"""Small helpers shared by the views."""

from __future__ import annotations

import asyncio
import logging
import weakref

import flet as ft
import httpx

from api.client import APIError, StreamError

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


def open_dialog(page: ft.Page | None, dialog: ft.DialogControl) -> None:
    """Show a dialog, a bottom sheet or a snackbar on the page."""
    if page is None:
        return
    try:
        page.show_dialog(dialog)
    except RuntimeError as ex:
        # Still on its way out: the client has not reported the last close yet.
        logger.warning("The dialog was not opened: %s", ex)


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
    except asyncio.CancelledError:
        raise
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
    except asyncio.CancelledError:
        raise
    except Exception as ex:  # noqa: BLE001
        logger.warning("Could not open the link: %s", ex)
        return False
    return True


def error_detail(error: BaseException) -> str:
    """Short text for the tail of "Грешка при ...: <detail>" messages.

    ``str()`` of a connection error is English jargon, and of a timeout it is
    empty - which left the snackbar ending in a bare colon.
    """
    if isinstance(error, httpx.TransportError):
        return "бекендът не е достъпен"
    return str(error) or type(error).__name__


def describe_error(error: BaseException) -> str:
    """User-facing text that tells apart the ways a chat request can fail."""
    if isinstance(error, httpx.TransportError):
        # Connection refused, DNS, timeout or a connection dropped mid-stream.
        return "Бекендът не е достъпен. Провери връзката и опитай отново."
    if isinstance(error, APIError):
        if error.status >= 500:
            return f"Грешка в бекенда (HTTP {error.status}). Опитай отново по-късно."
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
            bgcolor=ft.Colors.GREEN if success else ft.Colors.RED,
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
                style=ft.ButtonStyle(color=ft.Colors.RED) if destructive else None,
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
