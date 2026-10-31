from __future__ import annotations

"""Small helpers shared by the views."""

import asyncio
import logging

import flet as ft
import httpx

from api.client import APIError, StreamError

logger = logging.getLogger(__name__)


def is_attached(owner: ft.Control) -> bool:
    """True when the control is still part of a live page tree."""
    if isinstance(owner, ft.Page):
        return True
    return owner.page is not None


def safe_update(owner: ft.Control, *controls: ft.Control) -> None:
    """Update ``controls`` (or ``owner`` when none are given) if they are mounted.

    Flet 0.24 ``Control.update()`` asserts that the control is attached to a page
    and is a *synchronous* round trip to the Flutter client, so calling it from a
    background task after the view was disposed raises. Updating one leaf control
    instead of the whole view also avoids re-sending an entire control tree.
    """
    if not is_attached(owner):
        return
    for control in controls or (owner,):
        if control.uid is not None:
            control.update()


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
    if page is None:
        return
    page.open(
        ft.SnackBar(
            content=ft.Text(message, color=ft.colors.WHITE),
            bgcolor=ft.colors.GREEN if success else ft.colors.RED,
        )
    )


async def confirm_dialog(
    page: ft.Page | None,
    title: str,
    message: str,
    confirm_text: str = "Изтрий",
    cancel_text: str = "Отказ",
) -> bool:
    """Awaitable modal confirmation; returns True only when the user confirms."""
    if page is None:
        return False

    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    def close(result: bool) -> None:
        if not future.done():
            future.set_result(result)
        page.close(dialog)

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text(title),
        content=ft.Text(message),
        actions=[
            ft.TextButton(cancel_text, on_click=lambda e: close(False)),
            ft.TextButton(
                confirm_text,
                on_click=lambda e: close(True),
                style=ft.ButtonStyle(color=ft.colors.RED),
            ),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    page.open(dialog)
    try:
        return await future
    finally:
        if not future.done():
            future.cancel()
