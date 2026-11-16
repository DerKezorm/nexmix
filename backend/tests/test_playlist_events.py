"""The live events of a playlist: a second browser sees a change at once, with the version that tells it whether it
missed one; a person whose share is taken back hears no more; the hub hands events across threads."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.models import Account
from app.routers import playlists as router
from app.services.events import Hub

from . import made_up_catalog
from .conftest import make_account, new_client


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    made_up_catalog.install()
    monkeypatch.setattr(router, "STREAM_SECONDS", 3.0)
    monkeypatch.setattr(router, "PING_SECONDS", 0.3)


class Listener:
    """A browser with the playlist open: reads its event stream in a thread of its own. ``close`` waits until the
    stream ended (the server ends it after ``STREAM_SECONDS``): a test that opened one closes it before its client
    goes, so the thread never reads from a client already closed."""

    def __init__(self, browser: TestClient, playlist_id: int) -> None:
        self.events: list[tuple[str, Any]] = []
        self.ready = threading.Event()
        #: What ended the reading other than the end of the stream (raised where the test waits, not in the thread).
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._read, args=(browser, playlist_id), daemon=True)
        self.thread.start()
        assert self.ready.wait(5), "the stream did not open"

    def _read(self, browser: TestClient, playlist_id: int) -> None:
        name = ""
        try:
            with browser.stream("GET", f"/api/v1/playlists/{playlist_id}/events") as answer:
                self.status = answer.status_code
                for line in answer.iter_lines():
                    if line.startswith("event: "):
                        name = line[7:]
                    elif line.startswith("data: "):
                        self.events.append((name, json.loads(line[6:])))
                        if name == "hello":
                            self.ready.set()
        except Exception as exc:  # noqa: BLE001 - handed to the test, never left to the thread
            self.error = exc
        finally:
            self.ready.set()

    def wait_for(self, name: str, seconds: float = 3.0) -> Any:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for event, data in self.events:
                if event == name:
                    return data
            if self.error is not None:
                raise AssertionError(f"the stream broke off: {type(self.error).__name__}")
            time.sleep(0.02)
        raise AssertionError(f"no {name} event; got {[event for event, _data in self.events]}")

    def close(self, seconds: float = 10.0) -> None:
        """Waits for the stream to end, and says whether it ended cleanly."""
        self.thread.join(seconds)
        assert not self.thread.is_alive(), "the stream did not end"
        assert self.error is None, type(self.error).__name__


@pytest.fixture
def two() -> Iterator[tuple[TestClient, TestClient, Account, Account]]:
    owner_row, other_row = make_account("olivia"), make_account("edith")
    with new_client(owner_row) as owner, new_client(other_row) as other:
        yield owner, other, owner_row, other_row


def test_a_second_browser_sees_a_change_at_once_with_the_next_version(two: Any) -> None:
    owner, other, _owner_row, other_row = two
    playlist = owner.post("/api/v1/playlists", json={"name": "Party"}).json()
    owner.put(f"/api/v1/playlists/{playlist['id']}/shares/{other_row.id}", json={"access": "edit"})
    listener = Listener(owner, playlist["id"])
    hello = listener.wait_for("hello")
    added = other.post(f"/api/v1/playlists/{playlist['id']}/items", json={"tracks": [301, 317]}).json()
    event = listener.wait_for("items")
    assert event["version"] == hello["version"] + 1 == added["version"]
    assert event["op"] == "add" and event["by"]["name"] == "edith"
    assert [entry["track"] for entry in event["entries"]] == [301, 317]
    assert event["order"] == [entry["id"] for entry in event["entries"]]
    assert event["summary"]["count"] == 2
    other.put(f"/api/v1/playlists/{playlist['id']}", json={"name": "Große Party"})
    renamed = listener.wait_for("playlist")
    assert renamed["name"] == "Große Party" and renamed["version"] == event["version"] + 1


def test_whoever_loses_the_share_hears_no_more(two: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # The look at the rights between events comes too late here: the event of the share itself must end the stream.
    monkeypatch.setattr(router, "PING_SECONDS", 30.0)
    owner, other, _owner_row, other_row = two
    playlist = owner.post("/api/v1/playlists", json={"name": "Party"}).json()
    owner.put(f"/api/v1/playlists/{playlist['id']}/shares/{other_row.id}", json={"access": "read"})
    listener = Listener(other, playlist["id"])
    owner.delete(f"/api/v1/playlists/{playlist['id']}/shares/{other_row.id}")
    listener.wait_for("gone")
    owner.post(f"/api/v1/playlists/{playlist['id']}/items", json={"tracks": [301]})
    listener.thread.join(5)
    assert not listener.thread.is_alive()
    assert [event for event, _data in listener.events if event == "items"] == []


def test_a_playlist_in_the_trash_is_gone_for_those_it_was_shared_with(two: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "PING_SECONDS", 30.0)
    owner, other, _owner_row, other_row = two
    playlist = owner.post("/api/v1/playlists", json={"name": "Party"}).json()
    owner.put(f"/api/v1/playlists/{playlist['id']}/shares/{other_row.id}", json={"access": "edit"})
    theirs, mine = Listener(other, playlist["id"]), Listener(owner, playlist["id"])
    owner.delete(f"/api/v1/playlists/{playlist['id']}")
    theirs.wait_for("gone")
    assert mine.wait_for("playlist")["op"] == "deleted"


def test_nobody_listens_to_a_playlist_they_may_not_see(two: Any) -> None:
    owner, other, _owner_row, _other_row = two
    playlist = owner.post("/api/v1/playlists", json={"name": "Party"}).json()
    assert other.get(f"/api/v1/playlists/{playlist['id']}/events").status_code == 404
    with new_client() as stranger:
        assert stranger.get(f"/api/v1/playlists/{playlist['id']}/events").status_code == 401


def test_the_hub_hands_an_event_from_another_thread_to_the_streams_loop() -> None:
    hub = Hub()
    found: list[str] = []

    async def main() -> None:
        subscriber = hub.subscribe({"playlist:1"})
        other = hub.subscribe({"playlist:2"})
        threading.Thread(target=hub.publish, args=("playlist:1", "items", {"version": 2})).start()
        found.append(await asyncio.wait_for(subscriber.queue.get(), 2))
        assert other.queue.empty()
        hub.unsubscribe(subscriber)
        hub.unsubscribe(other)
        assert hub.connections == 0

    asyncio.run(main())
    assert found == ['event: items\ndata: {"version": 2}\n\n']


def test_a_full_queue_drops_events_instead_of_waiting() -> None:
    hub = Hub()

    async def main() -> None:
        subscriber = hub.subscribe({"t"})
        for number in range(250):
            hub.publish("t", "items", {"version": number})
        assert subscriber.queue.qsize() == 200

    asyncio.run(main())


def test_an_account_holds_only_so_many_streams_and_gets_its_place_back(two: Any,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    owner, other, _owner_row, _other_row = two
    monkeypatch.setattr(router, "STREAMS_PER_ACCOUNT", 1)
    monkeypatch.setattr(router, "STREAM_SECONDS", 0.6)
    playlist = owner.post("/api/v1/playlists", json={"name": "Party"}).json()
    first = Listener(owner, playlist["id"])
    first.wait_for("hello")
    refused = owner.get(f"/api/v1/playlists/{playlist['id']}/events")
    assert refused.status_code == 429 and refused.json()["detail"]["code"] == "too_many_streams"
    # Another account is not held up by it.
    other_playlist = other.post("/api/v1/playlists", json={"name": "Eigene"}).json()
    Listener(other, other_playlist["id"]).wait_for("hello")
    # The first stream ends: its place is free again.
    first.thread.join(5)
    again = Listener(owner, playlist["id"])
    assert again.status == 200
