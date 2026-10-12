from __future__ import annotations

from datetime import datetime

import httpx
from pydantic import BaseModel


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
