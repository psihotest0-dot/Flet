"""What the backend accepts: sizes, counts, ranges and file types."""

from __future__ import annotations

# The backend accepts 1 (trivia) .. 10 (critical) and answers 400 to anything
# else - including a missing value, which it reads as 0.
MIN_IMPORTANCE = 1
MAX_IMPORTANCE = 10
DEFAULT_IMPORTANCE = 5

# A memory (or message) body may be at most 32000 *bytes*: the backend checks
# Go's len(string) although its error text says "characters". UTF-8 spends two
# bytes on a Cyrillic letter, so Bulgarian text gets about 16000 letters.
MAX_CONTENT_BYTES = 32000

# The backend's MAX_UPLOAD_MB (100 unless its owner changed it). It is a limit on
# the whole request, not on the file, so a file just below it can still get a 413.
MAX_UPLOAD_MB = 100
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# The largest bill the backend reads (``bills.MaxFileBytes``, ``POST /v1/bills``).
MAX_BILL_BYTES = 20 << 20

# A file attached to one conversation (``/chat/attachments``, internal/attachments):
# at most 256 KB a file, 20 files and 1 MB for the whole conversation. The backend
# checks them again (413, 422); the client checks first, to answer in Bulgarian.
MAX_ATTACHMENT_BYTES = 256 * 1024
MAX_ATTACHMENTS = 20
MAX_ATTACHMENTS_TOTAL_BYTES = 1024 * 1024

# The backend refuses a message longer than this (estimated: four bytes a token;
# ``maxUserMessageTokens`` in internal/agent).
MESSAGE_TOKEN_LIMIT = 4000


def content_size(text: str) -> int:
    """Size of ``text`` as the backend measures it (UTF-8 bytes)."""
    return len(text.encode("utf-8"))


# File types the backend can turn into text (internal/ingest in C:\GO): text
# formats, documents and images (OCR). The upload endpoint accepts anything, but
# any other file ends as a failed ingest, so the picker offers only these.
SUPPORTED_EXTENSIONS = (
    "txt",
    "md",
    "csv",
    "json",
    "yaml",
    "yml",
    "log",
    "html",
    "htm",
    "pdf",
    "docx",
    "xlsx",
    "epub",
    "png",
    "jpg",
    "jpeg",
    "tif",
    "tiff",
    "bmp",
)


def is_supported_file(filename: str) -> bool:
    """Whether the backend can read a file with this name (by its extension)."""
    _, dot, ext = filename.rpartition(".")
    return bool(dot) and ext.lower() in SUPPORTED_EXTENSIONS
