"""The real websocket against the stand-in behind a socket (``fakema/serve.py``): the greeting, ``auth``, an answer in
parts put together, an event handed on, the end of the socket noticed, and a token Music Assistant refuses."""

from __future__ import annotations

import contextlib
import queue
import socket
import threading
import time
from collections.abc import Iterator

import pytest

from app.services import music_assistant
from app.services.music_assistant import Connection, MaError

from .fakema import FakeMA
from .fakema.serve import make_server


@pytest.fixture
def served() -> Iterator[tuple[FakeMA, str]]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    fake = FakeMA()
    server = make_server(fake, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake, f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        music_assistant.forget()


def test_the_websocket_signs_in_and_puts_parts_together(served: tuple[FakeMA, str]) -> None:
    fake, url = served
    connection = Connection(url, fake.token)
    connection.open()
    try:
        assert connection.server["schema"] == 65
        # More than one part: the stand-in sends lists of more than 400 in parts.
        for number in range(450):
            fake.add_track("Lumen Vale", "Viele", 2020, f"Stück {number}", 100, number=number + 1)
        found = connection.call("music/tracks/library_items", {"limit": 1000, "offset": 0})
        assert len(found) == len(fake.tracks)
        made = connection.call("music/playlists/create_playlist", {"name": "nm-socket"})
        deadline = time.monotonic() + 5
        event = None
        while time.monotonic() < deadline and event is None:
            with contextlib.suppress(queue.Empty):
                event = connection.events.get(timeout=0.2)
        assert event is not None and event["object_id"] == f"library://playlist/{made['item_id']}"
        fake.restart()
        deadline = time.monotonic() + 5
        while connection.alive and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not connection.alive
        with pytest.raises(MaError) as broken:
            connection.call("info")
        assert broken.value.code == "ma_unreachable"
    finally:
        connection.close()


def test_a_token_refused_says_so(served: tuple[FakeMA, str]) -> None:
    fake, url = served
    connection = Connection(url, fake.make_token(-1))
    with pytest.raises(MaError) as refused:
        connection.open()
    assert refused.value.code == "ma_refused"
