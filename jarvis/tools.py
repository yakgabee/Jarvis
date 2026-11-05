"""The things Jarvis can actually *do*.

Each tool is a Pydantic model (its input schema) plus an async handler. To add a
new ability, write a model + handler and register it with `@tool(...)`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .config import Settings
from .db import Database


@dataclass
class ToolContext:
    """Per-conversation state handed to every tool."""

    db: Database
    settings: Settings
    http: httpx.AsyncClient
    channel: Literal["voice", "sms", "cli"]
    end_call_requested: bool = False
    extra: dict = field(default_factory=dict)


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


Handler = Callable[[ToolInput, ToolContext], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[ToolInput]
    handler: Handler
    channels: tuple[str, ...] = ("voice", "sms", "cli")

    def definition(self) -> dict:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
            # Stream tool inputs as they're generated; we validate them ourselves below.
            "eager_input_streaming": True,
        }


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, input_model: type[ToolInput], channels=("voice", "sms", "cli")):
    def decorator(fn: Handler) -> Handler:
        REGISTRY[name] = Tool(name, description, input_model, fn, tuple(channels))
        return fn

    return decorator


def tool_definitions(channel: str) -> list[dict]:
    return [t.definition() for t in REGISTRY.values() if channel in t.channels]


async def run_tool(name: str, raw_input: object, ctx: ToolContext) -> tuple[str, bool]:
    """Validate and run a tool. Returns (result_text, is_error)."""
    spec = REGISTRY.get(name)
    if spec is None or ctx.channel not in spec.channels:
        return f"Unknown tool: {name}", True
    try:
        args = spec.input_model.model_validate(raw_input)
    except ValidationError as exc:
        # Covers truncated/garbled input from eager streaming as well as bad values.
        return f"Invalid input for {name}: {exc.errors(include_url=False)}", True
    try:
        return await spec.handler(args, ctx), False
    except ToolError as exc:
        return str(exc), True
    except Exception as exc:  # noqa: BLE001 - never crash the conversation over a tool
        return f"{name} failed: {type(exc).__name__}: {exc}", True


class ToolError(Exception):
    """A user-facing failure that Claude should see and explain."""


# ─────────────────────────────────────────────────────────────────────────────
# Time
# ─────────────────────────────────────────────────────────────────────────────


class NoInput(ToolInput):
    pass


@tool(
    "get_current_time",
    "Get the current local date and time for the owner.",
    NoInput,
)
async def get_current_time(_: NoInput, ctx: ToolContext) -> str:
    now = datetime.now(ctx.settings.tz)
    return now.strftime("%A, %B %d, %Y at %I:%M %p %Z")


# ─────────────────────────────────────────────────────────────────────────────
# Weather (Open-Meteo: free, no API key)
# ─────────────────────────────────────────────────────────────────────────────

WEATHER_CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


class WeatherInput(ToolInput):
    location: str | None = Field(
        default=None,
        description="City name, e.g. 'Chicago' or 'Paris, France'. Omit for the owner's home city.",
    )
    units: Literal["fahrenheit", "celsius"] = "fahrenheit"


@tool(
    "get_weather",
    "Get current conditions and a 3-day forecast for a location.",
    WeatherInput,
)
async def get_weather(args: WeatherInput, ctx: ToolContext) -> str:
    place = args.location or ctx.settings.home_city
    if not place:
        raise ToolError("No location given and HOME_CITY is not configured. Ask which city.")

    geo = await ctx.http.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": place.split(",")[0].strip(), "count": 5, "language": "en"},
    )
    geo.raise_for_status()
    results = geo.json().get("results") or []
    if not results:
        raise ToolError(f"Couldn't find a place called {place!r}.")
    # If the user gave "City, Region/Country", prefer a matching result.
    hint = place.split(",", 1)[1].strip().lower() if "," in place else ""
    match = next(
        (
            r for r in results
            if hint and hint in " ".join(str(r.get(k, "")) for k in ("admin1", "country", "country_code")).lower()
        ),
        results[0],
    )

    unit = "°F" if args.units == "fahrenheit" else "°C"
    wind_unit = "mph" if args.units == "fahrenheit" else "km/h"
    fc = await ctx.http.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": match["latitude"],
            "longitude": match["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "temperature_unit": args.units,
            "wind_speed_unit": "mph" if args.units == "fahrenheit" else "kmh",
            "timezone": "auto",
            "forecast_days": 3,
        },
    )
    fc.raise_for_status()
    data = fc.json()
    cur = data["current"]
    daily = data["daily"]

    name = ", ".join(x for x in (match.get("name"), match.get("admin1"), match.get("country")) if x)
    lines = [
        f"Weather for {name}:",
        f"Now: {cur['temperature_2m']}{unit} (feels like {cur['apparent_temperature']}{unit}), "
        f"{WEATHER_CODES.get(cur['weather_code'], 'unknown conditions')}, "
        f"humidity {cur['relative_humidity_2m']}%, wind {cur['wind_speed_10m']} {wind_unit}.",
    ]
    for i, day in enumerate(daily["time"]):
        label = ["Today", "Tomorrow"][i] if i < 2 else datetime.fromisoformat(day).strftime("%A")
        rain = daily["precipitation_probability_max"][i]
        lines.append(
            f"{label}: {WEATHER_CODES.get(daily['weather_code'][i], 'mixed')}, "
            f"high {daily['temperature_2m_max'][i]}{unit}, low {daily['temperature_2m_min'][i]}{unit}"
            + (f", {rain}% chance of precipitation" if rain is not None else "")
            + "."
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Reminders (delivered by text message)
# ─────────────────────────────────────────────────────────────────────────────


class SetReminderInput(ToolInput):
    message: str = Field(description="What to remind the owner about, phrased as the text they'll receive.")
    in_minutes: float | None = Field(
        default=None, gt=0, description="Remind this many minutes from now. Use this OR at_local_time."
    )
    at_local_time: str | None = Field(
        default=None,
        description="Local date/time in ISO format, e.g. '2026-11-05T15:30'. Use this OR in_minutes.",
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> "SetReminderInput":
        if (self.in_minutes is None) == (self.at_local_time is None):
            raise ValueError("Provide exactly one of in_minutes or at_local_time.")
        return self


@tool(
    "set_reminder",
    "Schedule a reminder. It is sent to the owner as a text message at the chosen time. "
    "Check get_current_time first if you need to work out a date.",
    SetReminderInput,
)
async def set_reminder(args: SetReminderInput, ctx: ToolContext) -> str:
    tz = ctx.settings.tz
    now = datetime.now(tz)
    if args.in_minutes is not None:
        due = now + timedelta(minutes=args.in_minutes)
    else:
        try:
            due = datetime.fromisoformat(args.at_local_time)  # type: ignore[arg-type]
        except ValueError as exc:
            raise ToolError(f"Couldn't read the time {args.at_local_time!r}: {exc}") from exc
        due = due.replace(tzinfo=tz) if due.tzinfo is None else due.astimezone(tz)
    if due <= now:
        raise ToolError(f"That time ({due:%Y-%m-%d %H:%M}) is in the past.")
    reminder_id = ctx.db.add_reminder(args.message, due)
    return f"Reminder #{reminder_id} set for {due:%A %B %d at %I:%M %p}: {args.message}"


@tool("list_reminders", "List upcoming reminders that haven't been sent yet.", NoInput)
async def list_reminders(_: NoInput, ctx: ToolContext) -> str:
    pending = ctx.db.pending_reminders()
    if not pending:
        return "No upcoming reminders."
    tz = ctx.settings.tz
    return "\n".join(
        f"#{r.id}: {r.due_at.astimezone(tz):%a %b %d %I:%M %p} — {r.message}" for r in pending
    )


class IdInput(ToolInput):
    id: int = Field(description="The numeric ID.")


@tool("cancel_reminder", "Cancel an upcoming reminder by its ID (see list_reminders).", IdInput)
async def cancel_reminder(args: IdInput, ctx: ToolContext) -> str:
    if ctx.db.cancel_reminder(args.id):
        return f"Reminder #{args.id} cancelled."
    raise ToolError(f"No upcoming reminder with ID {args.id}.")


# ─────────────────────────────────────────────────────────────────────────────
# Long-term memory
# ─────────────────────────────────────────────────────────────────────────────


class RememberInput(ToolInput):
    fact: str = Field(description="A short, self-contained fact about the owner or their preferences.")


@tool(
    "remember",
    "Save a fact about the owner to long-term memory (preferences, people, routines, "
    "things they ask you to remember). Saved facts appear in every future conversation.",
    RememberInput,
)
async def remember(args: RememberInput, ctx: ToolContext) -> str:
    fact_id = ctx.db.add_fact(args.fact.strip())
    return f"Saved as memory #{fact_id}."


@tool("forget", "Delete a saved memory by its ID (IDs are shown in your memory list).", IdInput)
async def forget(args: IdInput, ctx: ToolContext) -> str:
    if ctx.db.delete_fact(args.id):
        return f"Memory #{args.id} deleted."
    raise ToolError(f"No memory with ID {args.id}.")


# ─────────────────────────────────────────────────────────────────────────────
# Phone-call control
# ─────────────────────────────────────────────────────────────────────────────


@tool(
    "end_call",
    "Hang up the phone call. Use when the owner says goodbye or clearly wants to end the call. "
    "Say your goodbye in the same reply.",
    NoInput,
    channels=("voice",),
)
async def end_call(_: NoInput, ctx: ToolContext) -> str:
    ctx.end_call_requested = True
    return "The call will end after your reply is spoken."

