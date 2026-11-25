"""A bill for the backend to read: the item "Сметка" of the menu of the modes (2026-11-24).

The user picks the PDF of a bill (from an e-mail) or a picture of a paper one; the backend
(``POST /v1/bills``) reads it - OCR for a scan, then the local model - and writes the upload and
its answer into the open conversation: what was read and the question whether to keep it. The
history is loaded again to show them; the user's "да" in the chat keeps the bill.

``ChatBillsMixin`` is a part of ``ChatView``: it uses ``file_picker``, ``api_client``,
``session_id``, ``page_ref`` and ``load_history``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import flet as ft

from api.errors import APIError
from views.ui_utils import describe_error, file_kind, is_unexpected, show_snackbar

logger = logging.getLogger(__name__)

BILL_EXTENSIONS = ["pdf", "jpg", "jpeg", "png"]


def bill_error_text(error: BaseException) -> str:
    """Why the bill was not read, in words the user can act on."""
    if isinstance(error, APIError):
        if error.status == 404:
            return "сметките не са включени на сървъра (BILLS_ENABLED)"
        if error.status == 413:
            return "файлът е над 20 MB"
        if error.status < 500:
            return f"бекендът отказа файла ({error.message})"
    if isinstance(error, ValueError):
        return "файлът е над 20 MB"
    return describe_error(error)


class ChatBillsMixin:
    """The upload of a bill into the open conversation."""

    async def on_bill_click(self, e) -> None:
        """Open the system dialog for one bill and send it to be read."""
        try:
            files = await self.file_picker.pick_files(
                dialog_title="Сметка за прочитане (PDF или снимка)",
                allow_multiple=False,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=BILL_EXTENSIONS,
            )
        except Exception as ex:
            logger.error("The file dialog failed: %s", ex, exc_info=is_unexpected(ex))
            show_snackbar(self.page_ref, "Диалогът за избор на файл не се отвори.")
            return
        if not files or not files[0].path:
            return
        await self.send_bill(Path(files[0].path), files[0].name)

    async def send_bill(self, path: Path, name: str) -> None:
        """Send one bill and show the answer in the conversation."""
        session_id = self.session_id
        show_snackbar(self.page_ref, f"Чета сметката „{name}“…", success=True)
        try:
            await self.api_client.upload_bill(session_id, path, name)
        except Exception as ex:
            # The name of a bill is the user's own text: the log keeps its kind.
            logger.error(
                "A bill (%s) was not read: %s",
                file_kind(name),
                ex,
                exc_info=is_unexpected(ex),
            )
            show_snackbar(
                self.page_ref, f"Сметката не беше прочетена: {bill_error_text(ex)}"
            )
            return
        if session_id == self.session_id:
            await self.load_history()
