from __future__ import annotations

"""API client module for handling communication with the backend service."""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from httpx_sse import aconnect_sse
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# The backend accepts 1 (trivia) .. 10 (critical) and answers 400 to anything
# else - including a missing value, which it reads as 0.
MIN_IMPORTANCE = 1
MAX_IMPORTANCE = 10
DEFAULT_IMPORTANCE = 5

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


class StreamError(Exception):
    """Exception raised for errors in the stream."""


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


def _raise_for_status(response: httpx.Response) -> None:
    """``raise_for_status`` that raises :class:`APIError` with the backend text."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise APIError.from_response(response) from e


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
                return UploadResult(IngestedFile(**e.response.json()), duplicate=True)
            raise APIError.from_response(response) from e
        return UploadResult(IngestedFile(**response.json()))

    async def delete_file(self, file_id: str) -> None:
        """Delete a file by its ID."""
        headers = {"X-User-ID": self.user_id}
        response = await self.client.delete(f"/v1/files/{file_id}", headers=headers)
        _raise_for_status(response)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=4))
    async def _get(self, endpoint: str, **kwargs) -> httpx.Response:
        """Helper GET method wrapped with retry logic."""
        response = await self.client.get(endpoint, **kwargs)
        response.raise_for_status()
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
        return Memory(**response.json())

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
        return Memory(**response.json())

    async def delete_memory(self, memory_id: str) -> None:
        """Delete a memory by its ID."""
        headers = {"X-User-ID": self.user_id}
        response = await self.client.delete(f"/memories/{memory_id}", headers=headers)
        _raise_for_status(response)


    async def regenerate_chat(self, session_id: str):
        """Stream a regenerated chat response from the backend using Server-Sent Events (SSE)."""
        headers = {"X-User-ID": self.user_id}
        payload = {"session_id": session_id}

        try:
            self._current_stream_task = asyncio.current_task()
            async with aconnect_sse(self.client, "POST", "/chat/regenerate", headers=headers, json=payload, timeout=httpx.Timeout(5.0, read=None)) as event_source:
                try:
                    event_source.response.raise_for_status()
                except httpx.HTTPStatusError as e:
                    await e.response.aread()
                    logger.error("Error streaming regenerated chat. Status: %s, URL: %s, Response: %s", e.response.status_code, e.request.url, e.response.text)
                    raise APIError.from_response(e.response) from e

                async for sse in event_source.aiter_sse():
                    if sse.data == "[DONE]":
                        break

                    if sse.event == "error":
                        raise StreamError(sse.data)
                    elif sse.event == "sources":
                        try:
                            yield {"type": "sources", "data": json.loads(sse.data)}
                        except json.JSONDecodeError as e:
                            logger.error(f"JSONDecodeError parsing sources data: {e}, data: {sse.data}")
                            yield {"type": "sources", "data": sse.data}
                    else:
                        try:
                            chunk = json.loads(sse.data)
                            if "error" in chunk:
                                raise StreamError(chunk.get("error", "Unknown error"))
                            yield chunk
                        except json.JSONDecodeError as e:
                            logger.error(f"JSONDecodeError parsing stream data: {e}, data: {sse.data}")
                            yield sse.data
        finally:
            self._current_stream_task = None

    async def stream_chat(self, session_id: str, content: str):
        """Stream a chat response from the backend using Server-Sent Events (SSE)."""
        headers = {"X-User-ID": self.user_id}
        payload = {"session_id": session_id, "content": content}

        try:
            self._current_stream_task = asyncio.current_task()
            async with aconnect_sse(self.client, "POST", "/chat/stream", headers=headers, json=payload, timeout=httpx.Timeout(5.0, read=None)) as event_source:
                try:
                    event_source.response.raise_for_status()
                except httpx.HTTPStatusError as e:
                    await e.response.aread()
                    logger.error("Error streaming chat. Status: %s, URL: %s, Response: %s", e.response.status_code, e.request.url, e.response.text)
                    raise APIError.from_response(e.response) from e

                async for sse in event_source.aiter_sse():
                    if sse.data == "[DONE]":
                        break

                    if sse.event == "error":
                        raise StreamError(sse.data)
                    elif sse.event == "sources":
                        try:
                            yield {"type": "sources", "data": json.loads(sse.data)}
                        except json.JSONDecodeError as e:
                            logger.error(f"JSONDecodeError parsing sources data: {e}, data: {sse.data}")
                            yield {"type": "sources", "data": sse.data}
                    else:
                        try:
                            chunk = json.loads(sse.data)
                            if "error" in chunk:
                                raise StreamError(chunk.get("error", "Unknown error"))
                            yield chunk
                        except json.JSONDecodeError as e:
                            logger.error(f"JSONDecodeError parsing stream data: {e}, data: {sse.data}")
                            yield sse.data
        finally:
            self._current_stream_task = None
