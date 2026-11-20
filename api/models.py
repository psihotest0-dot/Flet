"""The data the backend sends and takes, as pydantic models (``docs/openapi.yaml``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator

# ``Memory.status``: a memory the model proposed waits as a candidate until the user
# activates it; only an active one is used in answers.
MEMORY_ACTIVE = "active"
MEMORY_CANDIDATE = "candidate"

# ``Proposal.kind`` of a write to one of the user's own text files; the other kinds
# are a global rule, a note on how to talk and a memory.
WRITE_FILE_KIND = "write_file"

# ``Rule.kind``: an instruction of the user, or a note on how he likes to be talked to.
RULE_KIND = "rule"
STYLE_KIND = "style"


class Message(BaseModel):
    """Represents a chat message."""

    id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    role: str | None = None
    content: str | None = None
    created_at: datetime | None = None
    # Where an assistant answer came from (same shape as the SSE ``sources``
    # event); stored with the message so the references survive a reload.
    sources: list[dict[str, Any]] | None = None


class Memory(BaseModel):
    """Represents a piece of memory stored in the system."""

    id: str | None = None
    user_id: str | None = None
    content: str | None = None
    status: str | None = None
    source_message_id: str | None = None
    importance: int | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_used_at: datetime | None = None


class Proposal(BaseModel):
    """A write the assistant asked for that waits for the user.

    It is a global rule, a note on how to talk, a memory, or a write to one of the
    user's own text files (``write_file``: ``file_name``, ``file_mode`` and the
    text in ``content``).

    ``external_sources`` names where untrusted text came from in the turn that
    made it (``web_search``, ``web_fetch``, ``search_documents``,
    ``retrieved_documents``, ``attachments``, ``user_files``); empty means the
    turn read only the conversation.
    """

    id: int
    kind: str | None = None
    status: str | None = None
    session_id: str | None = None
    summary: str = ""
    key: str | None = None
    value: str | None = None
    content: str | None = None
    importance: int | None = None
    # A file write only: the name, and ``create``, ``replace`` or ``append``.
    file_name: str | None = None
    file_mode: str | None = None
    external_sources: list[str] = []
    created_at: datetime | None = None


class UserFileInfo(BaseModel):
    """One of the text files the assistant keeps for the user (notes, lists, drafts)."""

    name: str
    size: int = 0
    modified: datetime | None = None


class UserFileList(BaseModel):
    """The files with what they take and the limits (``GET /workspace/files``)."""

    files: list[UserFileInfo] = []
    total_bytes: int = 0
    max_files: int = 0
    max_file_bytes: int = 0
    max_total_bytes: int = 0


class UserFile(BaseModel):
    """The text of one file: ``version`` is ``current`` or ``previous``.

    The previous version is the text a replacement took the place of; it has no
    size and no date.
    """

    name: str
    version: str = "current"
    content: str = ""
    size: int | None = None
    modified: datetime | None = None


class Rule(BaseModel):
    """A global rule or a note on how to talk: both are in the system prompt of every session.

    ``kind`` is ``rule`` for an instruction of the user and ``style`` for a note on how
    he likes to be talked to (the backend's plan item P42); an older backend sends no
    kind, and everything it has is a rule.
    """

    id: int
    kind: str = RULE_KIND
    key: str
    value: str
    enabled: bool = True
    updated_at: datetime | None = None


class OracleDecision(BaseModel):
    """One line of the Oracle journal: what the trading bot asked and what came back.

    ``status`` is ``answered`` (``verdict`` is ``APPROVE``, ``VETO`` or ``HOLD``)
    or ``failed`` (no verdict; ``error`` says why). In ``shadow`` mode the bot
    ignores the verdict, so the journal is the whole result.
    """

    id: int
    created_at: datetime | None = None
    request_id: str | None = None
    signal_id: str | None = None
    symbol: str | None = None
    mode: str | None = None
    status: str | None = None
    verdict: str | None = None
    reason: str | None = None
    model: str | None = None
    attempts: int | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    # ``signal_version`` is the version of the strategy that made the signal
    # ("oracle-probe" marks the bot's own test questions, which are not signals);
    # ``prompt_version`` is a 12-hex label of the question the model was asked.
    # Both are omitted when empty (``prompt_version`` on rows from before it was
    # recorded).
    signal_version: str | None = None
    prompt_version: str | None = None


class OracleDecisionPage(BaseModel):
    """A page of the journal, newest first.

    ``next_before_id`` is the ``before_id`` of the next page; 0 when this was the
    last one. An empty journal may arrive as ``"decisions": null``.
    """

    decisions: list[OracleDecision] = []
    next_before_id: int = 0

    @field_validator("decisions", mode="before")
    @classmethod
    def _no_decisions_is_an_empty_list(cls, value):
        return [] if value is None else value

    @field_validator("next_before_id", mode="before")
    @classmethod
    def _no_next_page_is_zero(cls, value):
        return 0 if value is None else value


class IngestedFile(BaseModel):
    """Represents a file uploaded to the system."""

    id: str | None = None
    user_id: str | None = None
    file_name: str | None = None
    file_hash: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    status: str | None = None
    category: str | None = None
    manual_override: bool | None = None
    # Vector state reported by the backend: a file that finished ingestion with
    # zero chunks cannot be found by retrieval, which the API reports as
    # embedding_status "empty" instead of "completed".
    chunk_count: int | None = None
    has_embeddings: bool | None = None
    embedding_status: str | None = None
    local_path: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ChatAttachment(BaseModel):
    """A text file attached to one conversation (code, a log, a long paste).

    It is not the library: the model sees it only in this conversation. ``created``,
    ``replaced`` and ``unchanged`` are set only in the answer to an upload.
    """

    id: str
    name: str
    bytes: int = 0
    lines: int = 0
    tokens: int = 0
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created: bool | None = None
    replaced: bool | None = None
    unchanged: bool | None = None


@dataclass(frozen=True)
class UploadResult:
    """The outcome of an upload: the stored file, and whether it was already there."""

    file: IngestedFile
    duplicate: bool = False
