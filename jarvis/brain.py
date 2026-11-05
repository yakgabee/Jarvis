"""Jarvis's brain: a streaming Claude conversation with tool use."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import AsyncIterator

import anthropic

from .config import Settings
from .tools import ToolContext, run_tool, tool_definitions

log = logging.getLogger("jarvis.brain")

MAX_TOOL_ROUNDS = 8

PERSONA = """You are J.A.R.V.I.S., a personal AI assistant built by and for {owner}. \
You are calm, witty, dryly British in humour, unfailingly loyal and extremely competent. \
Address the owner as "{owner}" occasionally, not in every sentence.

How you work:
- Be genuinely useful. Use your tools whenever they help: check the weather, set reminders, \
remember things, look things up. Don't ask permission for harmless actions; just do them and report back.
- When the owner tells you something worth knowing long-term (a preference, a person's name, a routine, \
"remember that..."), save it with the remember tool without being asked twice.
- If you don't know something and can't find out, say so plainly. Never invent facts, times or numbers.
- Each user message begins with a bracketed timestamp of the owner's current local time. Use it for \
anything time-related; never read it back verbatim unless asked.
- Reminders are delivered to the owner by text message."""

VOICE_STYLE = """You are speaking on a PHONE CALL. Everything you write is converted to speech:
- Keep replies short and conversational: usually one to three sentences. Offer more detail instead of dumping it.
- Plain spoken English only. No markdown, bullet points, lists, emoji, URLs or symbols.
- Write numbers, times and units the way they should be said, e.g. "seventy-two degrees", "three thirty PM".
- If the user says goodbye or wants to hang up, say a brief farewell and use the end_call tool."""

SMS_STYLE = """You are replying by TEXT MESSAGE:
- Be concise; aim for under 400 characters unless the owner asks for detail.
- Plain text only, no markdown. A short list with dashes is fine when it genuinely helps."""

CLI_STYLE = """You are chatting in a terminal test console. Keep replies concise, plain text."""

STYLES = {"voice": VOICE_STYLE, "sms": SMS_STYLE, "cli": CLI_STYLE}

# Voice needs low latency; texting can afford a bit more thought.
EFFORT = {"voice": "low", "sms": "medium", "cli": "low"}

FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")

SORRY = "Sorry, I can't help with that one."


def build_system(settings: Settings, ctx: ToolContext) -> list[dict]:
    facts = ctx.db.list_facts()
    memory = (
        "What you remember about the owner (memory ID: fact):\n"
        + "\n".join(f"#{fid}: {text}" for fid, text in facts)
        if facts
        else "You have no saved memories about the owner yet."
    )
    extras = []
    if settings.home_city:
        extras.append(f"The owner's home city is {settings.home_city}.")
    extras.append(f"The owner's timezone is {settings.timezone}.")
    return [
        {"type": "text", "text": PERSONA.format(owner=settings.owner_name)},
        {"type": "text", "text": STYLES[ctx.channel]},
        {"type": "text", "text": " ".join(extras) + "\n\n" + memory},
    ]


def stamp(text: str, settings: Settings) -> str:
    now = datetime.now(settings.tz)
    return f"[{now:%a %b %d %Y, %I:%M %p %Z}]\n{text}"


class Conversation:
    """One running conversation. History is append-only (required for thinking blocks)."""

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        settings: Settings,
        ctx: ToolContext,
        history: list[dict] | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.ctx = ctx
        self.messages: list[dict] = list(history or [])

    # ── request building ─────────────────────────────────
    def _tools(self) -> list[dict]:
        tools = tool_definitions(self.ctx.channel)
        if self.settings.enable_web_search:
            search: dict = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
            location = {"type": "approximate", "timezone": self.settings.timezone}
            if self.settings.home_city:
                location["city"] = self.settings.home_city
            search["user_location"] = location
            tools.append(search)
        return tools

    def _request(self, messages: list[dict]) -> dict:
        kwargs: dict = {
            "model": self.settings.model,
            "max_tokens": 16000,
            "system": build_system(self.settings, self.ctx),
            "tools": self._tools(),
            "messages": messages,
            "output_config": {"effort": EFFORT[self.ctx.channel]},
            # Automatic prompt caching: makes long calls cheaper and faster.
            "cache_control": {"type": "ephemeral"},
            "betas": [],
        }
        if self.settings.model.startswith(FALLBACK_MODELS):
            # If a safety classifier declines, retry server-side on a suitable model.
            kwargs["betas"].append("server-side-fallback-2026-07-01")
            kwargs["fallbacks"] = "default"
        return kwargs

    # ── main entry point ─────────────────────────────────
    async def respond(self, user_text: str) -> AsyncIterator[str]:
        """Send the owner's message; yield reply text as it streams.

        If the consumer stops early (e.g. the caller interrupts Jarvis), the turn is
        committed with whatever was said so far, so history stays valid.
        """
        pending: list[dict] = [{"role": "user", "content": stamp(user_text, self.settings)}]
        spoken: list[str] = []  # text yielded during the current API response
        committed = False

        def commit_partial(note: str) -> None:
            nonlocal committed
            if committed:
                return
            # Never leave a trailing assistant turn without a reply after it.
            while pending and pending[-1]["role"] == "assistant":
                pending.pop()
            text = "".join(spoken).strip()
            self.messages.extend(pending)
            self.messages.append(
                {"role": "assistant", "content": f"{text} {note}".strip() if text else note}
            )
            committed = True

        try:
            json_retries = 0
            rounds = 0
            while True:
                spoken.clear()
                try:
                    async with self.client.beta.messages.stream(
                        **self._request(self.messages + pending)
                    ) as stream:
                        async for event in stream:
                            if event.type == "text":
                                spoken.append(event.text)
                                yield event.text
                        response = await stream.get_final_message()
                    json_retries = 0
                except ValueError:
                    # A tool input the SDK couldn't parse at all; re-issue the turn.
                    json_retries += 1
                    if json_retries > 2:
                        raise
                    continue

                content = [block.to_dict() for block in response.content]

                if response.stop_reason == "refusal":
                    log.warning("Refusal: %s", response.stop_details)
                    if not spoken:
                        yield SORRY
                        spoken.append(SORRY)
                    commit_partial("")
                    return

                if response.stop_reason == "pause_turn":
                    # A server tool (web search) paused mid-turn; send it back to continue.
                    pending.append({"role": "assistant", "content": content})
                    continue

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if not tool_uses:
                    pending.append({"role": "assistant", "content": content})
                    break

                if response.stop_reason == "max_tokens":
                    log.warning("Hit max_tokens with a pending tool call; not running it.")
                    commit_partial("(I ran out of room mid-thought.)")
                    return

                rounds += 1
                if rounds > MAX_TOOL_ROUNDS:
                    commit_partial("(I stopped after too many steps.)")
                    return

                results = await asyncio.gather(
                    *(run_tool(b.name, b.input, self.ctx) for b in tool_uses)
                )
                tool_results = []
                for block, (result, is_error) in zip(tool_uses, results):
                    log.info("tool %s(%s) -> %s%s", block.name, block.input, "ERROR " if is_error else "", result[:200])
                    entry = {"type": "tool_result", "tool_use_id": block.id, "content": result}
                    if is_error:
                        entry["is_error"] = True
                    tool_results.append(entry)
                # Assistant turn + its tool results are added together so history is always valid.
                pending.append({"role": "assistant", "content": content})
                pending.append({"role": "user", "content": tool_results})

            self.messages.extend(pending)
            committed = True
        except (asyncio.CancelledError, GeneratorExit):
            commit_partial("(interrupted)")
            raise
        except anthropic.APIError:
            commit_partial("(error)")
            raise

    async def reply(self, user_text: str) -> str:
        """Non-streaming convenience: return the full reply text."""
        parts = [chunk async for chunk in self.respond(user_text)]
        return "".join(parts).strip()


def text_history(rows: list[tuple[str, str]]) -> list[dict]:
    """Turn stored (role, text) rows into a valid alternating message list."""
    messages: list[dict] = []
    for role, text in rows:
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n\n" + text
        else:
            messages.append({"role": role, "content": text})
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    while messages and messages[-1]["role"] != "assistant":
        messages.pop()
    return messages
