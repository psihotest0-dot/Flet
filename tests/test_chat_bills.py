"""The item "Сметка" of the menu: the bill is sent to ``POST /v1/bills`` and the history loaded again."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from api.client import APIClient
from api.errors import APIError
from tests.test_chat_modes import make_view, seed
from views.chat_bills import bill_error_text


@pytest.mark.asyncio
async def test_a_bill_is_sent_and_the_history_is_loaded_again(
    mock_page, mock_api_client, tmp_path
):
    seed()
    view = make_view(mock_page, mock_api_client)
    bill = tmp_path / "rechnung.pdf"
    bill.write_bytes(b"%PDF-1.4 test")
    mock_api_client.upload_bill = AsyncMock(return_value="Прочетох сметката")
    view.load_history = AsyncMock()
    await view.send_bill(bill, "rechnung.pdf")
    mock_api_client.upload_bill.assert_awaited_once_with(
        view.session_id, bill, "rechnung.pdf"
    )
    view.load_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_bill_that_fails_is_reported_and_nothing_is_loaded(
    mock_page, mock_api_client, tmp_path
):
    seed()
    view = make_view(mock_page, mock_api_client)
    bill = tmp_path / "rechnung.pdf"
    bill.write_bytes(b"%PDF-1.4 test")
    mock_api_client.upload_bill = AsyncMock(side_effect=APIError(404, "not found"))
    view.load_history = AsyncMock()
    await view.send_bill(bill, "rechnung.pdf")
    view.load_history.assert_not_awaited()


def test_the_errors_of_a_bill_in_words():
    assert "не са включени" in bill_error_text(APIError(404, "x"))
    assert "20 MB" in bill_error_text(APIError(413, "x"))
    assert "20 MB" in bill_error_text(ValueError("big"))


@pytest.fixture
def bill_file(tmp_path) -> Path:
    """A made-up bill on disk, written outside the event loop (ASYNC240)."""
    bill = tmp_path / "b.pdf"
    bill.write_bytes(b"%PDF-1.4 x")
    return bill


@pytest.mark.asyncio
async def test_the_client_sends_the_file_and_the_session(bill_file):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(200, json={"message": "Прочетох сметката"})

    client = APIClient(
        base_url="http://test", user_id="00000000-0000-0000-0000-000000000001"
    )
    client.client = httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )
    message = await client.upload_bill("s-1", bill_file, "b.pdf")
    assert message == "Прочетох сметката"
    assert seen["path"] == "/v1/bills"
    assert b'name="session_id"' in seen["body"] and b"s-1" in seen["body"]
    assert b'filename="b.pdf"' in seen["body"]
