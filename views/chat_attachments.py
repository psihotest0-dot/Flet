"""The files of a conversation in the chat: the chips, attaching, and a long paste (P33).

``ChatAttachmentsMixin`` holds what ``ChatView`` does with the files attached to the
open conversation: reading and drawing the list, the system dialog, attaching what was
chosen, and the offer to turn a very long text (pasted, or too long for a message)
into a file. The checks that need no screen (which file may be attached, how long a
message may be) are in ``views.attachments``.

It is a part of ``ChatView`` and not a class of its own: it works on the state that
``ChatView.__init__`` creates - ``api_client``, ``session_id``, ``page_ref``,
``attachments`` and ``attachments_row``, ``file_picker``, ``input_field`` (and its
``_input_text`` and ``_offering``), ``_set_input`` and ``load_attachments`` itself.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import flet as ft

from api.errors import APIError
from api.limits import MAX_ATTACHMENT_BYTES, MESSAGE_TOKEN_LIMIT
from api.models import ChatAttachment
from views.attachments import (
    FILE_TOO_BIG_TEXT,
    PASTE_THRESHOLD_CHARS,
    TOO_MANY_FILES_TEXT,
    NotAttachable,
    estimate_message_tokens,
    inserted_text,
    paste_file_name,
    read_text_file,
)
from views.ui_utils import (
    confirm_dialog,
    describe_error,
    error_for_log,
    file_kind,
    is_unexpected,
    safe_update,
    show_snackbar,
)

logger = logging.getLogger(__name__)


class ChatAttachmentsMixin:
    """The attached files of ``ChatView`` and the offer to attach a long text."""

    async def load_attachments(self, *_args) -> None:
        """Read the files attached to the open conversation and show them."""
        session_id = self.session_id
        try:
            found = await self.api_client.list_attachments(session_id)
        except Exception as e:
            # Not worth a message on screen: the chat works without the row, and the
            # next attach or reload reads the list again.
            logger.warning(
                "Failed to read the files of chat %s: %s",
                session_id[:8],
                e,
                exc_info=is_unexpected(e),
            )
            return
        if session_id != self.session_id:
            return
        self.attachments = found
        self._render_attachments()

    def _render_attachments(self) -> None:
        """Draw one chip for every attached file; the row is hidden when there are none."""
        self.attachments_row.controls = [
            ft.Chip(
                label=ft.Text(f"{a.name} · {a.lines} реда", size=12),
                leading=ft.Icon(ft.Icons.ATTACH_FILE, size=16),
                tooltip=(
                    f"~{a.tokens} токена, версия {a.version}. Лили вижда файла само в този разговор; "
                    "кръстчето го маха."
                ),
                delete_icon_tooltip="Махни файла от разговора",
                on_delete=self.on_attachment_delete,
                data=a.id,
            )
            for a in self.attachments
        ]
        self.attachments_row.visible = bool(self.attachments)
        safe_update(self, self.attachments_row)

    async def on_attachment_delete(self, e) -> None:
        attachment_id = e.control.data
        try:
            await self.api_client.delete_attachment(attachment_id)
        except Exception as ex:
            if isinstance(ex, APIError) and ex.status == 404:
                # Gone already (another window, or it expired): the list is just stale.
                logger.info("Attachment %s was already gone.", attachment_id)
            else:
                logger.error(
                    "Failed to remove attachment %s: %s",
                    attachment_id,
                    ex,
                    exc_info=is_unexpected(ex),
                )
                show_snackbar(
                    self.page_ref, f"Файлът не беше махнат. {describe_error(ex)}"
                )
                return
        await self.load_attachments()

    async def on_attach_click(self, e) -> None:
        """Open the system dialog and attach what the user chose in it."""
        try:
            files = await self.file_picker.pick_files(
                dialog_title="Прикачи файл към този разговор",
                allow_multiple=True,
                file_type=ft.FilePickerFileType.ANY,
            )
        except Exception as ex:
            logger.error("The file dialog failed: %s", ex, exc_info=is_unexpected(ex))
            show_snackbar(self.page_ref, "Диалогът за избор на файл не се отвори.")
            return
        await self.attach_files(files)

    async def attach_files(self, files: list[ft.FilePickerFile] | None) -> None:
        """Attach the chosen files; what cannot be attached is reported together at the end."""
        if not files:
            return
        attached: list[str] = []
        problems: list[str] = []
        for picked in files:
            if not picked.path:
                continue
            try:
                size = await asyncio.to_thread(
                    lambda p=picked.path: Path(p).stat().st_size
                )
                if size > MAX_ATTACHMENT_BYTES:
                    raise NotAttachable(picked.name, FILE_TOO_BIG_TEXT)
                raw = await asyncio.to_thread(Path(picked.path).read_bytes)
                text = read_text_file(raw, picked.name)
                await self._store_attachment(picked.name, text)
            except NotAttachable as ex:
                # The screen names the file; the log only says what kind it was.
                logger.warning("Skipped %s: %s", file_kind(picked.name), ex.reason)
                problems.append(str(ex))
            except OSError as ex:
                logger.warning(
                    "Could not read %s: %s", file_kind(picked.name), error_for_log(ex)
                )
                problems.append(f"{picked.name}: файлът не може да се прочете")
            except Exception as ex:
                logger.error(
                    "Failed to attach %s: %s",
                    file_kind(picked.name),
                    error_for_log(ex),
                    exc_info=is_unexpected(ex),
                )
                problems.append(f"{picked.name}: {self._attach_error_text(ex)}")
            else:
                attached.append(picked.name)
        await self.load_attachments()
        if attached:
            show_snackbar(
                self.page_ref,
                "Прикачено към разговора: " + ", ".join(attached),
                success=True,
            )
        if problems:
            show_snackbar(self.page_ref, "Не са прикачени — " + "; ".join(problems))

    async def _store_attachment(self, name: str, text: str) -> ChatAttachment:
        """Send one file to the backend for the open conversation."""
        return await self.api_client.upload_attachment(self.session_id, name, text)

    @staticmethod
    def _attach_error_text(error: BaseException) -> str:
        """Why the backend would not take a file, in words the user can act on."""
        if isinstance(error, APIError):
            if error.status == 413:
                return FILE_TOO_BIG_TEXT
            if error.status == 422:
                return TOO_MANY_FILES_TEXT
            if error.status < 500:
                return f"бекендът отказа файла ({error.message})"
        return describe_error(error)

    # ----------------------------------------------------- a long text becomes a file
    async def on_input_change(self, e) -> None:
        """Offer to attach a very long text as soon as it is pasted into the field.

        A message of that size would be refused by the backend, and the model reads a
        file better than a wall of text in a message. Only the jump counts: typing
        grows the field by a few characters at a time.
        """
        text = self.input_field.value or ""
        previous, self._input_text = self._input_text, text
        if self._offering or len(text) - len(previous) < PASTE_THRESHOLD_CHARS:
            return
        pasted = inserted_text(previous, text)
        tokens = estimate_message_tokens(pasted)
        self._offering = True
        try:
            accepted = await confirm_dialog(
                self.page_ref,
                "Дълъг текст",
                f"Поставеният текст е голям (около {tokens} токена). Да го прикача като файл към разговора? "
                "Така Лили го чете по-добре и не се брои в дължината на съобщението.",
                confirm_text="Прикачи като файл",
                cancel_text="Остави в съобщението",
                destructive=False,
            )
            if not accepted:
                return
            if await self._attach_text_as_file(pasted):
                self._set_input(previous)
                safe_update(self, self.input_field)
        finally:
            self._offering = False

    async def _attach_text_as_file(self, text: str) -> str | None:
        """Attach text as ``paste_N.ext``; the name, or None (and a message) if it failed."""
        name = paste_file_name(text, {a.name for a in self.attachments})
        try:
            await self._store_attachment(name, text)
        except Exception as ex:
            logger.error(
                "Failed to attach the pasted text: %s", ex, exc_info=is_unexpected(ex)
            )
            show_snackbar(
                self.page_ref,
                f"Текстът не беше прикачен — {self._attach_error_text(ex)}",
            )
            return None
        await self.load_attachments()
        show_snackbar(self.page_ref, f"Прикачено като {name}", success=True)
        return name

    async def _offer_file_instead_of_message(self, text: str) -> None:
        """A message over the limit: offer a file instead of the backend's refusal.

        On yes the whole text becomes a file and the field gets a short note about it,
        ready to be sent or changed; nothing is sent by itself.
        """
        if self._offering:
            return
        self._offering = True
        try:
            accepted = await confirm_dialog(
                self.page_ref,
                "Съобщението е твърде дълго",
                f"Съобщението е около {estimate_message_tokens(text)} токена, а най-много се пращат {MESSAGE_TOKEN_LIMIT}. "
                "Да го прикача като файл към разговора и в полето да остане кратка бележка?",
                confirm_text="Прикачи като файл",
                cancel_text="Ще го скъся",
                destructive=False,
            )
            if not accepted:
                return
            name = await self._attach_text_as_file(text)
            if name:
                self._set_input(f"Виж прикачения файл {name}.")
                safe_update(self, self.input_field)
        finally:
            self._offering = False
