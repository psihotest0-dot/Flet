"""API client module for handling communication with the backend service."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from httpx_sse import aconnect_sse
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from api.errors import APIError
from api.limits import DEFAULT_IMPORTANCE, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB
from api.models import (
    MEMORY_CANDIDATE,
    ChatAttachment,
    IngestedFile,
    Memory,
    Message,
    OracleDecisionPage,
    Proposal,
    Rule,
    TelegramSession,
    TelegramSessionList,
    UploadResult,
    UserFile,
    UserFileList,
)
from api.stream import StreamEvent, parse_frame

logger = logging.getLogger(__name__)


# The paths whose last part is the user's own text: the requests are built from them
# and the log (``_loggable_path``) recognises them by the same prefix, so a change
# here cannot leave one without the other.
FILES_PATH = "/workspace/files/"
SESSIONS_PATH = "/chat/sessions/"


def _loggable_path(path: str) -> str:
    """The path of a request as the log may hold it.

    The last part of most paths is an id of a file, a memory or a rule, which the
    log keeps. Two are not: ``/workspace/files/<name>`` ends in the name of a file
    of the user (their own text) and ``/chat/sessions/<id>`` in the whole key to a
    conversation, of which the log keeps the first 8 characters, like everywhere.
    """
    if path.startswith(FILES_PATH):
        return FILES_PATH + "{name}"
    if path.startswith(SESSIONS_PATH):
        return SESSIONS_PATH + path[len(SESSIONS_PATH) :][:8] + "..."
    return path


def _log_http_error(response: httpx.Response, error: APIError) -> None:
    """Write a refused request to the log: one line with what was asked and what came back.

    The views only log the backend's text; without the method and path the log
    cannot say which call failed. Only the path is written - the query string
    carries the ``session_id`` - and ``_loggable_path`` keeps a file name and a
    whole session id out of it. A 5xx is the backend's fault (error), a 4xx is
    the answer to this very request (warning).
    """
    request = response.request
    level = logging.ERROR if error.status >= 500 else logging.WARNING
    logger.log(
        level,
        "%s %s -> HTTP %s: %s",
        request.method,
        _loggable_path(request.url.path),
        error.status,
        error.message,
    )


def _raise_for_status(response: httpx.Response) -> None:
    """``raise_for_status`` that raises :class:`APIError` with the backend text."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        error = APIError.from_response(response)
        _log_http_error(response, error)
        raise error from e


def _is_transient(error: BaseException) -> bool:
    """Whether repeating the request may help.

    A refused or dropped connection and a 5xx can pass by themselves; a 4xx is
    the backend's final answer to this very request and would only repeat.
    """
    if isinstance(error, APIError):
        return error.status >= 500
    return isinstance(error, httpx.TransportError)


class APIClient:
    """Client for making asynchronous requests to the AI agent backend API."""

    def __init__(self, base_url: str, user_id: str):
        self.base_url = base_url
        self.user_id = user_id
        self.client = httpx.AsyncClient(
            base_url=self.base_url, headers={"X-User-ID": user_id}
        )

    def set_base_url(self, base_url: str) -> None:
        """Send the next requests to ``base_url``.

        The client stays the same object: the views, the chat messages and the
        proposal cards all hold it, and an answer being streamed keeps reading
        through it. A request already on its way finishes at the old address.
        """
        self.base_url = base_url
        self.client.base_url = base_url

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self.client.aclose()

    async def check_health(self) -> bool:
        """Check if the backend API is healthy and reachable."""
        try:
            response = await self.client.get("/healthz", timeout=5.0)
            return response.status_code == 200
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            # Polled every few seconds - keep it out of the error log.
            logger.debug("Health check failed: %s", e)
            return False

    async def upload_file(
        self,
        path: Path,
        filename: str,
        mime_type: str = "application/octet-stream",
    ) -> UploadResult:
        """Upload the file at ``path`` to the backend, named ``filename`` there.

        The file goes from the disk to the connection a piece at a time (httpx reads
        it in chunks of 64 KiB); it is never held in memory, which a 65 MB book
        taken three at a time used to do. ``ValueError`` for a file over
        ``MAX_UPLOAD_BYTES``.

        A file the backend already holds (same content) is not an error: the
        result carries the existing file with ``duplicate=True``.
        """
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        if size > MAX_UPLOAD_BYTES:
            raise ValueError(f"File too large. Maximum size is {MAX_UPLOAD_MB}MB.")
        source = await asyncio.to_thread(path.open, "rb")
        try:
            response = await self.client.post(
                "/v1/files",
                files={"file": (filename, source, mime_type)},
                timeout=httpx.Timeout(5.0, read=None),
            )
        finally:
            source.close()
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                await e.response.aread()
                existing = IngestedFile(**e.response.json())
                # The name of a file is the user's own text: the log keeps the id.
                logger.info(
                    "A file with the same content is already uploaded (id %s), "
                    "the existing one is used.",
                    existing.id,
                )
                return UploadResult(existing, duplicate=True)
            error = APIError.from_response(response)
            _log_http_error(response, error)
            raise error from e
        uploaded = IngestedFile(**response.json())
        logger.info("File uploaded (%d bytes, id %s).", size, uploaded.id)
        return UploadResult(uploaded)

    async def delete_file(self, file_id: str) -> None:
        """Delete a file by its ID."""
        response = await self.client.delete(f"/v1/files/{file_id}")
        _raise_for_status(response)
        logger.info("File %s deleted.", file_id)

    async def list_attachments(self, session_id: str) -> list[ChatAttachment]:
        """The files attached to a conversation, newest first (without their text)."""
        response = await self._get(
            "/chat/attachments", params={"session_id": session_id}
        )
        return [ChatAttachment(**item) for item in response.json()]

    async def upload_attachment(
        self, session_id: str, name: str, content: str
    ) -> ChatAttachment:
        """Attach a text file to a conversation.

        The same name again is a new version of the file (``replaced``), the same
        content again changes nothing (``unchanged``). The text is the user's own
        material - only the id and the sizes are logged.
        """
        payload = {"session_id": session_id, "name": name, "content": content}
        response = await self.client.post("/chat/attachments", json=payload)
        _raise_for_status(response)
        attachment = ChatAttachment(**response.json())
        logger.info(
            "Attachment %s stored (%d bytes, %d lines, version %d).",
            attachment.id,
            attachment.bytes,
            attachment.lines,
            attachment.version,
        )
        return attachment

    async def delete_attachment(self, attachment_id: str) -> None:
        """Remove a file attached to a conversation."""
        response = await self.client.delete(f"/chat/attachments/{attachment_id}")
        _raise_for_status(response)
        logger.info("Attachment %s deleted.", attachment_id)

    async def delete_session(self, session_id: str) -> None:
        """Delete a conversation on the backend for good.

        Its messages, rolling summary, traces and attached files go; the user's
        memories stay. A session that is not there is not an error (204), so
        asking twice is safe.
        """
        response = await self.client.delete(
            f"{SESSIONS_PATH}{quote(session_id, safe='')}"
        )
        _raise_for_status(response)
        # Only the first 8 characters: the full id is the key to the conversation.
        logger.info("Session %s deleted.", session_id[:8])

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception(_is_transient),
        # The caller gets the real error (APIError / httpx.TransportError), not
        # tenacity's RetryError wrapper.
        reraise=True,
        # Without this a repeated request leaves no trace in app.log at all.
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def _get(self, endpoint: str, **kwargs) -> httpx.Response:
        """GET that repeats a transient failure and raises :class:`APIError` otherwise."""
        response = await self.client.get(endpoint, **kwargs)
        _raise_for_status(response)
        return response

    async def list_files(self) -> list[IngestedFile]:
        """List all files uploaded by the user."""
        response = await self._get("/v1/files")
        return [IngestedFile(**item) for item in response.json()]

    async def get_file_status(self, file_id: str) -> IngestedFile:
        """Get the current status of a file."""
        response = await self._get(f"/v1/files/{file_id}")
        return IngestedFile(**response.json())

    async def update_file_category(self, file_id: str, category: str) -> IngestedFile:
        """Update the category of a file."""
        # Only the category: the backend itself marks the file as set by the user
        # (``manual_override`` is in the answer, not in the request).
        payload = {"category": category}
        response = await self.client.patch(
            f"/v1/files/{file_id}/category", json=payload
        )
        _raise_for_status(response)
        logger.info("File %s moved to category %s.", file_id, category)
        return IngestedFile(**response.json())

    async def create_memory(
        self,
        content: str,
        status: str = MEMORY_CANDIDATE,
        source_message_id: str | None = None,
        importance: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Memory:
        """Create a new memory."""
        payload = {"content": content, "status": status}
        if source_message_id:
            payload["source_message_id"] = source_message_id
        payload["importance"] = (
            importance if importance is not None else DEFAULT_IMPORTANCE
        )
        if metadata is not None:
            payload["metadata"] = metadata
        response = await self.client.post("/memories", json=payload)
        _raise_for_status(response)
        memory = Memory(**response.json())
        # The text of a memory is personal - only its id, status and importance are logged.
        logger.info(
            "Memory %s created (status %s, importance %s).",
            memory.id,
            status,
            payload["importance"],
        )
        return memory

    async def get_messages(
        self, session_id: str, limit: int | None = None, before: str | None = None
    ) -> list[Message]:
        """Get chat messages for a session (oldest first).

        Without ``limit`` the backend returns only its default page (50). ``before`` is
        the id of the oldest message the caller already has: the page is then the
        messages older than it (an empty list: there are none), so a long conversation
        can be read backwards. An id the backend does not know is a 404.
        """
        params: dict[str, Any] = {"session_id": session_id}
        if limit is not None:
            params["limit"] = limit
        if before:
            params["before"] = before
        response = await self._get("/messages", params=params)
        return [Message(**item) for item in response.json()]

    async def list_telegram_sessions(self) -> list[TelegramSession]:
        """The Telegram conversations of the user (``tg-<chat id>``), possibly none.

        A backend that does not have the endpoint answers 404, as for any unknown path:
        the caller treats that as "no Telegram conversation".
        """
        response = await self._get("/telegram/sessions")
        return TelegramSessionList(**response.json()).sessions

    async def list_memories(self, status: str | None = None) -> list[Memory]:
        """List memories for the user, optionally filtered by status."""
        params = {}
        if status is not None:
            params["status"] = status
        response = await self._get("/memories", params=params or None)
        return [Memory(**item) for item in response.json()]

    async def update_memory(
        self,
        memory_id: str,
        content: str,
        status: str,
        importance: int,
        metadata: dict[str, Any] | None = None,
    ) -> Memory:
        """Update an existing memory."""
        payload = {"content": content, "status": status, "importance": importance}
        if metadata is not None:
            payload["metadata"] = metadata
        response = await self.client.put(f"/memories/{memory_id}", json=payload)
        _raise_for_status(response)
        logger.info(
            "Memory %s updated (status %s, importance %s).",
            memory_id,
            status,
            importance,
        )
        return Memory(**response.json())

    async def delete_memory(self, memory_id: str) -> None:
        """Delete a memory by its ID."""
        response = await self.client.delete(f"/memories/{memory_id}")
        _raise_for_status(response)
        logger.info("Memory %s deleted.", memory_id)

    async def list_proposals(self) -> list[Proposal]:
        """The writes the assistant proposed that still wait for an answer, oldest first."""
        response = await self._get("/proposals")
        return [Proposal(**item) for item in response.json()]

    async def approve_proposal(self, proposal_id: int) -> Proposal:
        """Confirm a proposal: the backend saves the rule or the memory."""
        return await self._decide_proposal(proposal_id, "approve")

    async def reject_proposal(self, proposal_id: int) -> Proposal:
        """Reject a proposal: nothing is saved."""
        return await self._decide_proposal(proposal_id, "reject")

    async def _decide_proposal(self, proposal_id: int, verb: str) -> Proposal:
        response = await self.client.post(f"/proposals/{proposal_id}/{verb}")
        _raise_for_status(response)
        logger.info("Proposal %s: %s.", proposal_id, verb)
        return Proposal(**response.json())

    async def list_user_files(self) -> UserFileList:
        """The files the assistant keeps for the user.

        A backend without the file tools (``FILES_ENABLED`` off, or an older one) answers
        404, as for any unknown path: the caller treats that as "not turned on".
        """
        response = await self._get("/workspace/files")
        return UserFileList(**response.json())

    async def get_user_file(self, name: str, previous: bool = False) -> UserFile:
        """The whole text of a file, or of the version a replacement took the place of."""
        params = {"version": "previous"} if previous else None
        response = await self._get(f"{FILES_PATH}{quote(name, safe='')}", params=params)
        return UserFile(**response.json())

    async def delete_user_file(self, name: str) -> None:
        """Remove a file and its previous version. Only the user can; the assistant cannot."""
        response = await self.client.delete(f"{FILES_PATH}{quote(name, safe='')}")
        _raise_for_status(response)
        # The name is the user's own text; the log keeps the fact only.
        logger.info("A file of the assistant was deleted.")

    async def list_rules(self) -> list[Rule]:
        """The global rules of the user, newest first."""
        response = await self._get("/rules")
        return [Rule(**item) for item in response.json()]

    async def delete_rule(self, rule_id: int) -> None:
        """Delete a global rule."""
        response = await self.client.delete(f"/rules/{rule_id}")
        _raise_for_status(response)
        logger.info("Rule %s deleted.", rule_id)

    async def list_oracle_decisions(
        self,
        limit: int = 50,
        before_id: int | None = None,
        symbol: str | None = None,
    ) -> OracleDecisionPage:
        """A page of the Oracle journal, newest first.

        ``before_id`` is the ``next_before_id`` of the previous page (absent or 0
        starts at the newest); ``symbol`` limits the page to one stock. Reading
        the journal is not logged: the tab asks for it every few seconds.
        """
        params: dict[str, Any] = {"limit": limit}
        if before_id:
            params["before_id"] = before_id
        if symbol:
            params["symbol"] = symbol
        response = await self._get("/oracle/decisions", params=params)
        return OracleDecisionPage(**response.json())

    def regenerate_chat(
        self, session_id: str, use_paid: bool | None = None
    ) -> AsyncIterator[StreamEvent]:
        """Stream a regenerated chat response from the backend using Server-Sent Events (SSE).

        ``use_paid`` is the choice of the model, as for :meth:`stream_chat`.
        """
        payload: dict[str, Any] = {
            "session_id": session_id,
            "queue_events": True,
            "plan_events": True,
        }
        if use_paid is not None:
            payload["use_paid"] = use_paid
        return self._stream_sse("/chat/regenerate", payload)

    def stream_chat(
        self, session_id: str, content: str, use_paid: bool | None = None
    ) -> AsyncIterator[StreamEvent]:
        """Stream a chat response from the backend using Server-Sent Events (SSE).

        ``use_paid`` is the choice of the model for this turn: ``True`` - the paid
        one, ``False`` - the local one only, ``None`` (left out of the request) - what
        the backend is set to do.

        The request asks to be told when it has to wait for another reply in the same
        conversation (the one shared with Telegram): ``queue_events``; and which of the
        person's memories a planned answer used: ``plan_events``. A backend that does
        not know a field ignores it and never sends the frame.
        """
        payload: dict[str, Any] = {
            "session_id": session_id,
            "content": content,
            "queue_events": True,
            "plan_events": True,
        }
        if use_paid is not None:
            payload["use_paid"] = use_paid
        return self._stream_sse("/chat/stream", payload)

    async def _stream_sse(
        self, path: str, payload: dict[str, Any]
    ) -> AsyncIterator[StreamEvent]:
        """POST ``payload`` to ``path`` and yield the events the backend streams back.

        Both chat endpoints write the same frames (``sources``, text chunks,
        ``proposals``, ``model``, ``plan``, ``[DONE]``, ``error``), so one reader serves them;
        ``api.stream.parse_frame`` turns a frame into typed events. An ``error``
        frame raises :class:`StreamError`, a refused request :class:`APIError`.
        """
        async with aconnect_sse(
            self.client,
            "POST",
            path,
            json=payload,
            timeout=httpx.Timeout(5.0, read=None),
        ) as event_source:
            try:
                event_source.response.raise_for_status()
            except httpx.HTTPStatusError as e:
                await e.response.aread()
                error = APIError.from_response(e.response)
                _log_http_error(e.response, error)
                raise error from e

            async for sse in event_source.aiter_sse():
                if sse.data == "[DONE]":
                    break
                for event in parse_frame(sse.event, sse.data):
                    yield event
