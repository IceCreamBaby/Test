"""Zentrale Pfade der Anwendung."""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(os.environ.get("PODCAST_ANIMATOR_DATA", APP_DIR / "data")).resolve()
PROJECTS_DIR = DATA_DIR / "projects"
MODELS_DIR = DATA_DIR / "models"
PROFILES_DIR = DATA_DIR / "voice_profiles"
USER_CHARACTERS_DIR = DATA_DIR / "characters"
SETTINGS_FILE = DATA_DIR / "settings.json"

BUILTIN_CHARACTERS_DIR = APP_DIR / "characters"
FONTS_DIR = APP_DIR / "assets" / "fonts"
WEB_DIR = PACKAGE_DIR / "web"


def ensure_dirs() -> None:
    for d in (DATA_DIR, PROJECTS_DIR, MODELS_DIR, PROFILES_DIR, USER_CHARACTERS_DIR):
        d.mkdir(parents=True, exist_ok=True)
