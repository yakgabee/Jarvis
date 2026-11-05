"""Make sure only Twilio — and only calls/texts from you — can reach Jarvis."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Request
from twilio.request_validator import RequestValidator

from .config import Settings


def public_request_url(request: Request, settings: Settings) -> str:
    """The exact URL Twilio requested (Twilio signs this URL, not our internal one)."""
    url = settings.public_url + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    return url


async def is_valid_twilio_request(request: Request, settings: Settings) -> bool:
    if not settings.validate_twilio_signature:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature or not settings.twilio_auth_token:
        return False
    form = await request.form()
    params = {key: value for key, value in form.items()}
    validator = RequestValidator(settings.twilio_auth_token)
    return validator.validate(public_request_url(request, settings), params, signature)


def relay_token(call_sid: str, settings: Settings) -> str:
    """A token proving a voice WebSocket was set up by our own (validated) /voice webhook."""
    key = (settings.twilio_auth_token or "dev-only-secret").encode()
    return hmac.new(key, f"relay:{call_sid}".encode(), hashlib.sha256).hexdigest()


def check_relay_token(call_sid: str, token: str, settings: Settings) -> bool:
    return bool(call_sid) and hmac.compare_digest(relay_token(call_sid, settings), token or "")
