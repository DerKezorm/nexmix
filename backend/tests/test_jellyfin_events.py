"""Jellyfin's websocket as a nudge to look: ``LibraryChanged`` that names something makes a look at the list now,
``KeepAlive`` as often as Jellyfin asks, a socket gone silent counts as dead, the token goes in the header (never the
address), no redirect is followed, and a listener opens it again after it ended."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from websockets.sync.server import Server, ServerConnection, serve

from app.services import jellyfin_events, sync


class Socket:
    """A socket that hands out made-up messages, one per ``recv``, then waits."""

    def __init__(self, messages: list[Any]) -> None:
        self.messages = [json.dumps(one) if not isinstance(one, str) else one for one in messages]
        self.sent: list[str] = []

    def recv(self, timeout: float | None = None) -> str:
        if self.messages:
            return self.messages.pop(0)
        raise TimeoutError

    def send(self, text: str) -> None:
        self.sent.append(text)


def test_only_a_library_change_that_names_something_is_a_nudge() -> None:
    changed = {"MessageType": "LibraryChanged", "Data": {"ItemsAdded": [], "ItemsRemoved": [], "ItemsUpdated": ["x"]}}
    empty = {"MessageType": "LibraryChanged", "Data": {"ItemsAdded": [], "ItemsRemoved": [], "ItemsUpdated": []}}
    assert jellyfin_events.tells_of_changes(changed)
    assert not jellyfin_events.tells_of_changes(empty)
    assert not jellyfin_events.tells_of_changes({"MessageType": "RefreshProgress", "Data": {"ItemId": "x"}})
    assert not jellyfin_events.tells_of_changes("nonsense")


def test_keep_alive_as_often_as_asked_and_silence_ends_it() -> None:
    moments = iter([0.0, 0.0, 1.0, 2.0, 10.0, 20.0, 31.0, 50.0, 200.0, 201.0])
    socket = Socket([{"MessageType": "ForceKeepAlive", "Data": 20},
                     {"MessageType": "LibraryChanged", "Data": {"ItemsAdded": ["p"]}}, "not json"])
    nudges: list[int] = []
    count = jellyfin_events.read(socket, lambda: nudges.append(1), lambda: False, clock=lambda: next(moments))
    assert count == 1 and nudges == [1]
    # Asked for every 20 seconds: said every 10 at least, until the silence of more than 75 seconds ended it.
    assert socket.sent and all(json.loads(one) == {"MessageType": "KeepAlive"} for one in socket.sent)


def test_the_address_of_the_socket_follows_the_servers() -> None:
    assert jellyfin_events.socket_address("http://jellyfin.example.com:8096") == (
        "ws://jellyfin.example.com:8096/socket", "jellyfin.example.com", 8096, False)
    assert jellyfin_events.socket_address("https://jellyfin.example.com") == (
        "wss://jellyfin.example.com/socket", "jellyfin.example.com", 443, True)


@pytest.fixture
def websocket_server() -> Iterator[tuple[Server, list[dict[str, str]]]]:
    seen: list[dict[str, str]] = []

    def handler(connection: ServerConnection) -> None:
        request = connection.request
        assert request is not None
        seen.append({"path": request.path, "authorization": request.headers.get("Authorization", "")})
        if 'Token="t0ken"' not in request.headers.get("Authorization", ""):
            connection.close(1008)
            return
        connection.send(json.dumps({"MessageType": "ForceKeepAlive", "Data": 60}))
        connection.send(json.dumps({"MessageType": "LibraryChanged",
                                    "Data": {"ItemsAdded": [], "ItemsRemoved": ["p1"], "ItemsUpdated": []}}))
        time.sleep(0.3)

    server = serve(handler, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, seen
    server.shutdown()


def test_a_real_socket_signs_in_by_the_header_and_nudges_a_look(
        websocket_server: tuple[Server, list[dict[str, str]]], monkeypatch: pytest.MonkeyPatch) -> None:
    server, seen = websocket_server
    port = server.socket.getsockname()[1]
    looked: list[int | None] = []
    monkeypatch.setattr(sync, "look_now", lambda server_id=None: looked.append(server_id))
    opened = jellyfin_events.open_socket(f"http://127.0.0.1:{port}", "t0ken", "nexmix-abc")
    try:
        got = jellyfin_events.read(opened, lambda: sync.look_now(7), lambda: False)
    except Exception:  # noqa: BLE001 - the server closes it after its messages
        got = 1 if looked else 0
    finally:
        opened.close()
    assert got == 1 and looked == [7]
    assert seen[0]["path"] == "/socket"
    # The token in the header, never in the address.
    assert 'Token="t0ken"' in seen[0]["authorization"] and "t0ken" not in seen[0]["path"]


def test_a_redirect_is_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    def handler(connection: ServerConnection) -> None:
        return

    def redirect(connection: ServerConnection, request: Any) -> Response:
        return Response(302, "Found", Headers({"Location": "ws://127.0.0.1:9/socket"}))

    server = serve(handler, "127.0.0.1", 0, process_request=redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.socket.getsockname()[1]
        with pytest.raises(Exception) as caught:
            jellyfin_events.open_socket(f"http://127.0.0.1:{port}", "t0ken", "nexmix-abc")
        assert "redirect" in str(caught.value).lower() or "302" in str(caught.value)
    finally:
        server.shutdown()
