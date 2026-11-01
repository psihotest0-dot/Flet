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
