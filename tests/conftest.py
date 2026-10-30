import pytest

import state.settings as settings_module


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Never touch the real settings.json while views are constructed.

    ``ChatView.__init__`` reads and writes the settings file, so without this the
    tests would depend on (and mutate) whatever is in the working directory.
    """
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(settings_module, "SETTINGS_FILE", settings_file)
    return settings_file
