"""Cards for the writes the assistant proposes and the user confirms.

The assistant never saves a global rule by itself, and it saves a memory only
when the turn read nothing but the conversation: text from a web page or a book
could have told it what to remember. Everything else arrives as a proposal, here
shown as a card with the text and two buttons.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import flet as ft

from api.client import APIClient, APIError, Proposal
from views.ui_utils import describe_error, safe_update, show_snackbar

logger = logging.getLogger(__name__)

# The button that shows the whole text of a file write, and takes it away again.
SHOW_TEXT = "Покажи целия текст"
HIDE_TEXT = "Скрий текста"

# Where the text a proposal may rest on came from (the ``external_sources`` of the
# backend): the web tools, the files (an attached file, or one of the files the
# assistant keeps), or the user's own documents.
WEB_SOURCES = {"web_search", "web_fetch"}
FILE_SOURCES = {"attachments", "user_files"}

# 404: no such proposal (any more); 409: it was decided already. 422: approving
# it cannot be done as it stands - the user has too many global rules, or a file
# write finds the folder changed - and the proposal stays open.
CLOSED_STATUSES = (404, 409)
CANNOT_APPLY_STATUS = 422

# What a write to a file does, by the ``file_mode`` of the proposal.
FILE_MODE_HINTS = {
    "create": "Ще се направи нов файл.",
    "replace": "Старият текст ще се пази като предишна версия.",
    "append": "Текстът ще се добави в края на файла.",
}


def origin_warning(external_sources: list[str] | None) -> str | None:
    """Warning for a proposal made after the assistant read text from outside."""
    if not external_sources:
        return None
    places = []
    if any(source in WEB_SOURCES for source in external_sources):
        places.append("интернет")
    if any(
        source not in WEB_SOURCES and source not in FILE_SOURCES
        for source in external_sources
    ):
        places.append("документи")
    if any(source in FILE_SOURCES for source in external_sources):
        places.append("файлове")
    where = (
        places[0] if len(places) == 1 else ", ".join(places[:-1]) + " и " + places[-1]
    )
    return f"Предложено след четене на {where} — провери го."


def kind_label(proposal: Proposal) -> str:
    if proposal.kind == "save_global_rule":
        return "Правило"
    if proposal.kind == "save_style_note":
        return "Как говорим"
    if proposal.kind == "write_file":
        return "Запис на файл"
    return "Спомен"


class ProposalCard(ft.Container):
    """One proposal with the buttons that decide it.

    ``on_decided(proposal, approved)`` runs after the backend accepted the
    answer, so a list that shows the card can drop it.
    """

    def __init__(
        self,
        proposal: Proposal,
        api_client: APIClient,
        page: ft.Page | None = None,
        on_decided: Callable[[Proposal, bool], None] | None = None,
    ):
        super().__init__()
        self.proposal = proposal
        self.api_client = api_client
        self.page_ref = page
        self.on_decided = on_decided
        self._busy = False

        self.padding = 10
        self.border_radius = 8
        self.border = ft.Border.all(1, ft.Colors.AMBER_700)
        self.margin = ft.Margin.only(top=4)

        self.title = ft.Text(
            f"{kind_label(proposal)} — чака твоето потвърждение",
            size=12,
            color=ft.Colors.AMBER_200,
        )
        self.body = ft.Text(proposal.summary or "", selectable=True)
        warning = origin_warning(proposal.external_sources)
        self.warning = ft.Text(
            warning or "", size=12, color=ft.Colors.ORANGE_300, visible=bool(warning)
        )

        # A write to a file shows what it does, and the whole text on request: the
        # line above has only the beginning, and the user decides on the text.
        self.is_file_write = proposal.kind == "write_file"
        self.hint = ft.Text(
            FILE_MODE_HINTS.get(proposal.file_mode or "", ""),
            size=12,
            color=ft.Colors.GREY_400,
            visible=self.is_file_write and proposal.file_mode in FILE_MODE_HINTS,
        )
        self.full_text = ft.Container(
            content=ft.Column(
                [ft.Text(proposal.content or "", selectable=True, size=12)],
                scroll=ft.ScrollMode.AUTO,
            ),
            height=160,
            padding=8,
            border_radius=6,
            border=ft.Border.all(1, ft.Colors.GREY_700),
            visible=False,
        )
        self.show_text_btn = ft.TextButton(
            SHOW_TEXT, on_click=self._toggle_text, visible=self.is_file_write
        )

        self.approve_btn = ft.Button(
            "Запази", icon=ft.Icons.CHECK, on_click=self._approve
        )
        self.reject_btn = ft.TextButton("Откажи", on_click=self._reject)
        self.buttons = ft.Row([self.approve_btn, self.reject_btn], wrap=True)
        self.result = ft.Text("", size=12, color=ft.Colors.GREY_400, visible=False)

        self.content = ft.Column(
            [
                self.title,
                self.body,
                self.hint,
                self.warning,
                self.show_text_btn,
                self.full_text,
                self.buttons,
                self.result,
            ],
            spacing=4,
            tight=True,
        )

    async def _toggle_text(self, e):
        self.full_text.visible = not self.full_text.visible
        self.show_text_btn.content = HIDE_TEXT if self.full_text.visible else SHOW_TEXT
        safe_update(self, self.full_text, self.show_text_btn)

    async def _approve(self, e):
        await self._decide(True)

    async def _reject(self, e):
        await self._decide(False)

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.approve_btn.disabled = busy
        self.reject_btn.disabled = busy
        safe_update(self, self.buttons)

    def _show_result(self, text: str):
        self.buttons.visible = False
        self.result.value = text
        self.result.visible = True
        safe_update(self, self.buttons, self.result)

    async def _decide(self, approve: bool):
        if self._busy:
            return
        self._set_busy(True)
        try:
            if approve:
                await self.api_client.approve_proposal(self.proposal.id)
            else:
                await self.api_client.reject_proposal(self.proposal.id)
        except asyncio.CancelledError:
            raise
        except APIError as ex:
            if ex.status in CLOSED_STATUSES:
                # Decided elsewhere (Telegram) or expired: nothing is left to do here.
                logger.info("Proposal %s is already closed: %s", self.proposal.id, ex)
                self._show_result("Вече е решено или е изтекло.")
                self._tell_owner(approve=False)
                return
            logger.error("Failed to decide proposal %s: %s", self.proposal.id, ex)
            self._set_busy(False)
            show_snackbar(self.page_ref, _api_error_text(ex, self.proposal))
            return
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to decide proposal %s: %s", self.proposal.id, ex)
            self._set_busy(False)
            show_snackbar(self.page_ref, describe_error(ex))
            return

        self._show_result("✅ Запазено." if approve else "🗑 Отхвърлено.")
        self._tell_owner(approve)

    def _tell_owner(self, approve: bool):
        # Not ``_notify``: Flet calls a control's ``_notify`` on every property change.
        if self.on_decided is not None:
            self.on_decided(self.proposal, approve)


def _api_error_text(error: APIError, proposal: Proposal | None = None) -> str:
    """The backend refuses an approval (422) that cannot be done as it stands.

    For a rule it is the limit of global rules. For a file write the backend says
    why in Bulgarian (the file is there now, it is gone, there is no room): that
    text is the answer.
    """
    if error.status == CANNOT_APPLY_STATUS:
        if proposal is not None and proposal.kind == "write_file":
            return error.message
        return "Вече има твърде много глобални правила — първо изтрий някое от таба „Памет“."
    return describe_error(error)
