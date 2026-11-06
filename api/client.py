from __future__ import annotations

"""API client module for handling communication with the backend service."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from httpx_sse import aconnect_sse
from pydantic import BaseModel, field_validator
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# The backend accepts 1 (trivia) .. 10 (critical) and answers 400 to anything
# else - including a missing value, which it reads as 0.
MIN_IMPORTANCE = 1
MAX_IMPORTANCE = 10
DEFAULT_IMPORTANCE = 5

# A memory (or message) body may be at most 32000 *bytes*: the backend checks
# Go's len(string) although its error text says "characters". UTF-8 spends two
# bytes on a Cyrillic letter, so Bulgarian text gets about 16000 letters.
MAX_CONTENT_BYTES = 32000


def content_size(text: str) -> int:
    """Size of ``text`` as the backend measures it (UTF-8 bytes)."""
    return len(text.encode("utf-8"))

# File types the backend can turn into text (internal/ingest in C:\GO): text
# formats, documents and images (OCR). The upload endpoint accepts anything, but
# any other file ends as a failed ingest, so the picker offers only these.
SUPPORTED_EXTENSIONS = (
    "txt", "md", "csv", "json", "yaml", "yml", "log",
    "html", "htm", "pdf", "docx", "xlsx", "epub",
    "png", "jpg", "jpeg", "tif", "tiff", "bmp",
)


def is_supported_file(filename: str) -> bool:
    """Whether the backend can read a file with this name (by its extension)."""
    _, dot, ext = filename.rpartition(".")
    return bool(dot) and ext.lower() in SUPPORTED_EXTENSIONS


class Message(BaseModel):
    """Represents a chat message."""
    id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    role: str | None = None
    content: str | None = None
    created_at: datetime | None = None
    # Where an assistant answer came from (same shape as the SSE ``sources``
    # event); stored with the message so the references survive a reload.
    sources: list[dict[str, Any]] | None = None


class Memory(BaseModel):
    """Represents a piece of memory stored in the system."""
    id: str | None = None
    user_id: str | None = None
    content: str | None = None
    status: str | None = None
    embedding: list[float] | None = None
    embedding_model: str | None = None
    source_message_id: str | None = None
    importance: int | None = None
    metadata: dict[str, Any] | None = None
    content_hash: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_used_at: datetime | None = None


class Proposal(BaseModel):
    """A write the assistant asked for (a global rule or a memory) that waits for the user.

    ``external_sources`` names where untrusted text came from in the turn that
    made it (``web_search``, ``web_fetch``, ``search_documents``,
    ``retrieved_documents``); empty means the turn read only the conversation.
    """
    id: int
    kind: str | None = None
    status: str | None = None
    session_id: str | None = None
    summary: str = ""
    key: str | None = None
    value: str | None = None
    content: str | None = None
    importance: int | None = None
    external_sources: list[str] = []
    created_at: datetime | None = None


class Rule(BaseModel):
    """A global rule: the first thing in the system prompt of every session."""
    id: int
    key: str
    value: str
    enabled: bool = True
    updated_at: datetime | None = None


class OracleDecision(BaseModel):
    """One line of the Oracle journal: what the trading bot asked and what came back.

    ``status`` is ``answered`` (``verdict`` is ``APPROVE``, ``VETO`` or ``HOLD``)
    or ``failed`` (no verdict; ``error`` says why). In ``shadow`` mode the bot
    ignores the verdict, so the journal is the whole result.
    """
    id: int
    created_at: datetime | None = None
    request_id: str | None = None
    signal_id: str | None = None
    symbol: str | None = None
    mode: str | None = None
    status: str | None = None
    verdict: str | None = None
    reason: str | None = None
    model: str | None = None
    attempts: int | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    # Not in the contract yet (the backend is adding them): the kind of signal
    # ("oracle-probe" for the bot's test questions) and the version of the prompt.
    signal_version: str | None = None
    prompt_version: str | None = None


class OracleDecisionPage(BaseModel):
    """A page of the journal, newest first.

    ``next_before_id`` is the ``before_id`` of the next page; 0 when this was the
    last one. An empty journal may arrive as ``"decisions": null``.
    """
    decisions: list[OracleDecision] = []
    next_before_id: int = 0

    @field_validator("decisions", mode="before")
    @classmethod
    def _no_decisions_is_an_empty_list(cls, value):
        return [] if value is None else value

    @field_validator("next_before_id", mode="before")
    @classmethod
    def _no_next_page_is_zero(cls, value):
        return 0 if value is None else value


class StreamError(Exception):
    """Exception raised for errors in the stream."""


def _stream_error_text(data: str) -> str:
    """The text of an ``event: error`` frame.

    The backend sends ``{"error": "..."}`` (with a ``request_id`` to find the
    cause in its log); a plain-text frame is used as it is.
    """
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return data
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    return data


# Backend error bodies are short plain-text sentences; anything longer is a
# stack trace or an HTML page that does not belong in a snackbar.
ERROR_MESSAGE_LIMIT = 200


class APIError(Exception):
    """The backend answered with an error status.

    ``message`` is the plain-text body the backend sent (for example
    ``importance must be between 1 and 10``), so the UI can show it instead of
    the generic ``Client error '400 Bad Request' for url ...`` of httpx.
    """

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message

    @classmethod
    def from_response(cls, response: httpx.Response) -> APIError:
        text = " ".join(response.text.split())
        if len(text) > ERROR_MESSAGE_LIMIT:
            text = text[: ERROR_MESSAGE_LIMIT - 3].rstrip() + "..."
        if not text:
            text = f"HTTP {response.status_code} {response.reason_phrase}".strip()
        return cls(response.status_code, text)


def _log_http_error(response: httpx.Response, error: APIError) -> None:
    """Write a refused request to the log: one line with what was asked and what came back.

    The views only log the backend's text; without the method and path the log
    cannot say which call failed. Only the path is written - the query string
    carries the ``session_id``. A 5xx is the backend's fault (error), a 4xx is
    the answer to this very request (warning).
    """
    request = response.request
    level = logging.ERROR if error.status >= 500 else logging.WARNING
    logger.log(level, "%s %s -> HTTP %s: %s", request.method, request.url.path, error.status, error.message)


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


class IngestedFile(BaseModel):
    """Represents a file uploaded to the system."""
    id: str | None = None
    user_id: str | None = None
    file_name: str | None = None
    file_hash: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    status: str | None = None
    category: str | None = None
    manual_override: bool | None = None
    # Vector state reported by the backend: a file that finished ingestion with
    # zero chunks cannot be found by retrieval, which the API reports as
    # embedding_status "empty" instead of "completed".
    chunk_count: int | None = None
    has_embeddings: bool | None = None
    embedding_status: str | None = None
    local_path: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class UploadResult:
    """The outcome of an upload: the stored file, and whether it was already there."""
    file: IngestedFile
    duplicate: bool = False


class APIClient:
    """Client for making asynchronous requests to the AI agent backend API."""
    def __init__(self, base_url: str, user_id: str):
        self.base_url = base_url
        self.user_id = user_id
        self.client = httpx.AsyncClient(base_url=self.base_url)
        self._current_stream_task = None

    def cancel_stream(self):
        """Cancel the current streaming task."""
        if self._current_stream_task and not self._current_stream_task.done():
            self._current_stream_task.cancel()

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
            logger.debug(f"Health check failed: {e}")
            return False

    async def upload_file(self, file_content: bytes, filename: str, mime_type: str = "application/octet-stream") -> UploadResult:
        """Upload a file to the backend.

        A file the backend already holds (same content) is not an error: the
        result carries the existing file with ``duplicate=True``.
        """
        if len(file_content) > 100 * 1024 * 1024:
            raise ValueError("File too large. Maximum size is 100MB.")
        headers = {"X-User-ID": self.user_id}
        files = {"file": (filename, file_content, mime_type)}
        response = await self.client.post(
            "/v1/files",
            headers=headers,
            files=files,
            timeout=httpx.Timeout(5.0, read=None)
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                await e.response.aread()
                logger.info("File %s is already uploaded, the existing one is used.", filename)
                return UploadResult(IngestedFile(**e.response.json()), duplicate=True)
            error = APIError.from_response(response)
            _log_http_error(response, error)
            raise error from e
        uploaded = IngestedFile(**response.json())
        logger.info("File %s uploaded (%d bytes, id %s).", filename, len(file_content), uploaded.id)
        return UploadResult(uploaded)

    async def delete_file(self, file_id: str) -> None:
        """Delete a file by its ID."""
        headers = {"X-User-ID": self.user_id}
        response = await self.client.delete(f"/v1/files/{file_id}", headers=headers)
        _raise_for_status(response)
        logger.info("File %s deleted.", file_id)

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
        headers = {"X-User-ID": self.user_id}
        response = await self._get("/v1/files", headers=headers)
        return [IngestedFile(**item) for item in response.json()]

    async def get_file_status(self, file_id: str) -> IngestedFile:
        """Get the current status of a file."""
        headers = {"X-User-ID": self.user_id}
        response = await self._get(f"/v1/files/{file_id}", headers=headers)
        return IngestedFile(**response.json())

    async def update_file_category(self, file_id: str, category: str) -> IngestedFile:
        """Update the category of a file."""
        headers = {"X-User-ID": self.user_id}
        payload = {"category": category, "manual_override": True}
        response = await self.client.patch(f"/v1/files/{file_id}/category", headers=headers, json=payload)
        _raise_for_status(response)
        logger.info("File %s moved to category %s.", file_id, category)
        return IngestedFile(**response.json())

    async def create_memory(self, content: str, status: str = "candidate", source_message_id: str | None = None, importance: int | None = None, metadata: dict[str, Any] | None = None) -> Memory:
        """Create a new memory."""
        headers = {"X-User-ID": self.user_id}
        payload = {"content": content, "status": status}
        if source_message_id:
            payload["source_message_id"] = source_message_id
        payload["importance"] = importance if importance is not None else DEFAULT_IMPORTANCE
        if metadata is not None:
            payload["metadata"] = metadata
        response = await self.client.post("/memories", headers=headers, json=payload)
        _raise_for_status(response)
        memory = Memory(**response.json())
        # The text of a memory is personal - only its id, status and importance are logged.
        logger.info("Memory %s created (status %s, importance %s).", memory.id, status, payload["importance"])
        return memory

    async def get_messages(self, session_id: str, limit: int | None = None) -> list[Message]:
        """Get chat messages for a session (oldest first).

        Without ``limit`` the backend returns only its default page (50).
        """
        headers = {"X-User-ID": self.user_id}
        params: dict[str, Any] = {"session_id": session_id}
        if limit is not None:
            params["limit"] = limit
        response = await self._get("/messages", headers=headers, params=params)
        return [Message(**item) for item in response.json()]

    async def list_memories(self, status: str | None = None) -> list[Memory]:
        """List memories for the user, optionally filtered by status."""
        headers = {"X-User-ID": self.user_id}
        params = {}
        if status is not None:
            params["status"] = status
        response = await self._get("/memories", headers=headers, params=params or None)
        return [Memory(**item) for item in response.json()]

    async def update_memory(self, memory_id: str, content: str, status: str, importance: int, metadata: dict[str, Any] | None = None) -> Memory:
        """Update an existing memory."""
        headers = {"X-User-ID": self.user_id}
        payload = {"content": content, "status": status, "importance": importance}
        if metadata is not None:
            payload["metadata"] = metadata
        response = await self.client.put(f"/memories/{memory_id}", headers=headers, json=payload)
        _raise_for_status(response)
        logger.info("Memory %s updated (status %s, importance %s).", memory_id, status, importance)
        return Memory(**response.json())

    async def delete_memory(self, memory_id: str) -> None:
        """Delete a memory by its ID."""
        headers = {"X-User-ID": self.user_id}
        response = await self.client.delete(f"/memories/{memory_id}", headers=headers)
        _raise_for_status(response)
        logger.info("Memory %s deleted.", memory_id)

    async def list_proposals(self) -> list[Proposal]:
        """The writes the assistant proposed that still wait for an answer, oldest first."""
        headers = {"X-User-ID": self.user_id}
        response = await self.client.get("/proposals", headers=headers)
        _raise_for_status(response)
        return [Proposal(**item) for item in response.json()]

    async def approve_proposal(self, proposal_id: int) -> Proposal:
        """Confirm a proposal: the backend saves the rule or the memory."""
        return await self._decide_proposal(proposal_id, "approve")

    async def reject_proposal(self, proposal_id: int) -> Proposal:
        """Reject a proposal: nothing is saved."""
        return await self._decide_proposal(proposal_id, "reject")

    async def _decide_proposal(self, proposal_id: int, verb: str) -> Proposal:
        headers = {"X-User-ID": self.user_id}
        response = await self.client.post(f"/proposals/{proposal_id}/{verb}", headers=headers)
        _raise_for_status(response)
        logger.info("Proposal %s: %s.", proposal_id, verb)
        return Proposal(**response.json())

    async def list_rules(self) -> list[Rule]:
        """The global rules of the user, newest first."""
        headers = {"X-User-ID": self.user_id}
        response = await self.client.get("/rules", headers=headers)
        _raise_for_status(response)
        return [Rule(**item) for item in response.json()]

    async def delete_rule(self, rule_id: int) -> None:
        """Delete a global rule."""
        headers = {"X-User-ID": self.user_id}
        response = await self.client.delete(f"/rules/{rule_id}", headers=headers)
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
        headers = {"X-User-ID": self.user_id}
        params: dict[str, Any] = {"limit": limit}
        if before_id:
            params["before_id"] = before_id
        if symbol:
            params["symbol"] = symbol
        response = await self._get("/oracle/decisions", headers=headers, params=params)
        return OracleDecisionPage(**response.json())

    def regenerate_chat(self, session_id: str) -> AsyncIterator[Any]:
        """Stream a regenerated chat response from the backend using Server-Sent Events (SSE)."""
        return self._stream_sse("/chat/regenerate", {"session_id": session_id})

    def stream_chat(self, session_id: str, content: str) -> AsyncIterator[Any]:
        """Stream a chat response from the backend using Server-Sent Events (SSE)."""
        return self._stream_sse("/chat/stream", {"session_id": session_id, "content": content})

    async def _stream_sse(self, path: str, payload: dict[str, Any]) -> AsyncIterator[Any]:
        """POST ``payload`` to ``path`` and yield what the backend streams back.

        Both chat endpoints write the same events (``sources``, text chunks,
        ``proposals``, ``[DONE]``, ``error``), so one reader serves them. It yields
        ``{"type": "sources" | "proposals", "data": ...}`` for those two events and
        the decoded chunk for the rest. An ``error`` event raises
        :class:`StreamError`, a refused request :class:`APIError`.

        What the model wrote and what the documents say never reach the log: an
        undecodable frame is reported by its size only.
        """
        headers = {"X-User-ID": self.user_id}
        try:
            self._current_stream_task = asyncio.current_task()
            async with aconnect_sse(
                self.client, "POST", path, headers=headers, json=payload,
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

                    if sse.event == "error":
                        raise StreamError(_stream_error_text(sse.data))
                    elif sse.event == "sources":
                        try:
                            yield {"type": "sources", "data": json.loads(sse.data)}
                        except json.JSONDecodeError as e:
                            logger.error("Could not decode the sources frame (%d characters): %s", len(sse.data), e)
                            yield {"type": "sources", "data": sse.data}
                    elif sse.event == "proposals":
                        try:
                            proposals = json.loads(sse.data)
                        except json.JSONDecodeError as e:
                            logger.error("Could not decode the proposals frame (%d characters): %s", len(sse.data), e)
                            continue
                        if isinstance(proposals, list):
                            yield {"type": "proposals", "data": proposals}
                    else:
                        try:
                            chunk = json.loads(sse.data)
                        except json.JSONDecodeError as e:
                            logger.error("Could not decode a stream chunk (%d characters): %s", len(sse.data), e)
                            yield sse.data
                            continue
                        if isinstance(chunk, dict) and "error" in chunk:
                            raise StreamError(str(chunk["error"] or "Unknown error"))
                        yield chunk
        finally:
            self._current_stream_task = None
