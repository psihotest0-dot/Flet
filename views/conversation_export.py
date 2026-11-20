"""A conversation as a Markdown text, for the clipboard and for a file.

Pure functions, no Flet. ``ChatView`` collects what is on the screen into
:class:`ExportedMessage` (the sender, the text as it was written - images included,
like the copy button of a message -, when, who answered, the sources) and these
turn it into the text and the name of the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from views.sources import as_source, source_title
from views.timeline import full_label

# What Windows does not allow in a file name (and the control characters).
_UNSAFE_IN_FILE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DEFAULT_FILE_TITLE = "Разговор"
UNFINISHED_NOTE = "*Отговорът не е завършен.*"


@dataclass(frozen=True)
class ExportedMessage:
    """One message of the conversation as the export needs it."""

    sender: str
    text: str
    created_at: datetime | None = None
    # "☁ deepseek-flash" or "🏠 на този компютър", as the message shows it; empty if unknown.
    model: str = ""
    sources: tuple[Any, ...] = ()
    # An answer that was stopped or failed: what is on the screen is only a part of it.
    unfinished: bool = False


def message_count_text(count: int) -> str:
    """ "1 съобщение", "12 съобщения"."""
    return f"{count} съобщение" if count == 1 else f"{count} съобщения"


def sources_line(sources) -> str:
    """The files an answer was based on, once each with how many passages: "a.pdf (3), b.pdf"."""
    counts: dict[str, int] = {}
    for source in sources:
        title = str(source_title(as_source(source)))
        counts[title] = counts.get(title, 0) + 1
    return ", ".join(f"{t} ({n})" if n > 1 else t for t, n in counts.items())


def _message_markdown(message: ExportedMessage) -> str:
    head = [f"**{message.sender}**"]
    if message.created_at is not None:
        head.append(full_label(message.created_at))
    if message.model:
        head.append(message.model)
    parts = [" · ".join(head), message.text]
    if message.unfinished:
        parts.append(UNFINISHED_NOTE)
    if message.sources:
        parts.append(f"*Източници: {sources_line(message.sources)}*")
    return "\n\n".join(parts)


def conversation_markdown(
    title: str,
    messages: list[ExportedMessage],
    exported_at: datetime,
    truncated: bool = False,
) -> str:
    """The whole conversation: a heading, when it was exported, then every message.

    ``truncated`` says that older messages exist that the screen does not hold.
    """
    lines = [
        f"# {title}",
        f"Експортиран на {full_label(exported_at)} · {message_count_text(len(messages))}",
    ]
    if truncated:
        lines.append("> Има и по-стари съобщения: тук са само показаните.")
    blocks = [_message_markdown(message) for message in messages]
    return "\n\n".join(lines) + "\n\n---\n\n" + "\n\n---\n\n".join(blocks) + "\n"


def export_file_name(title: str, exported_at: datetime) -> str:
    """A name for the file that is safe on Windows: "Чат 1 2026-11-20.md".

    The date after the title also keeps a title such as "con" from being one of the
    names Windows reserves.
    """
    name = _UNSAFE_IN_FILE_NAME.sub("", title).strip(" .")[:60].strip(" .")
    return f"{name or DEFAULT_FILE_TITLE} {exported_at.astimezone():%Y-%m-%d}.md"
