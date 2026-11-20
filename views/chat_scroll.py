"""Keeping the end of the conversation in view while an answer streams (P33).

``ChatScrollMixin`` is the part of ``ChatView`` that scrolls the list of messages: to
the newest message, and after the text of a growing answer - but only while the reader
is at the bottom, so that scrolling up to re-read something is not undone by the next
chunk.

It is a part of ``ChatView`` and not a class of its own: it works on the state that
``ChatView.__init__`` creates - ``chat_list`` (whose ``on_scroll`` is ``_on_chat_scroll``
and whose ``scroll_interval`` is ``SCROLL_EVENT_INTERVAL``), ``page_ref``,
``_follow_bottom`` and ``_scroll_pending``.
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
        self.scroll_to_bottom()

    def scroll_to_bottom(self):
        """Jump to the newest message and follow what is appended after it."""
        self._follow_bottom = True
        self._show_to_bottom_button(False)
        if self.page_ref is not None and is_mounted(self.chat_list):
            self.page_ref.run_task(self._scroll_to_end, 100)
        # New rows are laid out on the client's next frame, after this scroll.
        self._follow_stream()

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
        """
        scrolled_up = (
            e.event_type == ft.ScrollType.USER
            and e.direction == ft.ScrollDirection.FORWARD
        ) or (e.event_type == ft.ScrollType.UPDATE and (e.scroll_delta or 0) < 0)
        if scrolled_up:
            self._follow_bottom = False
        elif (e.max_scroll_extent or 0) - (e.pixels or 0) <= BOTTOM_FOLLOW_THRESHOLD:
            self._follow_bottom = True
        self._show_to_bottom_button(not self._follow_bottom)
