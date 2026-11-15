"""A connected stand-in Jellyfin with its catalogue fetched, for the tests of Jellyfin: connecting it the way the
operator would (Quick Connect or an API key), and looking up nexmix's numbers of its tracks."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import SERVER_JELLYFIN, MediaServer
from app.services import jobs, media

from .fakejellyfin import FakeJellyfin

ADDRESS = "http://jellyfin.example.com:8096"


def quick_connect(client: TestClient, fake: FakeJellyfin, url: str = ADDRESS, user: str = "Erika") -> dict[str, Any]:
    """Quick Connect from end to end: the code shown, confirmed in Jellyfin by ``user``, redeemed."""
    begun = client.post("/api/v1/jellyfin/quick", json={"url": url})
    assert begun.status_code == 200, begun.text
    started = begun.json()
    assert client.get(f"/api/v1/jellyfin/quick/{started['id']}").json()["done"] is False
    assert fake.confirm(started["code"], user)
    assert client.get(f"/api/v1/jellyfin/quick/{started['id']}").json()["done"] is True
    connected = client.post("/api/v1/jellyfin/connect", json={"quick": started["id"]})
    assert connected.status_code == 200, connected.text
    return connected.json()


def connect(client: TestClient, fake: FakeJellyfin, *, user: str = "Erika", run: bool = True) -> int:
    """Connected by Quick Connect, the music library and the user chosen, the catalogue fetched. Returns the server."""
    found = quick_connect(client, fake)
    server = int(found["server"]["id"])
    if found["server"]["library"] is None:
        assert client.put(f"/api/v1/jellyfin/{server}/library",
                          json={"handle": found["libraries"][0]["handle"]}).status_code == 200
    if found["server"]["user"] is None:
        handle = next(one["handle"] for one in found["users"] if one["name"] == user)
        assert client.put(f"/api/v1/jellyfin/{server}/user", json={"handle": handle}).status_code == 200
    if run:
        assert jobs.run_pending(lane="catalog") == 1
    return server


def row() -> MediaServer:
    with SessionLocal() as db:
        found = db.scalars(select(MediaServer).where(MediaServer.kind == SERVER_JELLYFIN)
                           .order_by(MediaServer.id)).first()
        assert found is not None
        db.expunge(found)
    return found


def number_of(server: int, track_id: str) -> int:
    with SessionLocal() as db:
        return media.known_numbers(db, server, media.TRACK, [track_id])[track_id]


def key_of(server: int, number: int, kind: str = media.TRACK) -> str:
    with SessionLocal() as db:
        return media.keys(db, server, kind, [number])[number]


def numbers(server: int, track_ids: list[str]) -> list[int]:
    with SessionLocal() as db:
        found = media.known_numbers(db, server, media.TRACK, track_ids)
    return [found[one] for one in track_ids]


def keys(server: int, tracks: list[int]) -> list[str]:
    with SessionLocal() as db:
        found = media.keys(db, server, media.TRACK, tracks)
    return [found[one] for one in tracks]


def list_key(playlist_id: int) -> str:
    """The playlist's id in Jellyfin."""
    from .plex_world import mirror

    key = mirror(playlist_id).ext_key
    assert key is not None
    return key


def make(client: TestClient, server: int, name: str, tracks: list[int] | None = None) -> dict[str, Any]:
    """A playlist taking its tracks from the Jellyfin server, with ``tracks`` (nexmix's numbers)."""
    made = client.post("/api/v1/playlists", json={"name": name, "server": server})
    assert made.status_code == 201, made.text
    if tracks:
        added = client.post(f"/api/v1/playlists/{made.json()['id']}/items", json={"tracks": tracks})
        assert added.status_code == 200, added.text
    return client.get(f"/api/v1/playlists/{made.json()['id']}").json()


def both_equal(fake: FakeJellyfin, server: int, playlist_id: int) -> list[str]:
    """Both sides hold the same tracks in the same order, each once in nexmix (copies in Jellyfin count once);
    returns them by Jellyfin's ids."""
    from .plex_world import local_tracks

    here = keys(server, local_tracks(playlist_id))
    there = list(dict.fromkeys(fake.tracks_of(list_key(playlist_id))))
    assert here == there, (here, there)
    assert len(set(here)) == len(here)
    return here


def sync(rounds: int = 12, *, look: bool = False) -> int:
    """The clock and the lane until nothing is due any more; ``look``: with a look at every server's list first."""
    from .plex_world import sync as run

    return run(None, rounds=rounds, look_at_plex=look)
