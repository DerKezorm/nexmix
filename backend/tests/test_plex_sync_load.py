"""What the sync asks of Plex: requests per minute when nothing changes (3 and 200 playlists), the time an order
changed in Plex takes at most to arrive (as the Plex card says), a large new order written in parts with the lane free
in between, and another server: entries found there by their track's guid, title or song, never by the key alone."""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import timedelta
from itertools import pairwise

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock
from app.db import SessionLocal
from app.models import OPERATOR, Account
from app.services import jobs, media, playlists
from app.services import sync as server_sync

from .fakeplex import FakePlex
from .fakeplex.library import TRACK, Item
from .plex_world import both_equal, connect, keys_of, local_items, local_tracks, mirror, plex_key, sync, tick
from .test_plex_sync import add, quiet, synced

_shift = [0.0]


@pytest.fixture
def moving_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    real = clock.now
    _shift[0] = 0.0
    monkeypatch.setattr(clock, "now", lambda: real() + timedelta(seconds=_shift[0]))


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex) -> Iterator[FakePlex]:
    connect(client, fake_plex)
    server_sync.forget()
    yield fake_plex


def later(world: FakePlex, seconds: int) -> None:
    world.tick(seconds)
    _shift[0] += seconds


def minute(world: FakePlex) -> int:
    """One minute of looks every 15 seconds; the requests it made."""
    before = len(world.library_calls())
    for _look in range(4):
        later(world, 15)
        sync(world, look_at_plex=True)
    return len(world.library_calls()) - before


def _owner() -> Account:
    with SessionLocal() as db:
        owner = db.scalars(select(Account).where(Account.role == OPERATOR)).first()
        assert owner is not None
        db.expunge(owner)
    return owner


# --- Load -------------------------------------------------------------------------------------------------------------


def test_three_quiet_playlists_cost_a_few_requests_a_minute(client: TestClient, world: FakePlex, moving_clock: None,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    for number in range(3):
        synced(client, world, f"Ruhe {number}", [3001, 3003])
    quiet(world)
    counts = [minute(world) for _minute in range(6)]
    with capsys.disabled():
        print("\nquiet, 3 playlists, requests per minute:", counts)
    # Four looks a minute, and each playlist read once in two minutes.
    assert sum(counts) / len(counts) <= 6.5 and max(counts) <= 8, counts
    assert client.get("/api/v1/plex/playlists").json()["order_minutes"] == 2


def test_200_quiet_playlists_are_each_read_within_what_the_card_says(client: TestClient, world: FakePlex,
                                                                    moving_clock: None,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    owner = _owner()
    with SessionLocal() as db:
        for number in range(200):
            made = playlists.create(db, owner, f"Viele {number}")
            playlists.add(db, owner, made.id, [3001 + number % 20])
    sync(world, rounds=40)
    quiet(world)
    promised = client.get("/api/v1/plex/playlists").json()["order_minutes"]
    assert promised == math.ceil(server_sync.order_seconds(200) / 60) == 5
    reads: dict[int, list[float]] = {}
    counts = []
    for _minute in range(8):
        before = len(world.library_calls())
        for _look in range(4):
            later(world, 15)
            calls = len(world.library_calls())
            sync(world, look_at_plex=True)
            for _method, path, _params in world.library_calls()[calls:]:
                if path.endswith("/items"):
                    reads.setdefault(int(path.split("/")[2]), []).append(_shift[0])
        counts.append(len(world.library_calls()) - before)
    with capsys.disabled():
        print("\nquiet, 200 playlists, requests per minute:", counts)
    # The list four times a minute, ten playlists at each look: some 44 a minute.
    assert max(counts) <= 50, counts
    # Every playlist read again, never more than the promised minutes apart.
    keys = {plex_key(playlist_id) for playlist_id in range(1, 201)}
    assert keys <= set(reads)
    gaps = [later_one - earlier for times in reads.values() for earlier, later_one in pairwise(times)]
    assert gaps and max(gaps) <= promised * 60, max(gaps)


def test_a_sync_after_a_look_does_not_ask_for_the_list_again(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Einmal", [3001])
    quiet(world)
    sync(world, look_at_plex=True)
    world.calls.clear()
    add(client, playlist_id, 3003)
    sync(world)
    asked = [(method, path) for method, path, _params in world.library_calls()]
    assert ("GET", "/playlists") not in asked, asked
    assert both_equal(world, playlist_id) == [3001, 3003]
    # Without a fresh look it asks for the list (and never for the playlist itself).
    server_sync.forget()
    world.calls.clear()
    add(client, playlist_id, 3013)
    sync(world)
    asked = [(method, path) for method, path, _params in world.library_calls()]
    assert ("GET", "/playlists") in asked and ("GET", f"/playlists/{key}") not in asked
    assert world.lists.meta_reads == 0


def test_a_rename_just_written_is_not_taken_back_from_the_last_look(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Alt", [3001])
    quiet(world)
    sync(world, look_at_plex=True)
    assert client.put(f"/api/v1/playlists/{playlist_id}", json={"name": "Neu"}).status_code == 200
    sync(world)
    add(client, playlist_id, 3003)
    sync(world)
    assert world.lists.lists[key].title == "Neu"
    assert client.get(f"/api/v1/playlists/{playlist_id}").json()["name"] == "Neu"


# --- A large new order -----------------------------------------------------------------------------------------------


@pytest.fixture
def big_world(client: TestClient, operator: Account, fake_plex: FakePlex) -> Iterator[FakePlex]:
    for number in range(5000):
        fake_plex.items[100_000 + number] = Item(100_000 + number, TRACK, f"Song {number}", 2001,
                                                 index=number + 1, rating=10)
    connect(client, fake_plex)
    server_sync.forget()
    yield fake_plex


def test_a_large_new_order_goes_in_parts_with_the_lane_free_between(client: TestClient, big_world: FakePlex,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    world = big_world
    tracks = [100_000 + number for number in range(5000)]
    owner = _owner()
    with SessionLocal() as db:
        made = playlists.create(db, owner, "Gross")
        for start in range(0, 5000, 1000):
            playlists.add(db, owner, made.id, tracks[start:start + 1000])
        big = made.id
    other, _key = synced(client, world, "Klein", [3001])
    sync(world)
    assert both_equal(world, big) == tracks
    key = plex_key(big)
    # Turned round in nexmix: no entry can stay where it is but one.
    with SessionLocal() as db:
        playlists.sort_like(db, owner, big, list(reversed(tracks)))
    add(client, other, 3003)
    moves_before = len([one for one in world.lists.writes if one[1].endswith("/move")])
    rounds = 0
    other_done_while_open = False
    for _step in range(200):
        tick()
        ran = jobs.run_pending(lane="playlists", limit=1)
        rounds += ran
        if not ran and mirror(big).writing is None and mirror(big).due_at is None:
            break
        if mirror(big).writing is not None and world.lists.tracks(plex_key(other)) == [3001, 3003]:
            other_done_while_open = True
    moves = len([one for one in world.lists.writes if one[1].endswith("/move")]) - moves_before
    with capsys.disabled():
        print(f"\nturned round, 5000 entries: {moves} moves in {rounds} jobs")
    assert other_done_while_open, "the small playlist waited for the whole new order"
    assert world.lists.tracks(key) == list(reversed(tracks)) == local_tracks(big)
    assert moves == 4999
    assert mirror(big).writing is None and mirror(big).state == "synced"


def test_moves_keep_the_longest_run_in_place(client: TestClient, big_world: FakePlex) -> None:
    world = big_world
    tracks = [100_000 + number for number in range(1000)]
    owner = _owner()
    with SessionLocal() as db:
        made = playlists.create(db, owner, "Mittel")
        playlists.add(db, owner, made.id, tracks)
        playlist_id = made.id
    sync(world)
    # Ten entries to the front, the rest stays where it is: ten moves, not a thousand.
    wanted = tracks[500:510] + tracks[:500] + tracks[510:]
    with SessionLocal() as db:
        playlists.sort_like(db, owner, playlist_id, wanted)
    before = len([one for one in world.lists.writes if one[1].endswith("/move")])
    sync(world)
    assert both_equal(world, playlist_id) == wanted
    assert len([one for one in world.lists.writes if one[1].endswith("/move")]) - before == 10


# --- Another server ---------------------------------------------------------------------------------------------------


def test_another_server_gets_the_tracks_by_guid_title_or_song_never_by_key_alone(client: TestClient,
                                                                                  world: FakePlex) -> None:
    playlist_id, _key = synced(client, world, "Umzug", [3001, 3003, 3013, 3020])
    old = {track: world.items[track] for track in (3001, 3003, 3013, 3020)}
    # The other server: the same recordings under other keys; 3013 with a guid of its own (found by title, album and
    # length), 3020 not there at all, and its key 3003 given to another song.
    new_items = {key: item for key, item in world.items.items() if item.kind != TRACK}
    for track, item in old.items():
        if track == 3020:
            continue
        guid = "local://4711" if track == 3013 else f"plex://track/{track:024x}"
        new_items[track + 5000] = Item(track + 5000, TRACK, item.title, item.parent, duration=item.duration,
                                       original=item.original, rating=item.rating, guid=guid)
    new_items[3003] = Item(3003, TRACK, "Somebody Else Entirely", 2005, rating=5, guid="plex://track/somebody-else")
    world.items.clear()
    world.items.update(new_items)
    world.lists.lists.clear()
    world.servers[0].machine = "fake-machine-attic"
    connect(client, world)
    sync(world, look_at_plex=True)
    sync(world, look_at_plex=True)
    entries = client.get(f"/api/v1/playlists/{playlist_id}").json()["items"]
    assert [entry["track"] for entry in entries] == [8001, 8003, 8013, 3020]
    hints = {entry["track"]: entry["plex_hint"] for entry in entries}
    assert hints == {8001: None, 8003: None, 8013: None, 3020: "elsewhere"}
    there = world.lists.tracks(plex_key(playlist_id))
    assert there == [8001, 8003, 8013]
    assert 3003 not in there
    with SessionLocal() as db:
        server = media.get(db, mirror(playlist_id).server_id)
        assert server is not None and server.machine_id == "fake-machine-attic"
    # Who added what stays with the entry.
    assert all(item.added_by_name for item in local_items(playlist_id))


def test_a_new_entry_keeps_the_guid_of_its_track(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Mit Kennung", [3001])
    world.lists.add(key, [3003])
    world.tick()
    sync(world, look_at_plex=True)
    rows = {item.track_id: item.guid for item in local_items(playlist_id)}
    assert rows == {3001: f"plex://track/{3001:024x}", 3003: f"plex://track/{3003:024x}"}


def test_two_tracks_whose_keys_changed_places_on_the_other_server(client: TestClient, world: FakePlex) -> None:
    playlist_id, _key = synced(client, world, "Getauscht", [3001, 3003, 3013])
    first, second = world.items[3001], world.items[3003]
    world.items[3001] = Item(3001, TRACK, second.title, second.parent, duration=second.duration,
                             guid=f"plex://track/{3003:024x}")
    world.items[3003] = Item(3003, TRACK, first.title, first.parent, duration=first.duration,
                             guid=f"plex://track/{3001:024x}")
    world.lists.lists.clear()
    world.servers[0].machine = "fake-machine-attic"
    connect(client, world)
    sync(world, look_at_plex=True)
    titles = [entry["title"] for entry in client.get(f"/api/v1/playlists/{playlist_id}").json()["items"]]
    # The other server numbers its tracks itself: by its keys, the entries found the swapped tracks.
    assert keys_of(local_tracks(playlist_id)) == [3003, 3001, 3013]
    assert titles[:2] == [first.title, second.title]
    assert both_equal(world, playlist_id) == [3003, 3001, 3013]


def test_a_recording_renamed_on_the_other_server_is_found_by_its_guid(client: TestClient, world: FakePlex) -> None:
    playlist_id, _key = synced(client, world, "Nur die Kennung", [3001])
    # There it carries another title and sits on the later album; another version of the song, on the earlier album,
    # would be the one shown for the song.
    keep = {key: item for key, item in world.items.items() if item.kind != TRACK}
    keep[9001] = Item(9001, TRACK, "Northern Lights (2024 Mix)", 2003, duration=215_000, rating=10,
                      guid=f"plex://track/{3001:024x}")
    keep[9009] = Item(9009, TRACK, "Northern Lights", 2001, duration=200_000, rating=50_000,
                      guid="plex://track/another-recording")
    world.items.clear()
    world.items.update(keep)
    world.lists.lists.clear()
    world.servers[0].machine = "fake-machine-attic"
    connect(client, world)
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [9001]
