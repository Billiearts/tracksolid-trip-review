"""
config.py — central configuration loaded from .env.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(_PROJECT_ROOT / ".env")


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


# --- Tracksolid Pro credentials ---
APP_KEY      = _require("TRACKSOLID_APP_KEY")
APP_SECRET   = _require("TRACKSOLID_APP_SECRET")
USER_ID      = _require("TRACKSOLID_USER_ID")
USER_PWD_MD5 = _require("TRACKSOLID_USER_PWD_MD5")

# --- Paths ---
TEMPLATE_PATH = Path(os.environ.get(
    "TRACKSOLID_TEMPLATE_PATH",
    str(_PROJECT_ROOT / "Trip review template.xlsx"),
))
OUTPUT_DIR = Path(os.environ.get(
    "TRACKSOLID_OUTPUT_DIR",
    str(_PROJECT_ROOT / "reports"),
))

# --- Flask ---
FLASK_SECRET = os.environ.get("FLASK_SECRET", "dev-only-change-me")
FLASK_DEBUG  = os.environ.get("FLASK_DEBUG", "0") == "1"

# --- Supabase ---
SUPABASE_URL              = _require("SUPABASE_URL")
SUPABASE_ANON_KEY         = _require("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = _require("SUPABASE_SERVICE_ROLE_KEY")