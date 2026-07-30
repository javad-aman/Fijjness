"""Loads settings from .env with sensible defaults."""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

GOALS_PATH = BASE_DIR / "config" / "goals.yaml"


def _load_goals() -> dict:
    with open(GOALS_PATH, "r") as f:
        return yaml.safe_load(f)


GOALS = _load_goals()

GARMIN_EMAIL = os.getenv("GARMIN_EMAIL")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD")

TOKEN_DIR = Path(os.getenv("GARMIN_TOKEN_DIR", ".garmin_tokens"))
if not TOKEN_DIR.is_absolute():
    TOKEN_DIR = BASE_DIR / TOKEN_DIR

DB_PATH = Path(os.getenv("GARMIN_DB_PATH", "data/garmin.db"))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH

BACKFILL_DAYS = int(os.getenv("GARMIN_BACKFILL_DAYS", "365"))

# Delay between per-date API calls during a run, to stay rate-limit friendly.
REQUEST_DELAY_SECONDS = float(os.getenv("GARMIN_REQUEST_DELAY_SECONDS", "0.6"))

# Optional cloud DB (Turso/libSQL). When unset, db.py uses the local SQLite
# file at DB_PATH above - nothing changes for local-only use.
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

# Coach email (Gmail SMTP + app password)
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
TO_EMAIL = os.getenv("TO_EMAIL", GMAIL_ADDRESS)
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "")

# Dashboard password gate (only enforced when both are set - local dev
# without them stays open)
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")

# Coach LLM (Phase 3 - declared now, unused until then)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
