import pytest
import respx
import httpx
from api.client import APIClient, IngestedFile


@pytest.fixture
def client():
    return APIClient(base_url="http://localhost:8080", user_id="123e4567-e89b-12d3-a456-426614174000")


@respx.mock
@pytest.mark.asyncio
async def test_upload_file_success(client):
    mock_response = {
        "id": "123e4567-e89b-12d3-a456-426614174001",
        "user_id": "123e4567-e89b-12d3-a456-426614174000",
        "file_name": "test.txt",
        "file_hash": "dummyhash",
        "mime_type": "text/plain",
        "file_size": 1024,
        "status": "uploaded",
        "local_path": "/tmp/test.txt",
        "created_at": "2023-01-01T00:00:00Z",
        "updated_at": "2023-01-01T00:00:00Z"
    }
    respx.post("http://localhost:8080/v1/files").mock(return_value=httpx.Response(201, json=mock_response))

    result = await client.upload_file(b"test content", "test.txt", "text/plain")

    assert isinstance(result, IngestedFile)
    assert result.id == "123e4567-e89b-12d3-a456-426614174001"
    assert result.file_name == "test.txt"


@respx.mock
@pytest.mark.asyncio
async def test_upload_file_conflict(client):
    respx.post("http://localhost:8080/v1/files").mock(return_value=httpx.Response(409, json={"error": "Conflict"}))

    with pytest.raises(httpx.HTTPStatusError):
        await client.upload_file(b"test content", "test.txt", "text/plain")


@respx.mock
@pytest.mark.asyncio
async def test_upload_file_server_error(client):
    respx.post("http://localhost:8080/v1/files").mock(return_value=httpx.Response(500, json={"error": "Internal Server Error"}))

    with pytest.raises(httpx.HTTPStatusError):
        await client.upload_file(b"test content", "test.txt", "text/plain")


@respx.mock
@pytest.mark.asyncio
async def test_delete_file_success(client):
    respx.delete("http://localhost:8080/v1/files/123e4567-e89b-12d3-a456-426614174001").mock(return_value=httpx.Response(204))

    await client.delete_file("123e4567-e89b-12d3-a456-426614174001")


@respx.mock
@pytest.mark.asyncio
async def test_delete_file_not_found(client):
    respx.delete("http://localhost:8080/v1/files/123e4567-e89b-12d3-a456-426614174001").mock(return_value=httpx.Response(404))

    with pytest.raises(httpx.HTTPStatusError):
        await client.delete_file("123e4567-e89b-12d3-a456-426614174001")


@respx.mock
@pytest.mark.asyncio
async def test_list_files_success(client):
    mock_response = [
        {
            "id": "123e4567-e89b-12d3-a456-426614174001",
            "user_id": "123e4567-e89b-12d3-a456-426614174000",
            "file_name": "test1.txt",
            "status": "uploaded"
        },
        {
            "id": "123e4567-e89b-12d3-a456-426614174002",
            "user_id": "123e4567-e89b-12d3-a456-426614174000",
            "file_name": "test2.txt",
            "status": "processing"
        }
    ]
    respx.get("http://localhost:8080/v1/files").mock(return_value=httpx.Response(200, json=mock_response))

    result = await client.list_files()

    assert len(result) == 2
    assert isinstance(result[0], IngestedFile)
    assert result[0].id == "123e4567-e89b-12d3-a456-426614174001"
    assert result[0].file_name == "test1.txt"
    assert result[1].id == "123e4567-e89b-12d3-a456-426614174002"
    assert result[1].file_name == "test2.txt"


@respx.mock
@pytest.mark.asyncio
async def test_get_file_status_success(client):
    mock_response = {
        "id": "123e4567-e89b-12d3-a456-426614174001",
        "user_id": "123e4567-e89b-12d3-a456-426614174000",
        "file_name": "test.txt",
        "status": "processing"
    }
    respx.get("http://localhost:8080/v1/files/123e4567-e89b-12d3-a456-426614174001").mock(return_value=httpx.Response(200, json=mock_response))

    result = await client.get_file_status("123e4567-e89b-12d3-a456-426614174001")

    assert isinstance(result, IngestedFile)
    assert result.id == "123e4567-e89b-12d3-a456-426614174001"
    assert result.status == "processing"
