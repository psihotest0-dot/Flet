"""A small emoji menu that sits above the chat input."""

from __future__ import annotations

from collections.abc import Callable

import flet as ft

from views.ui_utils import safe_update

# category name -> (emoji shown on the category button, emoji of the category)
EMOJI_CATEGORIES: dict[str, tuple[str, list[str]]] = {
    "Усмивки": (
        "😀",
        [
            "😀",
            "😃",
            "😄",
            "😁",
            "😆",
            "😅",
            "😂",
            "🤣",
            "😊",
            "😇",
            "🙂",
            "🙃",
            "😉",
            "😌",
            "😍",
            "🥰",
            "😘",
            "😗",
            "😋",
            "😛",
            "😜",
            "🤪",
            "😝",
            "🤑",
            "🤗",
            "🤭",
            "🤫",
            "🤔",
            "🤐",
            "😐",
            "😑",
            "😶",
            "😏",
            "😒",
            "🙄",
            "😬",
            "😔",
            "😪",
            "😴",
            "😷",
            "🤒",
            "🤕",
            "🤢",
            "🤮",
            "🥵",
            "🥶",
            "😵",
            "🤯",
            "🤠",
            "🥳",
            "😎",
            "🤓",
            "🧐",
            "😕",
            "😟",
            "🙁",
            "😮",
            "😲",
            "😳",
            "🥺",
            "😦",
            "😨",
            "😰",
            "😥",
            "😢",
            "😭",
            "😱",
            "😖",
            "😣",
            "😞",
            "😓",
            "😩",
            "😫",
            "🥱",
            "😤",
            "😡",
            "😠",
            "🤬",
            "😈",
            "👿",
            "💀",
            "💩",
            "🤡",
            "👻",
            "👽",
            "🤖",
        ],
    ),
    "Жестове": (
        "👋",
        [
            "👍",
            "👎",
            "👌",
            "✌️",
            "🤞",
            "🤟",
            "🤘",
            "🤙",
            "👈",
            "👉",
            "👆",
            "👇",
            "✋",
            "🤚",
            "🖐️",
            "🖖",
            "👋",
            "🤝",
            "👏",
            "🙌",
            "👐",
            "🤲",
            "🙏",
            "💪",
            "🧠",
            "👀",
            "👂",
            "👃",
            "👄",
        ],
    ),
    "Сърца и символи": (
        "❤️",
        [
            "❤️",
            "🧡",
            "💛",
            "💚",
            "💙",
            "💜",
            "🖤",
            "🤍",
            "🤎",
            "💔",
            "❣️",
            "💕",
            "💞",
            "💓",
            "💗",
            "💖",
            "💘",
            "💝",
            "✨",
            "🌟",
            "⭐",
            "💫",
            "🔥",
            "💥",
            "💯",
            "✅",
            "❌",
            "❓",
            "❗",
            "⚠️",
            "🎉",
            "🎊",
        ],
    ),
    "Животни и природа": (
        "🐶",
        [
            "🐶",
            "🐱",
            "🐭",
            "🐹",
            "🐰",
            "🦊",
            "🐻",
            "🐼",
            "🐨",
            "🐯",
            "🦁",
            "🐮",
            "🐷",
            "🐸",
            "🐵",
            "🐔",
            "🐧",
            "🐦",
            "🦆",
            "🦉",
            "🐺",
            "🐴",
            "🦄",
            "🐝",
            "🦋",
            "🐌",
            "🐞",
            "🐢",
            "🐍",
            "🐙",
            "🐬",
            "🐳",
            "🌸",
            "🌹",
            "🌻",
            "🌲",
            "🌳",
            "🍀",
            "🌈",
            "☀️",
            "⛅",
            "☁️",
            "🌧️",
            "⚡",
            "❄️",
            "🌊",
        ],
    ),
    "Храна и напитки": (
        "🍕",
        [
            "🍎",
            "🍐",
            "🍊",
            "🍋",
            "🍌",
            "🍉",
            "🍇",
            "🍓",
            "🍒",
            "🍑",
            "🥭",
            "🍍",
            "🥝",
            "🍅",
            "🥑",
            "🥕",
            "🌽",
            "🥔",
            "🍞",
            "🧀",
            "🥚",
            "🍳",
            "🥓",
            "🍔",
            "🍟",
            "🍕",
            "🌭",
            "🥪",
            "🌮",
            "🍝",
            "🍜",
            "🍣",
            "🍦",
            "🍩",
            "🍪",
            "🎂",
            "🍰",
            "🍫",
            "🍬",
            "☕",
            "🍵",
            "🍺",
            "🍷",
            "🥂",
            "🍾",
        ],
    ),
    "Дейности и предмети": (
        "⚽",
        [
            "⚽",
            "🏀",
            "🏈",
            "⚾",
            "🎾",
            "🏐",
            "🎱",
            "🏓",
            "🎮",
            "🎲",
            "🎯",
            "🎨",
            "🎭",
            "🎤",
            "🎧",
            "🎸",
            "🎹",
            "🎬",
            "📚",
            "✏️",
            "💡",
            "💻",
            "📱",
            "📷",
            "🚗",
            "🚕",
            "🚌",
            "🚀",
            "✈️",
            "🚲",
            "🏠",
            "🏖️",
            "⛰️",
            "🎁",
            "🏆",
            "🥇",
            "💰",
            "💎",
            "🔑",
            "🔔",
            "📅",
            "⏰",
        ],
    ),
}

DEFAULT_CATEGORY = next(iter(EMOJI_CATEGORIES))

CELL_SIZE = 44
GRID_HEIGHT = 140
SELECTED_CATEGORY_BGCOLOR = ft.Colors.BLUE_GREY_700
UNSELECTED_CATEGORY_BGCOLOR = ft.Colors.TRANSPARENT


class EmojiPicker(ft.Container):
    """Category tabs plus a scrollable grid; ``on_pick(emoji)`` runs on a click.

    The picker starts hidden - the chat shows it with ``visible = True``.
    """

    def __init__(self, on_pick: Callable[[str], None]):
        super().__init__()
        self._on_pick = on_pick
        self.selected_category = DEFAULT_CATEGORY
        # Cells are built the first time a category is opened.
        self._cells: dict[str, list[ft.Control]] = {}

        self.category_buttons: dict[str, ft.Container] = {
            name: ft.Container(
                content=ft.Text(icon, size=20),
                padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                border_radius=8,
                ink=True,
                tooltip=name,
                data=name,
                bgcolor=(
                    SELECTED_CATEGORY_BGCOLOR
                    if name == DEFAULT_CATEGORY
                    else UNSELECTED_CATEGORY_BGCOLOR
                ),
                on_click=self._handle_category_click,
            )
            for name, (icon, _emojis) in EMOJI_CATEGORIES.items()
        }

        self.grid = ft.GridView(
            controls=self._cells_for(DEFAULT_CATEGORY),
            max_extent=CELL_SIZE,
            child_aspect_ratio=1,
            spacing=2,
            run_spacing=2,
            height=GRID_HEIGHT,
        )

        self.visible = False
        self.padding = 8
        self.border_radius = 10
        self.bgcolor = ft.Colors.BLUE_GREY_900
        self.content = ft.Column(
            controls=[
                ft.Row(list(self.category_buttons.values()), spacing=2, wrap=True),
                self.grid,
            ],
            spacing=4,
            tight=True,
        )

    def _cells_for(self, category: str) -> list[ft.Control]:
        if category not in self._cells:
            self._cells[category] = [
                ft.Container(
                    content=ft.Text(emoji, size=24),
                    alignment=ft.Alignment.CENTER,
                    border_radius=8,
                    ink=True,
                    data=emoji,
                    on_click=self._handle_emoji_click,
                )
                for emoji in EMOJI_CATEGORIES[category][1]
            ]
        return self._cells[category]

    def select_category(self, name: str) -> None:
        """Show the emoji of ``name``; an unknown or the current name is ignored."""
        if name not in EMOJI_CATEGORIES or name == self.selected_category:
            return
        previous = self.category_buttons[self.selected_category]
        current = self.category_buttons[name]
        previous.bgcolor = UNSELECTED_CATEGORY_BGCOLOR
        current.bgcolor = SELECTED_CATEGORY_BGCOLOR
        self.selected_category = name
        self.grid.controls = self._cells_for(name)
        safe_update(self, previous, current, self.grid)

    def _handle_category_click(self, e) -> None:
        self.select_category(e.control.data)

    def _handle_emoji_click(self, e) -> None:
        self._on_pick(e.control.data)
