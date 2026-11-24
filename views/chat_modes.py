"""The buttons of the modes: one menu in the place of the paperclip (the owner's idea, 2026-11-24).

A mode sends the next message down one road whatever it says - for the times the person does not
remember how a tool is asked for: the backend makes the turn call a tool of that road (``mode`` of
``POST /chat/stream``). It is for one message: the send clears it, and so does the cross on its
chip or another conversation. Without a mode everything is as before. The paperclip is the first
item of the menu.

``ChatModesMixin`` is a part of ``ChatView``: it works on the state that ``ChatView.__init__``
creates - ``input_field``, ``mode_chip`` and ``chat_mode`` - and calls ``on_attach_click``.
"""

from __future__ import annotations

import flet as ft

from views import theme
from views.ui_utils import safe_update

# (mode of the request, label, icon, the hint of the input while it is chosen)
MODES: tuple[tuple[str, str, ft.IconData, str], ...] = (
    ("books", "Книги", ft.Icons.MENU_BOOK, "Какво да потърся в книгите?"),
    ("web", "Интернет", ft.Icons.TRAVEL_EXPLORE, "Какво да потърся в интернет?"),
    (
        "library",
        "Библиотека",
        ft.Icons.LIBRARY_ADD,
        "Линк (или няколко) за библиотеката…",
    ),
    (
        "files",
        "Бележките на Лили",
        ft.Icons.EDIT_NOTE,
        "Какво да запиша / прочета в бележките?",
    ),
    ("memory", "Памет", ft.Icons.PSYCHOLOGY, "Какво да запомня или да си спомня?"),
    (
        "style",
        "Как да говорим",
        ft.Icons.RECORD_VOICE_OVER,
        "Как да ти отговарям отсега нататък?",
    ),
    (
        "reminder",
        "Напомняне",
        ft.Icons.ALARM,
        "Какво и кога да ти напомня? (напр. утре в 9 да платя тока)",
    ),
)
MODE_LABELS = {key: label for key, label, _icon, _hint in MODES}
MODE_HINTS = {key: hint for key, _label, _icon, hint in MODES}
MODE_ICONS = {key: icon for key, _label, icon, _hint in MODES}
DEFAULT_HINT = "Напиши съобщение..."


class ChatModesMixin:
    """The menu of the modes of ``ChatView`` and the chip of the chosen one."""

    def _build_mode_menu(self) -> ft.PopupMenuButton:
        """The menu: the paperclip first, then the modes."""
        items = [
            ft.PopupMenuItem(
                content="Прикачи файл",
                icon=ft.Icons.ATTACH_FILE,
                on_click=self.on_attach_click,
            ),
            ft.PopupMenuItem(
                content="Сметка (PDF или снимка)",
                icon=ft.Icons.RECEIPT_LONG,
                on_click=self.on_bill_click,
            ),
        ]
        for key, label, icon, _hint in MODES:
            items.append(
                ft.PopupMenuItem(
                    content=label,
                    icon=icon,
                    on_click=lambda e, key=key: self.choose_mode(key),
                )
            )
        return ft.PopupMenuButton(
            icon=ft.Icons.ADD_CIRCLE_OUTLINE,
            tooltip="Прикачи файл или избери пътя на следващото съобщение",
            items=items,
        )

    def _build_mode_chip(self) -> ft.Container:
        """The chip above the input while a mode is chosen (hidden until then)."""
        self.mode_chip_text = ft.Text("", size=13)
        self.mode_chip_icon = ft.Icon(ft.Icons.TUNE, size=16, color=theme.ACCENT)
        return ft.Container(
            content=ft.Row(
                [
                    self.mode_chip_icon,
                    self.mode_chip_text,
                    ft.IconButton(
                        icon=ft.Icons.CLOSE,
                        icon_size=16,
                        tooltip="Без режим",
                        on_click=lambda e: self.clear_mode(),
                    ),
                ],
                spacing=6,
                tight=True,
            ),
            padding=ft.Padding.only(left=10, right=2),
            border_radius=16,
            border=ft.Border.all(1, theme.ACCENT),
            visible=False,
        )

    def choose_mode(self, mode: str) -> None:
        """Send the next message down the road of ``mode``."""
        self.chat_mode = mode
        self._show_mode()
        self._focus_input()

    def clear_mode(self) -> None:
        """No mode: the next message goes as always."""
        if self.chat_mode is None:
            return
        self.chat_mode = None
        self._show_mode()

    def _mode_options(self) -> dict:
        """The ``mode`` of the request, when one is chosen."""
        return {"mode": self.chat_mode} if self.chat_mode else {}

    def _show_mode(self) -> None:
        mode = self.chat_mode
        self.mode_chip.visible = mode is not None
        if mode is not None:
            self.mode_chip_text.value = f"Следващото съобщение: {MODE_LABELS[mode]}"
            self.mode_chip_icon.icon = MODE_ICONS[mode]
        self.input_field.hint_text = MODE_HINTS[mode] if mode else DEFAULT_HINT
        safe_update(self, self.mode_chip, self.input_field)
