"""State management for application settings."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "settings.json"

DEFAULT_BASE_URL = "http://localhost:8080"

# How the user's own messages are labelled in the chat, and the longest name the
# label is allowed to be (the settings dialog enforces the same limit).
DEFAULT_DISPLAY_NAME = "Ти"
MAX_DISPLAY_NAME_CHARS = 40

# Writes go one at a time. Flet 1.x runs every event handler on the event loop,
# so two saves cannot overlap there any more; the lock stays for a save from a
# thread (``asyncio.to_thread``, a test), where an interleaved write would cost
# the user their settings.
_WRITE_LOCK = threading.Lock()

# Matches the identifying fields even when the surrounding document is broken.
_SCALAR_FIELD_PATTERN = re.compile(
    r'"(?P<key>base_url|user_id|session_id)"\s*:\s*"(?P<value>[^"\\]*)"'
)


def _clean_str(value: Any) -> str | None:
    """Return ``value`` as a stripped string, or ``None`` when it is empty."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (int, float)):
        stripped = str(value).strip()
        return stripped or None
    return None


def _coerce_chat_sessions(value: Any) -> list[dict]:
    """Normalise the persisted chat list into ``{"id": ..., "name": ...}`` dicts.

    Hand-edited and legacy files may hold bare chat names, a single entry
    instead of a list, or a JSON encoded string. None of that may invalidate
    the whole settings document, because validating the file must never cost
    the user their ``user_id``/``session_id``.
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                return _coerce_chat_sessions(json.loads(text))
            except json.JSONDecodeError:
                pass
        return [{"name": text}]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []

    chat_sessions: list[dict] = []
    for entry in value:
        if isinstance(entry, str):
            name = entry.strip()
            if name:
                chat_sessions.append({"name": name})
            continue
        if not isinstance(entry, dict):
            continue

        session_id = _clean_str(entry.get("id")) or _clean_str(entry.get("session_id"))
        name = _clean_str(entry.get("name")) or _clean_str(entry.get("title"))

        cleaned: dict[str, str] = {}
        if session_id:
            cleaned["id"] = session_id
        if name:
            cleaned["name"] = name
        if cleaned:
            chat_sessions.append(cleaned)
    return chat_sessions


class Settings(BaseModel):
    """Application settings, loaded from and saved to a local JSON file."""

    model_config = ConfigDict(extra="ignore")

    base_url: str = DEFAULT_BASE_URL
    user_id: str | None = None
    session_id: str | None = None
    chat_sessions: list[dict] = []
    # Left over from earlier versions, when "delete chat" only started a new session
    # and kept the old id here as the key to the history that stayed on the backend.
    # Deleting a chat now deletes it on the backend and adds nothing here; the field
    # stays so that old entries survive saving the settings.
    archived_chats: list[dict] = []
    display_name: str = DEFAULT_DISPLAY_NAME

    @field_validator("base_url", mode="before")
    @classmethod
    def _validate_base_url(cls, value: Any) -> str:
        return _clean_str(value) or DEFAULT_BASE_URL

    @field_validator("display_name", mode="before")
    @classmethod
    def _validate_display_name(cls, value: Any) -> str:
        name = _clean_str(value)
        return name[:MAX_DISPLAY_NAME_CHARS].strip() if name else DEFAULT_DISPLAY_NAME

    @field_validator("user_id", "session_id", mode="before")
    @classmethod
    def _validate_optional_id(cls, value: Any) -> str | None:
        return _clean_str(value)

    @field_validator("chat_sessions", "archived_chats", mode="before")
    @classmethod
    def _validate_chat_sessions(cls, value: Any) -> list[dict]:
        return _coerce_chat_sessions(value)


def _sanitize_json(text: str) -> str:
    """Drop comments and trailing commas outside of string literals.

    Hand-edited settings files frequently keep a trailing comma after the last
    entry (``..., }]``). ``str.replace``/regex approaches would also mangle URLs
    such as ``http://localhost:8080``, so the document is scanned instead.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)

    while index < length:
        char = text[index]

        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue

        if char == "/" and index + 1 < length:
            next_char = text[index + 1]
            if next_char == "/":
                line_end = text.find("\n", index)
                index = length if line_end == -1 else line_end
                continue
            if next_char == "*":
                block_end = text.find("*/", index + 2)
                index = length if block_end == -1 else block_end + 2
                continue

        if char == ",":
            lookahead = index + 1
            while lookahead < length and text[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead < length and text[lookahead] in "}]":
                index += 1
                continue

        out.append(char)
        index += 1

    return "".join(out)


def _decode_settings_text(text: str) -> tuple[dict[str, Any] | None, bool]:
    """Parse the file contents.

    Returns ``(data, repaired)`` where ``repaired`` tells the caller that the
    document was only readable after sanitising it, so the file has to be
    rewritten in canonical form. ``data`` is ``None`` when nothing usable can
    be recovered from the text.
    """
    for candidate, repaired in ((text, False), (_sanitize_json(text), True)):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data, repaired
        logger.error("Settings file does not contain a JSON object.")
        return None, repaired
    return None, False


def _salvage_fields(text: str) -> dict[str, Any]:
    """Best-effort extraction of the identifying fields from broken JSON."""
    salvaged: dict[str, Any] = {}
    for match in _SCALAR_FIELD_PATTERN.finditer(text):
        key = match.group("key")
        value = match.group("value").strip()
        if value and key not in salvaged:
            salvaged[key] = value
    return salvaged


def _backup_broken_settings() -> None:
    """Move an unreadable settings file aside instead of deleting it."""
    backup_file = SETTINGS_FILE.with_name(f"settings.json.broken.{int(time.time())}")
    try:
        SETTINGS_FILE.rename(backup_file)
        logger.info("Unreadable settings file backed up to %s", backup_file)
    except OSError as backup_error:
        logger.error("Failed to backup unreadable settings file: %s", backup_error)


def _backup_file() -> Path:
    """``settings.json.bak``: the settings as they were last read without trouble."""
    return SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".bak")


def _remember_good_copy(settings: Settings) -> None:
    """Keep settings that were just read without trouble in ``settings.json.bak``.

    The copy is taken when the file is read, not when it is saved, so a crash in
    the middle of a save cannot spoil both. Only settings with a ``user_id`` are
    worth keeping, and the copy is rewritten only when it changed.
    """
    if not settings.user_id:
        return
    text = settings.model_dump_json(indent=4)
    backup = _backup_file()
    try:
        if backup.read_text(encoding="utf-8") == text:
            return
    except OSError, ValueError:
        pass  # missing or unreadable: write it anew
    if not _write_atomically(backup, text):
        logger.warning("Could not refresh %s.", backup.name)


def _settings_from_backup() -> Settings | None:
    """The settings kept in ``settings.json.bak``, if they hold a ``user_id``."""
    backup = _backup_file()
    try:
        text = backup.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.error("Could not read %s: %s", backup.name, e)
        return None
    data, _ = _decode_settings_text(text)
    if data is None:
        return None
    try:
        settings = Settings(**data)
    except TypeError, ValueError:
        return None
    return settings if settings.user_id else None


def _use_restored(settings: Settings) -> Settings:
    """Make the settings from ``settings.json.bak`` the current ones."""
    # Only the first 8 characters, like everywhere else in the log.
    logger.warning(
        "settings.json gave no user_id; restored user_id=%s session_id=%s from %s",
        (settings.user_id or "")[:8],
        (settings.session_id or "")[:8],
        _backup_file().name,
    )
    save_settings(settings)
    return settings


def load_settings() -> Settings:
    """Load settings from the local JSON file.

    A hand-edited file must never cost the user their identity: ``user_id`` and
    ``session_id`` are what keep the chat, memory and file history reachable on
    the backend. The file is therefore repaired in place whenever possible and
    only moved aside when it holds no recoverable data at all. Before starting
    without a ``user_id`` the last good copy (``settings.json.bak``) is tried.
    """
    if not SETTINGS_FILE.exists():
        return Settings()

    try:
        text = SETTINGS_FILE.read_text(encoding="utf-8-sig")
    except (OSError, ValueError) as e:
        # ValueError covers a file that is not UTF-8 decodable at all.
        logger.error("Failed to read settings file: %s", e)
        restored = _settings_from_backup()
        if restored is None:
            return Settings()
        _backup_broken_settings()
        return _use_restored(restored)

    data, repaired = _decode_settings_text(text)

    if data is None:
        salvaged = _salvage_fields(text)
        _backup_broken_settings()
        if not salvaged.get("user_id"):
            # An empty or zeroed file (a power cut right after a save) or one whose
            # user_id cannot be read: the last good copy keeps the identity.
            restored = _settings_from_backup()
            if restored is not None:
                return _use_restored(restored)
        if not salvaged:
            # Nothing recoverable: the app starts with a fresh user_id, and the
            # old history can only be reached again from the backup file.
            logger.warning(
                "Settings file was unreadable and held no ids; starting with defaults."
            )
            return Settings()
        # The ids outlived the document: keep them instead of minting new ones.
        settings = Settings(**salvaged)
        # Only the first 8 characters, like everywhere else in the log: the full
        # ids are the key to the history and stay in settings.json.
        logger.warning(
            "Settings file was unreadable; recovered user_id=%s session_id=%s",
            (settings.user_id or "")[:8],
            (settings.session_id or "")[:8],
        )
        save_settings(settings)
        return settings

    try:
        settings = Settings(**data)
    except (TypeError, ValueError) as e:
        # The JSON itself is readable, so the file is kept for the user to fix;
        # only the offending values fall back to their defaults.
        logger.error("Failed to parse settings values, using defaults: %s", e)
        restored = _settings_from_backup()
        if restored is None:
            return Settings()
        # The file is still kept for the user, next to the restored one.
        _backup_broken_settings()
        return _use_restored(restored)

    if repaired:
        logger.warning(
            "settings.json was not valid JSON (trailing commas/comments); "
            "it has been rewritten in canonical form."
        )
        save_settings(settings)
    _remember_good_copy(settings)
    return settings


def _write_atomically(path: Path, text: str) -> bool:
    """Replace ``path`` with ``text`` so that a crash leaves the old or the new file.

    The text goes to a temporary file with a unique name next to ``path`` and is
    forced to the disk (``fsync``) before it takes the place of the old file:
    without that, a power cut right after the replace could leave an empty or
    zeroed file behind. Returns ``False`` and logs the reason on failure.
    """
    with _WRITE_LOCK:
        temp_file: Path | None = None
        try:
            # The prefix is git-ignored (settings.tmp.*).
            fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix="settings.tmp.")
            temp_file = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_file, path)
            return True
        except (OSError, ValueError) as e:
            logger.error("Failed to save %s: %s", path.name, e)
            try:
                if temp_file is not None and temp_file.exists():
                    temp_file.unlink()
            except OSError as unlink_error:
                logger.warning(
                    "Failed to remove temporary file %s: %s", temp_file, unlink_error
                )
            return False


def save_settings(settings: Settings) -> bool:
    """Save settings to the local JSON file (see :func:`_write_atomically`)."""
    try:
        text = settings.model_dump_json(indent=4)
    except ValueError as e:
        logger.error("Failed to save settings to file: %s", e)
        return False
    return _write_atomically(SETTINGS_FILE, text)
