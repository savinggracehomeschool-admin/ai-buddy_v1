"""Loads environment variables from .env and exposes typed settings.

Importing this module triggers a one-time read of .env via python-dotenv.
Anything else in the codebase should `from sgeg_nudge.config import settings`
rather than calling os.getenv directly — keeps secret access centralised.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required env var {name}. "
            f"Check {PROJECT_ROOT / '.env'} against .env.example."
        )
    return value


def _maybe_int(value: str | None) -> int | None:
    if not value or not value.strip():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name, "").strip().lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return default


@dataclass(frozen=True)
class Settings:
    # Canvas REST API
    canvas_base_url: str
    canvas_api_token: str
    anthropic_api_key: str

    # Admin dashboard
    admin_user: str
    admin_password: str
    admin_canvas_user_id: int | None
    curriculum_team_canvas_user_id: int | None

    # LTI 1.3
    lti_client_id: str          # Canvas Developer Key client ID
    lti_deployment_id: str      # Canvas LTI deployment ID
    lti_secret_key: str         # Signing key for session cookies
    lti_dev_mode: bool          # When True, /lti/dev creates mock sessions


# In LTI dev mode Canvas creds may be absent (no outbound Canvas calls needed
# for a pure mock launch). In production both are required.
_dev_mode = _bool("LTI_DEV_MODE", default=True)
_canvas_base = os.getenv("CANVAS_BASE_URL", "").rstrip("/")
_canvas_token = os.getenv("CANVAS_API_TOKEN", "")

if not _dev_mode:
    if not _canvas_base:
        raise RuntimeError("Missing required env var CANVAS_BASE_URL.")
    if not _canvas_token:
        raise RuntimeError("Missing required env var CANVAS_API_TOKEN.")

settings = Settings(
    canvas_base_url=_canvas_base,
    canvas_api_token=_canvas_token,
    anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
    admin_user=os.getenv("ADMIN_USER", "admin"),
    admin_password=os.getenv("ADMIN_PASSWORD", "changeme"),
    admin_canvas_user_id=_maybe_int(os.getenv("ADMIN_CANVAS_USER_ID")),
    curriculum_team_canvas_user_id=(
        _maybe_int(os.getenv("CURRICULUM_TEAM_CANVAS_USER_ID"))
        or _maybe_int(os.getenv("ADMIN_CANVAS_USER_ID"))
    ),
    lti_client_id=os.getenv("LTI_CLIENT_ID", ""),
    lti_deployment_id=os.getenv("LTI_DEPLOYMENT_ID", ""),
    lti_secret_key=os.getenv("LTI_SECRET_KEY", "dev-secret-change-me"),
    lti_dev_mode=_dev_mode,
)
