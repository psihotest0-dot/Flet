"""What the chat view needs to know about files attached to a conversation.

Pure functions, no Flet: which files can be attached, how a pasted text becomes a
file, how long a message may be. The limits are the backend's (``internal/attachments``
and ``maxUserMessageTokens`` in ``internal/agent``); the backend checks them again,
this is only so the user hears about a problem before the request and in Bulgarian.
"""

from __future__ import annotations

import json
import re

# The largest file the backend takes.
MAX_FILE_BYTES = 256 * 1024
# The backend refuses a message longer than this (estimated: four bytes a token).
MESSAGE_TOKEN_LIMIT = 4000
# A field that grows by this many characters at once got a paste: typing never does.
PASTE_THRESHOLD_CHARS = 3000


class NotAttachable(Exception):
    """The file cannot be attached; the text says why, in Bulgarian, for the user."""


def estimate_message_tokens(text: str) -> int:
    """The way the backend counts a message: four bytes of UTF-8 a token."""
    return (len(text.encode("utf-8")) + 3) // 4


def read_text_file(raw: bytes, name: str) -> str:
    """The text of a file's bytes, or :class:`NotAttachable`.

    UTF-8 (with or without a byte order mark) first, then Windows-1251, which is what
    older Bulgarian files are written in. A NUL byte means the file is not text.
    """
    if len(raw) > MAX_FILE_BYTES:
        raise NotAttachable(f"{name}: файлът е по-голям от {MAX_FILE_BYTES // 1024} KB")
    if b"\x00" in raw:
        raise NotAttachable(f"{name}: не е текстов файл")
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise NotAttachable(f"{name}: не е текст в UTF-8 или Windows-1251")
    if not text.strip():
        raise NotAttachable(f"{name}: файлът е празен")
    return text


def inserted_text(previous: str, current: str) -> str:
    """What was inserted into ``previous`` to make ``current`` (one insertion, as a paste is).

    The text before and after the insertion is the same in both, so the common start
    and the common end are taken away and what is left in ``current`` is the paste.
    """
    shortest = min(len(previous), len(current))
    start = 0
    while start < shortest and previous[start] == current[start]:
        start += 1
    end = 0
    while (
        end < shortest - start
        and previous[len(previous) - 1 - end] == current[len(current) - 1 - end]
    ):
        end += 1
    return current[start : len(current) - end]


_PYTHON = re.compile(
    r"^\s*(?:def |class |import |from \S+ import |if __name__)", re.MULTILINE
)


def guess_extension(text: str) -> str:
    """A file extension for a pasted text, by what it looks like; ``.txt`` when unsure."""
    head = text.lstrip()[:200].lower()
    if head.startswith(("<?xml",)):
        return ".xml"
    if head.startswith(("<!doctype html", "<html")):
        return ".html"
    if head[:1] in "{[":
        try:
            json.loads(text)
            return ".json"
        except ValueError:
            pass
    if re.search(r"^package \w+", text, re.MULTILINE) and "func " in text:
        return ".go"
    if _PYTHON.search(text):
        return ".py"
    return ".txt"


def paste_file_name(text: str, taken: set[str]) -> str:
    """``paste_1.go``, ``paste_2.txt``...: the first number that is not in ``taken``."""
    extension = guess_extension(text)
    number = 1
    while f"paste_{number}{extension}" in taken or f"paste_{number}" in {
        t.rsplit(".", 1)[0] for t in taken
    }:
        number += 1
    return f"paste_{number}{extension}"
