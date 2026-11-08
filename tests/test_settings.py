import json

import pytest

import state.settings
from state.settings import Settings, load_settings, save_settings


@pytest.fixture(autouse=True)
def mock_settings_file(tmp_path, monkeypatch):
    mock_file = tmp_path / "settings.json"
    monkeypatch.setattr(state.settings, "SETTINGS_FILE", mock_file)
    return mock_file

def test_load_default_settings():
    settings = load_settings()
    assert settings.base_url == "http://localhost:8080"
    assert settings.user_id is None


def test_save_settings_os_error(mock_settings_file, monkeypatch):
    import os
    def mock_replace(*args, **kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr(os, "replace", mock_replace)

    settings = Settings(base_url="http://example.com")
    result = save_settings(settings)

    assert result is False

def test_save_and_load_settings(mock_settings_file):
    settings = Settings(base_url="http://example.com", user_id="test-uuid")
    save_settings(settings)

    assert mock_settings_file.exists()

    loaded_settings = load_settings()
    assert loaded_settings.base_url == "http://example.com"
    assert loaded_settings.user_id == "test-uuid"

def test_load_settings_invalid_json(mock_settings_file):
    mock_settings_file.write_text("invalid json")

    settings = load_settings()
    assert settings.base_url == "http://localhost:8080"
    assert settings.user_id is None

    # Verify backup was created
    backups = list(mock_settings_file.parent.glob("settings.json.broken.*"))
    assert len(backups) == 1
    assert mock_settings_file.exists() is False


# ------------------------------------------------- hand edited / legacy files
ORIGINAL_USER_ID = "123e4567-e89b-12d3-a456-426614174000"
ORIGINAL_SESSION_ID = "763ebf47-618b-4410-bb08-7cce59fdff02"


def test_hand_edited_trailing_comma_keeps_the_stored_ids(mock_settings_file):
    # A trailing comma used to raise JSONDecodeError, which renamed the file to
    # settings.json.broken.* and returned defaults - losing user_id/session_id.
    mock_settings_file.write_text(
        "{\n"
        '    "base_url": "http://localhost:8080",\n'
        f'    "user_id": "{ORIGINAL_USER_ID}",\n'
        f'    "session_id": "{ORIGINAL_SESSION_ID}",\n'
        '    "chat_sessions": [\n'
        f'        {{"id": "{ORIGINAL_SESSION_ID}", "name": "Чат 1"}},\n'
        "    ]\n"
        "}\n",
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.user_id == ORIGINAL_USER_ID
    assert settings.session_id == ORIGINAL_SESSION_ID
    assert settings.chat_sessions == [{"id": ORIGINAL_SESSION_ID, "name": "Чат 1"}]
    # The file is repaired in place instead of being moved aside.
    assert list(mock_settings_file.parent.glob("settings.json.broken.*")) == []
    repaired = json.loads(mock_settings_file.read_text(encoding="utf-8"))
    assert repaired["user_id"] == ORIGINAL_USER_ID
    assert repaired["session_id"] == ORIGINAL_SESSION_ID


def test_utf8_bom_and_comments_keep_the_stored_ids(mock_settings_file):
    mock_settings_file.write_text(
        "// hand edited settings\n"
        "{\n"
        f'    "user_id": "{ORIGINAL_USER_ID}",\n'
        f'    "session_id": "{ORIGINAL_SESSION_ID}", /* active chat */\n'
        '    "base_url": "http://localhost:8080"\n'
        "}\n",
        encoding="utf-8-sig",
    )

    settings = load_settings()

    assert settings.user_id == ORIGINAL_USER_ID
    assert settings.session_id == ORIGINAL_SESSION_ID
    # A URL must not be mistaken for a // comment.
    assert settings.base_url == "http://localhost:8080"
    assert list(mock_settings_file.parent.glob("settings.json.broken.*")) == []


def test_legacy_string_chat_sessions_do_not_reset_the_ids(mock_settings_file):
    # ``chat_sessions`` used to be validated as list[dict]; a bare name made the
    # whole document invalid, so the ids were dropped along with it.
    mock_settings_file.write_text(
        json.dumps(
            {
                "user_id": ORIGINAL_USER_ID,
                "session_id": ORIGINAL_SESSION_ID,
                "chat_sessions": ["Чат 1"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.user_id == ORIGINAL_USER_ID
    assert settings.session_id == ORIGINAL_SESSION_ID
    assert settings.chat_sessions == [{"name": "Чат 1"}]
    assert list(mock_settings_file.parent.glob("settings.json.broken.*")) == []


def test_unusable_chat_session_entries_are_dropped_without_losing_ids(
    mock_settings_file,
):
    mock_settings_file.write_text(
        json.dumps(
            {
                "user_id": ORIGINAL_USER_ID,
                "session_id": ORIGINAL_SESSION_ID,
                "chat_sessions": [
                    None,
                    42,
                    {"id": ORIGINAL_SESSION_ID, "name": "Чат 1"},
                    {"name": "   "},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.user_id == ORIGINAL_USER_ID
    assert settings.chat_sessions == [{"id": ORIGINAL_SESSION_ID, "name": "Чат 1"}]


def test_truncated_file_still_recovers_the_stored_ids(mock_settings_file):
    # Even a document that cannot be repaired keeps its identity: the ids are
    # salvaged, the original bytes are backed up and the repaired file is saved.
    mock_settings_file.write_text(
        "{\n"
        f'    "user_id": "{ORIGINAL_USER_ID}",\n'
        f'    "session_id": "{ORIGINAL_SESSION_ID}"\n',
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.user_id == ORIGINAL_USER_ID
    assert settings.session_id == ORIGINAL_SESSION_ID
    backups = list(mock_settings_file.parent.glob("settings.json.broken.*"))
    assert len(backups) == 1
    repaired = json.loads(mock_settings_file.read_text(encoding="utf-8"))
    assert repaired["user_id"] == ORIGINAL_USER_ID
    assert repaired["session_id"] == ORIGINAL_SESSION_ID


# ----------------------------------------------------------------- display_name
def test_display_name_defaults_to_you():
    assert Settings().display_name == "Ти"
    assert load_settings().display_name == "Ти"


def test_display_name_is_saved_and_loaded(mock_settings_file):
    save_settings(Settings(display_name="Иван"))

    assert load_settings().display_name == "Иван"
    assert json.loads(mock_settings_file.read_text(encoding="utf-8"))["display_name"] == "Иван"


@pytest.mark.parametrize("raw", ["", "   ", None, True, [], {"a": 1}])
def test_blank_or_unusable_display_name_falls_back_to_the_default(raw):
    assert Settings(display_name=raw).display_name == "Ти"


def test_display_name_is_trimmed_and_limited():
    assert Settings(display_name="  Иван  ").display_name == "Иван"
    assert Settings(display_name="я" * 100).display_name == "я" * 40


def test_old_settings_file_without_a_display_name_still_loads(mock_settings_file):
    mock_settings_file.write_text(
        json.dumps({"base_url": "http://example.com", "user_id": "u-1"}),
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.display_name == "Ти"
    assert settings.user_id == "u-1"


def test_unusable_display_name_in_the_file_does_not_cost_the_identity(
    mock_settings_file,
):
    mock_settings_file.write_text(
        json.dumps({"user_id": "u-1", "session_id": "s-1", "display_name": 42}),
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.user_id == "u-1"
    assert settings.session_id == "s-1"
    assert settings.display_name == "42"


# ------------------------------------------------------- durable, serial writes
def test_save_forces_the_text_to_the_disk_before_replacing_the_file(
    mock_settings_file, monkeypatch
):
    import os

    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(
        os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1]
    )

    assert save_settings(Settings(user_id=ORIGINAL_USER_ID)) is True

    # Without the fsync a power cut right after the replace can leave an empty
    # or zeroed settings.json - and with it a new user_id at the next start.
    assert events == ["fsync", "replace"]
    assert load_settings().user_id == ORIGINAL_USER_ID


def test_saves_from_two_threads_at_once_all_succeed_and_leave_a_whole_file(
    mock_settings_file,
):
    import threading

    # Flet runs synchronous handlers in threads: the chat and the settings dialog
    # can save in the same second. The temporary file used to be named after the
    # second, so two such saves wrote into one file and one of them failed.
    saves_per_thread = 40
    start = threading.Barrier(2)
    results: list[bool] = []

    def save_many(name: str) -> None:
        start.wait()
        for _ in range(saves_per_thread):
            results.append(save_settings(Settings(user_id=ORIGINAL_USER_ID, display_name=name)))

    threads = [threading.Thread(target=save_many, args=(name,)) for name in ("Иван", "Мария")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [True] * (2 * saves_per_thread)
    saved = json.loads(mock_settings_file.read_text(encoding="utf-8"))
    assert saved["user_id"] == ORIGINAL_USER_ID
    assert saved["display_name"] in ("Иван", "Мария")
    assert list(mock_settings_file.parent.glob("settings.tmp.*")) == []


def test_writes_go_one_at_a_time(mock_settings_file, monkeypatch):
    import os
    import threading
    import time

    real_replace = os.replace
    lock = threading.Lock()
    inside = {"now": 0, "most": 0}

    def slow_replace(src, dst):
        with lock:
            inside["now"] += 1
            inside["most"] = max(inside["most"], inside["now"])
        time.sleep(0.005)
        with lock:
            inside["now"] -= 1
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", slow_replace)
    start = threading.Barrier(2)

    def save_many() -> None:
        start.wait()
        for _ in range(10):
            save_settings(Settings(user_id=ORIGINAL_USER_ID))

    threads = [threading.Thread(target=save_many) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert inside["most"] == 1


def test_every_save_uses_a_temporary_file_of_its_own(mock_settings_file, monkeypatch):
    import os

    # Also when two windows of the app save at once (a lock does not reach
    # another process): names made from the current second used to collide.
    used = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: (used.append(src), real_replace(src, dst))[1])

    for _ in range(5):
        save_settings(Settings(user_id=ORIGINAL_USER_ID))

    assert len(set(used)) == 5


# ------------------------------------------- settings.json.bak, the last good copy
def _remember(settings: Settings) -> None:
    """Save ``settings`` and read them back once - that is when the copy is taken."""
    save_settings(settings)
    load_settings()


@pytest.mark.parametrize(
    "damage",
    [
        b"",
        b"\x00" * 512,
        b"\xff\xfe not utf-8 \xc3",
        # Only the address can still be read out of it, not the user_id.
        b'{\n    "base_url": "http://localhost:8080",\n    "user_id": "123e',
    ],
    ids=["empty", "zeroed", "not-utf8", "cut-before-the-user-id"],
)
def test_a_wrecked_file_gets_the_identity_back_from_the_last_good_copy(
    mock_settings_file, damage, caplog
):
    _remember(
        Settings(
            user_id=ORIGINAL_USER_ID,
            session_id=ORIGINAL_SESSION_ID,
            chat_sessions=[{"id": ORIGINAL_SESSION_ID, "name": "Чат 1"}],
            display_name="Иван",
        )
    )
    # What NTFS can leave after a power cut, or a file that is not text at all.
    mock_settings_file.write_bytes(damage)

    with caplog.at_level("WARNING", logger="state.settings"):
        settings = load_settings()

    assert f"restored user_id={ORIGINAL_USER_ID[:8]}" in caplog.text
    assert ORIGINAL_USER_ID not in caplog.text

    assert settings.user_id == ORIGINAL_USER_ID
    assert settings.session_id == ORIGINAL_SESSION_ID
    assert settings.chat_sessions == [{"id": ORIGINAL_SESSION_ID, "name": "Чат 1"}]
    assert settings.display_name == "Иван"
    # The restored settings are saved, the wrecked file is kept aside.
    assert json.loads(mock_settings_file.read_text(encoding="utf-8"))["user_id"] == ORIGINAL_USER_ID
    assert len(list(mock_settings_file.parent.glob("settings.json.broken.*"))) == 1


def test_ids_still_readable_in_the_broken_file_win_over_the_copy(mock_settings_file):
    _remember(Settings(user_id="00000000-0000-0000-0000-000000000000"))
    # The broken file is newer than the copy, and its ids can still be read.
    mock_settings_file.write_text(
        f'{{"user_id": "{ORIGINAL_USER_ID}", "session_id": "{ORIGINAL_SESSION_ID}", broken',
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.user_id == ORIGINAL_USER_ID
    assert settings.session_id == ORIGINAL_SESSION_ID


def test_without_a_copy_a_wrecked_file_still_starts_with_defaults(mock_settings_file):
    mock_settings_file.write_bytes(b"\x00" * 64)

    settings = load_settings()

    assert settings.user_id is None
    assert len(list(mock_settings_file.parent.glob("settings.json.broken.*"))) == 1


def test_settings_without_a_user_id_never_replace_the_copy(mock_settings_file):
    backup = mock_settings_file.with_name("settings.json.bak")
    _remember(Settings(user_id=ORIGINAL_USER_ID))
    assert json.loads(backup.read_text(encoding="utf-8"))["user_id"] == ORIGINAL_USER_ID

    # A file that lost its user_id is read without trouble - but it is not worth
    # keeping, and it must not push the identity out of the copy.
    _remember(Settings(base_url="http://127.0.0.1:8080"))

    assert json.loads(backup.read_text(encoding="utf-8"))["user_id"] == ORIGINAL_USER_ID


def test_the_copy_follows_the_settings_that_were_read(mock_settings_file):
    backup = mock_settings_file.with_name("settings.json.bak")
    _remember(Settings(user_id=ORIGINAL_USER_ID, session_id="s-1"))
    _remember(Settings(user_id=ORIGINAL_USER_ID, session_id="s-2"))

    assert json.loads(backup.read_text(encoding="utf-8"))["session_id"] == "s-2"


def test_recovering_broken_settings_logs_only_the_start_of_the_ids(isolated_settings, caplog):
    user_id = "123e4567-e89b-12d3-a456-426614174000"
    session_id = "abcdef12-3456-7890-abcd-ef1234567890"
    # Unreadable as JSON, but the ids can still be read out of it.
    isolated_settings.write_text(
        f'{{"user_id": "{user_id}", "session_id": "{session_id}", broken', encoding="utf-8"
    )

    with caplog.at_level("WARNING", logger="state.settings"):
        settings = load_settings()

    assert settings.user_id == user_id
    assert settings.session_id == session_id
    assert user_id[:8] in caplog.text
    assert user_id not in caplog.text
    assert session_id not in caplog.text
