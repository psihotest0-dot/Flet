"""The two ways a call to the backend can fail with an answer of its own."""

from __future__ import annotations

import httpx


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
