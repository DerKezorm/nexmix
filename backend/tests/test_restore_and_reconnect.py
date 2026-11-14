"""A backup brought back after Plex went on, and Plex let go of and connected again: nothing doubled in Plex or in
nexmix, nothing lost.

* Restored over a Plex that changed since the backup (a playlist made, one deleted in nexmix, entries added and taken
  out): each playlist once on each side, what was made after the backup still there.
* Disconnected and connected again to the same server: the playlists find their copies, none is made twice.
* To another server and back again: the copies left on the first server are taken up again, not taken in as new
  playlists next to the ones they belong to."""

from __future__ import annotations

import shutil
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Account, Playlist, PlexPlaylist
from app.services import backups, plex_sync

from .fakeplex import FakePlex
from .plex_world import both_equal, connect, local_tracks, make, plex_key, sync


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex) -> Iterator[FakePlex]:
    shutil.rmtree(backups.pending_folder(), ignore_errors=True)
    connect(client, fake_plex)
    plex_sync.forget()
    yield fake_plex
    shutil.rmtree(backups.pending_folder(), ignore_errors=True)


def _alive() -> dict[int, str]:
    with SessionLocal() as db:
        return {row.id: row.name for row in db.scalars(select(Playlist).where(Playlist.deleted_at.is_(None)))}


def _trashed() -> set[int]:
    with SessionLocal() as db:
        return set(db.scalars(select(Playlist.id).where(Playlist.deleted_at.is_not(None))))


def _mirrored() -> dict[int, int]:
    with SessionLocal() as db:
        return {key: pid for key, pid in db.execute(select(PlexPlaylist.rating_key, PlexPlaylist.playlist_id)
                                                    .where(PlexPlaylist.rating_key.is_not(None)))}


def _quiet(world: FakePlex) -> None:
    for _round in range(3):
        with SessionLocal() as db:
            plex_sync.request_all(db)
        world.tick(20)
        sync(world, look_at_plex=True)


def _nothing_twice(world: FakePlex) -> None:
    mirrored = _mirrored()
    alive = _alive()
    for key in world.lists.lists:
        assert key in mirrored, ("in Plex, not in nexmix", key, world.lists.lists[key].title)
        assert mirrored[key] in alive, ("in Plex, in the trash here", key)
    assert len(set(mirrored.values())) == len(mirrored), "two copies of one playlist"
    names = [one.title for one in world.lists.lists.values()]
    assert len(names) == len(set(names)), ("a name twice in Plex", sorted(names))
    here = list(alive.values())
    assert len(here) == len(set(here)), ("a name twice in nexmix", sorted(here))


def test_a_restore_over_a_plex_that_went_on(client: TestClient, world: FakePlex) -> None:
    kept = make(client, "Behalten", [3001, 3003])["id"]
    gone_here = make(client, "Hier geloescht", [3005])["id"]
    gone_there = make(client, "Dort geloescht", [3007])["id"]
    sync(world)
    gone_there_key = plex_key(gone_there)
    name = backups.create(kind=backups.MANUAL).name

    # After the backup: entries change, one playlist is made, one deleted here, one deleted in Plex.
    assert client.post(f"/api/v1/playlists/{kept}/items", json={"tracks": [3013]}).status_code == 200
    later = make(client, "Nach der Sicherung", [3020, 3021])["id"]
    assert client.delete(f"/api/v1/playlists/{gone_here}").status_code == 200
    sync(world)
    world.lists.delete(gone_there_key)
    world.tick()
    _quiet(world)
    assert both_equal(world, kept) == [3001, 3003, 3013]
    assert gone_there in _trashed() and gone_here in _trashed()
    later_key = plex_key(later)

    backups.stage_restore(name)
    assert backups.apply_pending() is True
    plex_sync.forget()
    _quiet(world)

    _nothing_twice(world)
    # What Plex has since the backup is not lost: the entry of "kept", the playlist made after it.
    assert both_equal(world, kept) == [3001, 3003, 3013]
    assert later_key in _mirrored(), "the playlist made after the backup is not in nexmix"
    assert local_tracks(_mirrored()[later_key]) == [3020, 3021]
    # The one deleted in Plex after the backup: in the trash again, with its entries.
    assert gone_there in _trashed() and local_tracks(gone_there) == [3007]
    # The one deleted here after the backup (its copy in Plex went with it): back from the backup, in the trash or
    # made again, but once.
    assert gone_here in _trashed() or gone_here in _alive()
    assert local_tracks(gone_here) == [3005]


def test_disconnected_and_connected_again_to_the_same_server(client: TestClient, world: FakePlex) -> None:
    first = make(client, "Eins", [3001, 3003])["id"]
    second = make(client, "Zwei", [3005])["id"]
    sync(world)
    keys = {plex_key(first), plex_key(second)}
    assert client.delete("/api/v1/plex").status_code == 204
    # While Plex is let go of, the catalogue is gone too: nothing can be added (it says so, ``skipped``).
    assert client.post(f"/api/v1/playlists/{first}/items", json={"tracks": [3013]}).json()["skipped"] == [3013]
    connect(client, world)
    assert client.post(f"/api/v1/playlists/{first}/items", json={"tracks": [3013]}).status_code == 200
    _quiet(world)
    _nothing_twice(world)
    assert set(world.lists.lists) == keys, "made again in Plex"
    assert both_equal(world, first) == [3001, 3003, 3013]
    assert both_equal(world, second) == [3005]


def test_to_another_server_and_back_again(client: TestClient, world: FakePlex) -> None:
    first = make(client, "Eins", [3001, 3003])["id"]
    sync(world)
    old_key = plex_key(first)
    studio_lists = dict(world.lists.lists)
    studio_machine = world.servers[0].machine

    # Another server (the same recordings, the same keys for the reader's ease), its own playlists.
    world.lists.lists.clear()
    world.servers[0].machine = "fake-machine-attic"
    connect(client, world)
    _quiet(world)
    assert both_equal(world, first) == [3001, 3003]
    attic_lists = dict(world.lists.lists)

    # Back to the first server: its copy is still there.
    world.lists.lists.clear()
    world.lists.lists.update(studio_lists)
    world.servers[0].machine = studio_machine
    connect(client, world)
    _quiet(world)
    assert attic_lists  # the other server's copy stays where it is
    alive = _alive()
    copies = [one for one in world.lists.lists.values() if one.title == "Eins"]
    assert len(alive) == 1, ("the old copy came in as a playlist of its own", alive)
    assert len(copies) == 1, ("made twice in Plex", [one.key for one in copies], old_key)
    _nothing_twice(world)


def test_deleted_for_good_on_another_server_its_first_copy_goes_when_that_server_is_back(client: TestClient,
                                                                                         world: FakePlex) -> None:
    first = make(client, "Eins", [3001, 3003])["id"]
    sync(world)
    studio_lists = dict(world.lists.lists)
    studio_machine = world.servers[0].machine

    world.lists.lists.clear()
    world.servers[0].machine = "fake-machine-attic"
    connect(client, world)
    _quiet(world)
    assert client.delete(f"/api/v1/playlists/{first}").status_code == 200
    assert client.delete(f"/api/v1/playlists/{first}?purge=true").status_code == 200
    _quiet(world)
    assert not [one for one in world.lists.lists.values() if one.title == "Eins"]

    world.lists.lists.clear()
    world.lists.lists.update(studio_lists)
    world.servers[0].machine = studio_machine
    connect(client, world)
    _quiet(world)
    assert _alive() == {}, "the old copy came in as a playlist of its own"
    assert not [one for one in world.lists.lists.values() if one.title == "Eins"], "the old copy stayed in Plex"


def test_back_on_the_first_server_the_look_at_its_list_takes_the_old_copy_for_no_new_playlist(
        client: TestClient, world: FakePlex) -> None:
    """The list of Plex may be looked at before the playlist's own sync takes its old copy up again: the copy is not
    taken in as a playlist of its own meanwhile."""
    from app.services import plex_link
    from app.services import plex_playlists as plexlists

    first = make(client, "Eins", [3001, 3003])["id"]
    sync(world)
    studio_lists = dict(world.lists.lists)
    studio_machine = world.servers[0].machine
    world.lists.lists.clear()
    world.servers[0].machine = "fake-machine-attic"
    connect(client, world)
    _quiet(world)

    world.lists.lists.clear()
    world.lists.lists.update(studio_lists)
    world.servers[0].machine = studio_machine
    connect(client, world)
    with SessionLocal() as db:
        connection = plex_link.current(db)
        assert connection is not None
        with plex_link.server_of(db, connection, timeout=10.0) as server:
            listed = plexlists.listing(server)
        plex_sync.take_in(db, connection, listed, None)
    assert _alive() == {first: "Eins"}, "the old copy came in as a playlist of its own"
    _quiet(world)
    assert _alive() == {first: "Eins"}
    _nothing_twice(world)
