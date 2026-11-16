"""What the websockets of Jellyfin and Music Assistant send is not trusted: a message nested deeper than Python reads is
passed over like any other that is not JSON, and the websocket stays open."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.services import jellyfin_events, music_assistant

from .fakema import FakeMA
from .ma_world import connect

DEEP = "[" * 200_000 + "]" * 200_000


def test_a_message_of_music_assistant_nested_too_deep_is_passed_over(client: TestClient, operator: object,
                                                                     fake_ma: FakeMA) -> None:
    server = connect(client, fake_ma)
    opened = music_assistant.current(server)
    assert opened is not None
    fake_ma.channels[-1].inbox.put(DEEP)
    fake_ma.channels[-1].inbox.put('{"event": "media_item_updated", "object_id": "library://playlist/7", "data": null}')
    time.sleep(0.6)
    # The same websocket, alive, answers the next command; the event after the bad message came through.
    assert music_assistant.current(server) is opened and opened.alive
    assert opened.call("info", {}, timeout=5)["server_id"] == fake_ma.server_id
    seen = []
    while not opened.events.empty():
        seen.append(opened.events.get_nowait().get("event"))
    assert "media_item_updated" in seen


class _Socket:
    """A websocket of Jellyfin as ``jellyfin_events.read`` takes it: what it hands out, then silence."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)
        self.sent: list[str] = []

    def recv(self, timeout: float) -> str:
        if not self.messages:
            raise TimeoutError
        return self.messages.pop(0)

    def send(self, text: str) -> None:
        self.sent.append(text)


def test_a_message_of_jellyfin_nested_too_deep_is_passed_over() -> None:
    changed = '{"MessageType": "LibraryChanged", "Data": {"ItemsAdded": ["a1"]}}'
    socket_ = _Socket([DEEP, changed])
    told: list[int] = []
    moments = iter(range(1000))
    count = jellyfin_events.read(socket_, lambda: told.append(1), lambda: not socket_.messages and len(told) > 0,
                                 clock=lambda: float(next(moments)) * 0.01)
    assert count == 1 and told == [1]
