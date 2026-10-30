from __future__ import annotations

"""State management for application settings."""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "settings.json"

DEFAULT_BASE_URL = "http://localhost:8080"

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

    @field_validator("base_url", mode="before")
    @classmethod
    def _validate_base_url(cls, value: Any) -> str:
        return _clean_str(value) or DEFAULT_BASE_URL

    @field_validator("user_id", "session_id", mode="before")
    @classmethod
    def _validate_optional_id(cls, value: Any) -> str | None:
        return _clean_str(value)

    @field_validator("chat_sessions", mode="before")
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


def load_settings() -> Settings:
    """Load settings from the local JSON file.

    A hand-edited file must never cost the user their identity: ``user_id`` and
    ``session_id`` are what keep the chat, memory and file history reachable on
    the backend. The file is therefore repaired in place whenever possible and
    only moved aside when it holds no recoverable data at all.
    """
    if not SETTINGS_FILE.exists():
        return Settings()

    try:
        text = SETTINGS_FILE.read_text(encoding="utf-8-sig")
    except (OSError, ValueError) as e:
        # ValueError covers a file that is not UTF-8 decodable at all.
        logger.error(f"Failed to read settings file: {e}")
        return Settings()

    data, repaired = _decode_settings_text(text)

    if data is None:
        salvaged = _salvage_fields(text)
        _backup_broken_settings()
        if not salvaged:
            return Settings()
        # The ids outlived the document: keep them instead of minting new ones.
        settings = Settings(**salvaged)
        logger.warning(
            "Settings file was unreadable; recovered user_id=%s session_id=%s",
            settings.user_id,
            settings.session_id,
        )
        save_settings(settings)
        return settings

    try:
        settings = Settings(**data)
    except (TypeError, ValueError) as e:
        # The JSON itself is readable, so the file is kept for the user to fix;
        # only the offending values fall back to their defaults.
        logger.error(f"Failed to parse settings values, using defaults: {e}")
        return Settings()

    if repaired:
        logger.warning(
            "settings.json was not valid JSON (trailing commas/comments); "
            "it has been rewritten in canonical form."
        )
        save_settings(settings)
    return settings


def save_settings(settings: Settings) -> bool:
    """Save settings to the local JSON file using atomic write."""
    temp_file = SETTINGS_FILE.with_name(f"settings.tmp.{int(time.time())}")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(settings.model_dump_json(indent=4))
        os.replace(temp_file, SETTINGS_FILE)
        return True
    except (OSError, ValueError) as e:
        logger.error(f"Failed to save settings to file: {e}")
        try:
            if temp_file.exists():
                temp_file.unlink()
        except OSError as unlink_error:
            logger.warning(f"Failed to remove temporary file {temp_file}: {unlink_error}")
        return False
