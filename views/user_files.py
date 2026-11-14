"""The files the assistant keeps for the user: notes, lists, drafts it wrote down.

They are not the library of books (the "Файлове" tab). The assistant writes them when
asked, in a folder of its own on the backend; here the user sees them, reads them,
looks at the version a replacement took the place of, and removes them - the
assistant cannot remove anything. A write that waits for the user's answer (it would
replace a file, or the message had used text from outside) is shown as a card above
the list.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import flet as ft

from api.client import APIClient, APIError, UserFileInfo, UserFileList
from views.files import size_label
from views.proposals import ProposalCard
from views.ui_utils import (
    close_dialog,
    confirm_dialog,
    copy_text,
    error_detail,
    open_dialog,
    safe_update,
    show_snackbar,
)

logger = logging.getLogger(__name__)

# The kind of proposal made by the file tools (the backend's ``write_file``).
WRITE_FILE_KIND = "write_file"

NOT_ENABLED_TEXT = (
    "Файловете на Лили не са включени. Включват се с FILES_ENABLED=true в .env "
    "на бекенда и рестарт."
)
NO_FILES_TEXT = (
    "Още няма файлове. Помоли Лили в чата, например: „запиши това във файл бележки.md“."
)
NO_PREVIOUS_TEXT = "Няма предишна версия."


def files_label(count: int) -> str:
    """ "1 файл", "3 файла" - the Bulgarian plural."""
    return "1 файл" if count == 1 else f"{count} файла"


def usage_label(listing: UserFileList) -> str:
    """What the files take of what they may: "3 файла · 12 KB от 20,0 MB"."""
    return (
        f"{files_label(len(listing.files))} · {size_label(listing.total_bytes)} "
        f"от {size_label(listing.max_total_bytes)}"
    )


def modified_label(modified: datetime | None) -> str:
    """The time of the last change in local time, day first: "14.11.2026 20:15"."""
    if modified is None:
        return ""
    return modified.astimezone().strftime("%d.%m.%Y %H:%M")


def file_subtitle(info: UserFileInfo) -> str:
    parts = [size_label(info.size), modified_label(info.modified)]
    return " · ".join(part for part in parts if part)


class UserFilesView(ft.Container):
    """The tab with the files of the assistant."""

    def __init__(self, page: ft.Page, api_client: APIClient):
        super().__init__()
        # ``Control.page`` is read-only and raises until the view is on the page.
        self.page_ref = page
        self.api_client = api_client
        self.listing = UserFileList()
        self.enabled = True
        self.expand = True
        self.alignment = ft.Alignment.TOP_CENTER
        self.padding = 10
        self._load_task: asyncio.Task | None = None
        # Each load takes a number; only the newest one may write its result.
        self._load_seq = 0

        self.title = ft.Text("Файлове на Лили", size=20, weight=ft.FontWeight.BOLD)
        self.usage = ft.Text("", size=12, color=ft.Colors.GREY_400)
        self.refresh_btn = ft.IconButton(
            icon=ft.Icons.REFRESH, tooltip="Обнови", on_click=self.on_refresh_click
        )
        self.progress = ft.ProgressBar(visible=False)
        self.notice = ft.Text("", visible=False, color=ft.Colors.AMBER_200)

        self.proposals_title = ft.Text(
            "Чакат твоето потвърждение", size=14, weight=ft.FontWeight.BOLD
        )
        self.proposals_column = ft.Column(spacing=4, tight=True)
        self.proposals_section = ft.Column(
            [self.proposals_title, self.proposals_column], spacing=4, visible=False
        )
        self.list_column = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)

        self.content = ft.Column(
            [
                ft.Row(
                    [
                        ft.Column([self.title, self.usage], spacing=2, expand=True),
                        self.refresh_btn,
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                self.progress,
                self.notice,
                self.proposals_section,
                self.list_column,
            ],
            expand=True,
            spacing=8,
        )

    # ----------------------------------------------------------- lifecycle
    def did_mount(self):
        self._load_task = self.page_ref.run_task(self.load)

    def will_unmount(self):
        if self._load_task is not None:
            self._load_task.cancel()
            self._load_task = None

    async def refresh(self, *_args):
        """Read everything again; ``main`` calls this when the tab is opened once more."""
        await self.load()

    async def on_refresh_click(self, e):
        await self.load()

    # ------------------------------------------------------------- loading
    async def load(self, *_args):
        self._load_seq += 1
        sequence = self._load_seq

        self.progress.visible = True
        safe_update(self, self.progress)
        try:
            listing = await self.api_client.list_user_files()
        except asyncio.CancelledError:
            raise
        except APIError as ex:
            if sequence == self._load_seq:
                if ex.status == 404:
                    # No such route: the file tools are off (or the backend is older).
                    logger.info("The backend does not serve the assistant's files.")
                    self._show_disabled()
                else:
                    logger.error("Failed to load the assistant's files: %s", ex)
                    self._show_notice("Неуспешно зареждане на файловете.")
            return
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to load the assistant's files: %s", ex)
            if sequence == self._load_seq:
                self._show_notice(
                    f"Неуспешно зареждане на файловете: {error_detail(ex)}"
                )
            return
        finally:
            if sequence == self._load_seq:
                self.progress.visible = False
                safe_update(self, self.progress)

        if sequence != self._load_seq:
            return
        self.enabled = True
        self.listing = listing
        self._show_files()
        await self.load_proposals(sequence)

    def _show_notice(self, text: str):
        self.notice.value = text
        self.notice.visible = bool(text)
        safe_update(self, self.notice)

    def _show_disabled(self):
        self.enabled = False
        self.listing = UserFileList()
        self.usage.value = ""
        self.list_column.controls = []
        self.proposals_section.visible = False
        self.notice.value = NOT_ENABLED_TEXT
        self.notice.visible = True
        safe_update(
            self, self.usage, self.list_column, self.proposals_section, self.notice
        )

    def _show_files(self):
        self.usage.value = usage_label(self.listing)
        self.list_column.controls = (
            [self._tile(info) for info in self.listing.files]
            if self.listing.files
            else [ft.Text(NO_FILES_TEXT, color=ft.Colors.GREY_400)]
        )
        self.notice.visible = False
        safe_update(self, self.usage, self.list_column, self.notice)

    def _tile(self, info: UserFileInfo) -> ft.ListTile:
        return ft.ListTile(
            leading=ft.Icon(ft.Icons.DESCRIPTION_OUTLINED),
            title=ft.Text(info.name),
            subtitle=ft.Text(file_subtitle(info)),
            trailing=ft.Row(
                [
                    ft.IconButton(
                        icon=ft.Icons.OPEN_IN_NEW,
                        tooltip="Отвори",
                        on_click=lambda e, info=info: self.page_ref.run_task(
                            self.open_file, info
                        ),
                    ),
                    ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE,
                        tooltip="Изтрий",
                        on_click=lambda e, info=info: self.page_ref.run_task(
                            self.delete_file, info
                        ),
                    ),
                ],
                tight=True,
            ),
            on_click=lambda e, info=info: self.page_ref.run_task(self.open_file, info),
        )

    async def load_proposals(self, sequence: int | None = None):
        """The writes to files that wait for an answer. A failure leaves the list as it was."""
        try:
            proposals = await self.api_client.list_proposals()
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.warning("Failed to load proposals: %s", ex)
            return
        if sequence is not None and sequence != self._load_seq:
            return

        writes = [p for p in proposals if p.kind == WRITE_FILE_KIND]
        self.proposals_column.controls = [
            ProposalCard(
                proposal,
                self.api_client,
                page=self.page_ref,
                on_decided=self._on_proposal_decided,
            )
            for proposal in writes
        ]
        self.proposals_section.visible = bool(writes)
        safe_update(self, self.proposals_section)

    def _on_proposal_decided(self, proposal, approved: bool):
        """An approved write changes the files; the card keeps its result."""
        if approved and self.page_ref is not None:
            self.page_ref.run_task(self.load)

    # ---------------------------------------------------------------- open
    async def open_file(self, info: UserFileInfo):
        try:
            current = await self.api_client.get_user_file(info.name)
        except asyncio.CancelledError:
            raise
        except APIError as ex:
            logger.error("Failed to open a file of the assistant: %s", ex)
            show_snackbar(
                self.page_ref,
                "Файлът вече го няма."
                if ex.status == 404
                else f"Грешка при отваряне на файла: {ex}",
            )
            if ex.status == 404:
                await self.load()
            return
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to open a file of the assistant: %s", ex)
            show_snackbar(
                self.page_ref, f"Неуспешно отваряне на файла: {error_detail(ex)}"
            )
            return
        self._show_text_dialog(info.name, current.content)

    def _show_text_dialog(self, name: str, text: str):
        """The text in a dialog, with the previous version one click away."""
        state = {"previous": False, "current": text, "shown": text}
        body = ft.Text(text, selectable=True)
        switch_btn = ft.TextButton("Предишна версия")
        copy_btn = ft.TextButton("Копирай")

        async def switch(e):
            if state["previous"]:
                state["previous"] = False
                shown = state["current"]
            else:
                try:
                    previous = await self.api_client.get_user_file(name, previous=True)
                except asyncio.CancelledError:
                    raise
                except APIError as ex:
                    if ex.status != 404:
                        logger.error("Failed to open a previous version: %s", ex)
                        show_snackbar(
                            self.page_ref, f"Грешка при отваряне на версията: {ex}"
                        )
                        return
                    show_snackbar(self.page_ref, NO_PREVIOUS_TEXT)
                    return
                except Exception as ex:  # noqa: BLE001
                    logger.error("Failed to open a previous version: %s", ex)
                    show_snackbar(
                        self.page_ref,
                        f"Неуспешно отваряне на версията: {error_detail(ex)}",
                    )
                    return
                state["previous"] = True
                shown = previous.content
            body.value = shown
            switch_btn.content = (
                "Сегашна версия" if state["previous"] else "Предишна версия"
            )
            dialog.title = ft.Text(
                f"{name} (предишна версия)" if state["previous"] else name
            )
            state["shown"] = shown
            safe_update(dialog, dialog, body, switch_btn)

        async def copy(e):
            if await copy_text(self.page_ref, state["shown"]):
                show_snackbar(self.page_ref, "Копирано.", success=True)
            else:
                show_snackbar(self.page_ref, "Не успях да копирам.")

        switch_btn.on_click = switch
        copy_btn.on_click = copy
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(name),
            content=ft.Column([body], scroll=ft.ScrollMode.AUTO, width=560, height=360),
            actions=[
                switch_btn,
                copy_btn,
                ft.TextButton("Затвори", on_click=lambda e: close_dialog(dialog)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        open_dialog(self.page_ref, dialog)

    # -------------------------------------------------------------- delete
    async def delete_file(self, info: UserFileInfo):
        confirmed = await confirm_dialog(
            self.page_ref,
            "Изтриване на файл",
            f"Да изтрия ли „{info.name}“? Предишната му версия също ще се изтрие.",
        )
        if not confirmed:
            return

        try:
            await self.api_client.delete_user_file(info.name)
        except asyncio.CancelledError:
            raise
        except APIError as ex:
            if ex.status != 404:  # 404: already gone, the reload below shows it
                logger.error("Failed to delete a file of the assistant: %s", ex)
                show_snackbar(self.page_ref, f"Грешка при изтриване на файла: {ex}")
                return
            logger.info("A file of the assistant was already deleted.")
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to delete a file of the assistant: %s", ex)
            show_snackbar(
                self.page_ref, f"Неуспешно изтриване на файла: {error_detail(ex)}"
            )
            return

        show_snackbar(self.page_ref, "Файлът е изтрит.", success=True)
        await self.load()
