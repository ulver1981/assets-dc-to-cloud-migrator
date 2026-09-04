"""
config.py – central configuration loader.

Reads credentials and endpoints from a .env file (or real environment).
All other scripts import from here instead of hardcoding values.
"""

import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    print("[config] python-dotenv not installed – run: pip install python-dotenv")
    sys.exit(1)

# Load .env from the project root (same folder as this file)
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)
else:
    print(f"[config] WARNING: .env file not found at {_ENV_FILE}. "
          "Copy .env.template -> .env and fill in your credentials.")

# ─────────────────────────────────────────────────────────────────────────────
# DC (source)
# ─────────────────────────────────────────────────────────────────────────────
DC_BASE_URL: str = os.getenv("DC_BASE_URL", "").rstrip("/")
DC_TOKEN: str = os.getenv("DC_TOKEN", "")

# ─────────────────────────────────────────────────────────────────────────────
# Cloud (target)
# ─────────────────────────────────────────────────────────────────────────────
CLOUD_BASE_URL: str = os.getenv("CLOUD_BASE_URL", "").rstrip("/")
CLOUD_WORKSPACE_ID: str = os.getenv("CLOUD_WORKSPACE_ID", "")
CLOUD_EMAIL: str = os.getenv("CLOUD_EMAIL", "")
CLOUD_API_TOKEN: str = os.getenv("CLOUD_API_TOKEN", "")

# Convenience: full Cloud API base for this workspace
CLOUD_API_BASE: str = f"{CLOUD_BASE_URL}/{CLOUD_WORKSPACE_ID}/v1"


def dc_headers() -> dict:
    """Return headers for Jira DC Insight API calls."""
    return {
        "Authorization": f"Bearer {DC_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def cloud_auth():
    """Return HTTPBasicAuth for Jira Cloud Assets API calls."""
    from requests.auth import HTTPBasicAuth
    return HTTPBasicAuth(CLOUD_EMAIL, CLOUD_API_TOKEN)


def cloud_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def validate():
    """Call at startup to catch missing config early."""
    required = {
        "DC_BASE_URL": DC_BASE_URL,
        "DC_TOKEN": DC_TOKEN,
        "CLOUD_BASE_URL": CLOUD_BASE_URL,
        "CLOUD_WORKSPACE_ID": CLOUD_WORKSPACE_ID,
        "CLOUD_EMAIL": CLOUD_EMAIL,
        "CLOUD_API_TOKEN": CLOUD_API_TOKEN,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"[config] ERROR: missing environment variables: {', '.join(missing)}")
        print(f"         Copy .env.template -> .env and fill in the values.")
        sys.exit(1)


if __name__ == "__main__":
    validate()
    print("Config OK")
    print(f"  DC_BASE_URL         = {DC_BASE_URL}")
    print(f"  CLOUD_BASE_URL      = {CLOUD_BASE_URL}")
    print(f"  CLOUD_WORKSPACE_ID  = {CLOUD_WORKSPACE_ID}")
    print(f"  CLOUD_EMAIL         = {CLOUD_EMAIL}")
