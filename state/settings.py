from __future__ import annotations

"""State management for application settings."""

import json
import logging
import os
import time
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path("settings.json")

class Settings(BaseModel):
    """Application settings, loaded from and saved to a local JSON file."""
    base_url: str = "http://localhost:8080"
    user_id: str | None = None
    session_id: str | None = None
    chat_sessions: list[dict] = []

def load_settings() -> Settings:
    """Load settings from the local JSON file."""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Settings(**data)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.error(f"Failed to load settings from file: {e}")
            backup_file = SETTINGS_FILE.with_name(f"settings.json.broken.{int(time.time())}")
            try:
                SETTINGS_FILE.rename(backup_file)
                logger.info(f"Corrupted settings file backed up to {backup_file}")
            except OSError as backup_error:
                logger.error(f"Failed to backup corrupted settings file: {backup_error}")
            return Settings()
    return Settings()

def save_settings(settings: Settings) -> bool:
    """Save settings to the local JSON file using atomic write."""
    temp_file = SETTINGS_FILE.with_name(f"settings.tmp.{int(time.time())}")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(settings.model_dump_json(indent=4))
        os.replace(temp_file, SETTINGS_FILE)
        return True
    except (OSError, ValueError) as e:
        logger.error(f"Failed to save settings to file: {e}")
        try:
            if temp_file.exists():
                temp_file.unlink()
        except OSError as unlink_error:
            logger.warning(f"Failed to remove temporary file {temp_file}: {unlink_error}")
        return False
