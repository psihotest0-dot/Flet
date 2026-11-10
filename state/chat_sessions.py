"""The chat sessions kept in the settings: names, ids and repair of stale entries (P33).

A chat is an entry ``{"id", "name"}`` of ``Settings.chat_sessions``. These are pure
functions over ``Settings`` (the only side effect is ``save_settings`` in
``_ensure_chat_session``); the chat screen only calls them.
"""
from __future__ import annotations

import uuid

from state.settings import Settings, save_settings

CHAT_SESSION_PREFIX = "Чат"


def _next_chat_name(chat_sessions: list[dict]) -> str:
    """Return the first unused "Чат N" label for a brand new chat."""
    used = {session["name"] for session in chat_sessions}
    index = 1
    while f"{CHAT_SESSION_PREFIX} {index}" in used:
        index += 1
    return f"{CHAT_SESSION_PREFIX} {index}"


def _unique_chat_name(name: str, chat_sessions: list[dict]) -> str:
    """Return a label that no other chat session is using yet."""
    name = (name or "").strip()
    if not name:
        return _next_chat_name(chat_sessions)

    used = {session["name"] for session in chat_sessions}
    if name not in used:
        return name

    index = 2
    while f"{name} ({index})" in used:
        index += 1
    return f"{name} ({index})"


def _normalize_chat_sessions(settings: Settings) -> list[dict]:
    """Return ``settings.chat_sessions`` as clean ``{"id", "name"}`` entries.

    Older builds persisted bare names and a broken write can leave entries
    without an id. Such entries are re-bound to ``settings.session_id`` when
    that id is not listed yet, so an already existing chat stays reachable
    instead of being replaced by a freshly generated session id.
    """
    chat_sessions: list[dict] = []
    used_ids: set[str] = set()
    nameless_entries: list[dict] = []

    for entry in settings.chat_sessions or []:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            continue

        raw_id = entry.get("id") or entry.get("session_id")
        session_id = str(raw_id).strip() if raw_id else ""
        name = str(entry.get("name") or entry.get("title") or "").strip()

        if not session_id:
            # Without an id the entry cannot address a session; keep its name
            # around in case it belongs to the still active session.
            nameless_entries.append({"name": name})
            continue
        if session_id in used_ids:
            continue

        used_ids.add(session_id)
        chat_sessions.append(
            {"id": session_id, "name": _unique_chat_name(name, chat_sessions)}
        )

    active_id = (settings.session_id or "").strip()
    if active_id and active_id not in used_ids:
        # The active session predates `chat_sessions` (or lost its entry):
        # keep it and reuse the name of a legacy entry when there is one.
        name = nameless_entries.pop(0)["name"] if nameless_entries else ""
        chat_sessions.append(
            {"id": active_id, "name": _unique_chat_name(name, chat_sessions)}
        )

    return chat_sessions


def _resolve_session_id(chat_sessions: list[dict], selected: str | None) -> str | None:
    """Map a dropdown value (session id or chat name) to a saved session id."""
    value = (selected or "").strip()
    if not value:
        return None

    for session in chat_sessions:
        if session["id"] == value:
            return session["id"]
    for session in chat_sessions:
        if session["name"].casefold() == value.casefold():
            return session["id"]
    return None


def _restart_chat_session(settings: Settings, session_id: str) -> str | None:
    """Start ``session_id`` over: the same chat in the list, with a new empty session.

    The chat keeps its name and its place in the list, but now points at a fresh
    session id, so the conversation starts empty. This is the local half of
    deleting a chat: the caller deletes the old session on the backend first
    (``APIClient.delete_session``), which also leaves the user's long-term memory
    (it belongs to ``user_id``, not to a chat) alone. Nothing is archived any more:
    there is no history left to find. ``settings.archived_chats`` only keeps the
    entries that earlier versions wrote there.

    Returns the new session id, or ``None`` when ``session_id`` is not in the
    list (nothing is changed then).
    """
    chat_sessions = _normalize_chat_sessions(settings)
    index = next(
        (i for i, session in enumerate(chat_sessions) if session["id"] == session_id),
        None,
    )
    if index is None:
        return None

    old = chat_sessions[index]
    new_id = str(uuid.uuid4())
    chat_sessions[index] = {"id": new_id, "name": old["name"]}
    settings.chat_sessions = chat_sessions
    settings.session_id = new_id
    return new_id


def _ensure_chat_session(settings: Settings) -> tuple[str, list[dict]]:
    """Resolve the session the chat view must open and repair stale settings.

    A saved ``chat_sessions`` entry always wins over generating an id: a new
    UUID is created only when the settings hold no chat at all.
    """
    previous_sessions = settings.chat_sessions
    previous_session_id = settings.session_id

    chat_sessions = _normalize_chat_sessions(settings)
    if chat_sessions:
        active_id = (previous_session_id or "").strip()
        if not any(session["id"] == active_id for session in chat_sessions):
            # `session_id` is missing or points nowhere - open the first chat
            # that is actually stored instead of inventing a new one.
            active_id = chat_sessions[0]["id"]
    else:
        # No chat was ever saved (first run): the only case that mints an id.
        active_id = (previous_session_id or "").strip() or str(uuid.uuid4())
        chat_sessions = [{"id": active_id, "name": _next_chat_name(chat_sessions)}]

    if chat_sessions != previous_sessions or active_id != previous_session_id:
        settings.chat_sessions = chat_sessions
        settings.session_id = active_id
        save_settings(settings)
    return active_id, chat_sessions
