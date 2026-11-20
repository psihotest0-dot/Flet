"""Taking the conversation out of the chat: copy it, or save it as a Markdown file (P33).

``ChatExportMixin`` is the part of ``ChatView`` that turns what is on the screen into
text (``views.conversation_export``) and hands it to the clipboard or to the system's
"save file" dialog. What is saved is what is shown: the last
``HISTORY_PAGE_SIZE`` messages of a long conversation and what was written since;
the text says so when older ones exist.

It is a part of ``ChatView`` and not a class of its own: it works on the state that
``ChatView.__init__`` creates - ``chat_list``, ``session_dropdown``, ``session_id``,
``file_picker``, ``page_ref``, ``_is_streaming`` and ``_history_truncated``.
"""

from __future__ import annotations

import logging

import flet as ft

from views.chat_message import ChatMessage
from views.conversation_export import (
    ExportedMessage,
    conversation_markdown,
    export_file_name,
)
from views.timeline import now
from views.ui_utils import (
    copy_text,
    error_detail,
    error_for_log,
    is_unexpected,
    show_snackbar,
)

logger = logging.getLogger(__name__)

NOTHING_TO_EXPORT_TEXT = "В разговора още няма съобщения."
WAIT_FOR_THE_ANSWER_TEXT = "Изчакай края на отговора или го спри."


class ChatExportMixin:
    """Copying and saving the conversation of ``ChatView``."""

    def _messages_on_screen(self) -> list[ChatMessage]:
        """The messages of the open conversation, oldest first."""
        found = []
        for row in self.chat_list.controls:
            container = row.controls[0] if getattr(row, "controls", None) else None
            message = getattr(container, "content", None)
            if isinstance(message, ChatMessage):
                found.append(message)
        return found

    def _chat_name(self) -> str:
        """The name of the open chat as the dropdown shows it."""
        return next(
            (o.text for o in self.session_dropdown.options if o.key == self.session_id),
            "",
        )

    def _exported_messages(self) -> list[ExportedMessage]:
        """The messages that have a text; a placeholder of an answer not begun is left out."""
        return [
            ExportedMessage(
                sender=message.sender_name,
                text=message.text_value.strip(),
                created_at=message.created_at,
                model=message.model_label.value if message.model_label.visible else "",
                sources=tuple(message.sources),
                unfinished=message.error_text.visible or message.stopped_note.visible,
            )
            for message in self._messages_on_screen()
            if message.text_value.strip()
        ]

    def _conversation(self) -> tuple[str, int] | None:
        """The text of the conversation and how many messages it holds, or ``None``.

        ``None`` (after telling the user why) when there is nothing to take out yet,
        or when an answer is still being written.
        """
        if self._is_streaming:
            show_snackbar(self.page_ref, WAIT_FOR_THE_ANSWER_TEXT)
            return None
        messages = self._exported_messages()
        if not messages:
            show_snackbar(self.page_ref, NOTHING_TO_EXPORT_TEXT)
            return None
        text = conversation_markdown(
            self._chat_name() or "Разговор",
            messages,
            now(),
            truncated=self._history_truncated,
        )
        return text, len(messages)

    async def on_copy_conversation_click(self, e=None) -> None:
        """Put the whole conversation on the clipboard."""
        conversation = self._conversation()
        if conversation is None:
            return
        if await copy_text(self.page_ref, conversation[0]):
            show_snackbar(self.page_ref, "Разговорът е копиран.", success=True)
        else:
            show_snackbar(self.page_ref, "Не успях да копирам.")

    async def on_export_click(self, e=None) -> None:
        """Ask where to save the conversation and save it there as a ``.md`` file."""
        conversation = self._conversation()
        if conversation is None:
            return
        text, count = conversation
        data = text.encode("utf-8")
        try:
            # On the desktop the dialog writes ``src_bytes`` to the file that was chosen.
            path = await self.file_picker.save_file(
                dialog_title="Запази разговора",
                file_name=export_file_name(self._chat_name(), now()),
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["md"],
                src_bytes=data,
            )
        except Exception as ex:
            # The message of an OSError carries the path of a file of the user.
            logger.error(
                "The conversation was not saved: %s",
                error_for_log(ex),
                exc_info=is_unexpected(ex),
            )
            show_snackbar(
                self.page_ref, f"Разговорът не беше запазен. {error_detail(ex)}"
            )
            return
        if path is None:
            return  # the user closed the dialog
        # Only the size: the text is the user's own, and so is the name of the file.
        logger.info(
            "Chat %s saved to a file (%d messages, %d bytes).",
            self.session_id[:8],
            count,
            len(data),
        )
        show_snackbar(self.page_ref, "Разговорът е запазен.", success=True)
