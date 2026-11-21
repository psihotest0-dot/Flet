"""One turn of the conversation: sending a message, asking again, stopping (P33).

``ChatTurnsMixin`` is the part of ``ChatView`` that talks to the backend about the
answer: it sends the message, reads the stream of events (``api.stream``) into the
message that is being written, asks for the last answer again, and stops it ("Спри").
An answer is read by a task of its own, so that "Спри" cancels exactly that.

It is a part of ``ChatView`` and not a class of its own: it works on the state that
``ChatView.__init__`` creates - ``api_client``, ``session_id``, ``page_ref``,
``chat_list``, ``input_field`` and ``_set_input``, ``add_message``, ``scroll_to_bottom``
and ``_follow_stream``, ``_set_streaming_ui``, ``_chat_options`` and the flags
``_is_streaming``, ``_stop_generation``, ``_stream_task``, ``_last_assistant_msg`` and
``_last_assistant_row``.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from api.errors import APIError, StreamError
from api.limits import MESSAGE_TOKEN_LIMIT
from api.stream import ModelUsed, Proposals, Sources, TextChunk, Working
from views.attachments import estimate_message_tokens
from views.timeline import now
from views.ui_utils import describe_error, safe_update, show_snackbar

logger = logging.getLogger(__name__)

# Upper bound of control updates per second while a response is streaming.
# Each flush is a diff of the message plus a patch sent to the Flutter client.
STREAM_FLUSH_INTERVAL = 0.12
# How long nothing may change on screen before the wheel is shown (again).
ACTIVITY_INDICATOR_DELAY = 1.0
# What the wheel says. While frames keep coming (the model's hidden reasoning) the
# backend is plainly alive: "thinking". While nothing comes at all - the model is
# waiting for a tool, or for its first word, or is stuck - it says "working" and,
# from ACTIVITY_SECONDS_FROM on, for how long nothing has come.
ACTIVITY_THINKING_TEXT = "Лили мисли…"
ACTIVITY_WORKING_TEXT = "Лили работи…"
ACTIVITY_SECONDS_FROM = 5


def activity_label(silent_for: float) -> str:
    """The text beside the wheel, ``silent_for`` seconds after the last frame."""
    if silent_for < ACTIVITY_INDICATOR_DELAY:
        return ACTIVITY_THINKING_TEXT
    seconds = int(silent_for)
    if seconds < ACTIVITY_SECONDS_FROM:
        return ACTIVITY_WORKING_TEXT
    return f"{ACTIVITY_WORKING_TEXT} {seconds} s"


class ChatTurnsMixin:
    """Sending, regenerating and stopping an answer of ``ChatView``."""

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
        # The last frame of any kind, and the last one that changed what is on
        # screen (a frame with nothing to show is a frame, but not a change).
        last_frame_time = last_visible_time = time.monotonic()

        async def show_activity_after_delay():
            try:
                while stream_open:
                    await asyncio.sleep(0.25)
                    now_ = time.monotonic()
                    if stream_open and now_ - last_visible_time >= (
                        ACTIVITY_INDICATOR_DELAY
                    ):
                        chat_message.show_activity_indicator(
                            activity_label(now_ - last_frame_time)
                        )
            except asyncio.CancelledError:
                pass

        activity_task = None
        if self.page_ref is not None:
            activity_task = self.page_ref.run_task(show_activity_after_delay)

        pending_text = ""

        def flush_text():
            nonlocal pending_text
            if pending_text:
                chat_message.append_text(pending_text)
                pending_text = ""
                self._follow_stream()

        async def read_stream():
            nonlocal pending_text, last_frame_time, last_visible_time
            last_flush_time = time.monotonic()
            try:
                async for event in stream_coro:
                    last_frame_time = time.monotonic()
                    if not isinstance(event, Working):
                        last_visible_time = last_frame_time
                        chat_message.hide_activity_indicator()

                    if self._stop_generation:
                        break

                    match event:
                        case Sources(items):
                            chat_message.add_sources(items)
                            continue
                        case Proposals(items):
                            chat_message.add_proposals(items)
                            continue
                        case ModelUsed(who):
                            chat_message.set_model(who)
                            continue
                        case TextChunk(text):
                            pending_text += text
                        # Working: nothing to show, so the wheel stays; it only
                        # changes what it says (see activity_label).

                    current_time = time.monotonic()
                    if current_time - last_flush_time >= STREAM_FLUSH_INTERVAL:
                        flush_text()
                        last_flush_time = current_time

                flush_text()
            finally:
                # A stream left half read (the reader stopped) closes its connection
                # now, not whenever the garbage collector gets to it: that is what
                # tells the backend to stop writing the answer.
                close = getattr(stream_coro, "aclose", None)
                if close is not None:
                    await close()

        # The answer is read by a task of its own, so that "Спри" cancels exactly
        # that and nothing else; cancelling the task of this handler could not be
        # told from the window going away.
        self._stream_task = asyncio.ensure_future(read_stream())
        try:
            await self._stream_task
        except asyncio.CancelledError:
            flush_text()
            if asyncio.current_task().cancelling():
                # Not "Спри": this very handler is being cancelled.
                raise
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
            self._stream_task = None
            stream_open = False
            chat_message.hide_activity_indicator()
            if activity_task:
                activity_task.cancel()
            # Exactly one full-view update for the finished message.
            safe_update(self)
            if self.page_ref is not None:
                self.page_ref.run_task(self._scroll_after_layout)
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
        safe_update(self, self.input_field)
        self._focus_input()
        self._set_streaming_ui(True)

        if self._last_assistant_msg:
            self._last_assistant_msg.set_regenerate_visible(False)

        self.add_message("user", text, notify=False, created_at=now())
        chat_message = self.add_message("assistant", "", notify=False, created_at=now())
        safe_update(self)
        self.scroll_to_bottom()

        stopped = False
        try:
            error = await self._stream_response(
                chat_message,
                self.api_client.stream_chat(
                    session_id=session_id, content=text, **self._chat_options()
                ),
                text,
            )
            stopped = error is None and self._stop_generation
        finally:
            self._is_streaming = False
            self._stop_generation = False
            self._set_streaming_ui(False)
            if session_id == self.session_id:
                self._last_assistant_msg = chat_message
                self._last_assistant_row = chat_message.row
                chat_message.set_regenerate_visible(True)
                if stopped:
                    # The backend never stores a half-written answer.
                    chat_message.show_stopped_note()

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
            chat_message = self.add_message(
                "assistant", "", notify=False, created_at=now()
            )
            safe_update(self)
            self.scroll_to_bottom()
            error = await self._stream_response(
                chat_message,
                self.api_client.regenerate_chat(
                    session_id=session_id, **self._chat_options()
                ),
            )
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
                        show_snackbar(self.page_ref, error)
                    elif stopped:
                        show_snackbar(
                            self.page_ref,
                            "Регенерацията е спряна — предишният отговор е запазен.",
                            success=True,
                        )
                else:
                    self._last_assistant_msg = chat_message
                    self._last_assistant_row = chat_message.row
                    chat_message.set_regenerate_visible(True)

    def _restore_previous_answer(
        self, failed_message, previous_msg, previous_row, position
    ):
        """Put the replaced answer back after a failed regeneration.

        The backend swaps the stored answer only once a complete new one exists,
        so after a failure the previous reply is still the one in the history.
        """
        if failed_message is not None and failed_message.row in self.chat_list.controls:
            self.chat_list.controls.remove(failed_message.row)
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
        self._cancel_stream()
        self.stop_btn.disabled = True
        safe_update(self, self.stop_btn)
