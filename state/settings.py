from __future__ import annotations

import json
from pathlib import Path

from pydantic_settings import BaseSettings

SETTINGS_FILE = Path("settings.json")

class Settings(BaseSettings):
    base_url: str = "http://localhost:8080"
    api_key: str | None = None

def load_settings() -> Settings:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Settings(**data)
        except (json.JSONDecodeError, ValueError, TypeError):
            return Settings()
    return Settings()

def save_settings(settings: Settings) -> None:
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        f.write(settings.model_dump_json(indent=4))
