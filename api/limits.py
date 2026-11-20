"""What the backend accepts: sizes, ranges and file types."""

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
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


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
