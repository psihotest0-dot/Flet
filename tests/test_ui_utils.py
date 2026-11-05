from __future__ import annotations

import httpx
import pytest

from api.client import APIError, StreamError
from views.ui_utils import describe_error, error_detail


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ReadTimeout(""), httpx.RemoteProtocolError("dropped")],
)
def test_error_detail_hides_the_connection_jargon(error):
    assert error_detail(error) == "бекендът не е достъпен"


def test_error_detail_keeps_the_text_of_the_backend():
    assert error_detail(APIError(400, "importance must be between 1 and 10")) == (
        "importance must be between 1 and 10"
    )


def test_error_detail_never_ends_in_an_empty_text():
    assert error_detail(ValueError("file too large")) == "file too large"
    assert error_detail(RuntimeError()) == "RuntimeError"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ConnectError("refused"), "Бекендът не е достъпен"),
        (APIError(503, "down"), "Грешка в бекенда (HTTP 503)"),
        (APIError(400, "bad"), "Заявката е отхвърлена: bad"),
        (StreamError("x"), "Нещо се обърка"),
        (RuntimeError("boom"), "Грешка при получаване на отговора"),
    ],
)
def test_describe_error_tells_the_failures_apart(error, expected):
    assert expected in describe_error(error)
