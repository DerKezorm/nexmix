"""Tracks "only in <server>" and mirrors let go of, against the stand-in Plex and Jellyfin (``mirror_world``):

* a track only in the mirror survives moving the playlist there and back, several times, with nexmix started anew in
  between, and is never held out of a copy that became the source;
* an entry matched by hand to a track shown as "only there" takes it over: the track is written there once, never a
  second time, and an entry new in nexmix found as such a track takes it over too;
* never one track of a copy for an entry and as "only there" at once;
* a mirror let go of ("it stays there as it is") is never deleted on its server: not when the playlist goes for good,
  not after 30 days in the trash, not when its server was let go of and comes back."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock
from app.db import SessionLocal
from app.models import Account, MirrorExtra, MirrorItem
from app.services import jobs
from app.services import playlists as playlist_service
from app.services import sync as server_sync

from .fakejellyfin import FakeJellyfin
from .fakeplex import FakePlex
from .mirror_world import (
    connect_both,
    copy_row,
    detail,
    extras,
    jelly_numbers,
    jelly_titles,
    local_titles,
    mapping,
    mirror_key,
    plex_numbers,
    plex_titles,
)
from .plex_world import make, sync

PLAYLIST = [3001, 3003, 3013, 3017, 3026, 3021]
LEFT_OUT = frozenset({3021})


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex,
          fake_jellyfin: FakeJellyfin) -> tuple[FakePlex, FakeJellyfin, int, dict[int, str]]:
    _plex, jelly, twins = connect_both(client, fake_plex, fake_jellyfin, LEFT_OUT)
    return fake_plex, fake_jellyfin, jelly, twins


def mirrored(client: TestClient, world: tuple, tracks: list[int] | None = None, name: str = "Sommerfest") -> int:
    fake_plex, _fake, jelly, _twins = world
    made = make(client, name, plex_numbers(tracks or PLAYLIST))
    sync(fake_plex)
    answer = client.post(f"/api/v1/playlists/{made['id']}/mirrors", json={"server": jelly})
    assert answer.status_code == 201, answer.text
    sync(fake_plex)
    return int(made["id"])


def own_track(fake: FakeJellyfin, name: str) -> str:
    """A track of Jellyfin's own (the stand-in Plex has none of that name)."""
    return next(ident for ident, one in fake.tracks.items() if one.name == name)


def every_round() -> None:
    with SessionLocal() as db:
        server_sync.request_all(db)


def started_anew() -> None:
    """nexmix stopped and started again: nothing kept in memory, the lane recovered."""
    server_sync.forget()
    jobs.stopping.clear()
    with SessionLocal() as db:
        jobs.recover(db, server_sync.LANE)
        server_sync.settle(db)


def never_both() -> None:
    """No track of a copy stands for an entry and is shown as "only there" at once."""
    with SessionLocal() as db:
        doubled = db.execute(select(MirrorExtra.copy_id, MirrorExtra.track_id).join(
            MirrorItem, (MirrorItem.copy_id == MirrorExtra.copy_id) & (MirrorItem.track_id == MirrorExtra.track_id)
        )).all()
    assert doubled == []


# --- Moving there and back ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("anew", [False, True], ids=["running", "started-anew-between"])
def test_a_track_only_in_the_mirror_survives_moving_there_and_back_again_and_again(
        client: TestClient, world: tuple, anew: bool) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    own = own_track(fake, "Glasfluss")
    fake.list_add(mirror_key(playlist_id, jelly), [own])
    sync(fake_plex, look_at_plex=True)
    assert [one.title for one in extras(playlist_id, jelly)] == ["Glasfluss"]
    there: list[str] | None = None
    for _turn in range(3):
        assert client.post(f"/api/v1/playlists/{playlist_id}/move", json={"server": jelly}).status_code == 200
        if anew:
            started_anew()
        sync(fake_plex, look_at_plex=True)
        assert extras(playlist_id, jelly) == [], "a source holds nothing out"
        now = local_titles(playlist_id)
        assert "Glasfluss" in now
        if there is None:
            there = now
        assert now == there
        assert client.post(f"/api/v1/playlists/{playlist_id}/move", json={"server": 1}).status_code == 200
        if anew:
            started_anew()
        sync(fake_plex, look_at_plex=True)
        every_round()
        sync(fake_plex, look_at_plex=True)
        assert local_titles(playlist_id) == there, "an entry went when the playlist moved back"
        assert extras(playlist_id, 1) == [] and extras(playlist_id, jelly) == []
        never_both()
    raw = fake.tracks_of(mirror_key(playlist_id, jelly))
    assert raw.count(own) == 1 and len(raw) == len(set(raw))
    assert jelly_titles(fake, playlist_id, jelly) == [one for one in there or [] if one != "Op. 3 (No. 2)"]
    # The entry of Jellyfin's own track is not on Plex: it stays in nexmix, never written to Plex.
    assert plex_titles(fake_plex, playlist_id) == [one for one in there or [] if one != "Glasfluss"]


# --- Matched by hand, or found, as a track only there -------------------------------------------------------------


def test_an_entry_matched_by_hand_to_a_track_only_there_takes_it_over(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    own = own_track(fake, "Eisblume")
    fake.list_add(key, [own])
    sync(fake_plex, look_at_plex=True)
    assert [one.title for one in extras(playlist_id, jelly)] == ["Eisblume"]
    entry = next(item["id"] for item in detail(client, playlist_id)["items"] if item["title"] == "Op. 3 (No. 2)")
    answer = client.put(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}/entries/{entry}",
                        json={"track": jelly_numbers(jelly, [own])[0]})
    assert answer.status_code == 200, answer.text
    assert extras(playlist_id, jelly) == [], "the track stands for the entry now, it is only there no more"
    assert answer.json()["mirrors"][0]["extras"] == []
    writes = len(fake.list_writes)
    for _turn in range(6):
        every_round()
        sync(fake_plex, look_at_plex=True)
        never_both()
    raw = fake.tracks_of(key)
    assert raw.count(own) == 1, "the track went into Jellyfin a second time"
    assert len(fake.list_writes) == writes, "nothing to write: the track is there already"
    assert "Op. 3 (No. 2)" in local_titles(playlist_id) and "Eisblume" not in local_titles(playlist_id)
    assert mapping(playlist_id, jelly)[entry][0] == jelly_numbers(jelly, [own])[0]
    assert detail(client, playlist_id)["mirrors"][0]["absent"] == 0


def test_a_track_only_there_dropped_is_taken_by_no_entry(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    own = own_track(fake, "Eisblume")
    fake.list_add(key, [own])
    sync(fake_plex, look_at_plex=True)
    extra = extras(playlist_id, jelly)[0]
    # Dropped, then chosen for an entry before the mirror's sync took it out: it stays, for the entry, once.
    assert client.delete(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}/extras/{extra.id}").status_code == 204
    entry = next(item["id"] for item in detail(client, playlist_id)["items"] if item["title"] == "Op. 3 (No. 2)")
    assert client.put(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}/entries/{entry}",
                      json={"track": jelly_numbers(jelly, [own])[0]}).status_code == 200
    for _turn in range(3):
        every_round()
        sync(fake_plex, look_at_plex=True)
        never_both()
    assert fake.tracks_of(key).count(own) == 1


def test_a_new_entry_found_as_a_track_only_there_takes_it_over(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, twins = world
    playlist_id = mirrored(client, world, [3001, 3003])
    key = mirror_key(playlist_id, jelly)
    # Put into Jellyfin and shown as "only there" (as a sync leaves a track the source's library had no sure match
    # for at that time).
    with SessionLocal() as db:
        row = copy_row(playlist_id, jelly)
        number = jelly_numbers(jelly, [twins[3013]])[0]
        db.add(MirrorExtra(copy_id=row.id, track_id=number, title=fake_plex.items[3013].title, artist="", album="", duration=0,
                           found_at=clock.now()))
        db.commit()
    fake.list_add(key, [twins[3013]])
    sync(fake_plex, look_at_plex=True)
    # Now the same song is put into the playlist in nexmix: its entry stands for that track in Jellyfin.
    assert client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": plex_numbers([3013])}).status_code \
        == 200
    for _turn in range(3):
        every_round()
        sync(fake_plex, look_at_plex=True)
        never_both()
    assert extras(playlist_id, jelly) == []
    assert fake.tracks_of(key).count(twins[3013]) == 1
    assert local_titles(playlist_id).count(fake_plex.items[3013].title) == 1


# --- A mirror let go of stays where it is ------------------------------------------------------------------------


def test_a_mirror_let_go_of_stays_on_its_server_when_the_playlist_goes_for_good(client: TestClient,
                                                                                world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    tracks = list(fake.tracks_of(key))
    assert client.delete(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}").status_code == 204
    sync(fake_plex)
    assert client.delete(f"/api/v1/playlists/{playlist_id}").status_code == 200
    sync(fake_plex)
    assert client.delete(f"/api/v1/playlists/{playlist_id}?purge=true").status_code == 200
    every_round()
    sync(fake_plex, look_at_plex=True)
    assert key in fake.lists and fake.tracks_of(key) == tracks, "a mirror let go of was deleted on its server"
    # Never taken in as a new playlist either.
    assert client.get("/api/v1/playlists").json()["playlists"] == []


def test_a_mirror_let_go_of_stays_on_its_server_after_thirty_days_in_the_trash(client: TestClient,
                                                                              world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    plex_list = mirror_key(playlist_id, 1)
    assert client.delete(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}").status_code == 204
    assert client.delete(f"/api/v1/playlists/{playlist_id}").status_code == 200
    sync(fake_plex)
    with SessionLocal() as db:
        assert playlist_service.purge(db, clock.now() + timedelta(days=31)) == 1
    every_round()
    sync(fake_plex, look_at_plex=True)
    assert key in fake.lists
    # The source's copy went with the playlist, as before.
    assert int(plex_list) not in fake_plex.lists.lists


def test_a_mirror_on_a_server_let_go_of_stays_there_when_it_comes_back_and_the_playlist_goes(
        client: TestClient, world: tuple) -> None:
    from .jellyfin_world import connect as connect_jellyfin

    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    assert client.delete(f"/api/v1/servers/{jelly}").status_code == 204
    again = connect_jellyfin(client, fake)
    assert again == jelly
    server_sync.look_now()
    sync(fake_plex, look_at_plex=True)
    assert client.delete(f"/api/v1/playlists/{playlist_id}").status_code == 200
    sync(fake_plex)
    with SessionLocal() as db:
        playlist_service.purge(db, clock.now() + timedelta(days=31))
    every_round()
    sync(fake_plex, look_at_plex=True)
    assert key in fake.lists, "the mirror forgotten with its server was deleted there when it came back"
    assert client.get("/api/v1/playlists").json()["playlists"] == []


def test_a_mirror_let_go_of_and_made_again_is_taken_up_and_kept_in_step(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    assert client.delete(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}").status_code == 204
    sync(fake_plex)
    assert client.post(f"/api/v1/playlists/{playlist_id}/mirrors", json={"server": jelly}).status_code == 201
    sync(fake_plex, look_at_plex=True)
    assert mirror_key(playlist_id, jelly) == key, "the copy left there is taken up again"
    # Kept in step again: the playlist going for good takes it along now.
    assert client.delete(f"/api/v1/playlists/{playlist_id}").status_code == 200
    sync(fake_plex)
    with SessionLocal() as db:
        playlist_service.purge(db, clock.now() + timedelta(days=31))
    every_round()
    sync(fake_plex, look_at_plex=True)
    assert key not in fake.lists
