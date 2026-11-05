import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from jarvis import reminders
from jarvis.config import settings
from jarvis.db import Database
from jarvis.tools import ToolContext, run_tool


def ctx_with(handler=None):
    transport = httpx.MockTransport(handler or (lambda r: httpx.Response(404)))
    return ToolContext(
        db=Database(":memory:"), settings=settings, http=httpx.AsyncClient(transport=transport), channel="cli"
    )


def run(name, args, ctx):
    return asyncio.run(run_tool(name, args, ctx))


def test_current_time():
    result, err = run("get_current_time", {}, ctx_with())
    assert not err and str(datetime.now().year) in result


def test_reminder_in_minutes_and_list_and_cancel():
    ctx = ctx_with()
    result, err = run("set_reminder", {"message": "Call Pepper", "in_minutes": 30}, ctx)
    assert not err and "Reminder #1" in result
    listing, _ = run("list_reminders", {}, ctx)
    assert "Call Pepper" in listing
    result, err = run("cancel_reminder", {"id": 1}, ctx)
    assert not err
    result, err = run("cancel_reminder", {"id": 1}, ctx)
    assert err


def test_reminder_at_local_time_and_past_rejected():
    ctx = ctx_with()
    future = (datetime.now(settings.tz) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    _, err = run("set_reminder", {"message": "x", "at_local_time": future}, ctx)
    assert not err
    _, err = run("set_reminder", {"message": "x", "at_local_time": "2001-01-01T09:00"}, ctx)
    assert err


def test_reminder_rejects_both_or_neither():
    ctx = ctx_with()
    _, err = run("set_reminder", {"message": "x"}, ctx)
    assert err
    _, err = run("set_reminder", {"message": "x", "in_minutes": 5, "at_local_time": "2030-01-01T00:00"}, ctx)
    assert err


def test_memory_remember_and_forget():
    ctx = ctx_with()
    run("remember", {"fact": "Likes AC/DC"}, ctx)
    assert ctx.db.list_facts() == [(1, "Likes AC/DC")]
    _, err = run("forget", {"id": 1}, ctx)
    assert not err and ctx.db.list_facts() == []


def test_weather_uses_open_meteo():
    def handler(request: httpx.Request):
        if "geocoding" in request.url.host:
            return httpx.Response(200, json={"results": [
                {"name": "Malibu", "admin1": "California", "country": "United States", "latitude": 34.0, "longitude": -118.7}
            ]})
        return httpx.Response(200, json={
            "current": {"temperature_2m": 72, "apparent_temperature": 71, "relative_humidity_2m": 40,
                        "weather_code": 0, "wind_speed_10m": 5},
            "daily": {"time": ["2026-11-05", "2026-11-06", "2026-11-07"], "weather_code": [0, 2, 61],
                      "temperature_2m_max": [75, 74, 68], "temperature_2m_min": [60, 59, 55],
                      "precipitation_probability_max": [0, 10, 70]},
        })

    result, err = run("get_weather", {"location": "Malibu"}, ctx_with(handler))
    assert not err
    assert "Malibu, California" in result and "72°F" in result and "Tomorrow" in result and "light rain" in result


def test_reminder_delivery(monkeypatch):
    sent = []

    async def fake_send(_settings, body, to=None):
        sent.append(body)

    monkeypatch.setattr(reminders, "send_sms", fake_send)
    db = Database(":memory:")
    db.add_reminder("past due", datetime.now(timezone.utc) - timedelta(minutes=1))
    db.add_reminder("future", datetime.now(timezone.utc) + timedelta(hours=1))
    assert asyncio.run(reminders.deliver_due_reminders(db, settings)) == 1
    assert sent == ["⏰ Reminder: past due"]
    assert asyncio.run(reminders.deliver_due_reminders(db, settings)) == 0
