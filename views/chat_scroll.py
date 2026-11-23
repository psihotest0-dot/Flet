"""Keeping the end of the conversation in view while an answer streams (P33).

``ChatScrollMixin`` is the part of ``ChatView`` that scrolls the list of messages: to
the newest message, and after the text of a growing answer - but only while the reader
is at the bottom, so that scrolling up to re-read something is not undone by the next
chunk.

It is a part of ``ChatView`` and not a class of its own: it works on the state that
``ChatView.__init__`` creates - ``chat_list`` (whose ``on_scroll`` is ``_on_chat_scroll``
and whose ``scroll_interval`` is ``SCROLL_EVENT_INTERVAL``), ``page_ref``,
``_follow_bottom``, ``_scroll_pending``, ``_scroll_events`` and ``_settle_task``.
"""

from __future__ import annotations

import asyncio
import logging

import flet as ft

from views.ui_utils import is_mounted, safe_update

logger = logging.getLogger(__name__)

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
# The client does not know how tall the rows are until it has laid them out, and it
# only does so near where the list is scrolled to. A jump to "the end" therefore
# goes to the end as it is *estimated*, the rows there are measured, and the real
# end is further on (a long history ended up 1-2 screens short of it, with no event
# to tell). After the history is drawn the jump is repeated until one of them moves
# nothing, but not more than SETTLE_ROUNDS times.
SETTLE_ROUNDS = 10
# The client builds the page of a tab anew each time the tab is opened, and the list
# of messages with it - at its top. The jump back to the end waits this long
# (seconds) for the tabs to slide into place (their ``animation_duration`` in
# ``main.py`` is 300 ms) and for the rows to be laid out.
TAB_RETURN_DELAY = 0.45


class ChatScrollMixin:
    """Scrolling of the chat list: to the end, and following a growing answer."""

    def _show_to_bottom_button(self, show: bool) -> None:
        """Show or hide the "to the newest" button; only a change is sent to the client."""
        if self.to_bottom_btn.visible == show:
            return
        self.to_bottom_btn.visible = show
        safe_update(self, self.to_bottom_btn)

    async def on_to_bottom_click(self, e=None) -> None:
        """The button: back to the newest message, and follow the answer again."""
        # The reader may be far from the end, over rows that were never measured.
        self.scroll_to_bottom(settle=True)

    def scroll_to_bottom(self, settle: bool = False):
        """Jump to the newest message and follow what is appended after it.

        ``settle`` is for a list that was just filled with many rows, or one the
        reader is far away from: the jump is repeated until it reaches the real
        end (see ``_settle_at_end``).
        """
        self._follow_bottom = True
        self._show_to_bottom_button(False)
        if self.page_ref is not None and is_mounted(self.chat_list):
            self.page_ref.run_task(self._scroll_to_end, 100)
        if settle:
            self._start_settling()
        else:
            # New rows are laid out on the client's next frame, after this scroll.
            self._follow_stream()

    def return_to_end(self) -> None:
        """The chat tab was opened again: show the end of the conversation.

        The client built the list anew, at its top, and said nothing about it: the
        view still thought the reader was at the end, so the "to the newest" button
        stayed hidden and the next answer was followed from the wrong place.
        Where the reader was before the tab was left is lost on the client anyway.
        """
        self._follow_bottom = True
        self._show_to_bottom_button(False)
        self._start_settling(delay=TAB_RETURN_DELAY)

    def _start_settling(self, delay: float | None = None) -> None:
        """Start ``_settle_at_end``; one that is still running is replaced."""
        self._stop_settling()
        if self.page_ref is None:
            return
        if delay is None:
            self._settle_task = self.page_ref.run_task(self._settle_at_end)
        else:
            self._settle_task = self.page_ref.run_task(self._settle_at_end, delay)

    def _stop_settling(self) -> None:
        task = self._settle_task
        self._settle_task = None
        if task is not None and not task.done():
            task.cancel()

    def _is_settling(self) -> bool:
        task = self._settle_task
        return task is not None and not task.done()

    async def _settle_at_end(self, delay: float | None = None) -> None:
        """Jump to the end again and again, until a jump moves nothing.

        A jump that has nowhere to go raises no scroll event, one that moves the
        list raises a few: so no new event since the jump means the end was the
        real one. The pause before each jump is for the client to lay out the rows
        it was just sent; the reader taking over (see ``_on_chat_scroll``) cancels
        the task. ``delay`` replaces the first pause (``SCROLL_SETTLE_DELAY``).
        """
        await asyncio.sleep(SCROLL_SETTLE_DELAY if delay is None else delay)
        for _ in range(SETTLE_ROUNDS):
            seen = self._scroll_events
            await self._scroll_to_end()
            await asyncio.sleep(SCROLL_SETTLE_DELAY)
            if self._scroll_events == seen:
                return

    async def _scroll_to_end(self, duration: int | None = None) -> None:
        """Ask the client to show the end of the list.

        The call waits for the client's answer; a list that left the page in the
        meantime, or a window that was closed, is not worth more than a debug line.
        """
        if not is_mounted(self.chat_list):
            return
        try:
            await self.chat_list.scroll_to(offset=-1, duration=duration)
        except Exception as ex:  # noqa: BLE001
            logger.debug("The chat was not scrolled: %s", ex)

    def _follow_stream(self):
        """Keep the end of a growing answer in view, unless the reader left it.

        ``ListView.auto_scroll`` is off on purpose: it only reacts when the list
        itself is rebuilt (streamed text updates just the Markdown control, so an
        answer grew past the bottom edge unseen), and once our own scroll_to
        rebuilt the list it animated slowly against it, leaving the end 1-2 lines
        out of view. The scroll is deferred (see SCROLL_SETTLE_DELAY) and at most
        one is waiting at any time.
        """
        if self._scroll_pending or not self._follow_bottom or self.page_ref is None:
            return
        self._scroll_pending = True
        self.page_ref.run_task(self._scroll_after_layout)

    async def _scroll_after_layout(self):
        try:
            await asyncio.sleep(SCROLL_SETTLE_DELAY)
        finally:
            self._scroll_pending = False
        if self._follow_bottom:
            await self._scroll_to_end()

    def _on_chat_scroll(self, e: ft.OnScrollEvent):
        """Track whether the reader is at the bottom of the conversation.

        Our own scrolling only ever moves down, so a user event pointing back
        (mouse wheel, touch) or a negative delta (dragging the scrollbar) means
        the reader went up to older text and the stream must stop pulling the
        view down. Arriving at the bottom again resumes following.

        While ``_settle_at_end`` runs, a negative delta is not the reader: a list
        that was sent past its real end slides back by itself. Only a mouse wheel
        or a touch (the ``USER`` event) takes the reader's side then, and ends the
        settling.
        """
        self._scroll_events += 1
        pointed_up = (
            e.event_type == ft.ScrollType.USER
            and e.direction == ft.ScrollDirection.FORWARD
        )
        if pointed_up:
            self._stop_settling()
        scrolled_up = pointed_up or (
            e.event_type == ft.ScrollType.UPDATE
            and (e.scroll_delta or 0) < 0
            and not self._is_settling()
        )
        if scrolled_up:
            self._follow_bottom = False
        elif (e.max_scroll_extent or 0) - (e.pixels or 0) <= BOTTOM_FOLLOW_THRESHOLD:
            self._follow_bottom = True
        self._show_to_bottom_button(not self._follow_bottom)
