"""Talk to Jarvis in your terminal — no Twilio needed. Great for testing.

    python chat.py
"""

from __future__ import annotations

import asyncio

import anthropic
import httpx

from jarvis.brain import Conversation
from jarvis.config import settings
from jarvis.db import Database
from jarvis.tools import ToolContext


async def main() -> None:
    db = Database(settings.database_path)
    async with httpx.AsyncClient(timeout=15) as http, anthropic.AsyncAnthropic() as claude:
        ctx = ToolContext(db=db, settings=settings, http=http, channel="cli")
        conversation = Conversation(claude, settings, ctx)
        print(f"Jarvis ({settings.model}) — type 'quit' to exit.\n")
        while True:
            try:
                text = (await asyncio.to_thread(input, "You: ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text.lower() in {"quit", "exit"}:
                break
            if not text:
                continue
            print("Jarvis: ", end="", flush=True)
            async for chunk in conversation.respond(text):
                print(chunk, end="", flush=True)
            print("\n")


if __name__ == "__main__":
    asyncio.run(main())
