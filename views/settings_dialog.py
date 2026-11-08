"""Settings dialog: backend address, connection check, chat name and the user_id."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from urllib.parse import urlsplit

import flet as ft

from api.client import APIClient
from state.settings import DEFAULT_DISPLAY_NAME, MAX_DISPLAY_NAME_CHARS
from views.ui_utils import safe_update

logger = logging.getLogger(__name__)

USER_ID_NOTE = (
    "Това е ключът към историята ти - запази settings.json. "
    "Нужен е и за свързване на Telegram (/link)."
)


def validate_base_url(value: str | None) -> str:
    """Return ``value`` as a clean Base URL or raise ``ValueError`` with the reason.

    Surrounding blanks and a trailing ``/`` are dropped (both are paste
    artefacts); anything else that would not form a usable address is refused.
    """
    url = (value or "").strip()
    if not url:
        raise ValueError("Въведи адрес на бекенда.")
    if any(ch.isspace() for ch in url):
        raise ValueError("Адресът не може да съдържа интервали.")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("Адресът трябва да започва с http:// или https://")

    parts = urlsplit(url)
    try:
        _ = parts.port  # raises for a non-numeric or out-of-range port
    except ValueError:
        raise ValueError("Невалиден порт.") from None
    if not parts.hostname:
        raise ValueError("Липсва име на хост, напр. http://127.0.0.1:8080")
    if parts.query or parts.fragment:
        raise ValueError("Адресът не може да съдържа ? или #.")
    return url.rstrip("/")


def clean_display_name(value: str | None) -> str:
    """The name for the user's chat messages: trimmed, at most the allowed length.

    A blank name means "the default", as in ``Settings``.
    """
    name = (value or "").strip()[:MAX_DISPLAY_NAME_CHARS].strip()
    return name or DEFAULT_DISPLAY_NAME


class SettingsDialog:
    """Modal dialog for the Base URL, the chat name and a read-only ``user_id``.

    Saving only validates and hands the clean URL and name to ``on_save``;
    persisting them and swapping the API client stays with the app (``main.py``).
    """

    def __init__(
        self,
        page: ft.Page,
        user_id: str | None,
        base_url: str,
        on_save: Callable[[str, str], None],
        display_name: str = DEFAULT_DISPLAY_NAME,
    ):
        self.page = page
        self.user_id = user_id or ""
        self._on_save = on_save
        self._checking = False

        self.base_url_field = ft.TextField(
            label="Base URL",
            value=base_url,
            on_submit=self._save,
        )
        self.check_btn = ft.OutlinedButton(
            "Провери връзката",
            icon=ft.icons.NETWORK_CHECK,
            on_click=self._check_connection,
        )
        self.check_progress = ft.ProgressRing(
            width=16, height=16, stroke_width=2, visible=False
        )
        self.check_result = ft.Text("", size=13, visible=False)

        self.display_name_field = ft.TextField(
            label="Име в чата",
            value=display_name,
            max_length=MAX_DISPLAY_NAME_CHARS,
            helper_text=f"Как се казваш в чата. Празно = „{DEFAULT_DISPLAY_NAME}“.",
            on_submit=self._save,
        )

        self.user_id_field = ft.TextField(
            label="User ID",
            value=self.user_id,
            read_only=True,
            text_size=13,
            expand=True,
        )
        self.copy_btn = ft.IconButton(
            icon=ft.icons.COPY,
            tooltip="Копирай",
            on_click=self._copy_user_id,
        )

        self.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Настройки"),
            content=ft.Column(
                [
                    self.base_url_field,
                    ft.Row(
                        [self.check_btn, self.check_progress],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.check_result,
                    self.display_name_field,
                    ft.Divider(),
                    ft.Row([self.user_id_field, self.copy_btn]),
                    ft.Text(USER_ID_NOTE, size=12, color=ft.colors.GREY_500),
                ],
                tight=True,
                width=480,
            ),
            actions=[
                ft.TextButton("Отказ", on_click=self._cancel),
                ft.TextButton("Запази", on_click=self._save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

    # ------------------------------------------------------------------ open
    def open(self, base_url: str, display_name: str = DEFAULT_DISPLAY_NAME) -> None:
        """Show the dialog with the currently saved values and a clean state."""
        self.base_url_field.value = base_url
        self.base_url_field.error_text = None
        self.display_name_field.value = display_name
        self.check_result.visible = False
        self.copy_btn.icon = ft.icons.COPY
        self.copy_btn.icon_color = None
        self.copy_btn.tooltip = "Копирай"
        self.page.open(self.dialog)

    def _cancel(self, e) -> None:
        self.page.close(self.dialog)

    def _save(self, e) -> None:
        url = self._validated_url()
        if url is None:
            return
        self.page.close(self.dialog)
        self._on_save(url, clean_display_name(self.display_name_field.value))

    def _validated_url(self) -> str | None:
        """The clean address, or ``None`` after marking the field as invalid."""
        try:
            url = validate_base_url(self.base_url_field.value)
        except ValueError as ex:
            self.base_url_field.error_text = str(ex)
            url = None
        else:
            self.base_url_field.error_text = None
        safe_update(self.dialog, self.base_url_field)
        return url

    # ------------------------------------------------------------- user id
    def _copy_user_id(self, e) -> None:
        self.page.set_clipboard(self.user_id)
        self.copy_btn.icon = ft.icons.CHECK
        self.copy_btn.icon_color = ft.colors.GREEN
        self.copy_btn.tooltip = "Копирано"
        safe_update(self.dialog, self.copy_btn)

    # --------------------------------------------------------- connection
    async def _check_connection(self, e) -> None:
        """Probe ``/healthz`` of the address in the field (saved or not)."""
        if self._checking:
            return
        url = self._validated_url()
        if url is None:
            return

        self._set_checking(True)
        healthy = False
        try:
            # The probe has to use the typed address, not the saved client.
            async with APIClient(base_url=url, user_id=self.user_id) as client:
                healthy = await client.check_health()
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.warning("Connection check failed: %s", ex)
        finally:
            self._set_checking(False)

        logger.info("Connection check for %s: %s", url, "ok" if healthy else "no answer")
        if healthy:
            self._show_result("Връзката е успешна.", ft.colors.GREEN)
        else:
            self._show_result(f"Няма връзка с {url}", ft.colors.RED)

    def _set_checking(self, checking: bool) -> None:
        self._checking = checking
        self.check_btn.disabled = checking
        self.check_progress.visible = checking
        if checking:
            self.check_result.visible = False
        safe_update(
            self.dialog, self.check_btn, self.check_progress, self.check_result
        )

    def _show_result(self, text: str, color: str) -> None:
        self.check_result.value = text
        self.check_result.color = color
        self.check_result.visible = True
        safe_update(self.dialog, self.check_result)
