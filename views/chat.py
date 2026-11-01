from __future__ import annotations

"""Chat view components for interacting with the AI agent."""

import asyncio
import logging
import time
import uuid
from typing import Callable

import flet as ft
import httpx

from api.client import APIClient, APIError, Proposal, StreamError
from state.settings import Settings, load_settings, save_settings
from views.proposals import ProposalCard
from views.ui_utils import describe_error, safe_update, show_snackbar

logger = logging.getLogger(__name__)

DEBUG_MOCK_SOURCES = False

# Flet 0.24 ListView is not virtualised, so rendering a very long history means
# building hundreds of markdown widgets. Only the tail is rendered.
HISTORY_PAGE_SIZE = 100
# Upper bound of control updates per second while a response is streaming.
# Each flush is a tree walk plus a synchronous round trip to the Flutter client.
STREAM_FLUSH_INTERVAL = 0.12
# How long the stream may stay silent before the "..." indicator is shown again.
ACTIVITY_INDICATOR_DELAY = 1.0


def _get_mock_sources():
    return [
        {"title": "guidelines.pdf", "type": "Документ", "score": "95", "text_snippet": "Това е първият пример за намерен текст от документа guidelines.pdf."},
        {"title": "receipt_123.jpg", "type": "OCR", "score": "82", "text_snippet": "Извлечен текст от касова бележка. Сума: 12.50 BGN"},
        {"title": "manual.docx", "type": "Документ", "score": "78", "text_snippet": "Важно упътване от файла manual.docx относно инсталацията."},
        {"title": "notes.txt", "type": "Документ", "score": "60", "text_snippet": "Някакви случайни записки, които са намерени от RAG системата."},
        {"title": "screenshot.png", "type": "OCR", "score": "45", "text_snippet": "Текст от екранна снимка на браузър."},
    ]


class ChatMessage(ft.Column):
    """A component representing a single chat message in the UI."""

    def __init__(self, role: str, text: str, sources: list | None = None, page: ft.Page | None = None, on_regenerate: callable | None = None, api_client: APIClient | None = None):
        super().__init__()
        self.role = role
        self.api_client = api_client
        self.text_value = text
        self.sources = sources or []
        self.page_ref = page
        self.on_regenerate = on_regenerate
        self.spacing = 2
        self.horizontal_alignment = ft.CrossAxisAlignment.END if role == "user" else ft.CrossAxisAlignment.START

        self.text_color = ft.colors.LIGHT_BLUE_200 if role == "user" else ft.colors.PINK_200
        self.sender_name = "Ивайло" if role == "user" else "Лили"
        self.bgcolor = ft.colors.BLUE_GREY_800 if role == "user" else ft.colors.BLUE_GREY_900

        self.md = ft.Markdown(
            value=self.text_value,
            selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            code_theme="atom-one-dark",
            on_tap_link=self._open_link,
            md_style_sheet=ft.MarkdownStyleSheet(
                p_text_style=ft.TextStyle(color=self.text_color),
            )
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
            color=ft.colors.GREY_500,
        )

        self.copy_btn = ft.IconButton(
            icon=ft.icons.COPY,
            icon_size=14,
            tooltip="Копирай",
            on_click=self.copy_to_clipboard,
        )

        top_controls = [self.sender_label, self.copy_btn]
        if role != "user":
            self.regenerate_btn = ft.IconButton(icon=ft.icons.REFRESH, icon_size=14, tooltip="Опитай отново", on_click=self._handle_regenerate, visible=False)
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
        self.activity_indicator = ft.Text("...", size=20, color=ft.colors.GREY_500, visible=False)
        self.error_text = ft.Text("", color=ft.colors.RED, selectable=True, visible=False)

        self.controls = [
            self.top_row,
            self.msg_container,
            self.error_text,
            self.activity_indicator,
            self.sources_row,
            self.proposals_column,
        ]

    def did_mount(self):
        if self.sources:
            self.add_sources(self.sources)

    def _open_link(self, e):
        """Open only web/mail links, never arbitrary URL schemes."""
        url = (getattr(e, "data", None) or "").strip()
        if not url.lower().startswith(("http://", "https://", "mailto:")):
            logger.warning("Blocked non-http link: %s", url)
            return
        if self.page_ref is not None:
            self.page_ref.launch_url(url)

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
        if self.page_ref:
            self.page_ref.set_clipboard(self.md.value)
            self.page_ref.open(
                ft.SnackBar(
                    content=ft.Text("Копирано"),
                    duration=2000,
                )
            )

    def append_text(self, text: str):
        """Append streamed text; only the Markdown control is sent to the client."""
        self.md.value += text
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

        self.sources_row.controls.clear()
        self.sources_row.controls.append(ft.Text("Източници:", size=12, color=ft.colors.GREY_400))

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
            short_label = (label_text[:20] + "..") if len(label_text) > 20 else label_text

            chip = ft.Chip(
                label=ft.Text(short_label, size=12),
                leading=ft.Icon(ft.icons.INSERT_DRIVE_FILE, size=14),
                on_click=lambda e, file_sources=file_sources: self.open_sources_panel(file_sources),
            )
            self.sources_row.controls.append(chip)

        self.sources_row.visible = True
        if self.sources_row.uid is not None:
            safe_update(self, self.sources_row)
        else:
            # First paint (e.g. called from did_mount): send the whole message.
            safe_update(self)

    def open_sources_panel(self, sources_to_display: list | None = None):
        if not self.page_ref:
            return

        display_list = sources_to_display if sources_to_display is not None else self.sources

        list_view = ft.ListView(expand=True, spacing=10, padding=20)

        list_view.controls.append(ft.Text("Източници (RAG)", size=20, weight=ft.FontWeight.BOLD))

        for src in display_list:
            if not isinstance(src, dict):
                src = {"title": str(src)}
            title = src.get("title") or src.get("file_name") or "Unknown"
            src_type = src.get("type") or src.get("source_type") or "Документ"
            score = src.get("score", "N/A")
            snippet = src.get("content_snippet") or src.get("text_snippet") or src.get("text") or ""

            # If score is a number, format it, otherwise leave as is
            try:
                score_float = float(score)
                # Ensure it looks like a percentage if it's 0-1 or 0-100
                if score_float <= 1.0:
                    score_str = f"{score_float*100:.1f}%"
                else:
                    score_str = f"{score_float:.1f}%"
            except (ValueError, TypeError) as e:
                logger.warning("Failed to convert score '%s' to float: %s", score, e)
                score_str = str(score)

            item = ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Text(title, weight=ft.FontWeight.BOLD, size=16),
                        ft.Text(f"• {src_type} • Сходство: {score_str}", size=12, color=ft.colors.GREY_400)
                    ]),
                    ft.Text(snippet, selectable=True, size=14, color=ft.colors.GREY_300)
                ]),
                bgcolor=ft.colors.BLUE_GREY_900,
                padding=15,
                border_radius=10,
                border=ft.border.all(1, ft.colors.BLUE_GREY_700)
            )
            list_view.controls.append(item)

        if self.bottom_sheet is not None:
            self.page_ref.close(self.bottom_sheet)

        self.bottom_sheet = ft.BottomSheet(
            content=ft.Container(
                content=list_view,
                padding=10,
                height=400,
            ),
            open=True
        )
        self.page_ref.open(self.bottom_sheet)


CHAT_SESSION_PREFIX = "Чат"


def _next_chat_name(chat_sessions: list[dict]) -> str:
    """Return the first unused "Чат N" label for a brand new chat."""
    used = {session["name"] for session in chat_sessions}
    index = 1
    while f"{CHAT_SESSION_PREFIX} {index}" in used:
        index += 1
    return f"{CHAT_SESSION_PREFIX} {index}"


def _unique_chat_name(name: str, chat_sessions: list[dict]) -> str:
    """Return a label that no other chat session is using yet."""
    name = (name or "").strip()
    if not name:
        return _next_chat_name(chat_sessions)

    used = {session["name"] for session in chat_sessions}
    if name not in used:
        return name

    index = 2
    while f"{name} ({index})" in used:
        index += 1
    return f"{name} ({index})"


def _normalize_chat_sessions(settings: Settings) -> list[dict]:
    """Return ``settings.chat_sessions`` as clean ``{"id", "name"}`` entries.

    Older builds persisted bare names and a broken write can leave entries
    without an id. Such entries are re-bound to ``settings.session_id`` when
    that id is not listed yet, so an already existing chat stays reachable
    instead of being replaced by a freshly generated session id.
    """
    chat_sessions: list[dict] = []
    used_ids: set[str] = set()
    nameless_entries: list[dict] = []

    for entry in settings.chat_sessions or []:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            continue

        raw_id = entry.get("id") or entry.get("session_id")
        session_id = str(raw_id).strip() if raw_id else ""
        name = str(entry.get("name") or entry.get("title") or "").strip()

        if not session_id:
            # Without an id the entry cannot address a session; keep its name
            # around in case it belongs to the still active session.
            nameless_entries.append({"name": name})
            continue
        if session_id in used_ids:
            continue

        used_ids.add(session_id)
        chat_sessions.append(
            {"id": session_id, "name": _unique_chat_name(name, chat_sessions)}
        )

    active_id = (settings.session_id or "").strip()
    if active_id and active_id not in used_ids:
        # The active session predates `chat_sessions` (or lost its entry):
        # keep it and reuse the name of a legacy entry when there is one.
        name = nameless_entries.pop(0)["name"] if nameless_entries else ""
        chat_sessions.append(
            {"id": active_id, "name": _unique_chat_name(name, chat_sessions)}
        )

    return chat_sessions


def _resolve_session_id(chat_sessions: list[dict], selected: str | None) -> str | None:
    """Map a dropdown value (session id or chat name) to a saved session id."""
    value = (selected or "").strip()
    if not value:
        return None

    for session in chat_sessions:
        if session["id"] == value:
            return session["id"]
    for session in chat_sessions:
        if session["name"].casefold() == value.casefold():
            return session["id"]
    return None


def _ensure_chat_session(settings: Settings) -> tuple[str, list[dict]]:
    """Resolve the session the chat view must open and repair stale settings.

    A saved ``chat_sessions`` entry always wins over generating an id: a new
    UUID is created only when the settings hold no chat at all.
    """
    previous_sessions = settings.chat_sessions
    previous_session_id = settings.session_id

    chat_sessions = _normalize_chat_sessions(settings)
    if chat_sessions:
        active_id = (previous_session_id or "").strip()
        if not any(session["id"] == active_id for session in chat_sessions):
            # `session_id` is missing or points nowhere - open the first chat
            # that is actually stored instead of inventing a new one.
            active_id = chat_sessions[0]["id"]
    else:
        # No chat was ever saved (first run): the only case that mints an id.
        active_id = (previous_session_id or "").strip() or str(uuid.uuid4())
        chat_sessions = [{"id": active_id, "name": _next_chat_name(chat_sessions)}]

    if chat_sessions != previous_sessions or active_id != previous_session_id:
        settings.chat_sessions = chat_sessions
        settings.session_id = active_id
        save_settings(settings)
    return active_id, chat_sessions


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
        self.session_id, chat_sessions = _ensure_chat_session(load_settings())

        self._stop_generation = False
        self._is_streaming = False
        self._last_assistant_msg = None
        self._last_assistant_row = None
        self._load_task = None

        self.expand = True

        self.chat_list = ft.ListView(
            expand=True,
            spacing=10,
            auto_scroll=True,
            padding=ft.padding.only(left=20, right=20),
        )

        self.new_chat_btn = ft.IconButton(
            icon=ft.icons.ADD_COMMENT,
            tooltip="Нов чат",
            on_click=self.on_new_chat_click,
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
            controls=[self.session_dropdown, self.new_chat_btn],
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
        )

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

        input_row = ft.Row(
            controls=[self.input_field, self.stop_btn, self.send_btn],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.END,
        )

        self.content = ft.Column(
            controls=[
                self.top_row,
                self.chat_list,
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
        self.chat_list.controls.clear()
        safe_update(self)

    def _cancel_running_tasks(self):
        self.api_client.cancel_stream()
        if self._load_task is not None:
            self._load_task.cancel()
            self._load_task = None

    def _set_streaming_ui(self, streaming: bool):
        self.send_btn.visible = not streaming
        self.stop_btn.visible = streaming
        self.stop_btn.disabled = False
        safe_update(self, self.send_btn, self.stop_btn)

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
        save_settings(settings)

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
        save_settings(settings)

        self._apply_session_options(chat_sessions, new_session)
        # A brand new chat has no history to fetch, so only the UI is cleared.
        self._activate_session(new_session, reload_history=False)

    def scroll_to_bottom(self):
        if self.chat_list.uid is not None:
            self.chat_list.scroll_to(offset=-1, duration=100)

    def did_mount(self):
        if DEBUG_MOCK_SOURCES:
            self.add_message("assistant", "Здравейте! Аз съм Лили. Как мога да ви помогна днес?", sources=_get_mock_sources())
        if self.page is not None:
            self._load_task = self.page.run_task(self.load_history)

    def will_unmount(self):
        self._cancel_running_tasks()

    async def load_history(self, *_args):
        """Render the session history with a single UI update at the end."""
        session_id = self.session_id
        try:
            messages = await self.api_client.get_messages(
                session_id=session_id, limit=HISTORY_PAGE_SIZE
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to load chat history: %s", e)
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

    def add_message(self, role: str, text: str, sources: list | None = None, notify: bool = True) -> ChatMessage:
        is_user = role == "user"
        on_regenerate = self.regenerate_last if not is_user else None
        chat_message = ChatMessage(role=role, text=text, sources=sources, page=self.page, on_regenerate=on_regenerate, api_client=self.api_client)

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
                self.input_field.value = fallback_user_text
        finally:
            stream_open = False
            chat_message.hide_activity_indicator()
            if activity_task:
                activity_task.cancel()
            # Exactly one full-view update for the finished message.
            safe_update(self)
        return error_text

    async def send_message_click(self, e):
        if self._is_streaming:
            # A response is already being streamed - ignore double submits.
            return

        text = (self.input_field.value or "").strip()
        if not text:
            return

        self._is_streaming = True
        self._stop_generation = False
        session_id = self.session_id

        self.input_field.value = ""
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
            self._is_streaming = False
            self._stop_generation = False
            self._set_streaming_ui(False)
            if session_id == self.session_id:
                if error is not None or chat_message is None:
                    self._restore_previous_answer(
                        chat_message, previous_msg, previous_row, position
                    )
                    if error is not None:
                        show_snackbar(self.page, error)
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
        self._stop_generation = True
        self.api_client.cancel_stream()
        self.stop_btn.disabled = True
        safe_update(self, self.stop_btn)


