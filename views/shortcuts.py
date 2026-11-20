"""The keyboard shortcuts of the chat: which key does what (pure, no Flet).

Only keys that do not depend on the layout of the keyboard, plus one that is told in
both alphabets: with a Bulgarian layout the key of the Latin ``N`` is the Cyrillic
``н`` (the same in the phonetic and in the BDS layout), and the app reads the
character, not the place of the key. The letters that mean something else already
(``Ctrl+C`` copies the selection) are left alone on purpose.

* ``Esc`` - stop the answer that is being written;
* ``F2`` - rename the open chat;
* ``Ctrl+N`` - a new chat.
"""

from __future__ import annotations

STOP = "stop"
RENAME = "rename"
NEW_CHAT = "new_chat"

# The Latin and the Cyrillic letter of the key N.
_NEW_CHAT_KEYS = frozenset({"n", "н"})


def shortcut_action(
    key: str,
    ctrl: bool = False,
    shift: bool = False,
    alt: bool = False,
    meta: bool = False,
) -> str | None:
    """What the key means in the chat (``STOP``, ``RENAME``, ``NEW_CHAT``), or ``None``.

    A key with a modifier it does not use (``Ctrl+Esc``, ``Shift+F2``, ``Alt+Ctrl+N``)
    means nothing: those belong to the system or to another program.
    """
    if alt or meta or shift:
        return None
    name = (key or "").casefold()
    if not ctrl:
        return {"escape": STOP, "f2": RENAME}.get(name)
    return NEW_CHAT if name in _NEW_CHAT_KEYS else None
