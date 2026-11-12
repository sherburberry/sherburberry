"""config.py - Central configuration, loaded once from environment variables / .env."""
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()  # No-op on hosts that inject real env vars (Render, Railway, Docker)


def _require(name: str) -> str:
    """Return a required env var or fail fast with a helpful message."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name} (see .env.example)")
    return value


TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: int = int(_require("TELEGRAM_CHAT_ID"))
_require("ANTHROPIC_API_KEY")  # read automatically by the anthropic SDK; validated here

# Singapore has no DST, so 09:00 SGT is always 01:00 UTC.
TIMEZONE = ZoneInfo(os.getenv("BRIEFING_TZ", "Asia/Singapore"))
BRIEFING_HOUR: int = int(os.getenv("BRIEFING_HOUR", "9"))
BRIEFING_MINUTE: int = int(os.getenv("BRIEFING_MINUTE", "0"))
