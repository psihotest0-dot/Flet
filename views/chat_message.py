"""One message of the chat: its text, sources, proposals and buttons (P33).

``ChatView`` makes one ``ChatMessage`` for every message of the conversation and
feeds it the streamed text. What the model wrote passes ``disarm_images`` before it
is drawn (P20); the sources panel gets its strings from ``views.sources``.
"""

from __future__ import annotations

import logging

import flet as ft

from api.client import APIClient, Proposal
from state.settings import DEFAULT_DISPLAY_NAME
from views.markdown_safety import disarm_images
from views.proposals import ProposalCard
from views.sources import source_details
from views.ui_utils import (
    close_dialog,
    copy_text,
    is_mounted,
    open_dialog,
    open_url,
    safe_update,
)

logger = logging.getLogger(__name__)


class ChatMessage(ft.Column):
    """A component representing a single chat message in the UI."""

    def __init__(
        self,
        role: str,
        text: str,
        sources: list | None = None,
        page: ft.Page | None = None,
        on_regenerate: callable | None = None,
        api_client: APIClient | None = None,
        user_name: str = DEFAULT_DISPLAY_NAME,
    ):
        super().__init__()
        self.role = role
        self.api_client = api_client
        self.text_value = text
        self.sources = sources or []
        self.page_ref = page
        self.on_regenerate = on_regenerate
        self.spacing = 2
        self.horizontal_alignment = (
            ft.CrossAxisAlignment.END if role == "user" else ft.CrossAxisAlignment.START
        )

        self.text_color = (
            ft.Colors.LIGHT_BLUE_200 if role == "user" else ft.Colors.PINK_200
        )
        self.sender_name = user_name if role == "user" else "Лили"
        self.bgcolor = (
            ft.Colors.BLUE_GREY_800 if role == "user" else ft.Colors.BLUE_GREY_900
        )

        self.md = ft.Markdown(
            value=self._shown(self.text_value),
            selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            code_theme=ft.MarkdownCodeTheme.ATOM_ONE_DARK,
            on_tap_link=self._open_link,
            md_style_sheet=ft.MarkdownStyleSheet(
                p_text_style=ft.TextStyle(color=self.text_color),
            ),
        )

        self.msg_container = ft.Container(
            content=self.md,
            padding=10,
            border_radius=10,
            bgcolor=self.bgcolor,
        )

        self.sender_label = ft.Text(
            value=self.sender_name,
            size=12,
            color=ft.Colors.GREY_500,
        )

        self.copy_btn = ft.IconButton(
            icon=ft.Icons.COPY,
            icon_size=14,
            tooltip="Копирай",
            on_click=self.copy_to_clipboard,
        )

        top_controls = [self.sender_label, self.copy_btn]
        if role != "user":
            self.regenerate_btn = ft.IconButton(
                icon=ft.Icons.REFRESH,
                icon_size=14,
                tooltip="Опитай отново",
                on_click=self._handle_regenerate,
                visible=False,
            )
            top_controls.append(self.regenerate_btn)

        self.top_row = ft.Row(
            controls=top_controls,
            alignment=ft.MainAxisAlignment.START,
        )

        self.sources_row = ft.Row(wrap=True, visible=False)
        # Writes the assistant proposed in this turn; they wait for the user.
        self.proposals_column = ft.Column(spacing=4, visible=False)
        self._proposal_ids: set[int] = set()
        self.bottom_sheet = None
        self.activity_indicator = ft.Text(
            "...", size=20, color=ft.Colors.GREY_500, visible=False
        )
        self.error_text = ft.Text(
            "", color=ft.Colors.RED, selectable=True, visible=False
        )

        self.controls = [
            self.top_row,
            self.msg_container,
            self.error_text,
            self.activity_indicator,
            self.sources_row,
            self.proposals_column,
        ]
        # The sources a stored message came with are drawn with the message: the
        # row goes out in the patch that adds it, without an update of its own.
        self._render_sources()

    def _shown(self, text: str) -> str:
        """What the Markdown control draws: Lili's images only as links."""
        return text if self.role == "user" else disarm_images(text)

    async def _open_link(self, e):
        """Open only web/mail links, never arbitrary URL schemes."""
        url = (getattr(e, "data", None) or "").strip()
        if not url.lower().startswith(("http://", "https://", "mailto:")):
            logger.warning("Blocked non-http link: %s", url)
            return
        await open_url(self.page_ref, url)

    async def _handle_regenerate(self, e):
        if self.on_regenerate and self.page_ref:
            self.page_ref.run_task(self.on_regenerate)

    def set_regenerate_visible(self, visible: bool):
        if hasattr(self, "regenerate_btn"):
            self.regenerate_btn.visible = visible
            # Only the button is re-sent, not the whole markdown block.
            safe_update(self, self.regenerate_btn)

    def show_activity_indicator(self):
        if not self.activity_indicator.visible:
            self.activity_indicator.visible = True
            safe_update(self, self.activity_indicator)

    def hide_activity_indicator(self):
        if self.activity_indicator.visible:
            self.activity_indicator.visible = False
            safe_update(self, self.activity_indicator)

    def finalize_with_error(self, error_msg: str):
        """Show ``error_msg`` as its own red line under the message.

        The body is Markdown, which does not render inline HTML: the former
        ``<span style='color:red;'>`` was displayed as raw tags.
        """
        self.error_text.value = error_msg
        self.error_text.visible = True
        self.hide_activity_indicator()
        safe_update(self, self.error_text)

    def add_proposals(self, proposals: list):
        """Show a card for each write the assistant proposed in this turn.

        Entries the backend sent in an unexpected shape are skipped, and so is a
        proposal that already has its card (a regenerated answer repeats it).
        """
        if not proposals or self.api_client is None:
            return

        added = False
        for item in proposals:
            try:
                proposal = Proposal(**item) if isinstance(item, dict) else None
            except ValueError as ex:
                logger.warning("Ignoring a malformed proposal: %s", ex)
                continue
            if proposal is None or proposal.id in self._proposal_ids:
                continue
            self._proposal_ids.add(proposal.id)
            self.proposals_column.controls.append(
                ProposalCard(proposal, self.api_client, page=self.page_ref)
            )
            added = True

        if added:
            self.proposals_column.visible = True
            safe_update(self, self.proposals_column)

    async def copy_to_clipboard(self, e):
        # The text as it was written, images included.
        if await copy_text(self.page_ref, self.text_value):
            open_dialog(
                self.page_ref,
                ft.SnackBar(
                    content=ft.Text("Копирано"),
                    duration=2000,
                ),
            )

    def append_text(self, text: str):
        """Append streamed text; only the Markdown control is sent to the client.

        The whole text is disarmed again, not the new piece: a chunk can end in
        the middle of ``![alt](url)``.
        """
        self.text_value += text
        self.md.value = self._shown(self.text_value)
        safe_update(self, self.md)

    @staticmethod
    def _source_key(source) -> tuple:
        if not isinstance(source, dict):
            return (str(source), "")
        return (
            source.get("title") or source.get("file_name") or "Unknown",
            source.get("content_snippet")
            or source.get("text_snippet")
            or source.get("text")
            or "",
        )

    def add_sources(self, sources: list):
        if not sources:
            return

        if sources is not self.sources:
            known = {self._source_key(src) for src in self.sources}
            new_sources = [src for src in sources if self._source_key(src) not in known]
            if not new_sources:
                # The same references arrived twice - nothing to redraw.
                return
            self.sources.extend(new_sources)

        self._render_sources()
        if is_mounted(self.sources_row):
            safe_update(self, self.sources_row)
        else:
            # The message is not on screen yet: its own first update sends the row.
            safe_update(self)

    def _render_sources(self) -> None:
        """Build one chip for every file among ``self.sources``; no update is sent."""
        if not self.sources:
            return
        self.sources_row.controls.clear()
        self.sources_row.controls.append(
            ft.Text("Източници:", size=12, color=ft.Colors.GREY_400)
        )

        # Group sources by filename
        grouped_sources = {}
        for src in self.sources:
            if isinstance(src, dict):
                title = src.get("title") or src.get("file_name") or "Unknown"
            else:
                title = "Unknown"
            grouped_sources.setdefault(title, []).append(src)

        for title, file_sources in grouped_sources.items():
            count = len(file_sources)
            label_text = f"{title} ({count})" if count > 1 else title
            short_label = (
                (label_text[:20] + "..") if len(label_text) > 20 else label_text
            )

            chip = ft.Chip(
                label=ft.Text(short_label, size=12),
                leading=ft.Icon(ft.Icons.INSERT_DRIVE_FILE, size=14),
                on_click=lambda e, file_sources=file_sources: self.open_sources_panel(
                    file_sources
                ),
            )
            self.sources_row.controls.append(chip)

        self.sources_row.visible = True

    def open_sources_panel(self, sources_to_display: list | None = None):
        if not self.page_ref:
            return

        display_list = (
            sources_to_display if sources_to_display is not None else self.sources
        )

        list_view = ft.ListView(expand=True, spacing=10, padding=20)

        list_view.controls.append(
            ft.Text("Източници (RAG)", size=20, weight=ft.FontWeight.BOLD)
        )

        for src in display_list:
            if not isinstance(src, dict):
                src = {"title": str(src)}
            title = src.get("title") or src.get("file_name") or "Unknown"
            snippet = (
                src.get("content_snippet")
                or src.get("text_snippet")
                or src.get("text")
                or ""
            )

            item = ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Text(title, weight=ft.FontWeight.BOLD, size=16),
                                ft.Text(
                                    source_details(src),
                                    size=12,
                                    color=ft.Colors.GREY_400,
                                ),
                            ]
                        ),
                        ft.Text(
                            snippet, selectable=True, size=14, color=ft.Colors.GREY_300
                        ),
                    ]
                ),
                bgcolor=ft.Colors.BLUE_GREY_900,
                padding=15,
                border_radius=10,
                border=ft.Border.all(1, ft.Colors.BLUE_GREY_700),
            )
            list_view.controls.append(item)

        if self.bottom_sheet is not None:
            close_dialog(self.bottom_sheet)

        self.bottom_sheet = ft.BottomSheet(
            content=ft.Container(
                content=list_view,
                padding=10,
                height=400,
            ),
        )
        open_dialog(self.page_ref, self.bottom_sheet)
