from __future__ import annotations

"""Cards for the writes the assistant proposes and the user confirms.

The assistant never saves a global rule by itself, and it saves a memory only
when the turn read nothing but the conversation: text from a web page or a book
could have told it what to remember. Everything else arrives as a proposal, here
shown as a card with the text and two buttons.
"""

import asyncio
import logging
from collections.abc import Callable

import flet as ft

from api.client import APIClient, APIError, Proposal
from views.ui_utils import describe_error, safe_update, show_snackbar

logger = logging.getLogger(__name__)

# Where the text a proposal may rest on came from (the ``external_sources`` of the
# backend): the web tools, or the user's own documents.
WEB_SOURCES = {"web_search", "web_fetch"}

# 404: no such proposal (any more); 409: it was decided already. 422: approving
# it would exceed the limit of global rules - the proposal stays open.
CLOSED_STATUSES = (404, 409)
TOO_MANY_RULES_STATUS = 422


def origin_warning(external_sources: list[str] | None) -> str | None:
    """Warning for a proposal made after the assistant read text from outside."""
    if not external_sources:
        return None
    web = any(source in WEB_SOURCES for source in external_sources)
    docs = any(source not in WEB_SOURCES for source in external_sources)
    if web and docs:
        where = "интернет и документи"
    elif web:
        where = "интернет"
    else:
        where = "документи"
    return f"Предложено след четене на {where} — провери го."


def kind_label(proposal: Proposal) -> str:
    return "Правило" if proposal.kind == "save_global_rule" else "Спомен"


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
        self.border = ft.border.all(1, ft.colors.AMBER_700)
        self.margin = ft.margin.only(top=4)

        self.title = ft.Text(
            f"{kind_label(proposal)} — чака твоето потвърждение",
            size=12,
            color=ft.colors.AMBER_200,
        )
        self.body = ft.Text(proposal.summary or "", selectable=True)
        warning = origin_warning(proposal.external_sources)
        self.warning = ft.Text(
            warning or "", size=12, color=ft.colors.ORANGE_300, visible=bool(warning)
        )

        self.approve_btn = ft.ElevatedButton(
            "Запази", icon=ft.icons.CHECK, on_click=self._approve
        )
        self.reject_btn = ft.TextButton("Откажи", on_click=self._reject)
        self.buttons = ft.Row([self.approve_btn, self.reject_btn], wrap=True)
        self.result = ft.Text("", size=12, color=ft.colors.GREY_400, visible=False)

        self.content = ft.Column(
            [self.title, self.body, self.warning, self.buttons, self.result],
            spacing=4,
            tight=True,
        )

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
                self._notify(approve=False)
                return
            logger.error("Failed to decide proposal %s: %s", self.proposal.id, ex)
            self._set_busy(False)
            show_snackbar(self.page_ref, _api_error_text(ex))
            return
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to decide proposal %s: %s", self.proposal.id, ex)
            self._set_busy(False)
            show_snackbar(self.page_ref, describe_error(ex))
            return

        self._show_result("✅ Запазено." if approve else "🗑 Отхвърлено.")
        self._notify(approve)

    def _notify(self, approve: bool):
        if self.on_decided is not None:
            self.on_decided(self.proposal, approve)


def _api_error_text(error: APIError) -> str:
    """The backend refuses an approval (422) when the user already has too many rules."""
    if error.status == TOO_MANY_RULES_STATUS:
        return "Вече има твърде много глобални правила — първо изтрий някое от таба „Памет“."
    return describe_error(error)
