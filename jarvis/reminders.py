"""Background loop that texts you when a reminder is due."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .config import Settings
from .db import Database
from .messaging import send_sms

log = logging.getLogger("jarvis.reminders")

CHECK_EVERY_SECONDS = 20


async def deliver_due_reminders(db: Database, settings: Settings) -> int:
    sent = 0
    for reminder in db.due_reminders(datetime.now(timezone.utc)):
        try:
            await send_sms(settings, f"⏰ Reminder: {reminder.message}")
        except Exception:  # noqa: BLE001 - keep trying on the next tick
            log.exception("Failed to send reminder #%s", reminder.id)
            continue
        db.mark_reminder_sent(reminder.id)
        sent += 1
    return sent


async def reminder_loop(db: Database, settings: Settings) -> None:
    while True:
        try:
            await deliver_due_reminders(db, settings)
        except Exception:  # noqa: BLE001
            log.exception("Reminder loop error")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
