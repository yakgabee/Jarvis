"""Settings loaded from environment variables (or a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    # Claude
    model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-opus-5-5"))
    enable_web_search: bool = field(default_factory=lambda: _bool("ENABLE_WEB_SEARCH", True))

    # Twilio
    twilio_account_sid: str = field(default_factory=lambda: os.getenv("TWILIO_ACCOUNT_SID", ""))
    twilio_auth_token: str = field(default_factory=lambda: os.getenv("TWILIO_AUTH_TOKEN", ""))
    twilio_phone_number: str = field(default_factory=lambda: os.getenv("TWILIO_PHONE_NUMBER", ""))
    validate_twilio_signature: bool = field(
        default_factory=lambda: _bool("VALIDATE_TWILIO_SIGNATURE", True)
    )

    # Owner
    allowed_numbers: list[str] = field(default_factory=lambda: _list("ALLOWED_NUMBERS"))
    owner_name: str = field(default_factory=lambda: os.getenv("OWNER_NAME", "Sir"))
    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "UTC"))
    home_city: str = field(default_factory=lambda: os.getenv("HOME_CITY", ""))

    # Server
    public_url: str = field(default_factory=lambda: os.getenv("PUBLIC_URL", "").rstrip("/"))
    database_path: str = field(default_factory=lambda: os.getenv("DATABASE_PATH", "jarvis.db"))

    # Voice
    tts_provider: str = field(default_factory=lambda: os.getenv("TTS_PROVIDER", ""))
    tts_voice: str = field(default_factory=lambda: os.getenv("TTS_VOICE", ""))
    welcome_greeting: str = field(
        default_factory=lambda: os.getenv(
            "WELCOME_GREETING", "At your service. How can I help?"
        )
    )

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def ws_url(self) -> str:
        """WebSocket URL Twilio ConversationRelay connects to."""
        base = self.public_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}/voice/relay"

    def is_allowed(self, number: str | None) -> bool:
        return bool(number) and number in self.allowed_numbers


settings = Settings()
