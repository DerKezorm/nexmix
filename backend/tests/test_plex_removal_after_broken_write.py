"""A write to Plex that stopped after Plex had taken the new tracks (and said so), then the track taken out again in
Plex before the next sync: the removal holds. Before, the open write still named every track it was to add, so Plex
lacking one read as "not written yet" and the next sync put it back in. Found by the wide random test (seeds 1491,
1596, 1804) and by stopping the server hard in the middle of a write of 3,000 tracks."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Account
from app.services import jobs, plex_sync

from .fakeplex import FakePlex
from .plex_world import connect, local_tracks, mirror, sync
from .test_plex_sync import synced


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex) -> Iterator[FakePlex]:
    connect(client, fake_plex)
    plex_sync.forget()
    yield fake_plex


def test_a_track_taken_out_in_plex_after_a_write_that_stopped_stays_out(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Abgebrochen", [3001])
    assert client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": [3003, 3005]}).status_code == 200
    seen = {"items": 0}

    def hook(_count: int, path: str) -> None:
        # The sync reads the entries (the first request to them), then adds (the second): that one goes through and
        # is answered, and the server stops before the next request (the write is not done).
        if path == f"/playlists/{key}/items":
            seen["items"] += 1
            if seen["items"] == 2:
                jobs.stopping.set()
                world.hook = None

    world.hook = hook
    sync(world, rounds=3)
    world.hook = None
    assert world.lists.tracks(key) == [3001, 3003, 3005]
    assert mirror(playlist_id).writing is not None, "the write is to be open here"
    jobs.stopping.clear()
    with SessionLocal() as db:
        jobs.recover(db, plex_sync.LANE)
        plex_sync.settle(db)

    # Taken out in Plexamp before nexmix is back.
    world.lists.remove_track(key, 3003)
    world.tick()
    for _round in range(3):
        with SessionLocal() as db:
            plex_sync.request_all(db)
        world.tick(20)
        sync(world, look_at_plex=True)
    assert world.lists.tracks(key) == local_tracks(playlist_id)
    assert 3003 not in local_tracks(playlist_id), "the track taken out in Plex came back"


def _break_the_next_add(world: FakePlex, key: int, *, plex_did_it: bool) -> None:
    """The connection breaks at the next request that adds tracks to the playlist: after Plex took them (the answer is
    lost), or before Plex saw the request."""
    import httpx

    original = world.lists.handle

    def handle(request, path, params):
        if request.method == "PUT" and path == f"/playlists/{key}/items":
            world.lists.handle = original  # type: ignore[method-assign]
            if plex_did_it:
                original(request, path, params)
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        return original(request, path, params)

    world.lists.handle = handle  # type: ignore[method-assign]


def test_the_first_track_of_a_write_whose_answer_was_lost_taken_out_in_plex_stays_out(client: TestClient,
                                                                                      world: FakePlex) -> None:
    """The answer lost, so nothing said what Plex took; Plex holds the others of the same request, in a row: the
    request arrived, and the one missing was taken out there."""
    playlist_id, key = synced(client, world, "Verloren", [3001])
    assert client.post(f"/api/v1/playlists/{playlist_id}/items",
                       json={"tracks": [3003, 3005, 3007, 3009]}).status_code == 200
    _break_the_next_add(world, key, plex_did_it=True)
    sync(world, rounds=3)
    assert world.lists.tracks(key) == [3001, 3003, 3005, 3007, 3009]
    assert mirror(playlist_id).writing is not None, "the write is to be open here"
    world.lists.remove_track(key, 3003)
    world.tick()
    for _round in range(3):
        with SessionLocal() as db:
            plex_sync.request_all(db)
        world.tick(20)
        sync(world, look_at_plex=True)
    assert 3003 not in local_tracks(playlist_id), "the track taken out in Plex came back"
    assert world.lists.tracks(key) == local_tracks(playlist_id) == [3001, 3005, 3007, 3009]


def test_a_track_added_in_plex_too_says_nothing_about_the_others_of_a_write_that_never_arrived(
        client: TestClient, world: FakePlex) -> None:
    """The write broke off before Plex did anything; meanwhile somebody adds one of its tracks in Plex. That one track
    in Plex is no sign that the write arrived: the other track is still to be added, not taken out."""
    playlist_id, key = synced(client, world, "Nie angekommen", [3001])
    assert client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": [3003, 3005]}).status_code == 200
    _break_the_next_add(world, key, plex_did_it=False)
    sync(world, rounds=3)
    assert world.lists.tracks(key) == [3001]
    assert mirror(playlist_id).writing is not None, "the write is to be open here"
    world.lists.add(key, [3005])
    world.tick()
    for _round in range(3):
        with SessionLocal() as db:
            plex_sync.request_all(db)
        world.tick(20)
        sync(world, look_at_plex=True)
    assert 3003 in local_tracks(playlist_id), "a track added in nexmix was lost"
    assert sorted(world.lists.tracks(key)) == sorted(local_tracks(playlist_id)) == [3001, 3003, 3005]


def test_two_tracks_added_in_plex_by_hand_do_not_pass_for_a_write_that_never_arrived(client: TestClient,
                                                                                    world: FakePlex) -> None:
    """Two tracks of the broken write added in Plex by hand, one after the other with another in between: their ids do
    not lie as their places in the write, so they say nothing about the first track, which is still to be added."""
    playlist_id, key = synced(client, world, "Von Hand", [3001])
    assert client.post(f"/api/v1/playlists/{playlist_id}/items",
                       json={"tracks": [3003, 3005, 3007]}).status_code == 200
    _break_the_next_add(world, key, plex_did_it=False)
    sync(world, rounds=3)
    assert mirror(playlist_id).writing is not None, "the write is to be open here"
    world.lists.add(key, [3005])
    world.lists.add(key, [3011])
    world.lists.add(key, [3007])
    world.tick()
    for _round in range(3):
        with SessionLocal() as db:
            plex_sync.request_all(db)
        world.tick(20)
        sync(world, look_at_plex=True)
    assert 3003 in local_tracks(playlist_id), "a track added in nexmix was lost"
    assert sorted(world.lists.tracks(key)) == sorted(local_tracks(playlist_id)) == [3001, 3003, 3005, 3007, 3011]


def test_a_track_plex_took_while_it_refused_another_and_then_taken_out_there_stays_out(client: TestClient,
                                                                                       world: FakePlex) -> None:
    """Plex took one track of the part and refused the other, so its answer says less than was asked: what it took is
    only known from reading the entries again. The write stops right after that read; the track taken out in Plex
    afterwards stays out."""
    playlist_id, key = synced(client, world, "Halb genommen", [3001])
    world.lists.refuse.add(3005)
    assert client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": [3003, 3005]}).status_code == 200
    seen = {"items": 0}

    def hook(_count: int, path: str) -> None:
        # Read at the start, the add, the read after it: that one is answered, the server stops before the next.
        if path == f"/playlists/{key}/items":
            seen["items"] += 1
            if seen["items"] == 3:
                jobs.stopping.set()
                world.hook = None

    world.hook = hook
    sync(world, rounds=3)
    world.hook = None
    world.lists.refuse.clear()
    assert world.lists.tracks(key) == [3001, 3003]
    assert mirror(playlist_id).writing is not None, "the write is to be open here"
    jobs.stopping.clear()
    with SessionLocal() as db:
        jobs.recover(db, plex_sync.LANE)
        plex_sync.settle(db)
    world.lists.remove_track(key, 3003)
    world.tick()
    for _round in range(3):
        with SessionLocal() as db:
            plex_sync.request_all(db)
        world.tick(20)
        sync(world, look_at_plex=True)
    assert 3003 not in local_tracks(playlist_id), "the track taken out in Plex came back"
    assert world.lists.tracks(key) == local_tracks(playlist_id)
