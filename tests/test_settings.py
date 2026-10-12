import json
import pytest
from state.settings import Settings, load_settings, save_settings
import state.settings

@pytest.fixture(autouse=True)
def mock_settings_file(tmp_path, monkeypatch):
    mock_file = tmp_path / "settings.json"
    monkeypatch.setattr(state.settings, "SETTINGS_FILE", mock_file)
    return mock_file

def test_load_default_settings():
    settings = load_settings()
    assert settings.base_url == "http://localhost:8080"
    assert settings.api_key is None

def test_save_and_load_settings(mock_settings_file):
    settings = Settings(base_url="http://example.com", api_key="secret")
    save_settings(settings)

    assert mock_settings_file.exists()

    loaded_settings = load_settings()
    assert loaded_settings.base_url == "http://example.com"
    assert loaded_settings.api_key == "secret"

def test_load_settings_invalid_json(mock_settings_file):
    mock_settings_file.write_text("invalid json")

    settings = load_settings()
    assert settings.base_url == "http://localhost:8080"
    assert settings.api_key is None
