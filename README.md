# Homemade Jarvis

A personal AI assistant you can **call** or **text**. It runs on your own Twilio number and uses Claude as its brain.

- 📞 **Voice calls.** Natural conversation over the phone using Twilio ConversationRelay. Replies stream as they're generated, and you can interrupt mid-sentence.
- 💬 **Texting.** Same brain, same memory, over SMS.
- 🧠 **Long-term memory.** Tell it things ("remember my sister's name is Ava") and it remembers them in every future conversation.
- ⏰ **Reminders.** "Remind me to call Mom at 6" sends you a text at 6.
- 🌦️ **Weather** (free Open-Meteo API, no key needed), 🔎 **web search**, 🕒 **date and time**.
- 🔒 **Only answers you.** Calls and texts from any other number are rejected, and every Twilio request is signature-checked.

```
You ──call/text──► Twilio number ──► this server ──► Claude (+ tools)
                                         │
     ◄───── speech / text reply ─────────┘
```

---

## 1. Try it in your terminal first (no Twilio needed)

```bash
git clone https://github.com/yakgabee/homemade-jarvis.git
cd homemade-jarvis
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your ANTHROPIC_API_KEY in .env
python chat.py
```

```
You: what's the weather tomorrow?
Jarvis: Tomorrow in New York looks partly cloudy, high of 58, low of 44...
```

## 2. Configure `.env`

| Variable | What it is |
|---|---|
| `ANTHROPIC_API_KEY` | Your Claude API key from https://console.anthropic.com |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | From the Twilio Console home page |
| `TWILIO_PHONE_NUMBER` | Your Twilio number, e.g. `+15555550100` |
| `ALLOWED_NUMBERS` | **Your** cell number(s), comma-separated. Everyone else is rejected. |
| `OWNER_NAME` | What Jarvis calls you ("Sir", "Tony"...) |
| `TIMEZONE`, `HOME_CITY` | Used for times, reminders and default weather |
| `PUBLIC_URL` | The public `https://` address of your server (see step 3) |
| `CLAUDE_MODEL` | Optional. Default `claude-opus-5-5`. `claude-sonnet-5-5` or `claude-haiku-5-5` are faster and cheaper. |
| `TTS_PROVIDER`, `TTS_VOICE` | Optional voice choice for ConversationRelay (e.g. `ElevenLabs` and a voice ID) |
| `ENABLE_WEB_SEARCH` | `true`/`false`. Web searches are billed per search by Anthropic. |

## 3. Run the server somewhere Twilio can reach

The server needs a public **HTTPS** address and must support **WebSockets**.

**Option A: your own machine or homelab, plus a tunnel**
```bash
uvicorn jarvis.main:app --host 0.0.0.0 --port 8000 --proxy-headers
# in another terminal:
ngrok http 8000          # or: cloudflared tunnel --url http://localhost:8000
```
Put the `https://...` URL it prints into `PUBLIC_URL` and restart the server.

**Option B: Docker**, on any VPS or a host like Railway or Fly.io
```bash
docker build -t jarvis .
docker run -d --env-file .env -p 8000:8000 -v jarvis-data:/data jarvis
```
Keep `/data` on a persistent volume. That's where memories and reminders live.

Check it's up: `curl https://YOUR-URL/health` should return `{"status":"ok"}`.

## 4. Point your Twilio number at it

In the Twilio Console, go to **Phone Numbers → Manage → Active numbers → (your number)** and set:

- **Voice: "A call comes in"** → Webhook → `https://YOUR-URL/voice` → `HTTP POST`
- **Messaging: "A message comes in"** → Webhook → `https://YOUR-URL/sms` → `HTTP POST`

Two Twilio requirements to know about:
- **ConversationRelay** (the voice AI feature) may require you to accept Twilio's *Predictive and Generative AI/ML Features Addendum*. Look under **Voice → Settings → General** in the Console.
- **Texting US numbers** from a regular (10DLC) number requires **A2P 10DLC registration**. Toll-free numbers need *toll-free verification* instead. Until that's approved, outgoing texts (replies and reminders) may be blocked. Voice calls work without it.

Now call or text your Twilio number from your phone. 🎉

---

## Costs (rough)

- **Twilio:** about $1–2 a month for the number, plus a few cents per minute of calls (including ConversationRelay) and under a cent per text.
- **Claude:** usually a fraction of a cent to a few cents per exchange. Long calls cost more because the conversation is resent each turn (prompt caching keeps this down). Switching `CLAUDE_MODEL` to Sonnet or Haiku lowers it further.

## Teaching Jarvis new skills

All abilities live in [`jarvis/tools.py`](jarvis/tools.py). To add one, write an input model and a handler:

```python
class LightsInput(ToolInput):
    room: str = Field(description="Which room, e.g. 'kitchen'")
    on: bool

@tool("set_lights", "Turn the lights in a room on or off.", LightsInput)
async def set_lights(args: LightsInput, ctx: ToolContext) -> str:
    # e.g. call your Home Assistant API with ctx.http
    return f"{args.room} lights {'on' if args.on else 'off'}."
```

That's it. Claude sees the new tool automatically and decides when to use it. Ideas: Home Assistant (lights, thermostat), Google Calendar, Spotify, your homelab's status page.

> ⚠️ For tools that **spend money, send messages to other people, or unlock things**, make the handler require a confirmation step. Don't let one misheard sentence do something irreversible.

## Project layout

```
jarvis/
  main.py        FastAPI app: /voice (call webhook), /voice/relay (WebSocket), /sms
  brain.py       Claude conversation loop: streaming, tools, personality prompt
  tools.py       Everything Jarvis can do
  db.py          SQLite: memories, reminders, text history
  reminders.py   Background loop that texts you when reminders are due
  messaging.py   Sending texts via Twilio
  security.py    Twilio signature checks + call tokens
chat.py          Terminal chat for testing
tests/           pytest suite (uses a fake Claude, no API calls)
```

## Development

```bash
pip install -r requirements-dev.txt
pytest
```
