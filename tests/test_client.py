import json
import logging

import httpx
import pytest
import respx
from tenacity import wait_none

from api.client import (
    MAX_CONTENT_BYTES,
    SUPPORTED_EXTENSIONS,
    APIClient,
    APIError,
    IngestedFile,
    Memory,
    StreamError,
    UploadResult,
    content_size,
    is_supported_file,
)


@pytest.fixture
def client():
    return APIClient(base_url="http://localhost:8080", user_id="123e4567-e89b-12d3-a456-426614174000")


@pytest.mark.asyncio
async def test_client_context_manager():
    async with APIClient(base_url="http://localhost:8080", user_id="123e4567-e89b-12d3-a456-426614174000") as client:
        assert client.client.is_closed is False
    assert client.client.is_closed is True


@respx.mock
@pytest.mark.asyncio
async def test_check_health_success(client):
    respx.get("http://localhost:8080/healthz").mock(return_value=httpx.Response(200))
    result = await client.check_health()
    assert result is True


@respx.mock
@pytest.mark.asyncio
async def test_check_health_failure(client):
    respx.get("http://localhost:8080/healthz").mock(return_value=httpx.Response(500))
    result = await client.check_health()
    assert result is False


@respx.mock
@pytest.mark.asyncio
async def test_check_health_timeout(client):
    respx.get("http://localhost:8080/healthz").mock(side_effect=httpx.TimeoutException("Timeout"))
    result = await client.check_health()
    assert result is False




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

    assert isinstance(result, UploadResult)
    assert isinstance(result.file, IngestedFile)
    assert result.file.id == "123e4567-e89b-12d3-a456-426614174001"
    assert result.file.file_name == "test.txt"
    assert result.duplicate is False


@respx.mock
@pytest.mark.asyncio
async def test_upload_file_conflict(client):
    respx.post("http://localhost:8080/v1/files").mock(return_value=httpx.Response(409, json={"id": "conflict_id", "file_name": "test.txt", "status": "completed"}))

    result = await client.upload_file(b"test content", "test.txt")
    assert result.duplicate is True
    assert result.file.id == "conflict_id"
    assert result.file.file_name == "test.txt"


@pytest.mark.asyncio
async def test_upload_file_too_large(client):
    large_content = b"a" * (100 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="Maximum size is 100MB"):
        await client.upload_file(large_content, "large.txt")


@respx.mock
@pytest.mark.asyncio
async def test_upload_file_server_error(client):
    respx.post("http://localhost:8080/v1/files").mock(return_value=httpx.Response(500, json={"error": "Internal Server Error"}))

    with pytest.raises(APIError) as exc_info:
        await client.upload_file(b"test content", "test.txt", "text/plain")
    assert exc_info.value.status == 500


@respx.mock
@pytest.mark.asyncio
async def test_delete_file_success(client):
    respx.delete("http://localhost:8080/v1/files/123e4567-e89b-12d3-a456-426614174001").mock(return_value=httpx.Response(204))

    await client.delete_file("123e4567-e89b-12d3-a456-426614174001")


@respx.mock
@pytest.mark.asyncio
async def test_delete_session_sends_the_user_and_the_session(client):
    route = respx.delete("http://localhost:8080/chat/sessions/11111111-1111-1111-1111-111111111111").mock(
        return_value=httpx.Response(204)
    )

    await client.delete_session("11111111-1111-1111-1111-111111111111")

    assert route.called
    assert route.calls.last.request.headers["X-User-ID"] == "123e4567-e89b-12d3-a456-426614174000"


@respx.mock
@pytest.mark.asyncio
async def test_delete_session_escapes_the_id_and_reports_a_refusal(client):
    # An id with characters that mean something in a path stays one path segment.
    route = respx.delete("http://localhost:8080/chat/sessions/a%2Fb%20c").mock(
        return_value=httpx.Response(400, text="invalid session id")
    )

    with pytest.raises(APIError) as exc_info:
        await client.delete_session("a/b c")

    assert route.called
    # respx compares decoded paths, so look at what really went on the wire.
    assert route.calls.last.request.url.raw_path == b"/chat/sessions/a%2Fb%20c"
    assert exc_info.value.status == 400
    assert exc_info.value.message == "invalid session id"


@respx.mock
@pytest.mark.asyncio
async def test_delete_file_not_found(client):
    respx.delete("http://localhost:8080/v1/files/123e4567-e89b-12d3-a456-426614174001").mock(return_value=httpx.Response(404))

    with pytest.raises(APIError) as exc_info:
        await client.delete_file("123e4567-e89b-12d3-a456-426614174001")
    assert exc_info.value.status == 404


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
async def test_list_files_parses_embedding_metadata(client):
    mock_response = [
        {
            "id": "123e4567-e89b-12d3-a456-426614174001",
            "user_id": "123e4567-e89b-12d3-a456-426614174000",
            "file_name": "indexed.pdf",
            "mime_type": "application/pdf",
            "file_size": 2048,
            "status": "completed",
            "category": "general",
            "manual_override": False,
            "chunk_count": 42,
            "has_embeddings": True,
            "embedding_status": "completed",
            "created_at": "2023-01-01T00:00:00Z",
            "updated_at": "2023-01-01T00:00:00Z",
        },
        {
            "id": "123e4567-e89b-12d3-a456-426614174002",
            "file_name": "scan.pdf",
            "status": "completed",
            "category": "general",
            "chunk_count": 0,
            "has_embeddings": False,
            "embedding_status": "empty",
        },
    ]
    respx.get("http://localhost:8080/v1/files").mock(
        return_value=httpx.Response(200, json=mock_response)
    )

    result = await client.list_files()

    assert [file.chunk_count for file in result] == [42, 0]
    assert [file.has_embeddings for file in result] == [True, False]
    assert [file.embedding_status for file in result] == ["completed", "empty"]


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
async def test_update_file_category_success(client):
    mock_response = {
        "id": "123e4567-e89b-12d3-a456-426614174001",
        "user_id": "123e4567-e89b-12d3-a456-426614174000",
        "file_name": "test.txt",
        "category": "medicine",
        "manual_override": True
    }
    respx.patch("http://localhost:8080/v1/files/123e4567-e89b-12d3-a456-426614174001/category").mock(return_value=httpx.Response(200, json=mock_response))

    result = await client.update_file_category("123e4567-e89b-12d3-a456-426614174001", "medicine")

    assert isinstance(result, IngestedFile)
    assert result.id == "123e4567-e89b-12d3-a456-426614174001"
    assert result.category == "medicine"
    assert result.manual_override is True


@respx.mock
@pytest.mark.asyncio
async def test_update_file_category_not_found(client):
    respx.patch("http://localhost:8080/v1/files/123e4567-e89b-12d3-a456-426614174001/category").mock(return_value=httpx.Response(404))

    with pytest.raises(APIError) as exc_info:
        await client.update_file_category("123e4567-e89b-12d3-a456-426614174001", "medicine")
    assert exc_info.value.status == 404


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

    with pytest.raises(APIError) as exc_info:
        await client.create_memory("This is a memory")
    assert exc_info.value.status == 409

@respx.mock
@pytest.mark.asyncio
async def test_create_memory_server_error(client):
    respx.post("http://localhost:8080/memories").mock(return_value=httpx.Response(500, json={"error": "Internal Server Error"}))

    with pytest.raises(APIError) as exc_info:
        await client.create_memory("This is a memory")
    assert exc_info.value.status == 500

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

    with pytest.raises(APIError) as exc_info:
        await client.update_memory("123e4567-e89b-12d3-a456-426614174001", content="Updated memory", status="active", importance=2)
    assert exc_info.value.status == 404

@respx.mock
@pytest.mark.asyncio
async def test_delete_memory_success(client):
    respx.delete("http://localhost:8080/memories/123e4567-e89b-12d3-a456-426614174001").mock(return_value=httpx.Response(204))

    await client.delete_memory("123e4567-e89b-12d3-a456-426614174001")

@respx.mock
@pytest.mark.asyncio
async def test_delete_memory_not_found(client):
    respx.delete("http://localhost:8080/memories/123e4567-e89b-12d3-a456-426614174001").mock(return_value=httpx.Response(404))

    with pytest.raises(APIError) as exc_info:
        await client.delete_memory("123e4567-e89b-12d3-a456-426614174001")
    assert exc_info.value.status == 404

@respx.mock
@pytest.mark.asyncio
async def test_stream_chat_success(client):

    async def sse_stream():
        yield b'data: {"content": "Hello"}\n\n'
        yield b'data: {"source": "Doc 1"}\n\n'
        yield b'data: [DONE]\n\n'

    respx.post("http://localhost:8080/chat/stream").mock(return_value=httpx.Response(200, content=sse_stream(), headers={"Content-Type": "text/event-stream"}))

    generator = client.stream_chat(session_id="session-123", content="Hi")
    results = [item async for item in generator]

    assert len(results) == 2
    assert results[0] == {"content": "Hello"}
    assert results[1] == {"source": "Doc 1"}

@respx.mock
@pytest.mark.asyncio
async def test_stream_chat_error(client):
    from api.client import StreamError

    async def sse_stream():
        yield b'data: {"content": "Hello"}\n\n'
        yield b'event: error\n'
        yield b'data: Something went wrong\n\n'

    respx.post("http://localhost:8080/chat/stream").mock(return_value=httpx.Response(200, content=sse_stream(), headers={"Content-Type": "text/event-stream"}))

    generator = client.stream_chat(session_id="session-123", content="Hi")

    with pytest.raises(StreamError) as exc_info:
        [item async for item in generator]

    assert str(exc_info.value) == "Something went wrong"

@respx.mock
@pytest.mark.asyncio
async def test_stream_chat_http_error(client):
    respx.post("http://localhost:8080/chat/stream").mock(return_value=httpx.Response(500, json={"error": "Internal Error"}))

    generator = client.stream_chat(session_id="session-123", content="Hi")
    with pytest.raises(APIError) as exc_info:
        async for _ in generator:
            pass
    assert exc_info.value.status == 500

@respx.mock
@pytest.mark.asyncio
async def test_stream_chat_json_decode_error(client):
    async def sse_stream():
        yield b'data: invalid_json\n\n'
        yield b'event: sources\n'
        yield b'data: invalid_sources_json\n\n'
        yield b'data: [DONE]\n\n'

    respx.post("http://localhost:8080/chat/stream").mock(return_value=httpx.Response(200, content=sse_stream(), headers={"Content-Type": "text/event-stream"}))

    generator = client.stream_chat(session_id="session-123", content="Hi")
    results = [item async for item in generator]

    assert len(results) == 2
    assert results[0] == "invalid_json"
    assert results[1] == {"type": "sources", "data": "invalid_sources_json"}


@respx.mock
@pytest.mark.asyncio
async def test_get_messages_sends_limit_and_parses_sources(client):
    route = respx.get("http://localhost:8080/messages").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "m1", "role": "user", "content": "question"},
                {
                    "id": "m2",
                    "role": "assistant",
                    "content": "answer",
                    "sources": [
                        {
                            "chunk_id": "c1",
                            "file_name": "book.pdf",
                            "page_number": 3,
                            "content_snippet": "text",
                            "score": 87.5,
                            "score_type": "cosine",
                            "source_type": "text",
                        }
                    ],
                },
            ],
        )
    )

    result = await client.get_messages("session-123", limit=100)

    params = route.calls.last.request.url.params
    assert params["session_id"] == "session-123"
    assert params["limit"] == "100"
    assert result[0].sources is None
    assert result[1].sources[0]["file_name"] == "book.pdf"
    assert result[1].sources[0]["page_number"] == 3


@respx.mock
@pytest.mark.asyncio
async def test_get_messages_omits_limit_by_default(client):
    route = respx.get("http://localhost:8080/messages").mock(
        return_value=httpx.Response(200, json=[])
    )

    await client.get_messages("session-123")

    assert "limit" not in route.calls.last.request.url.params


# ------------------------------------------------------------------- APIError
@respx.mock
@pytest.mark.asyncio
async def test_api_error_carries_the_backend_text(client):
    # The backend answers with http.Error: a plain sentence plus a newline.
    respx.put("http://localhost:8080/memories/m1").mock(
        return_value=httpx.Response(
            400,
            text="importance must be between 1 and 10\n",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
    )

    with pytest.raises(APIError) as exc_info:
        await client.update_memory("m1", content="x", status="active", importance=0)

    error = exc_info.value
    assert error.status == 400
    assert error.message == "importance must be between 1 and 10"
    # The UI shows str(error) - not httpx's "Client error '400 Bad Request' ...".
    assert str(error) == "importance must be between 1 and 10"
    assert isinstance(error.__cause__, httpx.HTTPStatusError)


@respx.mock
@pytest.mark.asyncio
async def test_api_error_trims_a_long_body(client):
    respx.post("http://localhost:8080/memories").mock(
        return_value=httpx.Response(500, text="boom " * 200)
    )

    with pytest.raises(APIError) as exc_info:
        await client.create_memory("x")

    assert len(exc_info.value.message) <= 200
    assert exc_info.value.message.endswith("...")


@respx.mock
@pytest.mark.asyncio
async def test_api_error_without_a_body_names_the_status(client):
    respx.delete("http://localhost:8080/memories/m1").mock(
        return_value=httpx.Response(404)
    )

    with pytest.raises(APIError) as exc_info:
        await client.delete_memory("m1")

    assert exc_info.value.message == "HTTP 404 Not Found"


@respx.mock
@pytest.mark.asyncio
async def test_upload_error_shows_the_backend_text(client):
    respx.post("http://localhost:8080/v1/files").mock(
        return_value=httpx.Response(413, text="file too large\n")
    )

    with pytest.raises(APIError, match="file too large") as exc_info:
        await client.upload_file(b"x", "big.bin")

    assert exc_info.value.status == 413


@respx.mock
@pytest.mark.asyncio
async def test_category_error_shows_the_backend_text(client):
    respx.patch("http://localhost:8080/v1/files/f1/category").mock(
        return_value=httpx.Response(400, text="invalid category\n")
    )

    with pytest.raises(APIError, match="invalid category"):
        await client.update_file_category("f1", "nope")


@respx.mock
@pytest.mark.asyncio
async def test_stream_error_status_carries_the_backend_text(client):
    respx.post("http://localhost:8080/chat/regenerate").mock(
        return_value=httpx.Response(400, text="no user message found to regenerate from\n")
    )

    with pytest.raises(APIError) as exc_info:
        async for _ in client.regenerate_chat(session_id="session-123"):
            pass

    assert exc_info.value.status == 400
    assert exc_info.value.message == "no user message found to regenerate from"



@pytest.fixture
def no_retry_delay(monkeypatch):
    """Skip tenacity's back-off so a retried GET does not slow the tests down."""
    monkeypatch.setattr(APIClient._get.retry, "wait", wait_none())


@respx.mock
@pytest.mark.asyncio
async def test_get_does_not_repeat_a_rejected_request(client, no_retry_delay):
    route = respx.get("http://localhost:8080/memories").mock(
        return_value=httpx.Response(
            400, text="status must be one of: candidate, active, superseded\n"
        )
    )

    with pytest.raises(APIError) as exc_info:
        await client.list_memories(status="nope")

    # A 4xx is final: one request, and the backend's text instead of a RetryError.
    assert route.call_count == 1
    assert exc_info.value.status == 400
    assert exc_info.value.message == "status must be one of: candidate, active, superseded"


@respx.mock
@pytest.mark.asyncio
async def test_get_repeats_a_server_error_and_then_raises_it(client, no_retry_delay):
    route = respx.get("http://localhost:8080/v1/files").mock(
        return_value=httpx.Response(503, text="database unavailable\n")
    )

    with pytest.raises(APIError) as exc_info:
        await client.list_files()

    assert route.call_count == 3
    assert exc_info.value.status == 503
    assert exc_info.value.message == "database unavailable"


@respx.mock
@pytest.mark.asyncio
async def test_get_recovers_from_a_transient_server_error(client, no_retry_delay):
    route = respx.get("http://localhost:8080/v1/files").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=[])]
    )

    assert await client.list_files() == []
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_get_reports_an_unreachable_backend_as_the_connection_error(
    client, no_retry_delay
):
    route = respx.get("http://localhost:8080/v1/files").mock(
        side_effect=httpx.ConnectError("refused")
    )

    with pytest.raises(httpx.ConnectError):
        await client.list_files()

    assert route.call_count == 3


def test_content_size_counts_utf8_bytes_like_the_backend():
    assert MAX_CONTENT_BYTES == 32000
    assert content_size("abc") == 3
    # A Cyrillic letter is two bytes, so 16000 of them already reach the limit.
    assert content_size("я" * 16000) == MAX_CONTENT_BYTES


@respx.mock
@pytest.mark.asyncio
async def test_create_memory_always_sends_a_valid_importance(client):
    # The backend reads a missing importance as 0 and answers 400.
    route = respx.post("http://localhost:8080/memories").mock(
        return_value=httpx.Response(201, json={"id": "m1", "content": "x"})
    )

    await client.create_memory("x")
    await client.create_memory("y", importance=9)

    sent = [json.loads(call.request.content) for call in route.calls]
    assert [body["importance"] for body in sent] == [5, 9]


@pytest.mark.parametrize(
    "name",
    ["a.txt", "A.PDF", "book.epub", "scan.TIFF", "photo.jpeg", "notes.md", "table.xlsx", "page.htm"],
)
def test_supported_files(name):
    assert is_supported_file(name)


@pytest.mark.parametrize(
    "name", ["old.doc", "text.rtf", "slides.pptx", "archive.zip", "noextension", ".txt.bak", "logo.svg", ""]
)
def test_unsupported_files(name):
    assert not is_supported_file(name)


def test_picker_extensions_are_lowercase_without_dots():
    # FilePicker wants bare extensions.
    assert all(ext == ext.lower() and "." not in ext for ext in SUPPORTED_EXTENSIONS)


# --------------------------------------------------------------------- logging
@respx.mock
@pytest.mark.asyncio
async def test_refused_request_is_logged_with_method_path_and_status(client, caplog):
    respx.put("http://localhost:8080/memories/m1").mock(
        return_value=httpx.Response(400, text="importance must be between 1 and 10\n")
    )

    with caplog.at_level(logging.WARNING, logger="api.client"), pytest.raises(APIError):
        await client.update_memory("m1", "x", "active", 11)

    record = next(r for r in caplog.records if r.name == "api.client")
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "PUT /memories/m1 -> HTTP 400: importance must be between 1 and 10"
    )


@respx.mock
@pytest.mark.asyncio
async def test_server_error_is_logged_as_an_error_and_without_the_query(client, caplog, no_retry_delay):
    respx.get("http://localhost:8080/messages").mock(return_value=httpx.Response(503))

    with caplog.at_level(logging.WARNING, logger="api.client"), pytest.raises(APIError):
        await client.get_messages("secret-session-id")

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors
    assert "GET /messages -> HTTP 503" in errors[0].getMessage()
    # The query string carries the session id - it must stay out of the log.
    assert "secret-session-id" not in caplog.text


@respx.mock
@pytest.mark.asyncio
async def test_repeated_request_is_logged(client, caplog, no_retry_delay):
    respx.get("http://localhost:8080/v1/files").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=[])]
    )

    with caplog.at_level(logging.WARNING, logger="api.client"):
        await client.list_files()

    assert any("Retrying" in r.getMessage() for r in caplog.records)


@respx.mock
@pytest.mark.asyncio
async def test_changes_are_logged_without_the_text_of_the_memory(client, caplog):
    respx.post("http://localhost:8080/memories").mock(
        return_value=httpx.Response(201, json={"id": "m1", "content": "my private fact"})
    )
    respx.delete("http://localhost:8080/memories/m1").mock(return_value=httpx.Response(204))

    with caplog.at_level(logging.INFO, logger="api.client"):
        await client.create_memory("my private fact", importance=7)
        await client.delete_memory("m1")

    assert "Memory m1 created (status candidate, importance 7)." in caplog.text
    assert "Memory m1 deleted." in caplog.text
    assert "my private fact" not in caplog.text


@respx.mock
@pytest.mark.asyncio
async def test_upload_is_logged(client, caplog):
    respx.post("http://localhost:8080/v1/files").mock(
        return_value=httpx.Response(201, json={"id": "f1", "file_name": "a.txt"})
    )

    with caplog.at_level(logging.INFO, logger="api.client"):
        await client.upload_file(b"hello", "a.txt")

    assert "File a.txt uploaded (5 bytes, id f1)." in caplog.text


# ------------------------------------------------------------------- SSE reader
def _sse_response(*frames: bytes) -> httpx.Response:
    async def body():
        for frame in frames:
            yield frame

    return httpx.Response(200, content=body(), headers={"Content-Type": "text/event-stream"})


@pytest.mark.parametrize(
    ("method", "path", "args", "sent"),
    [
        ("stream_chat", "/chat/stream", ("s1", "Hi"), {"session_id": "s1", "content": "Hi"}),
        ("regenerate_chat", "/chat/regenerate", ("s1",), {"session_id": "s1"}),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_both_chat_endpoints_share_one_reader(client, method, path, args, sent):
    route = respx.post(f"http://localhost:8080{path}").mock(
        return_value=_sse_response(
            b'event: sources\ndata: [{"title": "a.pdf"}]\n\n',
            b'data: {"content": "Hello"}\n\n',
            b'event: proposals\ndata: [{"id": 1}]\n\n',
            b"data: [DONE]\n\n",
        )
    )

    results = [item async for item in getattr(client, method)(*args)]

    assert results == [
        {"type": "sources", "data": [{"title": "a.pdf"}]},
        {"content": "Hello"},
        {"type": "proposals", "data": [{"id": 1}]},
    ]
    assert json.loads(route.calls[0].request.content) == sent
    assert route.calls[0].request.headers["X-User-ID"] == client.user_id


@respx.mock
@pytest.mark.asyncio
async def test_stream_error_frame_of_the_backend_is_unwrapped(client):
    # What internal/agent/orchestrator_stream.go writes for a failed generation.
    respx.post("http://localhost:8080/chat/stream").mock(
        return_value=_sse_response(
            b'event: error\ndata: {"error": "Failed to generate a response. (request_id: abc)"}\n\n'
        )
    )

    with pytest.raises(StreamError) as exc_info:
        [item async for item in client.stream_chat("s1", "Hi")]

    assert str(exc_info.value) == "Failed to generate a response. (request_id: abc)"


@respx.mock
@pytest.mark.asyncio
async def test_stream_chunk_with_an_error_key_raises(client):
    respx.post("http://localhost:8080/chat/regenerate").mock(
        return_value=_sse_response(b'data: {"error": "model is down"}\n\n')
    )

    with pytest.raises(StreamError, match="model is down"):
        [item async for item in client.regenerate_chat("s1")]


@respx.mock
@pytest.mark.asyncio
async def test_stream_chunk_that_is_not_an_object_does_not_crash(client):
    respx.post("http://localhost:8080/chat/stream").mock(
        return_value=_sse_response(b"data: 123\n\n", b'data: "text"\n\n', b"data: [DONE]\n\n")
    )

    assert [item async for item in client.stream_chat("s1", "Hi")] == [123, "text"]


@respx.mock
@pytest.mark.asyncio
async def test_undecodable_frame_is_logged_by_size_not_by_content(client, caplog):
    respx.post("http://localhost:8080/chat/stream").mock(
        return_value=_sse_response(b"data: private words of the answer\n\n", b"data: [DONE]\n\n")
    )

    with caplog.at_level(logging.ERROR, logger="api.client"):
        results = [item async for item in client.stream_chat("s1", "Hi")]

    assert results == ["private words of the answer"]
    assert "Could not decode a stream chunk (27 characters)" in caplog.text
    assert "private words" not in caplog.text


@respx.mock
@pytest.mark.asyncio
async def test_refused_stream_request_is_logged_and_raised(client, caplog):
    respx.post("http://localhost:8080/chat/regenerate").mock(
        return_value=httpx.Response(400, text="no user message found to regenerate from\n")
    )

    with caplog.at_level(logging.WARNING, logger="api.client"), pytest.raises(APIError) as exc_info:
        [item async for item in client.regenerate_chat("s1")]

    assert exc_info.value.status == 400
    assert "POST /chat/regenerate -> HTTP 400: no user message found" in caplog.text


@respx.mock
@pytest.mark.asyncio
async def test_current_stream_task_is_cleared_when_the_stream_ends(client):
    respx.post("http://localhost:8080/chat/stream").mock(
        return_value=_sse_response(b'data: {"content": "x"}\n\n', b"data: [DONE]\n\n")
    )

    async for _ in client.stream_chat("s1", "Hi"):
        assert client._current_stream_task is not None
    assert client._current_stream_task is None
