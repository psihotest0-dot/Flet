import httpx
import pytest
import respx

from api.client import APIClient, IngestedFile, Memory


@pytest.fixture
def client():
    return APIClient(base_url="http://localhost:8080", user_id="123e4567-e89b-12d3-a456-426614174000")


@respx.mock
@pytest.mark.asyncio
async def test_check_health_success(client):
    respx.get("http://localhost:8080/healthz").mock(return_value=httpx.Response(200, text="OK"))
    result = await client.check_health()
    assert result is True


@respx.mock
@pytest.mark.asyncio
async def test_check_health_failure_status(client):
    respx.get("http://localhost:8080/healthz").mock(return_value=httpx.Response(500))
    result = await client.check_health()
    assert result is False


@respx.mock
@pytest.mark.asyncio
async def test_check_health_exception(client):
    respx.get("http://localhost:8080/healthz").mock(side_effect=httpx.ConnectError("Connection refused"))
    result = await client.check_health()
    assert result is False


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


@respx.mock
@pytest.mark.asyncio
async def test_create_memory_success(client):
    mock_response = {
        "id": "123e4567-e89b-12d3-a456-426614174001",
        "user_id": "123e4567-e89b-12d3-a456-426614174000",
        "content": "This is a memory",
        "status": "candidate",
        "importance": 1,
        "created_at": "2023-01-01T00:00:00Z",
        "updated_at": "2023-01-01T00:00:00Z"
    }
    respx.post("http://localhost:8080/memories").mock(return_value=httpx.Response(201, json=mock_response))

    result = await client.create_memory("This is a memory")

    assert isinstance(result, Memory)
    assert result.id == "123e4567-e89b-12d3-a456-426614174001"
    assert result.content == "This is a memory"

@respx.mock
@pytest.mark.asyncio
async def test_create_memory_conflict(client):
    respx.post("http://localhost:8080/memories").mock(return_value=httpx.Response(409, json={"error": "Conflict"}))

    with pytest.raises(httpx.HTTPStatusError):
        await client.create_memory("This is a memory")

@respx.mock
@pytest.mark.asyncio
async def test_create_memory_server_error(client):
    respx.post("http://localhost:8080/memories").mock(return_value=httpx.Response(500, json={"error": "Internal Server Error"}))

    with pytest.raises(httpx.HTTPStatusError):
        await client.create_memory("This is a memory")

@respx.mock
@pytest.mark.asyncio
async def test_list_memories_success(client):
    mock_response = [
        {
            "id": "123e4567-e89b-12d3-a456-426614174001",
            "user_id": "123e4567-e89b-12d3-a456-426614174000",
            "content": "Memory 1",
            "status": "candidate"
        },
        {
            "id": "123e4567-e89b-12d3-a456-426614174002",
            "user_id": "123e4567-e89b-12d3-a456-426614174000",
            "content": "Memory 2",
            "status": "active"
        }
    ]
    respx.get("http://localhost:8080/memories").mock(return_value=httpx.Response(200, json=mock_response))

    result = await client.list_memories()

    assert len(result) == 2
    assert isinstance(result[0], Memory)
    assert result[0].content == "Memory 1"
    assert result[1].content == "Memory 2"

@respx.mock
@pytest.mark.asyncio
async def test_list_memories_filtered(client):
    mock_response = [
        {
            "id": "123e4567-e89b-12d3-a456-426614174001",
            "user_id": "123e4567-e89b-12d3-a456-426614174000",
            "content": "Memory 1",
            "status": "candidate"
        },
        {
            "id": "123e4567-e89b-12d3-a456-426614174002",
            "user_id": "123e4567-e89b-12d3-a456-426614174000",
            "content": "Memory 2",
            "status": "active"
        }
    ]
    respx.get("http://localhost:8080/memories?status=active").mock(return_value=httpx.Response(200, json=[mock_response[1]]))

    result = await client.list_memories(status="active")

    assert len(result) == 1
    assert isinstance(result[0], Memory)
    assert result[0].content == "Memory 2"

@respx.mock
@pytest.mark.asyncio
async def test_update_memory_success(client):
    mock_response = {
        "id": "123e4567-e89b-12d3-a456-426614174001",
        "user_id": "123e4567-e89b-12d3-a456-426614174000",
        "content": "Updated memory",
        "status": "active",
        "importance": 2
    }
    respx.put("http://localhost:8080/memories/123e4567-e89b-12d3-a456-426614174001").mock(return_value=httpx.Response(200, json=mock_response))

    result = await client.update_memory("123e4567-e89b-12d3-a456-426614174001", content="Updated memory", status="active", importance=2)

    assert isinstance(result, Memory)
    assert result.content == "Updated memory"
    assert result.status == "active"
    assert result.importance == 2

@respx.mock
@pytest.mark.asyncio
async def test_update_memory_not_found(client):
    respx.put("http://localhost:8080/memories/123e4567-e89b-12d3-a456-426614174001").mock(return_value=httpx.Response(404))

    with pytest.raises(httpx.HTTPStatusError):
        await client.update_memory("123e4567-e89b-12d3-a456-426614174001", content="Updated memory", status="active", importance=2)

@respx.mock
@pytest.mark.asyncio
async def test_delete_memory_success(client):
    respx.delete("http://localhost:8080/memories/123e4567-e89b-12d3-a456-426614174001").mock(return_value=httpx.Response(204))

    await client.delete_memory("123e4567-e89b-12d3-a456-426614174001")

@respx.mock
@pytest.mark.asyncio
async def test_delete_memory_not_found(client):
    respx.delete("http://localhost:8080/memories/123e4567-e89b-12d3-a456-426614174001").mock(return_value=httpx.Response(404))

    with pytest.raises(httpx.HTTPStatusError):
        await client.delete_memory("123e4567-e89b-12d3-a456-426614174001")

@respx.mock
@pytest.mark.asyncio
async def test_stream_chat_success(client):

    async def sse_stream():
        yield b'data: {"content": "Hello"}\n\n'
        yield b'data: {"source": "Doc 1"}\n\n'
        yield b'event: error\n'
        yield b'data: Something went wrong\n\n'
        yield b'data: [DONE]\n\n'

    respx.post("http://localhost:8080/chat/stream").mock(return_value=httpx.Response(200, content=sse_stream(), headers={"Content-Type": "text/event-stream"}))

    generator = client.stream_chat(session_id="session-123", content="Hi")
    results = [item async for item in generator]

    assert len(results) == 3
    assert results[0] == {"content": "Hello"}
    assert results[1] == {"source": "Doc 1"}
    assert results[2] == {"error": "Something went wrong"}
