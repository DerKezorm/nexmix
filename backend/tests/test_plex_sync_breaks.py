"""The sync with the stand-in Plex when a write breaks off, and the cases around it: a change on either side never comes
undone because a write to Plex stopped half way; a playlist brought back from the trash is made in Plex anew; a track
Plex refuses is tried again later, not at every look; a playlist made in Plex whose answer was lost is taken, not made
twice; who may ask for a sync now."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock
from app.db import SessionLocal
from app.models import Account, ServerPlaylist
from app.services import jobs
from app.services import sync as server_sync

from .conftest import make_account, new_client
from .fakeplex import FakePlex
from .plex_world import both_equal, connect, local_tracks, make, mirror, plex_key, sync
from .test_plex_sync import add, move, quiet, remove, synced


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex) -> Iterator[FakePlex]:
    connect(client, fake_plex)
    server_sync.forget()
    yield fake_plex


def fail_on(world: FakePlex, method: str, needle: str, mode: str = "drop") -> None:
    """The first request ``method`` whose path holds ``needle`` fails (``mode``), and every one after it."""

    def hook(count: int, path: str) -> None:
        last = world.calls[-1]
        if last[0] == method and needle in path:
            world.fail_after = count - 1
            world.fail_mode = mode
            world.hook = None

    world.hook = hook


def stop_on(world: FakePlex, method: str, needle: str) -> None:
    """The server stops at the first request ``method`` whose path holds ``needle`` (it is not answered)."""

    def hook(_count: int, path: str) -> None:
        last = world.calls[-1]
        if last[0] == method and needle in path:
            jobs.stopping.set()
            world.hook = None

    world.hook = hook


def heal(world: FakePlex) -> None:
    world.fail_after = None
    world.hook = None


def restart() -> None:
    jobs.stopping.clear()
    with SessionLocal() as db:
        jobs.recover(db, server_sync.LANE)
        server_sync.settle(db)


_shift = [0.0]


@pytest.fixture
def moving_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    real = clock.now
    _shift[0] = 0.0
    monkeypatch.setattr(clock, "now", lambda: real() + timedelta(seconds=_shift[0]))


def later(world: FakePlex, seconds: int) -> None:
    """Time passes on both clocks."""
    world.tick(seconds)
    _shift[0] += seconds


# --- A write to Plex broken off -------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["drop", "timeout", "503", "stop"])
def test_a_removal_in_nexmix_survives_a_write_broken_off_before_the_delete(client: TestClient, world: FakePlex,
                                                                          mode: str) -> None:
    playlist_id, _key = synced(client, world, "Bruch Entfernen", [3001, 3003, 3013])
    remove(client, playlist_id, 3003)
    if mode == "stop":
        stop_on(world, "DELETE", "/items/")
    else:
        fail_on(world, "DELETE", "/items/", mode)
    sync(world)
    row = mirror(playlist_id)
    assert row.writing is not None
    assert 3003 not in local_tracks(playlist_id)
    heal(world)
    if mode == "stop":
        restart()
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3001, 3013]
    assert mirror(playlist_id).writing is None


def test_what_a_broken_write_had_still_to_do_is_kept_with_it(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Offen", [3001, 3003, 3013])
    remove(client, playlist_id, 3003)
    add(client, playlist_id, 3020)
    move(client, playlist_id, 3013, None)
    fail_on(world, "DELETE", "/items/")
    sync(world)
    writing = mirror(playlist_id).writing
    item_of = {track: item for item, track in world.lists.lists[key].items}
    assert [one[:2] for one in writing["remove"]] == [[3003, item_of[3003]]]
    assert writing["add"] == [3020]
    assert writing["order"] == [3013, 3001, 3020]
    # The ids Plex gave the entries of the old common state stay with it.
    assert {track: item for track, _entry, item in writing["items"] if item} == {
        3001: item_of[3001], 3013: item_of[3013]}


def test_a_removal_in_nexmix_put_back_in_plex_meanwhile_stays(client: TestClient, world: FakePlex) -> None:
    # Adding wins against taking out: Plexamp put it in again (a new entry) while the write was broken off.
    playlist_id, key = synced(client, world, "Wieder rein", [3001, 3003, 3013])
    remove(client, playlist_id, 3003)
    fail_on(world, "DELETE", "/items/")
    sync(world)
    heal(world)
    world.lists.remove_track(key, 3003)
    world.lists.add(key, [3003])
    world.tick()
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3001, 3013, 3003]


def test_a_removal_in_plex_survives_a_write_broken_off(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Bruch Plex", [3001, 3003, 3013])
    add(client, playlist_id, 3020)
    fail_on(world, "PUT", f"/playlists/{key}/items")
    sync(world)
    assert mirror(playlist_id).writing is not None
    heal(world)
    # Meanwhile Plexamp takes one out.
    world.lists.remove_track(key, 3001)
    world.tick()
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3003, 3013, 3020]


def test_a_track_taken_out_in_nexmix_and_put_in_again_in_plex_while_a_write_is_open_stays(
        client: TestClient, world: FakePlex) -> None:
    # Adding wins: Plexamp took it out and put it in again (a new entry) while nexmix took it out.
    playlist_id, key = synced(client, world, "Beide", [3001, 3003, 3013])
    add(client, playlist_id, 3020)
    fail_on(world, "PUT", f"/playlists/{key}/items")
    sync(world)
    heal(world)
    assert mirror(playlist_id).writing is not None
    remove(client, playlist_id, 3001)
    world.lists.remove_track(key, 3001)
    world.lists.add(key, [3001])
    world.tick()
    sync(world, look_at_plex=True)
    # Plex's order counts for nothing while a write is open: where it lands is nexmix's to say.
    assert sorted(both_equal(world, playlist_id)) == [3001, 3003, 3013, 3020]


def test_a_track_taken_out_in_plex_after_its_add_broke_off_is_added_again(client: TestClient,
                                                                         world: FakePlex) -> None:
    # Whether Plex took it before the fault is not known: added in nexmix, it stays (adding wins).
    playlist_id, key = synced(client, world, "Unbekannt", [3001, 3003])
    add(client, playlist_id, 3013)

    def lose_the_answer(_count: int, path: str) -> None:
        last = world.calls[-1]
        if last[0] == "PUT" and path == f"/playlists/{key}/items":
            world.lists.add(key, [3013])
            world.fail_after = 0
            world.fail_mode = "drop"
            world.hook = None

    world.hook = lose_the_answer
    sync(world)
    heal(world)
    world.lists.remove_track(key, 3013)
    world.tick()
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3001, 3003, 3013]


def test_a_rename_in_nexmix_survives_a_write_broken_off(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Alter Name", [3001])
    assert client.put(f"/api/v1/playlists/{playlist_id}", json={"name": "Neuer Name"}).status_code == 200
    fail_on(world, "PUT", f"/playlists/{key}")
    sync(world)
    heal(world)
    sync(world, look_at_plex=True)
    assert world.lists.lists[key].title == "Neuer Name"
    assert client.get(f"/api/v1/playlists/{playlist_id}").json()["name"] == "Neuer Name"


def test_a_rename_in_plex_while_a_write_was_broken_off_is_taken(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Erst so", [3001])
    add(client, playlist_id, 3003)
    fail_on(world, "PUT", f"/playlists/{key}/items")
    sync(world)
    heal(world)
    world.lists.rename(key, "Dann so")
    sync(world, look_at_plex=True)
    assert client.get(f"/api/v1/playlists/{playlist_id}").json()["name"] == "Dann so"
    assert both_equal(world, playlist_id) == [3001, 3003]


def test_a_change_in_nexmix_while_plex_is_written(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Waehrend", [3001, 3003, 3013])
    add(client, playlist_id, 3020)
    done: list[int] = []

    def meanwhile(_count: int, path: str) -> None:
        last = world.calls[-1]
        if not done and last[0] == "PUT" and path == f"/playlists/{key}/items":
            done.append(1)
            remove(client, playlist_id, 3003)
            add(client, playlist_id, 3026)

    world.hook = meanwhile
    sync(world)
    world.hook = None
    world.tick(5)
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3001, 3013, 3020, 3026]


def test_two_jobs_for_one_playlist_in_a_row(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Zweimal", [3001, 3003])
    add(client, playlist_id, 3013)
    world.lists.add(key, [3020])
    world.tick()
    with SessionLocal() as db:
        ident = db.scalars(select(ServerPlaylist.id).where(ServerPlaylist.playlist_id == playlist_id)).one()
        jobs.enqueue(db, server_sync.ROUND, ref=ident)
        jobs.enqueue(db, server_sync.ROUND, ref=ident)
        db.commit()
    jobs.run_pending(lane="playlists")
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3001, 3003, 3013, 3020]


# --- Brought back from the trash --------------------------------------------------------------------------------------


def _card(client: TestClient, name: str, trash: bool = False) -> dict:
    cards = client.get(f"/api/v1/playlists{'?trash=true' if trash else ''}").json()["playlists"]
    return next(card for card in cards if card["name"] == name)


def test_a_smart_playlist_deleted_in_plex_comes_back_as_an_ordinary_one(client: TestClient, world: FakePlex) -> None:
    smart = world.lists.make("Klug weg", [3001, 3003, 3013], smart=True)
    sync(world, look_at_plex=True)
    playlist_id = _card(client, "Klug weg")["id"]
    world.lists.delete(smart.key)
    sync(world, look_at_plex=True)
    assert _card(client, "Klug weg", trash=True)["plex"]["gone_in_plex"] is True
    assert client.post(f"/api/v1/playlists/{playlist_id}/restore").status_code == 200
    row = mirror(playlist_id)
    assert (row.smart, row.gone_there, row.state) == (False, False, "pending")
    sync(world)
    made = world.lists.by_title("Klug weg")
    assert made is not None and not made.smart
    assert both_equal(world, playlist_id) == [3001, 3003, 3013]
    view = client.get(f"/api/v1/playlists/{playlist_id}").json()
    assert view["smart"] is False and view["plex"]["state"] == "synced" and view["plex"]["gone_in_plex"] is False
    # Now it can be changed like any other.
    add(client, playlist_id, 3020)
    sync(world)
    assert both_equal(world, playlist_id) == [3001, 3003, 3013, 3020]


def test_an_ordinary_playlist_deleted_in_plex_comes_back_there(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Normal weg", [3001, 3003])
    world.lists.delete(key)
    sync(world, look_at_plex=True)
    assert mirror(playlist_id).gone_there is True
    assert client.post(f"/api/v1/playlists/{playlist_id}/restore").status_code == 200
    assert mirror(playlist_id).gone_there is False
    sync(world)
    view = client.get(f"/api/v1/playlists/{playlist_id}").json()["plex"]
    assert view["gone_in_plex"] is False and view["state"] == "synced"
    assert plex_key(playlist_id) != key
    assert both_equal(world, playlist_id) == [3001, 3003]


# --- A track Plex refuses -------------------------------------------------------------------------------------------


def _puts_of(world: FakePlex, key: int, track: int) -> int:
    return len([one for one in world.lists.writes
                if one[0] == "PUT" and one[1] == f"/playlists/{key}/items" and str(track) in one[2].get("uri", "")])


def test_a_refused_track_is_tried_again_less_and_less_often(client: TestClient, world: FakePlex,
                                                            moving_clock: None) -> None:
    playlist_id, key = synced(client, world, "Verweigert", [3001])
    world.lists.refuse.add(3013)
    add(client, playlist_id, 3013)
    later(world, 3)
    sync(world)
    assert _puts_of(world, key, 3013) == 1
    # Three minutes of looks every 15 seconds: one more try (after a minute), not one at every look.
    for _look in range(12):
        later(world, 15)
        sync(world, look_at_plex=True)
    assert _puts_of(world, key, 3013) == 2
    assert local_tracks(playlist_id) == [3001, 3013]
    entry = next(one for one in client.get(f"/api/v1/playlists/{playlist_id}").json()["items"]
                 if one["track"] == 3013)
    assert entry["plex_hint"] is None
    # The waits grow up to an hour; after the fifth try it stays in nexmix only, and says so.
    for _look in range(4 * 60 * 3):
        later(world, 15)
        sync(world, look_at_plex=True)
    assert _puts_of(world, key, 3013) == 5
    entry = next(one for one in client.get(f"/api/v1/playlists/{playlist_id}").json()["items"]
                 if one["track"] == 3013)
    assert entry["plex_hint"] == "refused"
    assert local_tracks(playlist_id) == [3001, 3013]
    assert world.lists.tracks(key) == [3001]
    assert mirror(playlist_id).state == "synced"


def test_a_refused_track_taken_by_plex_later_loses_its_hint(client: TestClient, world: FakePlex,
                                                           moving_clock: None) -> None:
    playlist_id, key = synced(client, world, "Spaeter doch", [3001])
    world.lists.refuse.add(3013)
    add(client, playlist_id, 3013)
    later(world, 3)
    sync(world)
    for _look in range(4 * 60 * 3):
        later(world, 15)
        sync(world, look_at_plex=True)
    assert _puts_of(world, key, 3013) == 5
    # Plexamp manages to put it in after all.
    world.lists.refuse.clear()
    world.lists.add(key, [3013])
    later(world, 15)
    sync(world, look_at_plex=True)
    entry = next(one for one in client.get(f"/api/v1/playlists/{playlist_id}").json()["items"]
                 if one["track"] == 3013)
    assert entry["plex_hint"] is None
    assert both_equal(world, playlist_id) == [3001, 3013]


def test_a_refused_track_taken_out_in_nexmix_is_forgotten(client: TestClient, world: FakePlex,
                                                          moving_clock: None) -> None:
    playlist_id, key = synced(client, world, "Weg damit", [3001])
    world.lists.refuse.add(3013)
    add(client, playlist_id, 3013)
    later(world, 3)
    sync(world)
    remove(client, playlist_id, 3013)
    later(world, 3)
    sync(world)
    for _look in range(8):
        later(world, 15)
        sync(world, look_at_plex=True)
    assert _puts_of(world, key, 3013) == 1
    assert mirror(playlist_id).refused in (None, {})
    assert both_equal(world, playlist_id) == [3001]


# --- A lost answer when made ------------------------------------------------------------------------------------------


def test_a_lost_answer_then_a_new_name_makes_no_second_playlist(client: TestClient, world: FakePlex,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.sources import plex as plexlists

    real = plexlists.PlexSource.create

    def made_then_lost(*args: object) -> object:
        real(*args)  # type: ignore[arg-type]
        raise server_sync.PlexError("plex_timeout", 504)

    monkeypatch.setattr(plexlists.PlexSource, "create", made_then_lost)
    playlist = make(client, "Erster Name", [3001, 3003])
    sync(world, rounds=1)
    monkeypatch.setattr(plexlists.PlexSource, "create", real)
    assert client.put(f"/api/v1/playlists/{playlist['id']}", json={"name": "Zweiter Name"}).status_code == 200
    for _round in range(3):
        world.tick(20)
        with SessionLocal() as db:
            server_sync.request_all(db)
        sync(world, look_at_plex=True)
    names = sorted(card["name"] for card in client.get("/api/v1/playlists").json()["playlists"])
    in_plex = sorted(one.title for one in world.lists.lists.values())
    assert names == ["Zweiter Name"] and in_plex == ["Zweiter Name"]
    assert both_equal(world, playlist["id"]) == [3001, 3003]


def test_an_orphan_of_another_name_or_with_entries_is_not_taken(client: TestClient, world: FakePlex,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.sources import plex as plexlists

    real = plexlists.PlexSource.create

    def lost_before(*_args: object) -> object:
        raise server_sync.PlexError("plex_timeout", 504)

    monkeypatch.setattr(plexlists.PlexSource, "create", lost_before)
    playlist = make(client, "Gleicher Name", [3001])
    sync(world, rounds=1)
    monkeypatch.setattr(plexlists.PlexSource, "create", real)
    # Meanwhile somebody made one of the same name in Plexamp, with entries: it is theirs, not the lost answer. No
    # look at the list comes before the next try (it would take that one in as a playlist of its own).
    world.lists.make("Gleicher Name", [3020])
    world.tick(20)
    with SessionLocal() as db:
        server_sync.request_all(db)
    monkeypatch.setitem(server_sync._polled_at, 1, clock.now())
    sync(world)
    assert local_tracks(playlist["id"]) == [3001]
    sync(world, look_at_plex=True)
    mine = world.lists.lists[plex_key(playlist["id"])]
    assert mine.tracks() == [3001]
    assert sorted(one.tracks() for one in world.lists.lists.values()) == [[3001], [3020]]


# --- Who may ask for a sync now -------------------------------------------------------------------------------------


def test_sync_now_needs_the_right_to_change(client: TestClient, world: FakePlex, operator: Account) -> None:
    playlist_id, _key = synced(client, world, "Nur lesen", [3001])
    reader = make_account("leserin")
    writer = make_account("schreiber")
    assert client.put(f"/api/v1/playlists/{playlist_id}/shares/{reader.id}", json={"access": "read"}).status_code == 200
    assert client.put(f"/api/v1/playlists/{playlist_id}/shares/{writer.id}", json={"access": "edit"}).status_code == 200
    assert new_client(reader).post(f"/api/v1/playlists/{playlist_id}/sync").status_code == 403
    assert new_client(writer).post(f"/api/v1/playlists/{playlist_id}/sync").status_code == 200
    assert client.post(f"/api/v1/playlists/{playlist_id}/sync").status_code == 200


# --- The token stays on the server ----------------------------------------------------------------------------------


def test_the_token_is_nowhere_after_faults(client: TestClient, world: FakePlex, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    playlist_id, _key = synced(client, world, "Geheim", [3001])
    add(client, playlist_id, 3003)
    fail_on(world, "PUT", "/items", mode="timeout")
    sync(world)
    heal(world)
    world.fail_after = 0
    world.fail_mode = "401"
    server_sync.look_now()
    sync(world)
    heal(world)
    texts = [client.get(f"/api/v1/playlists/{playlist_id}").text, client.get("/api/v1/plex/playlists").text,
             client.get("/api/v1/playlists").text, caplog.text]
    with SessionLocal() as db:
        texts += [str(row.error) + str(row.base) + str(row.writing) for row in db.scalars(select(ServerPlaylist))]
    for text in texts:
        assert world.account_token not in text


def test_moves_to_the_start_and_the_end_of_a_longer_list(client: TestClient, world: FakePlex) -> None:
    tracks = [3001, 3003, 3005, 3007, 3009, 3013, 3015, 3017, 3020, 3022]
    playlist_id, key = synced(client, world, "Lang", tracks)
    move(client, playlist_id, 3022, None)
    move(client, playlist_id, 3001, 3020)
    world.lists.move_track(key, 3009, None)
    world.tick()
    sync(world, look_at_plex=True)
    found = both_equal(world, playlist_id)
    assert found[0] == 3022 and found.index(3001) == found.index(3020) + 1 and 3009 in found
    quiet(world)
