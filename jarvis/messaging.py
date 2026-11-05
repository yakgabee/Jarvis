"""Outbound text messages through Twilio."""

from __future__ import annotations

import asyncio
from functools import lru_cache

from twilio.rest import Client

from .config import Settings

SMS_LIMIT = 1500  # Twilio's hard cap is 1600 characters per message


@lru_cache(maxsize=1)
def _client(sid: str, token: str) -> Client:
    return Client(sid, token)


async def send_sms(settings: Settings, body: str, to: str | None = None) -> None:
    to = to or (settings.allowed_numbers[0] if settings.allowed_numbers else None)
    if not to:
        raise RuntimeError("No destination number: set ALLOWED_NUMBERS.")
    if len(body) > SMS_LIMIT:
        body = body[: SMS_LIMIT - 1] + "…"
    client = _client(settings.twilio_account_sid, settings.twilio_auth_token)
    await asyncio.to_thread(
        client.messages.create, to=to, from_=settings.twilio_phone_number, body=body
    )
