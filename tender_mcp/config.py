"""
tender_mcp/config.py  –  Central configuration and credentials management
========================================================================
Ensures:
  - .env is loaded ONLY relative to project root, not current working directory.
  - HMEL credentials are read strictly from the local .env file.
  - Account lockout protection (.hmel_locked) is tracked and checked.
  - No credentials or full cookies are ever exposed or logged.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
LOCK_FILE = PROJECT_ROOT / ".hmel_locked"
LOG_FILE = PROJECT_ROOT / "refresh.log"
KEYWORDS_PATH = PROJECT_ROOT / "keywords.json"
DEFAULT_MIN_HOURS_TO_CLOSE: float = float(os.environ.get("MIN_HOURS_TO_CLOSE", "24.0"))

# Load environment variables relative to project root on import
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    logger.warning(".env file not found at %s", ENV_PATH)


def mask_credential(val: str) -> str:
    """Mask sensitive string showing only first and last characters."""
    val = (val or "").strip()
    if not val:
        return ""
    if len(val) <= 4:
        return "*" * len(val)
    return val[:2] + "*" * (len(val) - 4) + val[-2:]


def set_hmel_credentials(username: str, password: str) -> None:
    """
    Update HMEL_USERNAME and HMEL_PASSWORD in .env and current os.environ.
    Only updates fields if a non-empty string is provided.
    """
    username = username.strip()
    password = password.strip()

    if username:
        os.environ["HMEL_USERNAME"] = username
    if password:
        os.environ["HMEL_PASSWORD"] = password

    if not ENV_PATH.exists():
        ENV_PATH.write_text(f"HMEL_USERNAME={username}\nHMEL_PASSWORD={password}\n", encoding="utf-8")
        return

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    new_lines = []
    found_user = False
    found_pass = False

    for line in lines:
        if line.strip().startswith("HMEL_USERNAME="):
            if username:
                new_lines.append(f"HMEL_USERNAME={username}")
            else:
                new_lines.append(line)
            found_user = True
        elif line.strip().startswith("HMEL_PASSWORD="):
            if password:
                new_lines.append(f"HMEL_PASSWORD={password}")
            else:
                new_lines.append(line)
            found_pass = True
        else:
            new_lines.append(line)

    if username and not found_user:
        new_lines.insert(0, f"HMEL_USERNAME={username}")
    if password and not found_pass:
        insert_idx = 1 if (found_user or username) else 0
        new_lines.insert(insert_idx, f"HMEL_PASSWORD={password}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def get_hmel_credentials() -> tuple[str, str]:
    """
    Read HMEL_USERNAME and HMEL_PASSWORD strictly from local .env at project root.
    Returns (username, password). Returns ('', '') if unconfigured or default placeholder.
    """
    if not ENV_PATH.exists():
        return "", ""

    try:
        env_dict = dotenv_values(ENV_PATH)
        username = (env_dict.get("HMEL_USERNAME") or "").strip()
        password = (env_dict.get("HMEL_PASSWORD") or "").strip()

        # Reject template placeholders
        if username in ("", "your_username_here", "your_username"):
            return "", ""
        if password in ("", "your_password_here", "your_password"):
            return "", ""

        return username, password
    except Exception as exc:
        logger.error("Error reading credentials from %s: %s", ENV_PATH, exc)
        return "", ""


def is_hmel_locked() -> tuple[bool, str]:
    """
    Check if HMEL lockout protection is active.
    Returns (is_locked, reason_str).
    """
    if not LOCK_FILE.exists():
        return False, ""

    try:
        content = LOCK_FILE.read_text(encoding="utf-8").strip()
        data = json.loads(content)
        reason = data.get("reason", "Unknown reason")
        locked_at = data.get("locked_at", "Unknown time")
        return True, f"Locked at {locked_at}: {reason}"
    except Exception:
        # Fallback if text only
        return True, LOCK_FILE.read_text(encoding="utf-8").strip()


def lock_hmel(reason: str) -> None:
    """
    Write .hmel_locked to prevent subsequent login attempts until manually cleared.
    """
    locked_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "locked_at": locked_at,
        "reason": reason,
    }
    try:
        LOCK_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.warning(
            "hmel: Lockout file created at %s: %s", LOCK_FILE, reason
        )
    except Exception as exc:
        logger.error("Failed to write HMEL lock file: %s", exc)


def unlock_hmel() -> bool:
    """Remove .hmel_locked file."""
    if LOCK_FILE.exists():
        try:
            LOCK_FILE.unlink()
            logger.info("hmel: Lock file %s removed", LOCK_FILE)
            return True
        except Exception as exc:
            logger.error("Failed to remove HMEL lock file: %s", exc)
            return False
    return False


def get_dashboard_credentials() -> tuple[str, str]:
    """
    Read DASHBOARD_USER and DASHBOARD_PASSWORD strictly from environment or local .env.
    Returns (username, password).
    """
    env_dict = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    user = (os.environ.get("DASHBOARD_USER") or env_dict.get("DASHBOARD_USER") or "").strip()
    pw = (os.environ.get("DASHBOARD_PASSWORD") or env_dict.get("DASHBOARD_PASSWORD") or "").strip()
    return user, pw


def get_dashboard_owners() -> list[str]:
    """
    Read DASHBOARD_OWNERS from environment or local .env.
    Returns a list of owner names. If NONE or empty, returns [].
    """
    env_dict = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    raw = (os.environ.get("DASHBOARD_OWNERS") or env_dict.get("DASHBOARD_OWNERS") or "").strip()
    if raw.upper() == "NONE":
        return []
    return [name.strip() for name in raw.split(",") if name.strip()]


def get_smtp_config() -> dict[str, str]:
    """
    Read SMTP configuration from environment or local .env.
    Returns a dict with SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, DIGEST_FROM, DIGEST_TO.
    """
    env_dict = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    def _get(k: str, default: str = "") -> str:
        return (os.environ.get(k) or env_dict.get(k) or default).strip()

    return {
        "SMTP_HOST": _get("SMTP_HOST"),
        "SMTP_PORT": _get("SMTP_PORT", "587"),
        "SMTP_USER": _get("SMTP_USER"),
        "SMTP_PASSWORD": _get("SMTP_PASSWORD"),
        "DIGEST_FROM": _get("DIGEST_FROM"),
        "DIGEST_TO": _get("DIGEST_TO"),
    }


def get_slack_webhook_url() -> str:
    """Read SLACK_WEBHOOK_URL from environment or local .env."""
    env_dict = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    return (os.environ.get("SLACK_WEBHOOK_URL") or env_dict.get("SLACK_WEBHOOK_URL") or "").strip()



def get_digest_enabled() -> bool:
    """
    Read DIGEST_ENABLED from environment or local .env.
    Returns True only if explicitly set to 'true', '1', or 'yes'.
    """
    env_dict = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    val = (os.environ.get("DIGEST_ENABLED") or env_dict.get("DIGEST_ENABLED") or "false").strip().lower()
    return val in ("true", "1", "yes")


def get_digest_min_hours() -> int:
    """
    Read DIGEST_MIN_HOURS from environment or local .env.
    Default is 24.
    """
    env_dict = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    val = (os.environ.get("DIGEST_MIN_HOURS") or env_dict.get("DIGEST_MIN_HOURS") or "24").strip()
    try:
        return int(val)
    except ValueError:
        return 24


def get_digest_first_window_hours() -> int:
    """
    Read DIGEST_FIRST_WINDOW_HOURS from environment or local .env.
    Default is 24.
    """
    env_dict = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    val = (os.environ.get("DIGEST_FIRST_WINDOW_HOURS") or env_dict.get("DIGEST_FIRST_WINDOW_HOURS") or "24").strip()
    try:
        return int(val)
    except ValueError:
        return 24


def set_slack_webhook_url(url: str) -> None:
    """Save SLACK_WEBHOOK_URL to environment and .env."""
    url = url.strip()
    os.environ["SLACK_WEBHOOK_URL"] = url
    if not ENV_PATH.exists():
        ENV_PATH.write_text(f"SLACK_WEBHOOK_URL={url}\n", encoding="utf-8")
        return

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    new_lines = []
    found = False
    for line in lines:
        if line.strip().startswith("SLACK_WEBHOOK_URL="):
            new_lines.append(f"SLACK_WEBHOOK_URL={url}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"SLACK_WEBHOOK_URL={url}")
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def get_auto_refresh_interval() -> int:
    """
    Read AUTO_REFRESH_INTERVAL_MINUTES from environment or local .env.
    Default is 0 (disabled).
    """
    env_dict = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    val = (os.environ.get("AUTO_REFRESH_INTERVAL_MINUTES") or env_dict.get("AUTO_REFRESH_INTERVAL_MINUTES") or "0").strip()
    try:
        return max(0, int(val))
    except ValueError:
        return 0


def set_auto_refresh_interval(minutes: int) -> None:
    """Save AUTO_REFRESH_INTERVAL_MINUTES to environment and .env."""
    mins_str = str(max(0, int(minutes)))
    os.environ["AUTO_REFRESH_INTERVAL_MINUTES"] = mins_str
    if not ENV_PATH.exists():
        ENV_PATH.write_text(f"AUTO_REFRESH_INTERVAL_MINUTES={mins_str}\n", encoding="utf-8")
        return

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    new_lines = []
    found = False
    for line in lines:
        if line.strip().startswith("AUTO_REFRESH_INTERVAL_MINUTES="):
            new_lines.append(f"AUTO_REFRESH_INTERVAL_MINUTES={mins_str}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"AUTO_REFRESH_INTERVAL_MINUTES={mins_str}")
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")



