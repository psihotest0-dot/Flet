from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class Memory(BaseModel):
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
    id: str | None = None
    user_id: str | None = None
    file_name: str | None = None
    file_hash: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    status: str | None = None
    local_path: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class APIClient:
    def __init__(self, base_url: str, user_id: str):
        self.base_url = base_url
        self.user_id = user_id

    async def check_health(self) -> bool:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=5.0) as client:
                response = await client.get("/healthz")
                return response.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def upload_file(self, file_content: bytes, filename: str, mime_type: str = "application/octet-stream") -> IngestedFile:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            headers = {"X-User-ID": self.user_id}
            files = {"file": (filename, file_content, mime_type)}
            response = await client.post("/v1/files", headers=headers, files=files)
            response.raise_for_status()
            return IngestedFile(**response.json())

    async def delete_file(self, file_id: str) -> None:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            headers = {"X-User-ID": self.user_id}
            response = await client.delete(f"/v1/files/{file_id}", headers=headers)
            response.raise_for_status()

    async def list_files(self) -> list[IngestedFile]:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            headers = {"X-User-ID": self.user_id}
            response = await client.get("/v1/files", headers=headers)
            response.raise_for_status()
            return [IngestedFile(**item) for item in response.json()]

    async def get_file_status(self, file_id: str) -> IngestedFile:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            headers = {"X-User-ID": self.user_id}
            response = await client.get(f"/v1/files/{file_id}", headers=headers)
            response.raise_for_status()
            return IngestedFile(**response.json())

    async def create_memory(self, content: str, status: str = "candidate", source_message_id: str | None = None, importance: int | None = None, metadata: dict[str, Any] | None = None) -> Memory:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            headers = {"X-User-ID": self.user_id}
            payload = {"content": content, "status": status}
            if source_message_id is not None:
                payload["source_message_id"] = source_message_id
            if importance is not None:
                payload["importance"] = importance
            if metadata is not None:
                payload["metadata"] = metadata
            response = await client.post("/memories", headers=headers, json=payload)
            response.raise_for_status()
            return Memory(**response.json())

    async def list_memories(self, status: str | None = None) -> list[Memory]:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            headers = {"X-User-ID": self.user_id}
            params = {}
            if status is not None:
                params["status"] = status
            response = await client.get("/memories", headers=headers, params=params or None)
            response.raise_for_status()
            return [Memory(**item) for item in response.json()]

    async def update_memory(self, memory_id: str, content: str, status: str, importance: int, metadata: dict[str, Any] | None = None) -> Memory:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            headers = {"X-User-ID": self.user_id}
            payload = {"content": content, "status": status, "importance": importance}
            if metadata is not None:
                payload["metadata"] = metadata
            response = await client.put(f"/memories/{memory_id}", headers=headers, json=payload)
            response.raise_for_status()
            return Memory(**response.json())

    async def delete_memory(self, memory_id: str) -> None:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            headers = {"X-User-ID": self.user_id}
            response = await client.delete(f"/memories/{memory_id}", headers=headers)
            response.raise_for_status()

    async def stream_chat(self, session_id: str, content: str):
        async with httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(5.0, read=None)) as client:
            headers = {"X-User-ID": self.user_id}
            payload = {"session_id": session_id, "content": content}
            async with client.stream("POST", "/chat/stream", headers=headers, json=payload) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as e:
                    await e.response.aread()
                    logger.error("Error streaming chat. Status: %s, URL: %s, Response: %s", e.response.status_code, e.request.url, e.response.text)
                    raise
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        print(f"ПОЛУЧЕН ЧАНК: {data}")
                        try:
                            yield json.loads(data)
                        except json.JSONDecodeError:
                            yield data
