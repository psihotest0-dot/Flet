"""One message of the chat: its text, sources, proposals and buttons (P33).

``ChatView`` makes one ``ChatMessage`` for every message of the conversation and
feeds it the streamed text. What the model wrote passes ``disarm_images`` before it
is drawn (P20); the sources panel gets its strings from ``views.sources``.
"""

from __future__ import annotations

import logging
from datetime import datetime

import flet as ft

from api.client import APIClient
from api.models import Proposal
from state.settings import DEFAULT_DISPLAY_NAME
from views import theme
from views.links import WEB_PREFIXES, link_host, link_question
from views.markdown_safety import disarm_images
from views.proposals import ProposalCard
from views.sources import as_source, source_details, source_snippet, source_title
from views.timeline import when_label
from views.ui_utils import (
    COPY_FAILED_TEXT,
    close_dialog,
    confirm_dialog,
    copy_text,
    is_mounted,
    open_dialog,
    open_url,
    safe_update,
    show_snackbar,
)

logger = logging.getLogger(__name__)

# Under an answer that "Спри" cut off. The backend keeps only a complete answer, so
# after a reload this one is gone and its question waits for a new one.
STOPPED_NOTE = (
    "Спряно. Недописаният отговор не се запазва — „Опитай отново“ го пише наново."
)
# Before the memories a planned answer used (``remembered`` of the backend).
REMEMBERED_PREFIX = "Лили си спомни: "


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
        created_at: datetime | None = None,
        remembered: list[str] | None = None,
    ):
        super().__init__()
        self.role = role
        # When the message was written; ``None`` when that is not known (it is then
        # not shown, rather than guessed).
        self.created_at = created_at
        self.api_client = api_client
        self.text_value = text
        self.sources = sources or []
        self.page_ref = page
        self.on_regenerate = on_regenerate
        # The line of the chat list that holds this message; the chat sets it when it
        # adds the message, and takes the message out of the list by it.
        self.row: ft.Row | None = None
        self.spacing = 2
        self.horizontal_alignment = (
            ft.CrossAxisAlignment.END if role == "user" else ft.CrossAxisAlignment.START
        )

        self.text_color = theme.ACCENT if role == "user" else theme.LILI
        self.sender_name = user_name if role == "user" else "Лили"
        self.bgcolor = theme.YOU_BUBBLE if role == "user" else theme.LILI_BUBBLE

        self.md = ft.Markdown(
            value=self._shown(self.text_value),
            selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            code_theme=theme.code_theme(page),
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
            color=theme.MUTED,
        )

        self.copy_btn = ft.IconButton(
            icon=ft.Icons.COPY,
            icon_size=14,
            tooltip="Копирай",
            on_click=self.copy_to_clipboard,
        )

        self.time_label = ft.Text(
            when_label(created_at) if created_at else "",
            size=11,
            color=theme.FAINT,
            visible=created_at is not None,
        )

        top_controls = [self.sender_label, self.time_label, self.copy_btn]
        if role != "user":
            self.regenerate_btn = ft.IconButton(
                icon=ft.Icons.REFRESH,
                icon_size=14,
                tooltip="Опитай отново",
                on_click=self._handle_regenerate,
                visible=False,
            )
            top_controls.append(self.regenerate_btn)

        # Who answered, as the backend said at the end of the stream; empty until then
        # (the backend says nothing while it has no paid model).
        self.model_label = ft.Text("", size=11, color=theme.FAINT, visible=False)
        if role != "user":
            top_controls.append(self.model_label)

        # The name, the time and the buttons stand over the side the bubble is on.
        self.top_row = ft.Row(
            controls=top_controls,
            alignment=(
                ft.MainAxisAlignment.END
                if role == "user"
                else ft.MainAxisAlignment.START
            ),
        )

        self.sources_row = ft.Row(wrap=True, visible=False)
        # Writes the assistant proposed in this turn; they wait for the user.
        self.proposals_column = ft.Column(spacing=4, visible=False)
        self._proposal_ids: set[int] = set()
        self.bottom_sheet = None
        # A wheel and what it says, under the message while Lili is not writing
        # (thinking, waiting for a tool): so a long silence can be told from a
        # window that has hung.
        self.activity_label = ft.Text("", size=12, color=theme.MUTED)
        self.activity_indicator = ft.Row(
            [
                ft.ProgressRing(
                    width=18, height=18, stroke_width=2.5, color=theme.ACCENT
                ),
                self.activity_label,
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            visible=False,
        )
        self.error_text = ft.Text(
            "", color=theme.PROBLEM, selectable=True, visible=False
        )
        # The person's memories a planned answer used ("Лили си спомни: …"), quietly
        # under the bubble; hidden for every other message.
        self.remembered_label = ft.Text(
            "", size=11, color=theme.MUTED, italic=True, selectable=True, visible=False
        )
        self.stopped_note = ft.Text(
            STOPPED_NOTE, size=11, color=theme.MUTED, visible=False
        )

        self.controls = [
            self.top_row,
            self.msg_container,
            self.remembered_label,
            self.error_text,
            self.stopped_note,
            self.activity_indicator,
            self.sources_row,
            self.proposals_column,
        ]
        # The sources a stored message came with are drawn with the message: the
        # row goes out in the patch that adds it, without an update of its own.
        self._render_sources()
        self._render_remembered(remembered)

    def _shown(self, text: str) -> str:
        """What the Markdown control draws: Lili's images only as links."""
        return text if self.role == "user" else disarm_images(text)

    async def _open_link(self, e):
        """Open only web/mail links, and a web link only after saying where it goes.

        The text of a link is free, so ``[your bank](https://evil.example)`` looks
        like something it is not: the click names the host first and the link opens
        on a yes (``views.links``). A mail link only opens the mail program.
        """
        url = (getattr(e, "data", None) or "").strip()
        lowered = url.lower()
        if not lowered.startswith((*WEB_PREFIXES, "mailto:")):
            # The scheme only: the rest of the address is the model's text.
            logger.warning(
                "Blocked non-http link (scheme %r).", url.partition(":")[0][:20]
            )
            return
        if lowered.startswith(WEB_PREFIXES):
            if link_host(url) is None:
                logger.warning("Blocked a web link that has no host.")
                return
            if not await confirm_dialog(
                self.page_ref,
                "Отваряне на връзка",
                link_question(url),
                confirm_text="Отвори",
                destructive=False,
            ):
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

    def show_activity_indicator(self, label: str) -> None:
        """Show the wheel with ``label``; a label that changed is the only thing re-sent."""
        if not self.activity_indicator.visible:
            self.activity_label.value = label
            self.activity_indicator.visible = True
            safe_update(self, self.activity_indicator)
        elif self.activity_label.value != label:
            self.activity_label.value = label
            safe_update(self, self.activity_label)

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

    def show_stopped_note(self) -> None:
        """Say that this answer was cut off by the user and will not be kept."""
        self.stopped_note.visible = True
        safe_update(self, self.stopped_note)

    def _render_remembered(self, remembered: list[str] | None) -> bool:
        """Fill the line "Лили си спомни"; ``True`` when it changed."""
        items = [
            m.strip() for m in remembered or [] if isinstance(m, str) and m.strip()
        ]
        if self.role == "user" or not items:
            return False
        self.remembered_label.value = REMEMBERED_PREFIX + " · ".join(items)
        self.remembered_label.visible = True
        return True

    def set_remembered(self, remembered: list[str]) -> None:
        """Show the person's memories this answer used, as the stream told them."""
        if self._render_remembered(remembered):
            safe_update(self, self.remembered_label)

    def set_model(self, who: dict) -> None:
        """Show who gave this answer: the cloud (with the model) or this computer."""
        if self.role == "user" or not isinstance(who, dict):
            return
        provider = who.get("provider")
        if provider == "local":
            text, tip = "🏠 на този компютър", "Отговори локалният модел"
        elif isinstance(provider, str) and provider:
            model = who.get("model")
            name = model if isinstance(model, str) and model else provider
            text, tip = f"☁ {name}", "Отговори платеният модел в облака"
        else:
            return
        self.model_label.value = text
        self.model_label.tooltip = tip
        self.model_label.visible = True
        safe_update(self, self.model_label)

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
        else:
            # Like the other copy buttons: a click that did nothing says so.
            show_snackbar(self.page_ref, COPY_FAILED_TEXT)

    def restyle(self) -> None:
        """Give the code blocks the colours that fit the theme that is on now."""
        self.md.code_theme = theme.code_theme(self.page_ref)
        safe_update(self, self.md)

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
        source = as_source(source)
        return (source_title(source), source_snippet(source))

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
            ft.Text("Източници:", size=12, color=theme.MUTED)
        )

        # Group sources by filename
        grouped_sources = {}
        for src in self.sources:
            grouped_sources.setdefault(source_title(as_source(src)), []).append(src)

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

        for src in map(as_source, display_list):
            title = source_title(src)
            snippet = source_snippet(src)

            item = ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Text(title, weight=ft.FontWeight.BOLD, size=16),
                                ft.Text(
                                    source_details(src),
                                    size=12,
                                    color=theme.MUTED,
                                ),
                            ]
                        ),
                        ft.Text(snippet, selectable=True, size=14),
                    ]
                ),
                bgcolor=theme.CARD,
                padding=15,
                border_radius=10,
                border=ft.Border.all(1, theme.BORDER),
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
