"""A connected stand-in Music Assistant with its catalogue fetched, for the tests of Music Assistant: connecting it the
way the operator would (address and token, then the providers), and looking at both sides."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import SERVER_MA, MediaServer
from app.services import jobs, ma_events, media

from .fakema import FILES, FakeMA

ADDRESS = "http://ma.example.com:8095"


def connect(client: TestClient, fake: FakeMA, *, providers: tuple[str, ...] = (FILES,), run: bool = True,
            url: str = ADDRESS) -> int:
    """Connected by address and token, the providers chosen, the catalogue fetched. Returns the server."""
    answer = client.post("/api/v1/music-assistant/connect", json={"url": url, "token": fake.token})
    assert answer.status_code == 200, answer.text
    found = answer.json()
    server = int(found["server"]["id"])
    from app.services import ma_link

    handles = [ma_link.handle(fake.server_id, one) for one in providers]
    chosen = client.put(f"/api/v1/music-assistant/{server}/providers", json={"handles": handles})
    assert chosen.status_code == 200, chosen.text
    if run:
        assert jobs.run_pending(lane="catalog") >= 1
    return server


def row() -> MediaServer:
    with SessionLocal() as db:
        found = db.scalars(select(MediaServer).where(MediaServer.kind == SERVER_MA).order_by(MediaServer.id)).first()
        assert found is not None
        db.expunge(found)
    return found


def number_of(server: int, track: int) -> int:
    with SessionLocal() as db:
        return media.known_numbers(db, server, media.TRACK, [str(track)])[str(track)]


def numbers(server: int, tracks: list[int]) -> list[int]:
    with SessionLocal() as db:
        found = media.known_numbers(db, server, media.TRACK, [str(one) for one in tracks])
    return [found[str(one)] for one in tracks]


def keys(server: int, tracks: list[int]) -> list[int]:
    with SessionLocal() as db:
        found = media.keys(db, server, media.TRACK, tracks)
    return [int(found[one]) for one in tracks]


def list_key(playlist_id: int) -> int:
    from .plex_world import mirror

    key = mirror(playlist_id).ext_key
    assert key is not None
    return int(key)


def make(client: TestClient, server: int, name: str, tracks: list[int] | None = None) -> dict[str, Any]:
    """A playlist taking its tracks from the Music Assistant server, with ``tracks`` (nexmix's numbers)."""
    made = client.post("/api/v1/playlists", json={"name": name, "server": server})
    assert made.status_code == 201, made.text
    if tracks:
        added = client.post(f"/api/v1/playlists/{made.json()['id']}/items", json={"tracks": tracks})
        assert added.status_code == 200, added.text
    return client.get(f"/api/v1/playlists/{made.json()['id']}").json()


def both_equal(fake: FakeMA, server: int, playlist_id: int) -> list[int]:
    """Both sides hold the same tracks in the same order, each once; returns them by Music Assistant's ids."""
    from .plex_world import local_tracks

    here = keys(server, local_tracks(playlist_id))
    there = fake.entries(list_key(playlist_id))
    assert here == there, (here, there)
    assert len(set(here)) == len(here)
    return here


def settle(fake: FakeMA) -> None:
    """Until the websocket's thread has read everything the stand-in sent (it reads on its own)."""
    import time

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if all(one.inbox.empty() for one in fake.channels):
            time.sleep(0.03)
            if all(one.inbox.empty() for one in fake.channels):
                return
        time.sleep(0.01)


def events(server: int, fake: FakeMA | None = None) -> list[str]:
    """The events that came on the websocket, handed on (as the listener does)."""
    if fake is not None:
        settle(fake)
    else:
        import time

        time.sleep(0.25)
    return ma_events.handle_pending(server)


def sync(rounds: int = 12, *, look: bool = False) -> int:
    """The clock and the lane until nothing is due any more; ``look``: with a look at every server's list first."""
    from .plex_world import sync as run

    return run(None, rounds=rounds, look_at_plex=look)
