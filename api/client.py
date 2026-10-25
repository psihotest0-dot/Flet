from __future__ import annotations

"""API client module for handling communication with the backend service."""

import json
import logging
from datetime import datetime
from typing import Any

import httpx
from httpx_sse import aconnect_sse
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class Message(BaseModel):
    """Represents a chat message."""
    id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    role: str | None = None
    content: str | None = None
    created_at: datetime | None = None


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
    local_path: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class APIClient:
    """Client for making asynchronous requests to the AI agent backend API."""
    def __init__(self, base_url: str, user_id: str):
        self.base_url = base_url
        self.user_id = user_id
        self.client = httpx.AsyncClient(base_url=self.base_url)

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
            logger.error(f"Health check failed: {e}")
            return False

    async def upload_file(self, file_content: bytes, filename: str, mime_type: str = "application/octet-stream") -> IngestedFile:
        """Upload a file to the backend."""
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
                return IngestedFile(**e.response.json())
            raise
        return IngestedFile(**response.json())

    async def delete_file(self, file_id: str) -> None:
        """Delete a file by its ID."""
        headers = {"X-User-ID": self.user_id}
        response = await self.client.delete(f"/v1/files/{file_id}", headers=headers)
        response.raise_for_status()

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
        response.raise_for_status()
        return IngestedFile(**response.json())

    async def create_memory(self, content: str, status: str = "candidate", source_message_id: str | None = None, importance: int | None = None, metadata: dict[str, Any] | None = None) -> Memory:
        """Create a new memory."""
        headers = {"X-User-ID": self.user_id}
        payload = {"content": content, "status": status}
        if source_message_id:
            payload["source_message_id"] = source_message_id
        if importance is not None:
            payload["importance"] = importance
        if metadata is not None:
            payload["metadata"] = metadata
        response = await self.client.post("/memories", headers=headers, json=payload)
        response.raise_for_status()
        return Memory(**response.json())

    async def get_messages(self, session_id: str) -> list[Message]:
        """Get chat messages for a session."""
        headers = {"X-User-ID": self.user_id}
        params = {"session_id": session_id}
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
        response.raise_for_status()
        return Memory(**response.json())

    async def delete_memory(self, memory_id: str) -> None:
        """Delete a memory by its ID."""
        headers = {"X-User-ID": self.user_id}
        response = await self.client.delete(f"/memories/{memory_id}", headers=headers)
        response.raise_for_status()

    async def stream_chat(self, session_id: str, content: str):
        """Stream a chat response from the backend using Server-Sent Events (SSE)."""
        headers = {"X-User-ID": self.user_id}
        payload = {"session_id": session_id, "content": content}

        async with aconnect_sse(self.client, "POST", "/chat/stream", headers=headers, json=payload, timeout=httpx.Timeout(5.0, read=None)) as event_source:
            try:
                event_source.response.raise_for_status()
            except httpx.HTTPStatusError as e:
                await e.response.aread()
                logger.error("Error streaming chat. Status: %s, URL: %s, Response: %s", e.response.status_code, e.request.url, e.response.text)
                raise

            async for sse in event_source.aiter_sse():
                if sse.data == "[DONE]":
                    break

                if sse.event == "error":
                    yield {"error": sse.data}
                elif sse.event == "sources":
                    try:
                        yield {"type": "sources", "data": json.loads(sse.data)}
                    except json.JSONDecodeError as e:
                        logger.error(f"JSONDecodeError parsing sources data: {e}, data: {sse.data}")
                        yield {"type": "sources", "data": sse.data}
                else:
                    try:
                        yield json.loads(sse.data)
                    except json.JSONDecodeError as e:
                        logger.error(f"JSONDecodeError parsing stream data: {e}, data: {sse.data}")
                        yield sse.data
