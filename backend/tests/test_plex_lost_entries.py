"""Entries a Plex playlist loses while their tracks leave the library behave as with Jellyfin: no entry taken out,
the entry stays with the note "missing there", and goes back once the catalogue has the track again; many lost at once
while tracks left the library, or an empty playlist all of a sudden, are not taken."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Account
from app.services import catalog, jobs
from app.services import sync as server_sync

from .fakeplex import FakePlex
from .fakeplex.library import TRACK, Item
from .plex_world import both_equal, connect, local_items, local_tracks, sync
from .test_plex_sync import synced


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex) -> Iterator[FakePlex]:
    connect(client, fake_plex)
    server_sync.forget()
    yield fake_plex


def partial() -> None:
    with SessionLocal() as db:
        catalog.request_partial(db)
    jobs.run_pending(lane="catalog")


def test_an_entry_lost_with_its_track_says_so_and_goes_back_when_the_track_is_back(
        client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Verschwunden", [3001, 3003, 3013])
    kept = world.items[3003]
    world.remove(3003)
    world.lists.remove_track(key, 3003)
    world.tick()
    sync(world, look_at_plex=True)
    assert local_tracks(playlist_id) == [3001, 3003, 3013]
    assert [item.plex_hint for item in local_items(playlist_id)] == ["", "missing", ""]
    # Still gone after the catalogue's run: the note stays, nothing is written.
    partial()
    sync(world)
    assert [item.plex_hint for item in local_items(playlist_id)] == ["", "missing", ""]
    assert world.lists.tracks(key) == [3001, 3013]
    # Back in the library: written back after the next run.
    world.add(Item(3003, TRACK, kept.title, kept.parent, duration=kept.duration))
    partial()
    sync(world)
    assert both_equal(world, playlist_id) == [3001, 3013, 3003] or both_equal(world, playlist_id) == [3001, 3003, 3013]
    assert all(not item.plex_hint for item in local_items(playlist_id))


def test_many_lost_at_once_while_tracks_left_the_library_are_not_taken(client: TestClient, world: FakePlex) -> None:
    tracks = [3001, 3003, 3013, 3020, 3026, 3027]
    playlist_id, key = synced(client, world, "Viele", tracks)
    for track in tracks[:4]:
        world.lists.remove_track(key, track)
    world.remove(3027)
    world.lists.remove_track(key, 3027)
    world.tick()
    sync(world, look_at_plex=True)
    assert local_tracks(playlist_id) == tracks
    assert client.get(f"/api/v1/playlists/{playlist_id}").json()["plex"]["loss"]["count"] == 5
    # Kept them: the note goes, the entries go back to Plex after the next run of the catalogue.
    assert client.delete(f"/api/v1/playlists/{playlist_id}/missing").status_code == 200
    assert client.get(f"/api/v1/playlists/{playlist_id}").json()["plex"]["loss"] is None
    partial()
    sync(world)
    assert sorted(world.lists.tracks(key)) == sorted(tracks[:5])


def test_a_plex_playlist_emptied_at_once_is_not_taken(client: TestClient, world: FakePlex) -> None:
    tracks = [3001, 3003, 3013, 3020, 3026]
    playlist_id, key = synced(client, world, "Leer", tracks)
    for track in tracks:
        world.lists.remove_track(key, track)
    world.tick()
    sync(world, look_at_plex=True)
    assert local_tracks(playlist_id) == tracks
    partial()
    sync(world)
    assert both_equal(world, playlist_id) == tracks


def test_a_few_taken_out_in_plex_are_taken_out(client: TestClient, world: FakePlex) -> None:
    tracks = [3001, 3003, 3013, 3020, 3026, 3027]
    playlist_id, key = synced(client, world, "Wenige", tracks)
    for track in tracks[:4]:
        world.lists.remove_track(key, track)
    world.tick()
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == tracks[4:]
