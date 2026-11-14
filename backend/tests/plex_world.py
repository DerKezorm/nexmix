"""A connected stand-in Plex with its catalogue fetched, for the tests of the playlists' sync: helpers to run the sync
the way the server's clock and lane would, and to look at both sides."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock
from app.db import SessionLocal
from app.models import Playlist, PlaylistItem, PlexPlaylist
from app.services import jobs, plex_sync

from .fakeplex import FakePlex


def connect(client: TestClient, fake: FakePlex) -> None:
    answer = client.post("/api/v1/plex/connect", json={"url": "http://127.0.0.1:32400", "token": fake.account_token})
    assert answer.status_code == 200, answer.text
    assert jobs.run_pending(lane="catalog") == 1


def tick(seconds: float = 3.0) -> int:
    with SessionLocal() as db:
        return plex_sync.tick(db, clock.now() + timedelta(seconds=seconds))


def look() -> None:
    """A look at the list of Plex right now (as every 15 seconds), and what it makes due."""
    plex_sync.look_now()


def sync(fake: FakePlex | None = None, *, rounds: int = 12, look_at_plex: bool = False) -> int:
    """Runs the clock and the lane until nothing is due any more. ``look_at_plex``: with a look at the list of Plex
    first (Plex's ``updatedAt`` all there by then)."""
    if fake is not None:
        fake.lists.settle()
    if look_at_plex:
        look()
    ran = 0
    for _round in range(rounds):
        queued = tick()
        done = jobs.run_pending(lane="playlists")
        ran += done
        if not queued and not done:
            return ran
    return ran


def mirror(playlist_id: int) -> PlexPlaylist:
    with SessionLocal() as db:
        row = db.scalars(select(PlexPlaylist).where(PlexPlaylist.playlist_id == playlist_id)).one()
        db.expunge(row)
    return row


def local_tracks(playlist_id: int) -> list[int]:
    with SessionLocal() as db:
        return list(db.scalars(select(PlaylistItem.track_id).where(
            PlaylistItem.playlist_id == playlist_id, PlaylistItem.removed_at.is_(None))
            .order_by(PlaylistItem.position, PlaylistItem.id)))


def local_items(playlist_id: int) -> list[PlaylistItem]:
    with SessionLocal() as db:
        found = list(db.scalars(select(PlaylistItem).where(
            PlaylistItem.playlist_id == playlist_id, PlaylistItem.removed_at.is_(None))
            .order_by(PlaylistItem.position, PlaylistItem.id)))
        for one in found:
            db.expunge(one)
    return found


def playlist_row(playlist_id: int) -> Playlist:
    with SessionLocal() as db:
        row = db.get(Playlist, playlist_id)
        assert row is not None
        db.expunge(row)
    return row


def plex_key(playlist_id: int) -> int:
    key = mirror(playlist_id).rating_key
    assert key is not None
    return key


def make(client: TestClient, name: str, tracks: list[int] | None = None) -> dict[str, Any]:
    made = client.post("/api/v1/playlists", json={"name": name})
    assert made.status_code == 201, made.text
    if tracks:
        added = client.post(f"/api/v1/playlists/{made.json()['id']}/items", json={"tracks": tracks})
        assert added.status_code == 200, added.text
    return client.get(f"/api/v1/playlists/{made.json()['id']}").json()


def entry_of(client: TestClient, playlist_id: int, track: int) -> int:
    for item in client.get(f"/api/v1/playlists/{playlist_id}").json()["items"]:
        if item["track"] == track:
            return int(item["id"])
    raise AssertionError(f"no entry for {track}")


def both_equal(fake: FakePlex, playlist_id: int) -> list[int]:
    """Both sides hold the same tracks in the same order, each once; returns them."""
    here = local_tracks(playlist_id)
    there = fake.lists.tracks(plex_key(playlist_id))
    assert here == there, (here, there)
    assert len(set(here)) == len(here)
    return here
