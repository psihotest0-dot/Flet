"""A conversation shared with Telegram: the chats, older messages, what arrives meanwhile (task 4).

``ChatPairedMixin`` is the part of ``ChatView`` that deals with the conversations the bot
writes to (``tg-<chat id>``). The app opens them as chats of its own, so one conversation
is read in two places: it adds the Telegram chats to the list (``load_paired_sessions``),
reads a long one backwards ("Зареди по-стари съобщения", ``on_load_older_click``) and,
while such a chat is open and the tab is the visible one, looks every few seconds for what
was written to it from Telegram (``refresh_paired_chat``).

It is a part of ``ChatView`` and not a class of its own: it works on the state that
``ChatView.__init__`` creates - ``api_client``, ``settings``, ``session_id``, ``chat_list``,
``page_ref``, ``_is_streaming``, ``_load_task``, ``_last_assistant_msg`` and the four of
its own: ``_oldest_id`` (the first message on the screen), ``_tail_id`` (the last one),
``_older_row`` and ``_paired_active``.
"""

from __future__ import annotations

import asyncio
import logging

import flet as ft

from api.errors import APIError
from state.chat_sessions import (
    ensure_paired_sessions,
    is_paired_session,
    normalize_chat_sessions,
)
from state.settings import save_settings
from views.chat_scroll import SCROLL_SETTLE_DELAY
from views.ui_utils import (
    SETTINGS_NOT_SAVED_TEXT,
    describe_error,
    is_mounted,
    is_unexpected,
    safe_update,
    show_snackbar,
)

logger = logging.getLogger(__name__)

# The chat list builds every row at once (see ``build_controls_on_demand`` in ChatView),
# so a very long history would mean hundreds of markdown widgets. A page is this many.
HISTORY_PAGE_SIZE = 100
# One message more than is shown: its presence is how the view knows that older messages
# exist (the backend returns the newest N, oldest first).
HISTORY_REQUEST_LIMIT = HISTORY_PAGE_SIZE + 1

# How often an open Telegram conversation is looked at, and how far a failing backend
# pushes that (it doubles after every failure).
PAIRED_POLL_INTERVAL = 10.0
PAIRED_POLL_MAX_INTERVAL = 60.0

# The first message that was on the screen before older ones were put above it: the
# list is scrolled back to it, so the reader keeps their place.
OLDER_ANCHOR_KEY = "older-anchor"
OLDER_BUTTON_TEXT = "Зареди по-стари съобщения"
OLDER_HINT = f"Показани са последните {HISTORY_PAGE_SIZE} съобщения."
# ``_tail_id`` of a conversation known to be empty; ``None`` is "not known yet".
EMPTY_TAIL = ""


class ChatPairedMixin:
    """Telegram conversations, older messages and the poll of an open one."""

    # ------------------------------------------------------------------ the chats
    async def load_paired_sessions(self, *_args) -> None:
        """Add the Telegram conversations the backend lists to the chats of the dropdown.

        A failure is not the user's business (the chats they have still work): it is
        in the log, and a backend without the endpoint (404) is only "no Telegram".
        """
        try:
            sessions = await self.api_client.list_telegram_sessions()
        except APIError as ex:
            if ex.status == 404:
                logger.info("The backend lists no Telegram conversations (404).")
            else:
                logger.warning("Could not list the Telegram conversations: %s", ex)
            return
        except Exception as ex:
            logger.warning(
                "Could not list the Telegram conversations: %s",
                ex,
                exc_info=is_unexpected(ex),
            )
            return

        settings = self.settings
        snapshot = settings.snapshot()
        added = ensure_paired_sessions(
            settings, [(item.session_id, item.name) for item in sessions]
        )
        if not added:
            return
        if not save_settings(settings):
            # The dropdown stays as it is, so the settings stay as they were.
            settings.restore(snapshot)
            show_snackbar(self.page_ref, SETTINGS_NOT_SAVED_TEXT)
            return
        logger.info("%d Telegram conversation(s) were added to the chats.", len(added))
        self._apply_session_options(normalize_chat_sessions(settings), self.session_id)

    # ------------------------------------------------------------ older messages
    def _make_older_row(self) -> ft.Control:
        """The row above the oldest message on the screen, with the button for older ones."""
        self._older_button = ft.TextButton(
            OLDER_BUTTON_TEXT,
            icon=ft.Icons.EXPAND_LESS,
            tooltip=OLDER_HINT,
            on_click=self.on_load_older_click,
        )
        self._older_row = ft.Row(
            [self._older_button], alignment=ft.MainAxisAlignment.CENTER
        )
        return self._older_row

    def _note_history(self, shown: list) -> None:
        """Remember where the messages on the screen begin and end (their ids)."""
        self._oldest_id = shown[0].id if shown else None
        self._tail_id = shown[-1].id if shown else EMPTY_TAIL

    async def on_load_older_click(self, e=None) -> None:
        """Put the messages older than the oldest shown above it, without moving the reader."""
        if self._loading_older or not self._oldest_id or self._older_row is None:
            return
        session_id = self.session_id
        self._loading_older = True
        self._older_button.disabled = True
        safe_update(self, self._older_button)
        try:
            older = await self.api_client.get_messages(
                session_id, limit=HISTORY_REQUEST_LIMIT, before=self._oldest_id
            )
        except Exception as ex:
            logger.error(
                "Could not load older messages of chat %s: %s",
                session_id[:8],
                ex,
                exc_info=is_unexpected(ex),
            )
            self._loading_older = False
            if session_id != self.session_id:
                return
            self._older_button.disabled = False
            safe_update(self, self._older_button)
            show_snackbar(
                self.page_ref,
                f"По-старите съобщения не се заредиха. {describe_error(ex)}",
            )
            return
        self._loading_older = False
        if session_id != self.session_id:
            return

        more = len(older) > HISTORY_PAGE_SIZE
        shown = older[-HISTORY_PAGE_SIZE:] if more else older
        controls = self.chat_list.controls
        if shown and self._older_row in controls:
            first = controls.index(self._older_row) + 1
            anchor = controls[first] if first < len(controls) else None
            if anchor is not None:
                anchor.key = OLDER_ANCHOR_KEY
            for offset, msg in enumerate(shown):
                self.add_message(
                    msg.role or "user",
                    msg.content or "",
                    sources=msg.sources,
                    notify=False,
                    created_at=msg.created_at,
                    index=first + offset,
                    remembered=msg.remembered,
                )
            self._oldest_id = shown[0].id
            # The reader is up in the old text, not at the end of the conversation.
            self._follow_bottom = False
            self._show_to_bottom_button(True)
            self._history_truncated = more
            if anchor is not None and self.page_ref is not None:
                self.page_ref.run_task(self._keep_place, anchor)
        if not more:
            # The start of the conversation is on the screen: nothing more to ask for.
            if self._older_row in controls:
                controls.remove(self._older_row)
            self._older_row = None
            self._history_truncated = False
        else:
            self._older_button.disabled = False
        safe_update(self)

    async def _keep_place(self, anchor: ft.Control) -> None:
        """Scroll back to the message the reader was at, once the new rows are laid out."""
        await asyncio.sleep(SCROLL_SETTLE_DELAY)
        try:
            if is_mounted(self.chat_list):
                await self.chat_list.scroll_to(scroll_key=OLDER_ANCHOR_KEY, duration=0)
        except Exception as ex:  # noqa: BLE001
            logger.debug("The chat was not scrolled back to its place: %s", ex)
        finally:
            anchor.key = None

    # ---------------------------------------------- what arrives from Telegram
    def set_active(self, active: bool) -> None:
        """Look at an open Telegram conversation only while the chat tab is the visible one."""
        self._paired_active = active

    def _start_paired_poll(self) -> None:
        if self._paired_task is None and self.page_ref is not None:
            self._paired_task = self.page_ref.run_task(self._poll_paired)

    def _stop_paired_poll(self) -> None:
        if self._paired_task is not None:
            self._paired_task.cancel()
            self._paired_task = None

    def _paired_wanted(self) -> bool:
        """Whether the open conversation should be looked at now."""
        loading = self._load_task is not None and not self._load_task.done()
        return (
            self._paired_active
            and is_paired_session(self.session_id)
            and not self._is_streaming
            and not loading
        )

    async def _poll_paired(self, *_args) -> None:
        delay = PAIRED_POLL_INTERVAL
        failed = False
        while True:
            await asyncio.sleep(delay)
            if not self._paired_wanted():
                continue
            try:
                await self.refresh_paired_chat()
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                # One line when it starts to fail, not one per try while the backend is down.
                if not failed:
                    logger.warning(
                        "Could not refresh the Telegram conversation: %s",
                        ex,
                        exc_info=is_unexpected(ex),
                    )
                    failed = True
                delay = min(delay * 2, PAIRED_POLL_MAX_INTERVAL)
            else:
                failed = False
                delay = PAIRED_POLL_INTERVAL

    async def refresh_paired_chat(self) -> bool:
        """Draw what was written to the open conversation since the screen was made.

        One small request asks for the newest message; only when its id is not the one the
        screen ends with is a page fetched, and only the messages after the known one are
        added. If the known one is not in the page (the conversation was emptied, or more
        than a page was written), the conversation is read again from the start. The first
        look after a turn of the app only learns where the conversation stands. Returns
        whether something was drawn.
        """
        session_id = self.session_id
        latest = await self.api_client.get_messages(session_id, limit=1)
        if session_id != self.session_id or self._is_streaming or not latest:
            return False
        latest_id = latest[0].id
        if latest_id is None:
            return False
        if self._tail_id is None:
            self._tail_id = latest_id
            return False
        if latest_id == self._tail_id:
            return False

        page = await self.api_client.get_messages(
            session_id, limit=HISTORY_REQUEST_LIMIT
        )
        if session_id != self.session_id or self._is_streaming or not page:
            return False
        ids = [message.id for message in page]
        if self._tail_id in ids:
            new = page[ids.index(self._tail_id) + 1 :]
            if new:
                self._append_new(new)
            return bool(new)
        await self.load_history()
        return True

    def _append_new(self, new: list) -> None:
        """Add messages that arrived from the other channel at the end of the screen."""
        if self._last_assistant_msg is not None:
            self._last_assistant_msg.set_regenerate_visible(False)
        last = None
        for msg in new:
            last = self.add_message(
                msg.role or "user",
                msg.content or "",
                sources=msg.sources,
                notify=False,
                created_at=msg.created_at,
                remembered=msg.remembered,
            )
        self._tail_id = new[-1].id
        self._show_empty_state(False)
        safe_update(self)
        # Only when the reader is at the end: scrolling up to re-read is not undone.
        self._follow_stream()
        if last is not None and last.role == "assistant":
            self._last_assistant_msg = last
            self._last_assistant_row = last.row
            last.set_regenerate_visible(True)
        else:
            self._last_assistant_msg = None
            self._last_assistant_row = None

    def _forget_tail(self) -> None:
        """After a turn of the app: what the conversation ends with is not known any more.

        The turn added messages the screen has no ids for. The next look learns the end
        (``_learn_tail`` does it at once), so that those messages are not fetched again
        and drawn twice.
        """
        self._tail_id = None
        if is_paired_session(self.session_id) and self.page_ref is not None:
            self.page_ref.run_task(self._learn_tail)

    async def _learn_tail(self, *_args) -> None:
        session_id = self.session_id
        try:
            latest = await self.api_client.get_messages(session_id, limit=1)
        except Exception as ex:  # noqa: BLE001
            logger.debug("The end of the conversation was not learned: %s", ex)
            return
        if session_id == self.session_id and latest and self._tail_id is None:
            self._tail_id = latest[0].id
