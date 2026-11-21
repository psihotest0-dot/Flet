"""The Telegram conversations as chats of the app: added to the list, never lost (task 4)."""

from __future__ import annotations

import uuid

import pytest

from state.chat_sessions import (
    MAX_CHAT_NAME_CHARS,
    MAX_PAIRED_SESSIONS,
    ChatSession,
    clear_chat_session,
    ensure_paired_sessions,
    is_paired_session,
    normalize_chat_sessions,
    reset_chat_session,
)
from state.settings import Settings

CHAT_1 = "11111111-1111-1111-1111-111111111111"
CHAT_2 = "22222222-2222-2222-2222-222222222222"
TG = "tg-1607252727"


def settings_with(*chats: tuple[str, str], active: str | None = None) -> Settings:
    return Settings(
        user_id="user-1",
        session_id=active if active is not None else chats[0][0],
        chat_sessions=[{"id": chat_id, "name": name} for chat_id, name in chats],
    )


def ids(settings: Settings) -> list[str]:
    return [chat.id for chat in normalize_chat_sessions(settings)]


def names(settings: Settings) -> list[str]:
    return [chat.name for chat in normalize_chat_sessions(settings)]


# ------------------------------------------------------------ which sessions
@pytest.mark.parametrize(
    ("session_id", "paired"),
    [
        (TG, True),
        ("tg--1001234567890", True),
        (CHAT_1, False),
        ("xtg-1", False),
        ("TG-1", False),
        ("", False),
        (None, False),
    ],
)
def test_only_a_tg_session_is_shared_with_telegram(session_id, paired):
    assert is_paired_session(session_id) is paired


# ------------------------------------------------------------ adding them
def test_a_telegram_conversation_the_list_lacks_is_added():
    settings = settings_with((CHAT_1, "Чат 1"))

    added = ensure_paired_sessions(settings, [(TG, "Telegram")])

    assert added == [ChatSession(TG, "Telegram")]
    assert ids(settings) == [CHAT_1, TG]
    assert names(settings) == ["Чат 1", "Telegram"]


def test_the_open_chat_stays_the_open_chat():
    settings = settings_with((CHAT_1, "Чат 1"), (CHAT_2, "Чат 2"), active=CHAT_2)

    ensure_paired_sessions(settings, [(TG, "Telegram")])

    assert settings.session_id == CHAT_2


def test_a_conversation_that_is_already_there_is_left_as_it_is():
    settings = settings_with((CHAT_1, "Чат 1"), (TG, "Разговорите ми с Лили"))

    added = ensure_paired_sessions(settings, [(TG, "Telegram")])

    assert added == []
    assert names(settings) == ["Чат 1", "Разговорите ми с Лили"], (
        "the user's own name stays"
    )
    assert ids(settings).count(TG) == 1


def test_nothing_added_means_nothing_changed():
    settings = settings_with((CHAT_1, "Чат 1"), (TG, "Telegram"))
    before = settings.snapshot()

    ensure_paired_sessions(settings, [(TG, "Telegram")])
    ensure_paired_sessions(settings, [])

    assert settings == before


def test_nothing_added_leaves_even_an_untidy_file_as_it_is():
    # A file that an older build wrote: a bare name, an entry without an id. Saving it back
    # tidied would be a change nobody asked for; only an addition rewrites the list.
    settings = Settings(
        user_id="user-1",
        session_id=CHAT_1,
        chat_sessions=["Старо име", {"id": CHAT_1, "name": "Чат 1"}],
    )
    before = settings.snapshot()

    assert ensure_paired_sessions(settings, [(CHAT_1, "Чат 1"), ("nope", "x")]) == []

    assert settings == before


def test_a_second_conversation_is_added_next_to_the_first():
    settings = settings_with((CHAT_1, "Чат 1"))

    ensure_paired_sessions(settings, [(TG, "Telegram")])
    added = ensure_paired_sessions(
        settings, [(TG, "Telegram"), ("tg-42", "Telegram 2")]
    )

    assert added == [ChatSession("tg-42", "Telegram 2")]
    assert ids(settings) == [CHAT_1, TG, "tg-42"]


def test_a_conversation_that_leaves_the_backends_list_is_not_removed():
    settings = settings_with((CHAT_1, "Чат 1"), (TG, "Telegram"))

    ensure_paired_sessions(settings, [])
    ensure_paired_sessions(settings, [("tg-42", "Telegram 2")])

    assert TG in ids(settings), "the chat and its history are the user's"


def test_a_name_another_chat_has_gets_a_number():
    settings = settings_with((CHAT_1, "Telegram"))

    added = ensure_paired_sessions(settings, [(TG, "Telegram")])

    assert added == [ChatSession(TG, "Telegram (2)")]


def test_a_blank_name_is_Telegram_and_a_long_one_is_cut():
    settings = settings_with((CHAT_1, "Чат 1"))

    added = ensure_paired_sessions(
        settings, [("tg-1", "   "), ("tg-2", "  " + "я" * 100 + "  "), ("tg-3", "")]
    )

    assert [chat.name for chat in added] == [
        "Telegram",
        "я" * MAX_CHAT_NAME_CHARS,
        "Telegram (2)",
    ]


@pytest.mark.parametrize(
    "bad",
    [
        CHAT_2,
        "",
        "tg",
        "TG-1",
        "tg-" + "9" * 200,
        "tg-1\n2",
        "tg-1\x00",
        None,
        42,
    ],
)
def test_an_id_that_is_not_a_telegram_session_is_not_added(bad):
    settings = settings_with((CHAT_1, "Чат 1"))
    before = settings.snapshot()

    added = ensure_paired_sessions(settings, [(bad, "Telegram")])

    assert added == []
    assert settings == before


def test_the_same_conversation_twice_in_the_list_is_added_once():
    settings = settings_with((CHAT_1, "Чат 1"))

    added = ensure_paired_sessions(settings, [(TG, "Telegram"), (TG, "Telegram again")])

    assert [chat.id for chat in added] == [TG]


def test_no_more_than_the_limit_is_taken_in():
    settings = settings_with((CHAT_1, "Чат 1"))

    added = ensure_paired_sessions(
        settings, [(f"tg-{n}", f"Telegram {n}") for n in range(MAX_PAIRED_SESSIONS + 5)]
    )

    assert len(added) == MAX_PAIRED_SESSIONS
    assert len(ids(settings)) == 1 + MAX_PAIRED_SESSIONS


def test_a_settings_file_without_chats_gets_the_conversation_and_keeps_its_session():
    settings = Settings(user_id="user-1", session_id=CHAT_1, chat_sessions=[])

    ensure_paired_sessions(settings, [(TG, "Telegram")])

    assert settings.session_id == CHAT_1
    assert ids(settings) == [TG, CHAT_1] or ids(settings) == [CHAT_1, TG]
    assert set(ids(settings)) == {CHAT_1, TG}


# ------------------------------------------------------------ deleting one
def test_clearing_a_telegram_chat_keeps_its_id():
    settings = settings_with((CHAT_1, "Чат 1"), (TG, "Telegram"), active=TG)
    before = settings.snapshot()

    assert clear_chat_session(settings, TG) == TG
    assert settings == before, "nothing in the settings changes"


def test_clearing_a_chat_that_is_not_there_changes_nothing():
    settings = settings_with((CHAT_1, "Чат 1"))
    before = settings.snapshot()

    assert clear_chat_session(settings, TG) is None
    assert settings == before


def test_deleting_a_telegram_chat_keeps_its_id_and_its_model():
    settings = settings_with((CHAT_1, "Чат 1"), (TG, "Telegram"), active=TG)
    settings.cloud_modes[TG] = "cloud"

    assert reset_chat_session(settings, TG) == TG

    assert settings.session_id == TG
    assert settings.cloud_modes == {TG: "cloud"}
    assert ids(settings) == [CHAT_1, TG]


def test_deleting_an_ordinary_chat_gives_it_a_new_id():
    settings = settings_with((CHAT_1, "Чат 1"), (TG, "Telegram"), active=CHAT_1)
    settings.cloud_modes[CHAT_1] = "home"

    new_id = reset_chat_session(settings, CHAT_1)

    assert new_id is not None
    assert new_id != CHAT_1
    uuid.UUID(new_id)
    assert settings.session_id == new_id
    assert settings.cloud_modes == {new_id: "home"}
    assert ids(settings) == [new_id, TG]


def test_resetting_a_chat_that_is_not_there_changes_nothing():
    settings = settings_with((CHAT_1, "Чат 1"))
    before = settings.snapshot()

    assert reset_chat_session(settings, CHAT_2) is None
    assert reset_chat_session(settings, "tg-9") is None
    assert settings == before
