"""The events of a chat turn, as the backend streams them (``docs/openapi.yaml``).

``/chat/stream`` and ``/chat/regenerate`` write the same Server-Sent Events frames,
so one reader serves both (``APIClient._stream_sse``). ``parse_frame`` turns a frame
into typed events, so that the chat never has to tell a dict from a string:

* :class:`TextChunk` - a piece of the answer;
* :class:`Sources` - the documents and memories it is based on (more may follow);
* :class:`Proposals` - the writes the assistant asks the user to confirm;
* :class:`ModelUsed` - who gave the answer (only while a paid model is configured);
* :class:`Queued` - the request waits for another reply in the same conversation;
* :class:`Working` - a frame with nothing to show (the model's reasoning, which
  clients hide): the backend is alive, and that is all it says.

An ``event: error`` frame raises :class:`~api.errors.StreamError`. What the model
wrote and what the documents say never reach the log: an undecodable frame is
reported by its size only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from api.errors import StreamError

logger = logging.getLogger(__name__)

# The keys older backends put the passages under in a frame that was not
# ``event: sources`` (the contract has only ``event: sources`` with an array).
_SOURCE_KEYS = ("facts", "chunks", "sources")
# The keys of a data frame that hold a piece of the answer: ``content`` is the
# contract, ``text`` the older name.
_TEXT_KEYS = ("text", "content")


@dataclass(frozen=True)
class TextChunk:
    """A piece of the answer."""

    text: str


@dataclass(frozen=True)
class Sources:
    """Where the answer comes from: ``SourceInfo`` entries as the backend sent them."""

    items: list[Any]


@dataclass(frozen=True)
class Proposals:
    """The writes the assistant asked for in this turn: ``Proposal`` entries, as sent."""

    items: list[Any]


@dataclass(frozen=True)
class ModelUsed:
    """Who gave the answer: ``{"provider": "local"}`` or ``{"provider": ..., "model": ...}``."""

    who: dict[str, Any]


@dataclass(frozen=True)
class Working:
    """A frame with nothing to show; the stream is alive."""


@dataclass(frozen=True)
class Queued:
    """The request waits: another reply in the same conversation is still being written.

    Only a request that asked for it (``queue_events``) is sent this, and before anything
    else; the answer follows when the other one is done.
    """


StreamEvent = TextChunk | Sources | Proposals | ModelUsed | Working | Queued


def _stream_error_text(data: str) -> str:
    """The text of an ``event: error`` frame.

    The backend sends ``{"error": "..."}`` (with a ``request_id`` to find the
    cause in its log); a plain-text frame is used as it is.
    """
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return data
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    return data


def _source_lists(data: Any) -> list[Any]:
    """The passages of a sources frame: the array, or the lists of an older object."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [
            item
            for key in _SOURCE_KEYS
            if isinstance(data.get(key), list)
            for item in data[key]
        ]
    return []


def _sources_frame(data: str) -> list[StreamEvent]:
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as e:
        logger.error(
            "Could not decode the sources frame (%d characters): %s", len(data), e
        )
        return [Working()]
    items = _source_lists(parsed)
    return [Sources(items)] if items else [Working()]


def _model_frame(data: str) -> list[StreamEvent]:
    # Who answered; a frame this client cannot read is not worth a failure.
    try:
        who = json.loads(data)
    except json.JSONDecodeError:
        return []
    return [ModelUsed(who)] if isinstance(who, dict) else []


def _proposals_frame(data: str) -> list[StreamEvent]:
    try:
        proposals = json.loads(data)
    except json.JSONDecodeError as e:
        logger.error(
            "Could not decode the proposals frame (%d characters): %s", len(data), e
        )
        return []
    return [Proposals(proposals)] if isinstance(proposals, list) else []


def _data_frame(data: str) -> list[StreamEvent]:
    """A frame without an event name: a piece of the answer, or something to skip."""
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError as e:
        logger.error(
            "Could not decode a stream chunk (%d characters): %s", len(data), e
        )
        return [TextChunk(data)]
    if isinstance(chunk, str):
        return [TextChunk(chunk)]
    if not isinstance(chunk, dict):
        return [Working()]
    if "error" in chunk:
        raise StreamError(str(chunk["error"] or "Unknown error"))

    events: list[StreamEvent] = [
        TextChunk(chunk[key]) for key in _TEXT_KEYS if isinstance(chunk.get(key), str)
    ]
    if sources := _source_lists(chunk):
        events.append(Sources(sources))
    return events or [Working()]


def parse_frame(event: str | None, data: str) -> list[StreamEvent]:
    """The events of one frame (``event`` is its ``event:`` name, ``data`` its text).

    ``[DONE]`` is not a frame of this kind: the reader stops at it before asking.
    An ``error`` frame, and a data frame with an ``error`` key, raise
    :class:`~api.errors.StreamError`.
    """
    if event == "error":
        raise StreamError(_stream_error_text(data))
    if event == "sources":
        return _sources_frame(data)
    if event == "model":
        return _model_frame(data)
    if event == "proposals":
        return _proposals_frame(data)
    if event == "queued":
        return [Queued()]
    return _data_frame(data)
