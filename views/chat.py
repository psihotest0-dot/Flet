"""The chat screen: the conversations, the model of each, the history and the input.

``ChatView`` is one class in several files, so that none is longer than a screen can
hold: this one has the state (``__init__``), the chats (open, new, delete), the model
button, the history and the message field; the files of a conversation are in
``views.chat_attachments``, the conversations shared with Telegram (their chats, older
messages, what arrives meanwhile) in ``views.chat_paired``, the scrolling in
``views.chat_scroll`` and the sending, regenerating and stopping of an answer in
``views.chat_turns``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import datetime

import flet as ft

from api.client import APIClient
from api.models import ChatAttachment
from state.chat_sessions import (
    MAX_CHAT_NAME_CHARS,
    ChatSession,
    ensure_chat_session,
    is_paired_session,
    next_chat_name,
    normalize_chat_sessions,
    rename_chat_session,
    reset_chat_session,
    resolve_session_id,
    store_chat_sessions,
    unique_chat_name,
)
from state.settings import Settings, load_settings, save_settings
from views import theme
from views.chat_attachments import ChatAttachmentsMixin
from views.chat_bills import ChatBillsMixin
from views.chat_export import ChatExportMixin
from views.chat_message import ChatMessage
from views.chat_modes import ChatModesMixin
from views.chat_paired import (
    EMPTY_TAIL,
    HISTORY_PAGE_SIZE,
    HISTORY_REQUEST_LIMIT,
    OLDER_HINT,
    ChatPairedMixin,
)
from views.chat_scroll import SCROLL_EVENT_INTERVAL, ChatScrollMixin
from views.chat_task import ChatTaskMixin
from views.chat_turns import ChatTurnsMixin
from views.emoji_picker import EmojiPicker
from views.shortcuts import NEW_CHAT, RENAME, STOP, shortcut_action
from views.ui_utils import (
    SETTINGS_NOT_SAVED_TEXT,
    close_dialog,
    confirm_dialog,
    describe_error,
    is_mounted,
    is_unexpected,
    open_dialog,
    safe_update,
    show_snackbar,
)

logger = logging.getLogger(__name__)

# HISTORY_PAGE_SIZE and HISTORY_REQUEST_LIMIT (how much of a long history is drawn, and
# the one message more that tells there is older) are in ``views.chat_paired``.


# The model a conversation is sent to: what the button shows and what the backend is told.
# "auto" leaves the choice to the backend (use_paid is left out of the request), "cloud"
# sends the whole conversation to the paid model (use_paid true), "home" keeps it on this
# computer even for a question to the library (use_paid false). The button goes round them:
# the word "auto" in the first mode (an empty cloud there looked like "cloud, off"), a cloud
# in the second, a house in the third. Each entry is (label, icon, colour, tooltip).
CLOUD_MODE_ORDER = ("auto", "cloud", "home")
CLOUD_MODE_VIEW = {
    "auto": (
        "auto",
        None,
        theme.MUTED,
        "Модел: както е настроено (въпросите към библиотеката могат да отиват в облака). "
        "Натисни, за да пращаш целия разговор в облака.",
    ),
    "cloud": (
        None,
        ft.Icons.CLOUD,
        theme.ACCENT,
        "Целият разговор отива в облака (платеният модел). "
        "Натисни за „само на този компютър“.",
    ),
    "home": (
        None,
        ft.Icons.HOME,
        theme.OK,
        "Само на този компютър: нищо не отива в облака. Натисни за автоматичния избор.",
    ),
}
CLOUD_MODE_USE_PAID = {"auto": None, "cloud": True, "home": False}

# What an empty chat offers to begin with. A click only puts the text into the
# message field (and the caret after it): nothing is sent until the user does it.
EMPTY_CHAT_SUGGESTIONS = (
    "Какво си спомняш за мен?",
    "Какво пише в моите файлове за ",
    "Помогни ми да обмисля нещо: ",
)


class ChatView(
    ChatAttachmentsMixin,
    ChatBillsMixin,
    ChatModesMixin,
    ChatTaskMixin,
    ChatExportMixin,
    ChatPairedMixin,
    ChatScrollMixin,
    ChatTurnsMixin,
    ft.Container,
):
    """The main view for the chat interface (the parts are mixins, see the module)."""

    def __init__(
        self,
        page: ft.Page,
        api_client: APIClient,
        on_session_changed: Callable[[str], None] | None = None,
        settings: Settings | None = None,
    ):
        super().__init__()
        # ``Control.page`` is read-only and raises until the view is on the page;
        # the page the app was started with is kept under its own name.
        self.page_ref = page
        self.api_client = api_client
        self.on_session_changed = on_session_changed

        # Never mint a new id while a saved chat exists: the dropdown has to
        # keep pointing at the session ids stored in settings.json.
        # One object for the whole app (``main`` hands over its own): what a handler
        # changes is what every other one reads, with no file read in between.
        self.settings = settings if settings is not None else load_settings()
        self.session_id, chats = ensure_chat_session(self.settings)
        # How the user's own messages are labelled (the ``display_name`` setting).
        self.display_name = self.settings.display_name

        self._stop_generation = False
        self._is_streaming = False
        self._last_assistant_msg = None
        self._last_assistant_row = None
        self._load_task = None
        self._attachments_task = None
        # The task that reads the answer being streamed; "Спри" cancels it.
        self._stream_task: asyncio.Future | None = None
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
        # How many scroll events the list has reported, and the task that repeats the
        # jump to the end until the list has settled (``None``: none); see
        # ``views.chat_scroll``.
        self._scroll_events = 0
        self._settle_task: asyncio.Future | None = None
        # True when the history has older messages than the ones on the screen.
        self._history_truncated = False
        # The ids of the first and of the last message on the screen (``None``: not
        # known), the row with the button for older ones, and whether an open Telegram
        # conversation is being looked at (the chat tab is the visible one); see
        # ``views.chat_paired``.
        self._oldest_id: str | None = None
        self._tail_id: str | None = None
        # The end the screen had before the turns of the app it has no ids for, and the
        # text each of them sent (``None``: a regeneration); ``None`` when there are none.
        self._unseen_turns: tuple[str | None, list[str | None]] | None = None
        # A history that arrived while an answer was being written: drawing it then
        # would take that answer off the screen, so it is read again after the turn.
        self._reload_after_turn = False
        self._older_row: ft.Control | None = None
        self._older_button: ft.TextButton | None = None
        self._loading_older = False
        self._paired_active = True
        self._paired_task: asyncio.Future | None = None

        self.expand = True

        self.chat_list = ft.ListView(
            left=0,
            top=0,
            right=0,
            bottom=0,
            spacing=10,
            auto_scroll=False,
            # Rows built only when they scroll into view have an estimated height
            # until then, and "scroll to the end" would stop short of the real end.
            build_controls_on_demand=False,
            padding=ft.Padding.only(left=20, right=20),
            on_scroll=self._on_chat_scroll,
            scroll_interval=SCROLL_EVENT_INTERVAL,
        )

        # What an empty conversation says instead of a blank area; see
        # _show_empty_state. It lies over the list, which is empty then.
        self.empty_state = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.CHAT_BUBBLE_OUTLINE, size=48, color=theme.FAINT),
                    ft.Text("Започни разговор с Лили", size=20),
                    ft.Text(
                        "Пиши свободно или започни с някой от примерите.",
                        color=theme.MUTED,
                    ),
                    ft.Row(
                        [
                            ft.Chip(
                                label=ft.Text(text.strip()),
                                on_click=lambda e, text=text: self._use_suggestion(
                                    text
                                ),
                            )
                            for text in EMPTY_CHAT_SUGGESTIONS
                        ],
                        wrap=True,
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=10,
                tight=True,
            ),
            alignment=ft.Alignment.CENTER,
            left=0,
            top=0,
            right=0,
            bottom=0,
            visible=False,
        )
        # Shown while the reader is above the end of the conversation (see
        # _on_chat_scroll): one click brings the newest message back into view.
        self.to_bottom_btn = ft.IconButton(
            icon=ft.Icons.KEYBOARD_ARROW_DOWN,
            tooltip="Към най-новото",
            bgcolor=theme.FLOATING,
            visible=False,
            on_click=self.on_to_bottom_click,
        )
        self.messages_area = ft.Stack(
            [
                self.chat_list,
                self.empty_state,
                ft.Container(self.to_bottom_btn, right=24, bottom=12),
            ],
            expand=True,
        )

        self.new_chat_btn = ft.IconButton(
            icon=ft.Icons.ADD_COMMENT,
            tooltip="Нов чат (Ctrl+N)",
            on_click=self.on_new_chat_click,
        )

        self.delete_chat_btn = ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE,
            tooltip="Изтрий разговора (чатът остава, празен)",
            on_click=self.on_delete_chat_click,
        )

        self.session_dropdown = ft.Dropdown(
            options=self._dropdown_options(chats),
            value=self.session_id,
            width=200,
            on_select=self.on_session_change,
        )

        # What can be done with the open chat besides opening, starting and deleting.
        self.rename_item = ft.PopupMenuItem(
            content="Преименувай чата (F2)",
            icon=ft.Icons.EDIT,
            on_click=self.on_rename_click,
        )
        self.copy_item = ft.PopupMenuItem(
            content="Копирай разговора",
            icon=ft.Icons.COPY_ALL,
            on_click=self.on_copy_conversation_click,
        )
        self.export_item = ft.PopupMenuItem(
            content="Запази като файл (.md)",
            icon=ft.Icons.SAVE_ALT,
            on_click=self.on_export_click,
        )
        self.more_btn = ft.PopupMenuButton(
            icon=ft.Icons.MORE_VERT,
            tooltip="Още за този чат",
            items=[self.rename_item, self.copy_item, self.export_item],
        )

        self.top_row = ft.Row(
            controls=[
                ft.Row([self.session_dropdown, self.more_btn], spacing=0),
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

        # A service, not a control: Flet registers it with the page by itself.
        self.file_picker = ft.FilePicker()
        self.attach_btn = ft.IconButton(
            icon=ft.Icons.ATTACH_FILE,
            tooltip="Прикачи файл към този разговор (код, лог, текст)",
            on_click=self.on_attach_click,
        )
        # The paperclip and the modes of the next message, in one menu (views.chat_modes).
        self.chat_mode: str | None = None
        self.mode_menu = self._build_mode_menu()
        self.mode_chip = self._build_mode_chip()
        # The task the server walks in the open conversation (views.chat_task).
        self._task_watch: asyncio.Future | None = None
        self.task_bar = self._build_task_bar()
        # Which model the conversation is sent to; see CLOUD_MODE_VIEW.
        self.cloud_btn = ft.TextButton(on_click=self.on_cloud_click)
        self._show_cloud_mode()
        # The files of this conversation, with a cross to take one away.
        self.attachments_row = ft.Row(
            wrap=True, spacing=6, run_spacing=4, visible=False
        )

        self.send_btn = ft.IconButton(
            icon=ft.Icons.SEND,
            tooltip="Изпрати",
            on_click=self.send_message_click,
        )

        self.stop_btn = ft.IconButton(
            icon=ft.Icons.STOP,
            icon_color=theme.PROBLEM,
            tooltip="Спри (Esc)",
            visible=False,
            on_click=self.stop_generation_click,
        )

        self.emoji_picker = EmojiPicker(on_pick=self.insert_emoji)
        self.emoji_btn = ft.IconButton(
            icon=ft.Icons.EMOJI_EMOTIONS_OUTLINED,
            tooltip="Емоджита",
            on_click=self.toggle_emoji_picker,
        )

        input_row = ft.Row(
            controls=[
                self.input_field,
                self.mode_menu,
                self.cloud_btn,
                self.emoji_btn,
                self.stop_btn,
                self.send_btn,
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.END,
        )

        self.content = ft.Column(
            controls=[
                self.top_row,
                self.messages_area,
                self.emoji_picker,
                self.attachments_row,
                self.task_bar,
                self.mode_chip,
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
        self._history_truncated = False
        self._oldest_id = None
        self._tail_id = None
        self._unseen_turns = None
        self._reload_after_turn = False
        self._older_row = None
        self._loading_older = False
        self._show_to_bottom_button(False)
        self._show_empty_state(False)
        self.chat_list.controls.clear()
        # The files of the other conversation are not this one's, nor is its mode.
        self.attachments = []
        if getattr(self, "chat_mode", None) is not None:
            self.clear_mode()
        if getattr(self, "task_bar", None) is not None:
            self._stop_task_watch()
        self._render_attachments()
        safe_update(self)

    def _cancel_stream(self) -> None:
        """Stop reading the answer that is being streamed, if there is one."""
        task = self._stream_task
        if task is not None and not task.done():
            task.cancel()

    def _cancel_running_tasks(self):
        self._cancel_stream()
        self._stop_settling()
        if self._load_task is not None:
            self._load_task.cancel()
            self._load_task = None
        if self._attachments_task is not None:
            self._attachments_task.cancel()
            self._attachments_task = None

    def _show_empty_state(self, show: bool) -> None:
        """Show or hide what an empty chat says; only a change is sent to the client."""
        if self.empty_state.visible == show:
            return
        self.empty_state.visible = show
        safe_update(self, self.empty_state)

    def _use_suggestion(self, text: str) -> None:
        """Put an example into the message field for the user to finish or send."""
        self._set_input(text)
        safe_update(self, self.input_field)
        self._focus_input()

    def _set_streaming_ui(self, streaming: bool):
        self.send_btn.visible = not streaming
        self.stop_btn.visible = streaming
        self.stop_btn.disabled = False
        # Removing a chat would cut the answer that is being written into it, and
        # a copy or a file of it would hold half an answer that looks finished.
        self.delete_chat_btn.disabled = streaming
        self.copy_item.disabled = streaming
        self.export_item.disabled = streaming
        safe_update(
            self, self.send_btn, self.stop_btn, self.delete_chat_btn, self.more_btn
        )

    @staticmethod
    def _dropdown_options(chats: list[ChatSession]) -> list[ft.DropdownOption]:
        """One option per chat, keyed by its session id."""
        return [ft.DropdownOption(key=chat.id, text=chat.name) for chat in chats]

    def _apply_session_options(
        self, chats: list[ChatSession], selected_id: str | None
    ) -> None:
        """Re-key the dropdown so every option points at a real session id."""
        self.session_dropdown.options = self._dropdown_options(chats)
        self.session_dropdown.value = selected_id
        safe_update(self, self.session_dropdown)

    def _cloud_mode(self) -> str:
        """The model the open conversation is sent to: "auto", "cloud" or "home"."""
        return self.settings.cloud_modes.get(self.session_id, "auto")

    def _use_paid(self) -> bool | None:
        """The ``use_paid`` of a request for the open conversation (``None``: left out)."""
        return CLOUD_MODE_USE_PAID[self._cloud_mode()]

    def _chat_options(self) -> dict:
        """The extra arguments of a chat request: ``use_paid`` only when it is chosen."""
        use_paid = self._use_paid()
        return {} if use_paid is None else {"use_paid": use_paid}

    def _show_cloud_mode(self) -> None:
        """Draw the button for the mode of the open conversation (no update is sent)."""
        label, icon, color, tooltip = CLOUD_MODE_VIEW[self._cloud_mode()]
        self.cloud_btn.content = label
        self.cloud_btn.icon = icon
        self.cloud_btn.icon_color = color
        self.cloud_btn.style = ft.ButtonStyle(color=color)
        self.cloud_btn.tooltip = tooltip

    async def on_cloud_click(self, e):
        """Go to the next model for this conversation: auto, cloud, home, auto."""
        settings = self.settings
        current = settings.cloud_modes.get(self.session_id, "auto")
        following = CLOUD_MODE_ORDER[
            (CLOUD_MODE_ORDER.index(current) + 1) % len(CLOUD_MODE_ORDER)
        ]
        snapshot = settings.snapshot()
        if following == "auto":
            settings.cloud_modes.pop(self.session_id, None)
        else:
            settings.cloud_modes[self.session_id] = following
        if not save_settings(settings):
            # The button keeps showing the old mode, so the settings keep it too.
            settings.restore(snapshot)
            show_snackbar(self.page_ref, SETTINGS_NOT_SAVED_TEXT)
            return
        logger.info(
            "Chat %s: the model of the conversation is %s.",
            self.session_id[:8],
            following,
        )
        self._show_cloud_mode()
        safe_update(self, self.cloud_btn)

    def _notify_session_changed(self, session_id: str) -> None:
        """Tell the hosting app to reload the views tied to the active chat."""
        if self.on_session_changed is None:
            return
        try:
            self.on_session_changed(session_id)
        except Exception as ex:
            logger.warning(
                "Session change listener failed: %s", ex, exc_info=is_unexpected(ex)
            )

    def _activate_session(self, session_id: str, reload_history: bool = True) -> None:
        """Point the view at ``session_id`` and reload what belongs to it."""
        self.session_id = session_id
        self._show_cloud_mode()
        self._reset_conversation()
        self._cancel_running_tasks()
        if not reload_history:
            # A new or an emptied chat has no history to wait for (and nothing in it).
            self._tail_id = EMPTY_TAIL
            self._show_empty_state(True)
        if reload_history and self.page_ref is not None:
            # The files first, the history last: the history is what the view waits for.
            self._start_task_watch()
            self._attachments_task = self.page_ref.run_task(self.load_attachments)
            self._load_task = self.page_ref.run_task(self.load_history)
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

        settings = self.settings
        chats = normalize_chat_sessions(settings)
        session_id = resolve_session_id(chats, selected)

        if session_id is None:
            # A chat that was never persisted: the only legitimate new id.
            session_id = str(uuid.uuid4())
            chats.append(
                ChatSession(session_id, unique_chat_name(str(selected), chats))
            )

        store_chat_sessions(settings, chats, session_id)
        if not save_settings(settings):
            # The chat still opens, but after a restart the app would forget it.
            show_snackbar(self.page_ref, SETTINGS_NOT_SAVED_TEXT)

        # Re-sync the widget as well: it may have carried a chat name instead
        # of an id, and the options must stay keyed by the saved ids.
        self._apply_session_options(chats, session_id)

        if session_id == self.session_id:
            return

        self._activate_session(session_id)

    async def on_new_chat_click(self, e):
        settings = self.settings
        chats = normalize_chat_sessions(settings)

        new_session = str(uuid.uuid4())
        chats.append(ChatSession(new_session, next_chat_name(chats)))

        store_chat_sessions(settings, chats, new_session)
        if not save_settings(settings):
            # The chat still opens, but after a restart the app would forget it.
            show_snackbar(self.page_ref, SETTINGS_NOT_SAVED_TEXT)
        logger.info("New chat %s started.", new_session[:8])

        self._apply_session_options(chats, new_session)
        # A brand new chat has no history to fetch, so only the UI is cleared.
        self._activate_session(new_session, reload_history=False)

    def handle_key(self, e) -> bool:
        """Do what a pressed key says in the chat; ``True`` if it was a shortcut that did.

        ``e`` is Flet's ``KeyboardEvent``. ``Esc`` only means something while an answer
        is written (it is then the same as "Спри"); the other keys work at any time.
        """
        action = shortcut_action(e.key, e.ctrl, e.shift, e.alt, e.meta)
        if action == STOP and self._is_streaming:
            self.page_ref.run_task(self.stop_generation_click, None)
        elif action == NEW_CHAT:
            self.page_ref.run_task(self.on_new_chat_click, None)
        elif action == RENAME:
            self.on_rename_click()
        else:
            return False
        return True

    def on_rename_click(self, e=None):
        """Ask for a new name of the open chat; only the name changes, never the id."""
        current = next(
            (o.text for o in self.session_dropdown.options if o.key == self.session_id),
            "",
        )
        field = ft.TextField(
            label="Име на чата",
            value=current,
            max_length=MAX_CHAT_NAME_CHARS,
            autofocus=True,
        )

        def save(_=None):
            if not (field.value or "").strip():
                field.error = "Въведи име на чата."
                safe_update(dialog, field)
                return
            self._rename_chat(field.value)
            close_dialog(dialog)

        field.on_submit = save
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Преименуване на чата"),
            content=ft.Container(field, width=360),
            actions=[
                ft.TextButton("Отказ", on_click=lambda _: close_dialog(dialog)),
                ft.TextButton("Запази", on_click=save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        open_dialog(self.page_ref, dialog)

    def _rename_chat(self, name: str) -> None:
        """Rename the open chat and save it; the screen follows only if the save worked."""
        settings = self.settings
        snapshot = settings.snapshot()
        new_name = rename_chat_session(settings, self.session_id, name)
        if new_name is None:
            return
        if not save_settings(settings):
            # The screen stays as it is, so the settings stay as they were.
            settings.restore(snapshot)
            show_snackbar(self.page_ref, SETTINGS_NOT_SAVED_TEXT)
            return
        # Only that it happened: the name of a chat is the user's own text.
        logger.info("Chat %s was renamed.", self.session_id[:8])
        self._apply_session_options(normalize_chat_sessions(settings), self.session_id)
        if new_name != " ".join(name.split())[:MAX_CHAT_NAME_CHARS].strip():
            show_snackbar(
                self.page_ref, f"Името вече е заето: чатът се казва „{new_name}“."
            )

    async def on_delete_chat_click(self, e):
        """Delete the open chat's conversation for good and stay in the chat, empty.

        The conversation is deleted on the backend first (messages, summary,
        attached files; the long-term memories stay). If that fails nothing
        changes and the user can try again; asking twice is safe. Then the chat
        keeps its name and its place in the list and continues as a new empty
        session (see ``restart_chat_session``).
        """
        if self._is_streaming:
            show_snackbar(
                self.page_ref,
                "Изчакай края на отговора или го спри, преди да изтриеш чата.",
            )
            return

        name = next(
            (
                opt.text
                for opt in self.session_dropdown.options
                if opt.key == self.session_id
            ),
            "Този чат",
        )
        shared = is_paired_session(self.session_id)
        confirmed = await confirm_dialog(
            self.page_ref,
            "Изтриване на чата",
            f"Разговорът в „{name}“ ще бъде изтрит завинаги: съобщенията, "
            "резюмето и прикачените файлове. Чатът остава в списъка, но празен. "
            "Спомените на Лили за теб (дългосрочната памет) не се пипат."
            + (
                " Това е общият разговор с Telegram: ще се изтрие и там — "
                "в Telegram също ще започнеш празен разговор."
                if shared
                else ""
            ),
            confirm_text="Изтрий",
        )
        if not confirmed:
            return

        old_id = self.session_id
        try:
            await self.api_client.delete_session(old_id)
        except Exception as ex:
            logger.error(
                "Failed to delete chat %s on the backend: %s",
                old_id[:8],
                ex,
                exc_info=is_unexpected(ex),
            )
            show_snackbar(
                self.page_ref,
                f"Разговорът не беше изтрит, чатът е непроменен. {describe_error(ex)}",
            )
            return

        settings = self.settings
        snapshot = settings.snapshot()
        # An ordinary chat starts over under a new id; the conversation shared with
        # Telegram keeps its id, which is the key to both channels.
        new_id = reset_chat_session(settings, old_id)
        if new_id is None:
            return

        if not save_settings(settings):
            # The screen stays as it is, so the settings stay as they were.
            settings.restore(snapshot)
            show_snackbar(self.page_ref, SETTINGS_NOT_SAVED_TEXT)
            return
        if new_id == old_id:
            logger.info(
                "Chat %s was deleted on the backend, it keeps its session.", old_id[:8]
            )
        else:
            logger.info(
                "Chat %s was deleted on the backend, it continues as session %s.",
                old_id[:8],
                new_id[:8],
            )

        self._apply_session_options(normalize_chat_sessions(settings), new_id)
        # The new session has no history to fetch, so only the screen is cleared.
        self._activate_session(new_id, reload_history=False)
        show_snackbar(
            self.page_ref,
            "Разговорът е изтрит. Спомените са запазени.",
            success=True,
        )

    def toggle_emoji_picker(self, e=None):
        self._show_emoji_picker(not self.emoji_picker.visible)

    def _show_emoji_picker(self, show: bool) -> None:
        """Show or hide the emoji picker; only a change is sent to the client."""
        picker = self.emoji_picker
        if picker.visible == show:
            return
        picker.visible = show
        self.emoji_btn.icon = (
            ft.Icons.EMOJI_EMOTIONS
            if picker.visible
            else ft.Icons.EMOJI_EMOTIONS_OUTLINED
        )
        safe_update(self, picker, self.emoji_btn)
        # The list got shorter or taller; keep its end in view.
        self._follow_stream()

    def insert_emoji(self, emoji: str):
        """Add ``emoji`` to the message being typed.

        The caret position of a TextField is not known to Python, so the emoji
        goes at the end of the text. The picker closes after the pick.
        """
        self._set_input((self.input_field.value or "") + emoji)
        safe_update(self, self.input_field)
        # One emoji is what a pick is for; the button opens the picker again.
        self._show_emoji_picker(False)
        self._focus_input()

    def _focus_input(self) -> None:
        """Put the caret back into the message field (a click on a button took it)."""
        if self.page_ref is not None and is_mounted(self.input_field):
            self.page_ref.run_task(self._request_input_focus)

    async def _request_input_focus(self) -> None:
        # Its own task: the call waits for the client's answer, and nothing that
        # matters (sending a message) may wait for that.
        if not is_mounted(self.input_field):
            return
        try:
            await self.input_field.focus()
        except Exception as ex:  # noqa: BLE001
            logger.debug("The message field was not focused: %s", ex)

    def set_active(self, active: bool) -> None:
        """The main screen says whether the chat tab is the visible one.

        Coming back to the tab, the reader is taken to the end of the conversation
        (see ``return_to_end``); the open Telegram conversation is watched only
        while the tab is visible (``ChatPairedMixin.set_active``).
        """
        came_back = active and not self._paired_active
        super().set_active(active)
        if came_back:
            self.return_to_end()

    def did_mount(self):
        if self.page_ref is not None:
            # The Telegram conversations join the dropdown; an open one is watched.
            self.page_ref.run_task(self.load_paired_sessions)
            self._start_paired_poll()
            # The files first, the history last: the history is what the view waits for.
            self._start_task_watch()
            self._attachments_task = self.page_ref.run_task(self.load_attachments)
            self._load_task = self.page_ref.run_task(self.load_history)

    def will_unmount(self):
        self._cancel_running_tasks()
        self._stop_paired_poll()
        self._stop_task_watch()

    # ---------------------------------------------------- the message field, history
    def _set_input(self, text: str) -> None:
        """Put text into the input field from the code, not from the user.

        The change event only comes from the user's typing; the remembered text has
        to follow the field, or the next paste would be measured against old text.
        """
        self.input_field.value = text
        self._input_text = text

    async def load_history(self, *_args):
        """Render the session history with a single UI update at the end."""
        session_id = self.session_id
        try:
            messages = await self.api_client.get_messages(
                session_id=session_id, limit=HISTORY_REQUEST_LIMIT
            )
        except Exception as e:
            logger.error(
                "Failed to load the history of chat %s: %s",
                session_id[:8],
                e,
                exc_info=is_unexpected(e),
            )
            # Not when the user already moved to another chat: the failure is not theirs to see.
            if session_id == self.session_id:
                show_snackbar(
                    self.page_ref,
                    f"Историята на чата не се зареди. {describe_error(e)}",
                )
            return

        # The user switched sessions while the request was in flight.
        if session_id != self.session_id:
            return
        if self._is_streaming:
            # Drawn now, it would take the answer being written off the screen (a bill
            # read meanwhile, or a message sent before the history came): it is read
            # again when the turn ends (``_reload_if_waiting``).
            self._reload_after_turn = True
            return

        self.chat_list.controls.clear()
        self._last_assistant_msg = None
        self._last_assistant_row = None

        truncated = len(messages) > HISTORY_PAGE_SIZE
        self._history_truncated = truncated
        visible = messages[-HISTORY_PAGE_SIZE:] if truncated else messages
        self._note_history(visible)
        self._older_row = None
        if truncated and self._oldest_id:
            # The button reads the older ones page by page, from the oldest shown.
            self.chat_list.controls.append(self._make_older_row())
        elif truncated:
            # A backend that sends no ids cannot be asked for "older than this".
            self.chat_list.controls.append(
                ft.Text(OLDER_HINT, size=11, color=theme.MUTED)
            )

        last_msg_obj = None
        for msg in visible:
            last_msg_obj = self.add_message(
                msg.role or "user",
                msg.content or "",
                sources=msg.sources,
                notify=False,
                created_at=msg.created_at,
                remembered=msg.remembered,
            )

        self._show_empty_state(not self.chat_list.controls)
        # One update + one scroll for the whole history instead of one per message.
        # The client has not measured these rows yet: the scroll settles at the end.
        safe_update(self)
        self.scroll_to_bottom(settle=True)

        if last_msg_obj and last_msg_obj.role == "assistant":
            self._last_assistant_msg = last_msg_obj
            self._last_assistant_row = last_msg_obj.row
            last_msg_obj.set_regenerate_visible(True)

    def set_display_name(self, name: str) -> None:
        """Use ``name`` for the user's messages, the ones already on screen too."""
        self.display_name = name
        changed = []
        for message in self._messages_on_screen():
            if message.role == "user":
                message.sender_name = name
                message.sender_label.value = name
                changed.append(message.sender_label)
        if changed:
            safe_update(self, *changed)

    def restyle_messages(self) -> None:
        """After a change of theme: the code blocks of the messages on screen."""
        for message in self._messages_on_screen():
            message.restyle()

    def add_message(
        self,
        role: str,
        text: str,
        sources: list | None = None,
        notify: bool = True,
        created_at: datetime | None = None,
        index: int | None = None,
        remembered: list[str] | None = None,
    ) -> ChatMessage:
        """Add a message to the list: at the end, or at ``index`` (older ones go above)."""
        is_user = role == "user"
        on_regenerate = self.regenerate_last if not is_user else None
        chat_message = ChatMessage(
            role=role,
            text=text,
            sources=sources,
            page=self.page_ref,
            on_regenerate=on_regenerate,
            api_client=self.api_client,
            user_name=self.display_name,
            created_at=created_at,
            remembered=remembered,
        )

        container_wrapper = ft.Container(
            content=chat_message,
            margin=ft.Margin.only(
                left=50 if is_user else 0, right=0 if is_user else 50
            ),
            expand=True,
            alignment=ft.Alignment.TOP_RIGHT if is_user else ft.Alignment.TOP_LEFT,
        )

        row = ft.Row(
            controls=[container_wrapper],
            alignment=ft.MainAxisAlignment.END
            if is_user
            else ft.MainAxisAlignment.START,
        )

        if index is None:
            self.chat_list.controls.append(row)
        else:
            self.chat_list.controls.insert(index, row)
        self._show_empty_state(False)
        chat_message.row = row
        if notify:
            safe_update(self)
            self.scroll_to_bottom()
        return chat_message
