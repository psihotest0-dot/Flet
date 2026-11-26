"""The item "Сметка" of the menu: the bill is sent to ``POST /v1/bills`` and the history loaded again."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from api.client import APIClient
from api.errors import APIError, FileTooLargeError
from api.stream import TextChunk
from tests.test_chat_modes import make_view, seed
from views.chat_bills import BILL_UNREADABLE_TEXT, bill_error_text


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
    assert "20 MB" in bill_error_text(FileTooLargeError("big"))
    assert bill_error_text(FileNotFoundError(2, "gone")) == BILL_UNREADABLE_TEXT


def test_an_answer_that_is_not_json_is_not_called_too_large():
    try:
        json.loads("<html>")
    except ValueError as ex:
        assert "20 MB" not in bill_error_text(ex)


@pytest.mark.asyncio
async def test_a_bill_read_while_an_answer_is_written_leaves_the_answer_on_screen(
    mock_page, mock_api_client, tmp_path
):
    seed()
    view = make_view(mock_page, mock_api_client)
    bill = tmp_path / "b.pdf"
    bill.write_bytes(b"%PDF-1.4 x")
    bill_read = asyncio.Event()
    go_on = asyncio.Event()

    async def upload_bill(*_args, **_kwargs):
        await bill_read.wait()
        return "Прочетох сметката"

    async def stream(**_kwargs):
        yield TextChunk("начало ")
        await go_on.wait()
        yield TextChunk("край")

    mock_api_client.upload_bill = AsyncMock(side_effect=upload_bill)
    mock_api_client.stream_chat = MagicMock(side_effect=lambda **kw: stream(**kw))
    reading = asyncio.create_task(view.send_bill(bill, "b.pdf"))
    await asyncio.sleep(0)
    view.input_field.value = "здравей"
    answering = asyncio.create_task(view.send_message_click(None))
    await asyncio.sleep(0.05)
    assert view._is_streaming
    answer = view._messages_on_screen()[-1]

    bill_read.set()
    await reading  # its history comes in the middle of the answer
    assert answer in view._messages_on_screen(), "the answer was taken off the screen"
    mock_page.run_task.reset_mock()
    go_on.set()
    await answering

    assert answer.text_value == "начало край"
    # The history the bill wrote is read once the turn is over.
    mock_page.run_task.assert_any_call(view.load_history)
    assert view._reload_after_turn is False


@pytest.mark.asyncio
async def test_a_bill_that_cannot_be_read_keeps_its_name_and_path_out_of_the_log(
    mock_page, mock_api_client, tmp_path, caplog
):
    seed()
    view = make_view(mock_page, mock_api_client)
    missing = tmp_path / "диагноза_Иван.pdf"
    # The real client: it fails on the disk, before any request is made.
    client = APIClient(base_url="http://127.0.0.1:9", user_id="u")
    mock_api_client.upload_bill = client.upload_bill

    with caplog.at_level(logging.ERROR):
        await view.send_bill(missing, missing.name)
    await client.aclose()

    assert "диагноза_Иван" not in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "FileNotFoundError" in caplog.text
    snackbar = mock_page.show_dialog.call_args.args[0]
    assert BILL_UNREADABLE_TEXT in snackbar.content.value


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
