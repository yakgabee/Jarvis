"""A scripted stand-in for the Anthropic client, so tests don't hit the API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace


@dataclass
class Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        if self.type == "text":
            return {"type": "text", "text": self.text}
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


def text_turn(text: str, chunks: int = 3) -> SimpleNamespace:
    return SimpleNamespace(content=[Block("text", text=text)], stop_reason="end_turn", stop_details=None, _chunks=chunks)


def tool_turn(name: str, args: dict, tool_id: str = "toolu_1", preface: str = "") -> SimpleNamespace:
    content = ([Block("text", text=preface)] if preface else []) + [
        Block("tool_use", id=tool_id, name=name, input=args)
    ]
    return SimpleNamespace(content=content, stop_reason="tool_use", stop_details=None, _chunks=1)


def refusal_turn() -> SimpleNamespace:
    return SimpleNamespace(content=[], stop_reason="refusal", stop_details={"category": "cyber"}, _chunks=0)


class FakeStream:
    def __init__(self, response, delay: float):
        self.response = response
        self.delay = delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for block in self.response.content:
            if block.type != "text" or not block.text:
                continue
            n = max(1, self.response._chunks)
            size = max(1, -(-len(block.text) // n))
            for i in range(0, len(block.text), size):
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield SimpleNamespace(type="text", text=block.text[i : i + size])

    async def get_final_message(self):
        return self.response


class FakeClient:
    """`client.beta.messages.stream(**kwargs)` returns scripted responses in order."""

    def __init__(self, responses, delay: float = 0.0):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.delay = delay
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    async def close(self):
        pass

    def _stream(self, **kwargs):
        # Snapshot messages: the caller's list keeps changing after the call.
        self.requests.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        return FakeStream(self.responses.pop(0), self.delay)
