from __future__ import annotations

"""State management for application settings."""

import json
import logging
from pathlib import Path

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path("settings.json")

class Settings(BaseSettings):
    """Application settings, loaded from and saved to a local JSON file."""
    base_url: str = "http://localhost:8080"
    api_key: str | None = None

def load_settings() -> Settings:
    """Load settings from the local JSON file."""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Settings(**data)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.error(f"Failed to load settings from file: {e}")
            return Settings()
    return Settings()

def save_settings(settings: Settings) -> None:
    """Save settings to the local JSON file."""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            f.write(settings.model_dump_json(indent=4))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to save settings to file: {e}")
