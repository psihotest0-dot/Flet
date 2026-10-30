from __future__ import annotations

"""Small helpers shared by the views."""

import asyncio
import logging

import flet as ft

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
