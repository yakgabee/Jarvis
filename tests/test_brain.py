import asyncio

import httpx
import pytest

from jarvis.brain import Conversation, text_history
from jarvis.config import settings
from jarvis.db import Database
from jarvis.tools import ToolContext

from .fakes import FakeClient, refusal_turn, text_turn, tool_turn


def make(channel="cli", responses=(), delay=0.0):
    db = Database(":memory:")
    ctx = ToolContext(db=db, settings=settings, http=httpx.AsyncClient(), channel=channel)
    client = FakeClient(responses, delay=delay)
    return Conversation(client, settings, ctx), client, db


def assert_valid_history(messages):
    assert messages[0]["role"] == "user"
    for a, b in zip(messages, messages[1:]):
        assert a["role"] != b["role"], messages
    assert messages[-1]["role"] == "assistant"


def test_simple_reply_streams_and_records_history():
    conv, client, _ = make(responses=[text_turn("Good evening, Tony.")])
    reply = asyncio.run(conv.reply("Hello"))
    assert reply == "Good evening, Tony."
    assert_valid_history(conv.messages)
    # Timestamp is prepended to the user's text.
    assert conv.messages[0]["content"].startswith("[")
    assert conv.messages[0]["content"].endswith("Hello")


def test_request_shape():
    conv, client, db = make(channel="voice", responses=[text_turn("Hi.")])
    db.add_fact("Prefers tea over coffee")
    asyncio.run(conv.reply("Hello"))
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert "thinking" not in req
    assert req["output_config"] == {"effort": "low"}
    assert req["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req["betas"]
    names = [t["name"] for t in req["tools"]]
    assert "end_call" in names and "web_search" in names and "set_reminder" in names
    system_text = " ".join(b["text"] for b in req["system"])
    assert "Prefers tea over coffee" in system_text
    assert "PHONE CALL" in system_text


def test_sms_channel_has_no_end_call():
    conv, client, _ = make(channel="sms", responses=[text_turn("Hi.")])
    asyncio.run(conv.reply("Hello"))
    names = [t["name"] for t in client.requests[0]["tools"]]
    assert "end_call" not in names
    assert client.requests[0]["output_config"] == {"effort": "medium"}


def test_tool_loop_runs_tool_and_continues():
    conv, client, db = make(
        responses=[
            tool_turn("remember", {"fact": "Birthday is May 29"}),
            text_turn("Noted, Tony."),
        ]
    )
    reply = asyncio.run(conv.reply("Remember my birthday is May 29"))
    assert reply == "Noted, Tony."
    assert db.list_facts() == [(1, "Birthday is May 29")]
    second = client.requests[1]["messages"]
    tool_result = second[-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_1"
    assert "is_error" not in tool_result
    assert_valid_history(conv.messages)


def test_bad_tool_input_is_reported_as_error_not_crash():
    conv, client, _ = make(
        responses=[tool_turn("set_reminder", {"message": "x"}), text_turn("When should I remind you?")]
    )
    asyncio.run(conv.reply("remind me"))
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "exactly one" in result["content"]


def test_unknown_tool_is_error():
    conv, client, _ = make(responses=[tool_turn("launch_missiles", {}), text_turn("I can't do that.")])
    asyncio.run(conv.reply("go"))
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True


def test_refusal_says_sorry_and_keeps_history_valid():
    conv, _, _ = make(responses=[refusal_turn()])
    reply = asyncio.run(conv.reply("something bad"))
    assert "can't help" in reply
    assert_valid_history(conv.messages)


def test_interruption_commits_partial_reply():
    conv, _, _ = make(responses=[text_turn("This is a very long answer that keeps going.", chunks=10)], delay=0.01)

    async def run():
        received = []

        async def consume():
            async for chunk in conv.respond("Tell me a story"):
                received.append(chunk)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.035)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return received

    received = asyncio.run(run())
    assert 0 < len(received) < 10
    assert_valid_history(conv.messages)
    assert conv.messages[-1]["content"].endswith("(interrupted)")


def test_end_call_tool_sets_flag():
    conv, _, _ = make(channel="voice", responses=[tool_turn("end_call", {}, preface=""), text_turn("Goodbye, Tony.")])
    asyncio.run(conv.reply("bye"))
    assert conv.ctx.end_call_requested is True


def test_text_history_normalises():
    rows = [("assistant", "orphan"), ("user", "a"), ("user", "b"), ("assistant", "c"), ("user", "d")]
    assert text_history(rows) == [
        {"role": "user", "content": "a\n\nb"},
        {"role": "assistant", "content": "c"},
    ]
