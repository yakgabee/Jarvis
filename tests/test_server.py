import time

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from jarvis import main
from jarvis.config import settings
from jarvis.security import relay_token

from .fakes import FakeClient, text_turn, tool_turn

OWNER = "+15551112222"
STRANGER = "+15559998888"


def signed_post(client, path, params):
    url = settings.public_url + path
    sig = RequestValidator(settings.twilio_auth_token).compute_signature(url, params)
    return client.post(path, data=params, headers={"X-Twilio-Signature": sig})


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def test_voice_rejects_unsigned(client):
    r = client.post("/voice", data={"From": OWNER, "CallSid": "CA1"})
    assert r.status_code == 403


def test_voice_rejects_stranger(client):
    r = signed_post(client, "/voice", {"From": STRANGER, "CallSid": "CA1"})
    assert r.status_code == 200 and "<Reject" in r.text


def test_voice_connects_owner(client):
    r = signed_post(client, "/voice", {"From": OWNER, "CallSid": "CA1"})
    assert "<ConversationRelay" in r.text
    assert 'url="wss://jarvis.example.com/voice/relay"' in r.text
    assert relay_token("CA1", settings) in r.text


def test_relay_rejects_bad_token(client):
    with client.websocket_connect("/voice/relay") as ws:
        ws.send_json({"type": "setup", "callSid": "CA1", "from": OWNER, "customParameters": {"token": "nope"}})
        with pytest.raises(Exception):
            ws.receive_json()


def test_relay_conversation(client):
    main.state.claude = FakeClient([text_turn("Good evening, Tony.")])
    with client.websocket_connect("/voice/relay") as ws:
        ws.send_json({"type": "setup", "callSid": "CA1", "from": OWNER,
                      "customParameters": {"token": relay_token("CA1", settings)}})
        ws.send_json({"type": "prompt", "voicePrompt": "Hello Jarvis", "last": True})
        tokens = []
        while True:
            msg = ws.receive_json()
            assert msg["type"] == "text"
            tokens.append(msg["token"])
            if msg["last"]:
                break
    assert "".join(tokens) == "Good evening, Tony."


def test_relay_end_call(client, monkeypatch):
    monkeypatch.setattr(main, "_speech_seconds", lambda text: 0)
    main.state.claude = FakeClient([tool_turn("end_call", {}), text_turn("Goodbye.")])
    with client.websocket_connect("/voice/relay") as ws:
        ws.send_json({"type": "setup", "callSid": "CA2", "from": OWNER,
                      "customParameters": {"token": relay_token("CA2", settings)}})
        ws.send_json({"type": "prompt", "voicePrompt": "bye", "last": True})
        msgs = []
        while not msgs or msgs[-1]["type"] != "end":
            msgs.append(ws.receive_json())
    assert msgs[-1] == {"type": "end"}


def test_sms_reply(client, monkeypatch):
    sent = []

    async def fake_send(_settings, body, to=None):
        sent.append((to, body))

    monkeypatch.setattr(main, "send_sms", fake_send)
    fake = FakeClient([text_turn("Sunny and 72, Tony."), text_turn("You're welcome.")])
    main.state.claude = fake
    r = signed_post(client, "/sms", {"From": OWNER, "Body": "Weather?"})
    assert r.status_code == 200 and "<Response" in r.text
    r = signed_post(client, "/sms", {"From": OWNER, "Body": "Thanks"})
    deadline = time.time() + 2
    while len(sent) < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert sent == [(OWNER, "Sunny and 72, Tony."), (OWNER, "You're welcome.")]
    # Second text sees the first exchange as history.
    second = fake.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]


def test_sms_ignores_stranger(client, monkeypatch):
    main.state.claude = FakeClient([])
    r = signed_post(client, "/sms", {"From": STRANGER, "Body": "hi"})
    assert r.status_code == 200
