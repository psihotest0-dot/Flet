"""The chat sessions kept in the settings: names, ids and repair of stale entries (P33).

``Settings.chat_sessions`` is what the file holds: a list of dicts that may lack an id
or a name (a hand-edited or an old file), kept as it is so that reading the file never
costs the user their ``session_id``. Everything here works on the clean form,
:class:`ChatSession`, and writes it back with :func:`store_chat_sessions`. These are
functions over ``Settings`` (the only side effect is ``save_settings`` in
``ensure_chat_session``); the chat screen only calls them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from state.settings import Settings, save_settings

CHAT_SESSION_PREFIX = "Чат"
# The longest name of a chat: the dropdown that shows it is 200 px wide.
MAX_CHAT_NAME_CHARS = 40
# The conversations the bot writes to are ``tg-<chat id>``; the app opens them as chats
# of its own, so one conversation is shared with Telegram.
PAIRED_SESSION_PREFIX = "tg-"
# The backend's longest session id, and the most Telegram chats the app takes in: a
# list longer than a person has chats is a fault, not a reason to fill the dropdown.
MAX_SESSION_ID_CHARS = 128
MAX_PAIRED_SESSIONS = 10
DEFAULT_PAIRED_NAME = "Telegram"


@dataclass(frozen=True)
class ChatSession:
    """A chat of the dropdown: the id of its conversation on the backend, and its name."""

    id: str
    name: str

    def entry(self) -> dict[str, str]:
        """The form it has in ``settings.json``."""
        return {"id": self.id, "name": self.name}


def next_chat_name(chats: list[ChatSession]) -> str:
    """Return the first unused "Чат N" label for a brand new chat."""
    used = {chat.name for chat in chats}
    index = 1
    while f"{CHAT_SESSION_PREFIX} {index}" in used:
        index += 1
    return f"{CHAT_SESSION_PREFIX} {index}"


def unique_chat_name(name: str, chats: list[ChatSession]) -> str:
    """Return a label that no other chat session is using yet."""
    name = (name or "").strip()
    if not name:
        return next_chat_name(chats)

    used = {chat.name for chat in chats}
    if name not in used:
        return name

    index = 2
    while f"{name} ({index})" in used:
        index += 1
    return f"{name} ({index})"


def normalize_chat_sessions(settings: Settings) -> list[ChatSession]:
    """Return ``settings.chat_sessions`` as clean chats, each with an id and a name.

    Older builds persisted bare names and a broken write can leave entries
    without an id. Such entries are re-bound to ``settings.session_id`` when
    that id is not listed yet, so an already existing chat stays reachable
    instead of being replaced by a freshly generated session id.
    """
    chats: list[ChatSession] = []
    used_ids: set[str] = set()
    nameless_entries: list[str] = []

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
            nameless_entries.append(name)
            continue
        if session_id in used_ids:
            continue

        used_ids.add(session_id)
        chats.append(ChatSession(session_id, unique_chat_name(name, chats)))

    active_id = (settings.session_id or "").strip()
    if active_id and active_id not in used_ids:
        # The active session predates `chat_sessions` (or lost its entry):
        # keep it and reuse the name of a legacy entry when there is one.
        name = nameless_entries.pop(0) if nameless_entries else ""
        chats.append(ChatSession(active_id, unique_chat_name(name, chats)))

    return chats


def store_chat_sessions(
    settings: Settings, chats: list[ChatSession], active_id: str
) -> None:
    """Make ``chats`` the chats of the settings and ``active_id`` the open one.

    The settings are changed in memory only; saving them is the caller's step.
    """
    settings.chat_sessions = [chat.entry() for chat in chats]
    settings.session_id = active_id


def resolve_session_id(chats: list[ChatSession], selected: str | None) -> str | None:
    """Map a dropdown value (session id or chat name) to a saved session id."""
    value = (selected or "").strip()
    if not value:
        return None

    for chat in chats:
        if chat.id == value:
            return chat.id
    for chat in chats:
        if chat.name.casefold() == value.casefold():
            return chat.id
    return None


def rename_chat_session(settings: Settings, session_id: str, name: str) -> str | None:
    """Give the chat ``session_id`` a new name; return the name it got.

    Only the name changes: the id is the key to the conversation on the backend, and
    the chat keeps its place in the list and the model it is sent to. Blanks are
    collapsed and the name is cut to ``MAX_CHAT_NAME_CHARS``; a name another chat
    already has gets " (2)" (the chat's own name does not count, so keeping it is fine).
    Returns ``None`` - and changes nothing - for a blank name or an unknown chat.
    """
    cleaned = " ".join((name or "").split())[:MAX_CHAT_NAME_CHARS].strip()
    chats = normalize_chat_sessions(settings)
    index = next((i for i, chat in enumerate(chats) if chat.id == session_id), None)
    if not cleaned or index is None:
        return None

    others = chats[:index] + chats[index + 1 :]
    chats[index] = ChatSession(session_id, unique_chat_name(cleaned, others))
    store_chat_sessions(
        settings, chats, (settings.session_id or "").strip() or session_id
    )
    return chats[index].name


def restart_chat_session(settings: Settings, session_id: str) -> str | None:
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
    chats = normalize_chat_sessions(settings)
    index = next((i for i, chat in enumerate(chats) if chat.id == session_id), None)
    if index is None:
        return None

    new_id = str(uuid.uuid4())
    chats[index] = ChatSession(new_id, chats[index].name)
    store_chat_sessions(settings, chats, new_id)
    # The chat keeps its place in the list, so it keeps the model it was sent to.
    mode = settings.cloud_modes.pop(session_id, None)
    if mode is not None:
        settings.cloud_modes[new_id] = mode
    return new_id


def is_paired_session(session_id: str | None) -> bool:
    """Whether ``session_id`` is a conversation shared with Telegram (``tg-<chat id>``)."""
    return bool(session_id) and str(session_id).startswith(PAIRED_SESSION_PREFIX)


def _is_usable_paired_id(session_id: object) -> bool:
    """A Telegram session id the app may add: ``tg-...``, in the backend's length, no control characters."""
    return (
        isinstance(session_id, str)
        and is_paired_session(session_id)
        and len(session_id) <= MAX_SESSION_ID_CHARS
        and session_id.isprintable()
    )


def ensure_paired_sessions(
    settings: Settings, paired: list[tuple[str, str]]
) -> list[ChatSession]:
    """Add the Telegram conversations the settings lack; return the chats added.

    ``paired`` is ``(session id, name)`` as the backend lists them. A chat that is
    already there is left alone (its name may be the user's own), nothing is ever
    taken away - a conversation that disappears from the list is not deleted from
    the app - and the open chat stays the open chat. An id that is not a Telegram
    session, a duplicate, and whatever is past ``MAX_PAIRED_SESSIONS`` is skipped.
    The settings are changed in memory only, and only when something is added; saving
    is the caller's step.
    """
    chats = normalize_chat_sessions(settings)
    known = {chat.id for chat in chats}
    added: list[ChatSession] = []
    for session_id, name in paired:
        if len(added) >= MAX_PAIRED_SESSIONS:
            break
        if not _is_usable_paired_id(session_id) or session_id in known:
            continue
        cleaned = " ".join((name or "").split())[:MAX_CHAT_NAME_CHARS].strip()
        chat = ChatSession(
            session_id, unique_chat_name(cleaned or DEFAULT_PAIRED_NAME, chats)
        )
        chats.append(chat)
        added.append(chat)
        known.add(session_id)

    if added:
        active_id = (settings.session_id or "").strip() or chats[0].id
        store_chat_sessions(settings, chats, active_id)
    return added


def clear_chat_session(settings: Settings, session_id: str) -> str | None:
    """Empty a chat that keeps its id: the same chat, the same conversation on the backend.

    This is the local half of deleting a conversation shared with Telegram. The id is
    the key to both channels, so unlike ``restart_chat_session`` it does not change; a
    new one would cut the app off from the Telegram conversation. Nothing in the
    settings changes. Returns ``session_id``, or ``None`` when it is not in the list.
    """
    chats = normalize_chat_sessions(settings)
    if not any(chat.id == session_id for chat in chats):
        return None
    return session_id


def reset_chat_session(settings: Settings, session_id: str) -> str | None:
    """The id the chat has after its conversation was deleted on the backend.

    A conversation shared with Telegram keeps its id (:func:`clear_chat_session`); any
    other chat starts over under a new one (:func:`restart_chat_session`). ``None``:
    the chat is not in the list, nothing was changed.
    """
    if is_paired_session(session_id):
        return clear_chat_session(settings, session_id)
    return restart_chat_session(settings, session_id)


def ensure_chat_session(settings: Settings) -> tuple[str, list[ChatSession]]:
    """Resolve the session the chat view must open and repair stale settings.

    A saved ``chat_sessions`` entry always wins over generating an id: a new
    UUID is created only when the settings hold no chat at all.
    """
    previous_sessions = settings.chat_sessions
    previous_session_id = settings.session_id

    chats = normalize_chat_sessions(settings)
    if chats:
        active_id = (previous_session_id or "").strip()
        if not any(chat.id == active_id for chat in chats):
            # `session_id` is missing or points nowhere - open the first chat
            # that is actually stored instead of inventing a new one.
            active_id = chats[0].id
    else:
        # No chat was ever saved (first run): the only case that mints an id.
        active_id = (previous_session_id or "").strip() or str(uuid.uuid4())
        chats = [ChatSession(active_id, next_chat_name(chats))]

    if [chat.entry() for chat in chats] != previous_sessions or (
        active_id != previous_session_id
    ):
        store_chat_sessions(settings, chats, active_id)
        save_settings(settings)
    return active_id, chats
