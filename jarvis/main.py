"""FastAPI server: Twilio voice (ConversationRelay) + SMS webhooks."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager, suppress

import anthropic
import httpx
from fastapi import BackgroundTasks, FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import Connect, VoiceResponse

from .brain import Conversation, text_history
from .config import settings
from .db import Database
from .messaging import send_sms
from .reminders import reminder_loop
from .security import check_relay_token, is_valid_twilio_request, relay_token
from .tools import ToolContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jarvis")

TWIML = "application/xml"


class State:
    db: Database
    claude: anthropic.AsyncAnthropic
    http: httpx.AsyncClient
    sms_lock: asyncio.Lock


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.db = Database(settings.database_path)
    state.claude = anthropic.AsyncAnthropic()
    state.http = httpx.AsyncClient(timeout=15)
    state.sms_lock = asyncio.Lock()
    reminders = asyncio.create_task(reminder_loop(state.db, settings))
    if not settings.allowed_numbers:
        log.warning("ALLOWED_NUMBERS is empty: Jarvis will reject every call and text.")
    log.info("Jarvis online. Model: %s", settings.model)
    yield
    reminders.cancel()
    await state.http.aclose()
    await state.claude.close()


app = FastAPI(title="Homemade Jarvis", lifespan=lifespan)


def new_context(channel: str) -> ToolContext:
    return ToolContext(db=state.db, settings=settings, http=state.http, channel=channel)  # type: ignore[arg-type]


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# Voice
# ─────────────────────────────────────────────────────────────────────────────


@app.post("/voice")
async def incoming_call(request: Request) -> Response:
    if not await is_valid_twilio_request(request, settings):
        return Response("Invalid signature", status_code=403)
    form = await request.form()
    caller = str(form.get("From", ""))
    call_sid = str(form.get("CallSid", ""))

    twiml = VoiceResponse()
    if not settings.is_allowed(caller):
        log.warning("Rejected call from %s", caller)
        twiml.reject()
        return Response(str(twiml), media_type=TWIML)

    connect = Connect()
    options = {"url": settings.ws_url, "welcome_greeting": settings.welcome_greeting}
    if settings.tts_provider:
        options["tts_provider"] = settings.tts_provider
    if settings.tts_voice:
        options["voice"] = settings.tts_voice
    relay = connect.conversation_relay(**options)
    relay.parameter(name="token", value=relay_token(call_sid, settings))
    twiml.append(connect)
    return Response(str(twiml), media_type=TWIML)


def _speech_seconds(text: str) -> float:
    """Rough time it takes to say `text` out loud."""
    return min(10.0, 1.0 + len(text) / 14)


@app.websocket("/voice/relay")
async def voice_relay(ws: WebSocket) -> None:
    await ws.accept()
    conversation: Conversation | None = None
    ctx: ToolContext | None = None
    current: asyncio.Task | None = None
    heard_before_interrupt: str | None = None

    async def speak(prompt: str) -> None:
        assert conversation is not None and ctx is not None
        said: list[str] = []
        try:
            async for chunk in conversation.respond(prompt):
                said.append(chunk)
                await ws.send_json({"type": "text", "token": chunk, "last": False})
            await ws.send_json({"type": "text", "token": "", "last": True})
        except anthropic.APIError:
            log.exception("Claude API error during call")
            await ws.send_json(
                {"type": "text", "token": "Sorry, I'm having trouble thinking right now.", "last": True}
            )
            return
        if ctx.end_call_requested:
            await asyncio.sleep(_speech_seconds("".join(said)))
            await ws.send_json({"type": "end"})

    try:
        while True:
            message = json.loads(await ws.receive_text())
            kind = message.get("type")

            if kind == "setup":
                params = message.get("customParameters") or {}
                call_sid = message.get("callSid", "")
                caller = message.get("from", "")
                if not check_relay_token(call_sid, params.get("token", ""), settings) or not settings.is_allowed(caller):
                    log.warning("Rejected relay connection (call %s from %s)", call_sid, caller)
                    await ws.close(code=1008)
                    return
                ctx = new_context("voice")
                conversation = Conversation(state.claude, settings, ctx)
                log.info("Call connected: %s", call_sid)

            elif conversation is None:
                # Nothing is allowed before a valid setup message.
                await ws.close(code=1008)
                return

            elif kind == "prompt":
                if message.get("last") is False:
                    continue  # partial transcript; wait for the final one
                text = (message.get("voicePrompt") or "").strip()
                if not text:
                    continue
                if heard_before_interrupt is not None:
                    text = (
                        f"(I cut you off; I only heard you say: \"{heard_before_interrupt}\")\n{text}"
                    )
                    heard_before_interrupt = None
                if current and not current.done():
                    current.cancel()
                    with suppress(asyncio.CancelledError):
                        await current
                log.info("Caller: %s", text)
                current = asyncio.create_task(speak(text))

            elif kind == "interrupt":
                heard_before_interrupt = message.get("utteranceUntilInterrupt", "")
                if current and not current.done():
                    current.cancel()
                    with suppress(asyncio.CancelledError):
                        await current

            elif kind == "error":
                log.error("ConversationRelay error: %s", message.get("description"))

    except WebSocketDisconnect:
        log.info("Call ended")
    finally:
        if current and not current.done():
            current.cancel()


# ─────────────────────────────────────────────────────────────────────────────
# Text messages
# ─────────────────────────────────────────────────────────────────────────────


async def handle_sms(body: str, sender: str) -> None:
    # One text at a time, so replies stay in order and history stays consistent.
    async with state.sms_lock:
        ctx = new_context("sms")
        history = text_history(state.db.recent_sms(limit=20))
        conversation = Conversation(state.claude, settings, ctx, history)
        try:
            reply = await conversation.reply(body)
        except anthropic.APIError:
            log.exception("Claude API error during SMS")
            reply = "Sorry, I'm having trouble thinking right now. Try again in a moment."
        reply = reply or "Done."
        state.db.add_sms("user", body)
        state.db.add_sms("assistant", reply)
        try:
            await send_sms(settings, reply, to=sender)
        except Exception:  # noqa: BLE001
            log.exception("Failed to send SMS reply")


@app.post("/sms")
async def incoming_sms(request: Request, background: BackgroundTasks) -> Response:
    if not await is_valid_twilio_request(request, settings):
        return Response("Invalid signature", status_code=403)
    form = await request.form()
    sender = str(form.get("From", ""))
    body = str(form.get("Body", "")).strip()
    if settings.is_allowed(sender) and body:
        # Reply asynchronously: Claude + tools can take longer than Twilio's 15s webhook timeout.
        background.add_task(handle_sms, body, sender)
    else:
        log.warning("Ignored text from %s", sender)
    return Response(str(MessagingResponse()), media_type=TWIML)
