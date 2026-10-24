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
