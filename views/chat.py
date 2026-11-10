"""Chat view components for interacting with the AI agent."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import flet as ft
import httpx

from api.client import APIClient, APIError, ChatAttachment, StreamError
from state.chat_sessions import (
    _ensure_chat_session,
    _next_chat_name,
    _normalize_chat_sessions,
    _resolve_session_id,
    _restart_chat_session,
    _unique_chat_name,
)
from state.settings import load_settings, save_settings
from views.attachments import (
    MAX_FILE_BYTES,
    MESSAGE_TOKEN_LIMIT,
    PASTE_THRESHOLD_CHARS,
    NotAttachable,
    estimate_message_tokens,
    inserted_text,
    paste_file_name,
    read_text_file,
)
from views.chat_message import ChatMessage
from views.emoji_picker import EmojiPicker
from views.ui_utils import confirm_dialog, describe_error, safe_update, show_snackbar

logger = logging.getLogger(__name__)

# Flet 0.24 ListView is not virtualised, so rendering a very long history means
# building hundreds of markdown widgets. Only the tail is rendered.
HISTORY_PAGE_SIZE = 100
# One message more than is shown: its presence is how load_history knows that
# older messages exist (the backend returns the newest N, oldest first).
HISTORY_REQUEST_LIMIT = HISTORY_PAGE_SIZE + 1
# Upper bound of control updates per second while a response is streaming.
# Each flush is a tree walk plus a synchronous round trip to the Flutter client.
STREAM_FLUSH_INTERVAL = 0.12
# How long the stream may stay silent before the "..." indicator is shown again.
ACTIVITY_INDICATOR_DELAY = 1.0
# While an answer streams the chat keeps its end in view, but only as long as the
# reader is at the bottom (within BOTTOM_FOLLOW_THRESHOLD px of it): scrolling up
# to re-read something must not be undone by the next chunk. The ListView reports
# its position at most every SCROLL_EVENT_INTERVAL ms.
BOTTOM_FOLLOW_THRESHOLD = 40
SCROLL_EVENT_INTERVAL = 50
# The client lays a text update out on its next frame, after a scroll_to sent
# together with it was already handled - so the scroll goes out this long
# (seconds) after the text, which also caps it at a few scrolls per second.
SCROLL_SETTLE_DELAY = 0.15


class ChatView(ft.Container):
    """The main view for the chat interface."""

    def __init__(
        self,
        page: ft.Page,
        api_client: APIClient,
        on_session_changed: Callable[[str], None] | None = None,
    ):
        super().__init__()
        self.page = page
        self.api_client = api_client
        self.on_session_changed = on_session_changed

        # Never mint a new id while a saved chat exists: the dropdown has to
        # keep pointing at the session ids stored in settings.json.
        settings = load_settings()
        self.session_id, chat_sessions = _ensure_chat_session(settings)
        # How the user's own messages are labelled (the ``display_name`` setting).
        self.display_name = settings.display_name

        self._stop_generation = False
        self._is_streaming = False
        self._last_assistant_msg = None
        self._last_assistant_row = None
        self._load_task = None
        self._attachments_task = None
        # The files attached to the open conversation, newest first.
        self.attachments: list[ChatAttachment] = []
        # What the input field held at the last change: a jump of thousands of
        # characters in one change is a paste (see on_input_change).
        self._input_text = ""
        self._offering = False
        # False once the reader scrolled up; see _on_chat_scroll.
        self._follow_bottom = True
        # True while a _scroll_after_layout task is waiting to scroll.
        self._scroll_pending = False

        self.expand = True

        self.chat_list = ft.ListView(
            expand=True,
            spacing=10,
            auto_scroll=False,
            padding=ft.padding.only(left=20, right=20),
            on_scroll=self._on_chat_scroll,
            on_scroll_interval=SCROLL_EVENT_INTERVAL,
        )

        self.new_chat_btn = ft.IconButton(
            icon=ft.icons.ADD_COMMENT,
            tooltip="Нов чат",
            on_click=self.on_new_chat_click,
        )

        self.delete_chat_btn = ft.IconButton(
            icon=ft.icons.DELETE_OUTLINE,
            tooltip="Изтрий разговора (чатът остава, празен)",
            on_click=self.on_delete_chat_click,
        )

        self.session_dropdown = ft.Dropdown(
            options=[
                ft.dropdown.Option(key=session["id"], text=session["name"])
                for session in chat_sessions
            ],
            value=self.session_id,
            width=200,
            on_change=self.on_session_change,
        )

        self.top_row = ft.Row(
            controls=[
                self.session_dropdown,
                ft.Row([self.delete_chat_btn, self.new_chat_btn], spacing=0),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        )

        self.input_field = ft.TextField(
            hint_text="Напиши съобщение...",
            autofocus=True,
            shift_enter=True,
            min_lines=1,
            max_lines=5,
            expand=True,
            on_submit=self.send_message_click,
            on_change=self.on_input_change,
        )

        self.file_picker = ft.FilePicker(on_result=self.on_attach_result)
        self.attach_btn = ft.IconButton(
            icon=ft.icons.ATTACH_FILE,
            tooltip="Прикачи файл към този разговор (код, лог, текст)",
            on_click=self.on_attach_click,
        )
        # The files of this conversation, with a cross to take one away.
        self.attachments_row = ft.Row(wrap=True, spacing=6, run_spacing=4, visible=False)

        self.send_btn = ft.IconButton(
            icon=ft.icons.SEND,
            tooltip="Изпрати",
            on_click=self.send_message_click,
        )

        self.stop_btn = ft.IconButton(
            icon=ft.icons.STOP,
            icon_color=ft.colors.RED,
            tooltip="Спри",
            visible=False,
            on_click=self.stop_generation_click,
        )

        self.emoji_picker = EmojiPicker(on_pick=self.insert_emoji)
        self.emoji_btn = ft.IconButton(
            icon=ft.icons.EMOJI_EMOTIONS_OUTLINED,
            tooltip="Емоджита",
            on_click=self.toggle_emoji_picker,
        )

        input_row = ft.Row(
            controls=[self.input_field, self.attach_btn, self.emoji_btn, self.stop_btn, self.send_btn],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.END,
        )

        self.content = ft.Column(
            controls=[
                self.top_row,
                self.chat_list,
                self.emoji_picker,
                self.attachments_row,
                input_row,
            ],
            expand=True,
        )

    # ------------------------------------------------------------------ state
    def _reset_conversation(self):
        """Drop everything that points at the previous session."""
        self._stop_generation = True
        self._last_assistant_msg = None
        self._last_assistant_row = None
        self._follow_bottom = True
        self.chat_list.controls.clear()
        # The files of the other conversation are not this one's.
        self.attachments = []
        self._render_attachments()
        safe_update(self)

    def _cancel_running_tasks(self):
        self.api_client.cancel_stream()
        if self._load_task is not None:
            self._load_task.cancel()
            self._load_task = None
        if self._attachments_task is not None:
            self._attachments_task.cancel()
            self._attachments_task = None

    def _set_streaming_ui(self, streaming: bool):
        self.send_btn.visible = not streaming
        self.stop_btn.visible = streaming
        self.stop_btn.disabled = False
        # Removing a chat would cut the answer that is being written into it.
        self.delete_chat_btn.disabled = streaming
        safe_update(self, self.send_btn, self.stop_btn, self.delete_chat_btn)

    def _apply_session_options(self, chat_sessions: list[dict], selected_id: str | None) -> None:
        """Re-key the dropdown so every option points at a real session id."""
        self.session_dropdown.options = [
            ft.dropdown.Option(key=session["id"], text=session["name"])
            for session in chat_sessions
        ]
        self.session_dropdown.value = selected_id
        safe_update(self, self.session_dropdown)

    def _notify_session_changed(self, session_id: str) -> None:
        """Tell the hosting app to reload the views tied to the active chat."""
        if self.on_session_changed is None:
            return
        try:
            self.on_session_changed(session_id)
        except Exception as ex:  # noqa: BLE001
            logger.warning("Session change listener failed: %s", ex)

    def _activate_session(self, session_id: str, reload_history: bool = True) -> None:
        """Point the view at ``session_id`` and reload what belongs to it."""
        self.session_id = session_id
        self._reset_conversation()
        self._cancel_running_tasks()
        if reload_history and self.page is not None:
            # The files first, the history last: the history is what the view waits for.
            self._attachments_task = self.page.run_task(self.load_attachments)
            self._load_task = self.page.run_task(self.load_history)
        self._notify_session_changed(session_id)

    async def on_session_change(self, e):
        """Open the chat the user picked, always with its saved session id.

        The dropdown is keyed by session id, but the selected value is resolved
        against ``settings.json`` first (by id, then by chat name) so that a
        legacy name-keyed option or an entry without an id still maps back to
        the session that owns the history, memories and files. A new UUID is
        generated only for a chat that is not stored yet.
        """
        event_control = getattr(e, "control", None)
        selected = getattr(event_control, "value", None) or self.session_dropdown.value
        if not selected:
            return

        settings = load_settings()
        chat_sessions = _normalize_chat_sessions(settings)
        session_id = _resolve_session_id(chat_sessions, selected)

        if session_id is None:
            # A chat that was never persisted: the only legitimate new id.
            session_id = str(uuid.uuid4())
            chat_sessions.append(
                {"id": session_id, "name": _unique_chat_name(str(selected), chat_sessions)}
            )

        settings.chat_sessions = chat_sessions
        settings.session_id = session_id
        if not save_settings(settings):
            # The chat still opens, but after a restart the app would forget it.
            show_snackbar(self.page, "Неуспешно запазване на настройките.")

        # Re-sync the widget as well: it may have carried a chat name instead
        # of an id, and the options must stay keyed by the saved ids.
        self._apply_session_options(chat_sessions, session_id)

        if session_id == self.session_id:
            return

        self._activate_session(session_id)

    async def on_new_chat_click(self, e):
        settings = load_settings()
        chat_sessions = _normalize_chat_sessions(settings)

        new_session = str(uuid.uuid4())
        chat_sessions.append({"id": new_session, "name": _next_chat_name(chat_sessions)})

        settings.chat_sessions = chat_sessions
        settings.session_id = new_session
        if not save_settings(settings):
            # The chat still opens, but after a restart the app would forget it.
            show_snackbar(self.page, "Неуспешно запазване на настройките.")
        logger.info("New chat %s started.", new_session[:8])

        self._apply_session_options(chat_sessions, new_session)
        # A brand new chat has no history to fetch, so only the UI is cleared.
        self._activate_session(new_session, reload_history=False)

    async def on_delete_chat_click(self, e):
        """Delete the open chat's conversation for good and stay in the chat, empty.

        The conversation is deleted on the backend first (messages, summary,
        attached files; the long-term memories stay). If that fails nothing
        changes and the user can try again; asking twice is safe. Then the chat
        keeps its name and its place in the list and continues as a new empty
        session (see ``_restart_chat_session``).
        """
        if self._is_streaming:
            show_snackbar(self.page, "Изчакай края на отговора или го спри, преди да изтриеш чата.")
            return

        name = next(
            (opt.text for opt in self.session_dropdown.options if opt.key == self.session_id),
            "Този чат",
        )
        confirmed = await confirm_dialog(
            self.page,
            "Изтриване на чата",
            f"Разговорът в „{name}“ ще бъде изтрит завинаги: съобщенията, "
            "резюмето и прикачените файлове. Чатът остава в списъка, но празен. "
            "Спомените на Лили за теб (дългосрочната памет) не се пипат.",
            confirm_text="Изтрий",
        )
        if not confirmed:
            return

        old_id = self.session_id
        try:
            await self.api_client.delete_session(old_id)
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to delete chat %s on the backend: %s", old_id[:8], ex)
            show_snackbar(self.page, f"Разговорът не беше изтрит, чатът е непроменен. {describe_error(ex)}")
            return

        # Read after the dialog and the call: the settings may have been saved meanwhile.
        settings = load_settings()
        new_id = _restart_chat_session(settings, old_id)
        if new_id is None:
            return

        if not save_settings(settings):
            show_snackbar(self.page, "Неуспешно запазване на настройките.")
            return
        logger.info("Chat %s was deleted on the backend, it continues as session %s.", old_id[:8], new_id[:8])

        self._apply_session_options(settings.chat_sessions, new_id)
        # The new session has no history to fetch, so only the screen is cleared.
        self._activate_session(new_id, reload_history=False)
        show_snackbar(
            self.page,
            "Разговорът е изтрит. Спомените са запазени.",
            success=True,
        )

    def scroll_to_bottom(self):
        """Jump to the newest message and follow what is appended after it."""
        self._follow_bottom = True
        if self.chat_list.uid is not None:
            self.chat_list.scroll_to(offset=-1, duration=100)
        # New rows are laid out on the client's next frame, after this scroll.
        self._follow_stream()

    def _follow_stream(self):
        """Keep the end of a growing answer in view, unless the reader left it.

        ``ListView.auto_scroll`` is off on purpose: it only reacts when the list
        itself is rebuilt (streamed text updates just the Markdown control, so an
        answer grew past the bottom edge unseen), and once our own scroll_to
        rebuilt the list it animated slowly against it, leaving the end 1-2 lines
        out of view. The scroll is deferred (see SCROLL_SETTLE_DELAY) and at most
        one is waiting at any time.
        """
        if self._scroll_pending or not self._follow_bottom or self.page is None:
            return
        self._scroll_pending = True
        self.page.run_task(self._scroll_after_layout)

    async def _scroll_after_layout(self):
        try:
            await asyncio.sleep(SCROLL_SETTLE_DELAY)
        finally:
            self._scroll_pending = False
        if self._follow_bottom and self.chat_list.uid is not None:
            self.chat_list.scroll_to(offset=-1)

    def _on_chat_scroll(self, e: ft.OnScrollEvent):
        """Track whether the reader is at the bottom of the conversation.

        Our own scrolling only ever moves down, so a user event pointing back
        (mouse wheel, touch) or a negative delta (dragging the scrollbar) means
        the reader went up to older text and the stream must stop pulling the
        view down. Arriving at the bottom again resumes following.
        """
        scrolled_up = (e.event_type == "user" and e.direction == "forward") or (
            e.event_type == "update" and (e.scroll_delta or 0) < 0
        )
        if scrolled_up:
            self._follow_bottom = False
        elif (e.max_scroll_extent or 0) - (e.pixels or 0) <= BOTTOM_FOLLOW_THRESHOLD:
            self._follow_bottom = True

    def toggle_emoji_picker(self, e=None):
        picker = self.emoji_picker
        picker.visible = not picker.visible
        self.emoji_btn.icon = (
            ft.icons.EMOJI_EMOTIONS if picker.visible else ft.icons.EMOJI_EMOTIONS_OUTLINED
        )
        safe_update(self, picker, self.emoji_btn)
        # The list got shorter or taller; keep its end in view.
        self._follow_stream()

    def insert_emoji(self, emoji: str):
        """Add ``emoji`` to the message being typed.

        The caret position of a TextField is not known to Python, so the emoji
        goes at the end of the text.
        """
        self._set_input((self.input_field.value or "") + emoji)
        if self.input_field.uid is not None:
            # focus() also sends the new value, and keeps the keyboard in the field.
            self.input_field.focus()

    def did_mount(self):
        if self.page is not None:
            if self.file_picker not in self.page.overlay:
                self.page.overlay.append(self.file_picker)
            # The files first, the history last: the history is what the view waits for.
            self._attachments_task = self.page.run_task(self.load_attachments)
            self._load_task = self.page.run_task(self.load_history)

    def will_unmount(self):
        self._cancel_running_tasks()
        if self.page is not None and self.file_picker in self.page.overlay:
            self.page.overlay.remove(self.file_picker)

    # ------------------------------------------------------------ attached files
    def _set_input(self, text: str) -> None:
        """Put text into the input field from the code, not from the user.

        The change event only comes from the user's typing; the remembered text has
        to follow the field, or the next paste would be measured against old text.
        """
        self.input_field.value = text
        self._input_text = text

    async def load_attachments(self, *_args) -> None:
        """Read the files attached to the open conversation and show them."""
        session_id = self.session_id
        try:
            found = await self.api_client.list_attachments(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # Not worth a message on screen: the chat works without the row, and the
            # next attach or reload reads the list again.
            logger.warning("Failed to read the files of chat %s: %s", session_id[:8], e)
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
                leading=ft.Icon(ft.icons.ATTACH_FILE, size=16),
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
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            if isinstance(ex, APIError) and ex.status == 404:
                # Gone already (another window, or it expired): the list is just stale.
                logger.info("Attachment %s was already gone.", attachment_id)
            else:
                logger.error("Failed to remove attachment %s: %s", attachment_id, ex)
                show_snackbar(self.page, f"Файлът не беше махнат. {describe_error(ex)}")
                return
        await self.load_attachments()

    def on_attach_click(self, e) -> None:
        self.file_picker.pick_files(
            dialog_title="Прикачи файл към този разговор",
            allow_multiple=True,
            file_type=ft.FilePickerFileType.ANY,
        )

    async def on_attach_result(self, e: ft.FilePickerResultEvent) -> None:
        """Attach the chosen files; what cannot be attached is reported together at the end."""
        if not e.files:
            return
        attached: list[str] = []
        problems: list[str] = []
        for picked in e.files:
            if not picked.path:
                continue
            try:
                size = await asyncio.to_thread(lambda p=picked.path: Path(p).stat().st_size)
                if size > MAX_FILE_BYTES:
                    raise NotAttachable(f"{picked.name}: файлът е по-голям от {MAX_FILE_BYTES // 1024} KB")
                raw = await asyncio.to_thread(Path(picked.path).read_bytes)
                text = read_text_file(raw, picked.name)
                await self._store_attachment(picked.name, text)
            except asyncio.CancelledError:
                raise
            except NotAttachable as ex:
                logger.warning("Skipped %s: %s", picked.name, ex)
                problems.append(str(ex))
            except OSError as ex:
                logger.warning("Could not read %s: %s", picked.name, ex)
                problems.append(f"{picked.name}: файлът не може да се прочете")
            except Exception as ex:  # noqa: BLE001
                logger.error("Failed to attach %s: %s", picked.name, ex)
                problems.append(f"{picked.name}: {self._attach_error_text(ex)}")
            else:
                attached.append(picked.name)
        await self.load_attachments()
        if attached:
            show_snackbar(self.page, "Прикачено към разговора: " + ", ".join(attached), success=True)
        if problems:
            show_snackbar(self.page, "Не са прикачени — " + "; ".join(problems))

    async def _store_attachment(self, name: str, text: str) -> ChatAttachment:
        """Send one file to the backend for the open conversation."""
        return await self.api_client.upload_attachment(self.session_id, name, text)

    @staticmethod
    def _attach_error_text(error: BaseException) -> str:
        """Why the backend would not take a file, in words the user can act on."""
        if isinstance(error, APIError):
            if error.status == 413:
                return f"файлът е по-голям от {MAX_FILE_BYTES // 1024} KB"
            if error.status == 422:
                return "в разговора вече има 20 файла или файловете му надхвърлят 1 MB"
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
                self.page,
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
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to attach the pasted text: %s", ex)
            show_snackbar(self.page, f"Текстът не беше прикачен — {self._attach_error_text(ex)}")
            return None
        await self.load_attachments()
        show_snackbar(self.page, f"Прикачено като {name}", success=True)
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
                self.page,
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

    async def load_history(self, *_args):
        """Render the session history with a single UI update at the end."""
        session_id = self.session_id
        try:
            messages = await self.api_client.get_messages(
                session_id=session_id, limit=HISTORY_REQUEST_LIMIT
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to load the history of chat %s: %s", session_id[:8], e)
            # Not when the user already moved to another chat: the failure is not theirs to see.
            if session_id == self.session_id:
                show_snackbar(self.page, f"Историята на чата не се зареди. {describe_error(e)}")
            return

        # The user switched sessions while the request was in flight.
        if session_id != self.session_id:
            return

        self.chat_list.controls.clear()
        self._last_assistant_msg = None
        self._last_assistant_row = None

        truncated = len(messages) > HISTORY_PAGE_SIZE
        visible = messages[-HISTORY_PAGE_SIZE:] if truncated else messages
        if truncated:
            self.chat_list.controls.append(
                ft.Text(
                    f"Показани са последните {HISTORY_PAGE_SIZE} съобщения.",
                    size=11,
                    color=ft.colors.GREY_500,
                )
            )

        last_msg_obj = None
        for msg in visible:
            last_msg_obj = self.add_message(
                msg.role or "user",
                msg.content or "",
                sources=msg.sources,
                notify=False,
            )

        # One update + one scroll for the whole history instead of one per message.
        safe_update(self)
        self.scroll_to_bottom()

        if last_msg_obj and last_msg_obj.role == "assistant":
            self._last_assistant_msg = last_msg_obj
            self._last_assistant_row = last_msg_obj._row
            last_msg_obj.set_regenerate_visible(True)

    def set_display_name(self, name: str) -> None:
        """Use ``name`` for the user's messages, the ones already on screen too."""
        self.display_name = name
        changed = []
        for row in self.chat_list.controls:
            container = row.controls[0] if getattr(row, "controls", None) else None
            message = getattr(container, "content", None)
            if isinstance(message, ChatMessage) and message.role == "user":
                message.sender_name = name
                message.sender_label.value = name
                changed.append(message.sender_label)
        if changed:
            safe_update(self, *changed)

    def add_message(self, role: str, text: str, sources: list | None = None, notify: bool = True) -> ChatMessage:
        is_user = role == "user"
        on_regenerate = self.regenerate_last if not is_user else None
        chat_message = ChatMessage(role=role, text=text, sources=sources, page=self.page, on_regenerate=on_regenerate, api_client=self.api_client, user_name=self.display_name)

        container_wrapper = ft.Container(
            content=chat_message,
            margin=ft.margin.only(left=50 if is_user else 0, right=0 if is_user else 50),
            expand=True,
            alignment=ft.alignment.top_right if is_user else ft.alignment.top_left,
        )

        row = ft.Row(
            controls=[container_wrapper],
            alignment=ft.MainAxisAlignment.END if is_user else ft.MainAxisAlignment.START,
        )

        self.chat_list.controls.append(row)
        chat_message._row = row
        if notify:
            safe_update(self)
            self.scroll_to_bottom()
        return chat_message

    async def _stream_response(self, chat_message, stream_coro, fallback_user_text=""):
        """Consume the SSE stream and paint it with as few updates as possible.

        Text chunks are buffered locally and flushed at most once per
        STREAM_FLUSH_INTERVAL; the view is updated once when the stream ends
        (instead of once per chunk, which used to walk and re-send the whole
        chat tree dozens of times per second).

        Returns the user-facing error text when the stream failed (it is also
        shown under the message), ``None`` when it finished or was stopped.
        """
        error_text = None
        stream_open = True
        last_chunk_time = time.time()

        async def show_activity_after_delay():
            try:
                while stream_open:
                    await asyncio.sleep(0.25)
                    if stream_open and (time.time() - last_chunk_time) >= ACTIVITY_INDICATOR_DELAY:
                        chat_message.show_activity_indicator()
            except asyncio.CancelledError:
                pass

        activity_task = None
        if self.page is not None:
            activity_task = self.page.run_task(show_activity_after_delay)

        pending_text = ""

        def flush_text():
            nonlocal pending_text
            if pending_text:
                chat_message.append_text(pending_text)
                pending_text = ""
                self._follow_stream()

        try:
            last_flush_time = time.time()

            async for chunk in stream_coro:
                last_chunk_time = time.time()
                chat_message.hide_activity_indicator()

                if self._stop_generation:
                    break

                if isinstance(chunk, dict):
                    if chunk.get("type") == "sources":
                        data = chunk.get("data", {})
                        if isinstance(data, list):
                            chat_message.add_sources(data)
                        elif isinstance(data, dict):
                            for key in ("facts", "chunks", "sources"):
                                if isinstance(data.get(key), list):
                                    chat_message.add_sources(data[key])
                        continue

                    if chunk.get("type") == "proposals":
                        chat_message.add_proposals(chunk.get("data") or [])
                        continue

                    for key in ("text", "content"):
                        value = chunk.get(key)
                        if isinstance(value, str):
                            pending_text += value

                    if "error" in chunk:
                        pending_text += f"\n\n**Грешка:** {chunk['error']}"

                    for source_key in ("facts", "chunks", "sources"):
                        if isinstance(chunk.get(source_key), list):
                            chat_message.add_sources(chunk[source_key])

                elif isinstance(chunk, str):
                    pending_text += chunk

                current_time = time.time()
                if current_time - last_flush_time >= STREAM_FLUSH_INTERVAL:
                    flush_text()
                    last_flush_time = current_time

            flush_text()

        except asyncio.CancelledError:
            flush_text()
        except Exception as ex:
            if isinstance(ex, (StreamError, APIError, httpx.TransportError)):
                logger.error("Chat request failed: %r", ex)
            else:
                logger.exception("Unexpected streaming error")
            flush_text()
            error_text = describe_error(ex)
            chat_message.finalize_with_error(error_text)
            # After a StreamError the backend already has the user's turn, so
            # putting it back in the input would only invite a duplicate.
            if fallback_user_text and not isinstance(ex, StreamError):
                self._set_input(fallback_user_text)
        finally:
            stream_open = False
            chat_message.hide_activity_indicator()
            if activity_task:
                activity_task.cancel()
            # Exactly one full-view update for the finished message.
            safe_update(self)
            if self.page is not None:
                self.page.run_task(self._scroll_after_layout)
        return error_text

    async def send_message_click(self, e):
        if self._is_streaming:
            # A response is already being streamed - ignore double submits.
            return

        text = (self.input_field.value or "").strip()
        if not text:
            return
        if estimate_message_tokens(text) > MESSAGE_TOKEN_LIMIT:
            # The backend would refuse it; a file is what a text this long should be.
            await self._offer_file_instead_of_message(text)
            return

        self._is_streaming = True
        self._stop_generation = False
        session_id = self.session_id
        # Only the length: what the user writes does not belong in the log.
        logger.info("Chat %s: message sent (%d characters).", session_id[:8], len(text))

        self._set_input("")
        if self.input_field.uid is not None:
            # focus() calls update() internally, so skip it for a detached field.
            self.input_field.focus()
        self._set_streaming_ui(True)

        if self._last_assistant_msg:
            self._last_assistant_msg.set_regenerate_visible(False)

        self.add_message("user", text, notify=False)
        chat_message = self.add_message("assistant", "", notify=False)
        safe_update(self)
        self.scroll_to_bottom()

        try:
            await self._stream_response(
                chat_message,
                self.api_client.stream_chat(session_id=session_id, content=text),
                text,
            )
        except asyncio.CancelledError:
            pass
        finally:
            self._is_streaming = False
            self._stop_generation = False
            self._set_streaming_ui(False)
            if session_id == self.session_id:
                self._last_assistant_msg = chat_message
                self._last_assistant_row = chat_message._row
                chat_message.set_regenerate_visible(True)

    async def regenerate_last(self, *_args):
        if self._is_streaming:
            return
        if not self._last_assistant_msg or not self._last_assistant_row:
            return
        if self._last_assistant_row not in self.chat_list.controls:
            return

        self._is_streaming = True
        self._stop_generation = False
        session_id = self.session_id
        logger.info("Chat %s: the last answer is regenerated.", session_id[:8])

        previous_msg = self._last_assistant_msg
        previous_row = self._last_assistant_row
        position = self.chat_list.controls.index(previous_row)

        previous_msg.set_regenerate_visible(False)
        self.chat_list.controls.remove(previous_row)
        self._last_assistant_msg = None
        self._last_assistant_row = None

        chat_message = None
        error = None
        try:
            self._set_streaming_ui(True)
            chat_message = self.add_message("assistant", "", notify=False)
            safe_update(self)
            self.scroll_to_bottom()
            error = await self._stream_response(
                chat_message, self.api_client.regenerate_chat(session_id=session_id)
            )
        except asyncio.CancelledError:
            pass
        finally:
            # "Стоп" ends the stream without an error, but the backend replaces
            # the stored answer only after a complete new one - an interrupted
            # regeneration leaves the old one in the history.
            stopped = self._stop_generation
            self._is_streaming = False
            self._stop_generation = False
            self._set_streaming_ui(False)
            if session_id == self.session_id:
                if error is not None or stopped or chat_message is None:
                    self._restore_previous_answer(
                        chat_message, previous_msg, previous_row, position
                    )
                    if error is not None:
                        show_snackbar(self.page, error)
                    elif stopped:
                        show_snackbar(
                            self.page,
                            "Регенерацията е спряна — предишният отговор е запазен.",
                            success=True,
                        )
                else:
                    self._last_assistant_msg = chat_message
                    self._last_assistant_row = chat_message._row
                    chat_message.set_regenerate_visible(True)

    def _restore_previous_answer(self, failed_message, previous_msg, previous_row, position):
        """Put the replaced answer back after a failed regeneration.

        The backend swaps the stored answer only once a complete new one exists,
        so after a failure the previous reply is still the one in the history.
        """
        if failed_message is not None and failed_message._row in self.chat_list.controls:
            self.chat_list.controls.remove(failed_message._row)
        if previous_row not in self.chat_list.controls:
            self.chat_list.controls.insert(
                min(position, len(self.chat_list.controls)), previous_row
            )
        self._last_assistant_msg = previous_msg
        self._last_assistant_row = previous_row
        previous_msg.set_regenerate_visible(True)
        safe_update(self)

    async def stop_generation_click(self, e):
        logger.info("Chat %s: generation stopped by the user.", self.session_id[:8])
        self._stop_generation = True
        self.api_client.cancel_stream()
        self.stop_btn.disabled = True
        safe_update(self, self.stop_btn)


