"""The sync of the playlists with the stand-in Plex: nexmix to Plex, Plex to nexmix, and every mix of both, with the
quirks a real Plex has (duplicates refused without a word, ``updatedAt`` late and in whole seconds, the ``after``
trap, 404 for entries gone, 204 on delete, an empty body on rename), Plex failing in the middle, a stop in the middle,
tracks gone from the library or not known yet, smart playlists, long and many playlists, and who may do what."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock
from app.db import SessionLocal
from app.models import OPERATOR, Account, Job, Playlist, PlaylistItem, PlexPlaylist
from app.services import catalog, jobs, playlists, plex_sync
from app.services import plex_playlists as plexlists

from .conftest import make_account, new_client
from .fakeplex import FakePlex
from .fakeplex.library import TRACK, Item
from .plex_world import (
    both_equal,
    connect,
    entry_of,
    local_items,
    local_tracks,
    make,
    mirror,
    playlist_row,
    plex_key,
    sync,
    tick,
)


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex) -> Iterator[FakePlex]:
    connect(client, fake_plex)
    plex_sync.forget()
    yield fake_plex


def quiet(fake: FakePlex) -> None:
    """A look at Plex well after the last change: nothing is due, nothing is to be looked at again."""
    fake.tick(20)
    sync(fake, look_at_plex=True)
    fake.tick(20)
    sync(fake, look_at_plex=True)
    with SessionLocal() as db:
        assert not any(db.scalars(select(PlexPlaylist.recheck)))


def writes(fake: FakePlex, since: int = 0) -> list[tuple[str, str, dict[str, str]]]:
    return fake.lists.writes[since:]


def synced(client: TestClient, fake: FakePlex, name: str, tracks: list[int]) -> tuple[int, int]:
    """A playlist made in nexmix and synced: (its id, its key in Plex)."""
    playlist = make(client, name, tracks)
    sync(fake)
    assert both_equal(fake, playlist["id"]) == tracks
    fake.tick()
    sync(fake, look_at_plex=True)
    return playlist["id"], plex_key(playlist["id"])


def move(client: TestClient, playlist_id: int, track: int, after_track: int | None) -> None:
    after = entry_of(client, playlist_id, after_track) if after_track is not None else None
    answer = client.post(f"/api/v1/playlists/{playlist_id}/items/move",
                         json={"entry": entry_of(client, playlist_id, track), "after": after})
    assert answer.status_code == 200, answer.text


def remove(client: TestClient, playlist_id: int, track: int) -> None:
    answer = client.post(f"/api/v1/playlists/{playlist_id}/items/remove",
                         json={"entries": [entry_of(client, playlist_id, track)]})
    assert answer.status_code == 200, answer.text


def add(client: TestClient, playlist_id: int, *tracks: int) -> None:
    answer = client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": list(tracks)})
    assert answer.status_code == 200, answer.text


# --- nexmix to Plex ----------------------------------------------------------------------------------------------------


def test_a_playlist_made_in_nexmix_is_made_in_plex_with_its_entries(client: TestClient, world: FakePlex) -> None:
    playlist = make(client, "Grillabend", [3001, 3003, 3013])
    assert playlist["plex"]["state"] == "pending"
    sync(world)
    row = mirror(playlist["id"])
    assert row.state == "synced" and row.rating_key is not None
    assert world.lists.lists[row.rating_key].title == "Grillabend"
    assert both_equal(world, playlist["id"]) == [3001, 3003, 3013]
    assert client.get(f"/api/v1/playlists/{playlist['id']}").json()["plex"]["state"] == "synced"
    # Made empty (Plex wants the library as its address), then filled at the end in one request.
    made = [one for one in writes(world) if one[0] == "POST"]
    assert made[0][2]["uri"].endswith("/com.plexapp.plugins.library") and made[0][2]["smart"] == "0"


def test_an_empty_playlist_is_made_empty_in_plex(client: TestClient, world: FakePlex) -> None:
    playlist = make(client, "Noch leer")
    sync(world)
    assert world.lists.tracks(plex_key(playlist["id"])) == []
    assert mirror(playlist["id"]).state == "synced"


def test_changes_gather_two_seconds_and_go_in_one_sync(client: TestClient, world: FakePlex) -> None:
    playlist_id, _key = synced(client, world, "Sammeln", [3001, 3003, 3013])
    before = len(writes(world))
    started = clock.now()
    add(client, playlist_id, 3020)
    due = mirror(playlist_id).due_at
    assert due is not None and timedelta(seconds=1.5) < due - started < timedelta(seconds=2.5)
    assert mirror(playlist_id).state == "pending"
    remove(client, playlist_id, 3003)
    move(client, playlist_id, 3020, None)
    # More changes in the two seconds go along: the moment stays.
    assert mirror(playlist_id).due_at == due
    # Not due yet: a moment later it is.
    with SessionLocal() as db:
        assert plex_sync.tick(db, clock.now()) <= 1
    assert jobs.run_pending(lane="playlists") <= 1
    assert len(writes(world)) == before
    # No look at Plex is needed for it: the change itself made it due.
    plex_sync._polled_at = clock.now() + timedelta(hours=1)
    sync(world)
    done = writes(world, before)
    assert [method for method, _path, _params in done].count("PUT") >= 1
    assert both_equal(world, playlist_id) == [3020, 3001, 3013]
    # One round wrote it all: a single request to add, a single one to take out.
    assert sum(1 for method, path, _params in done if method == "PUT" and path.endswith("/items")) == 1
    assert sum(1 for method, _path, _params in done if method == "DELETE") == 1


def test_moves_go_to_the_start_without_a_place_and_never_after_zero(client: TestClient, world: FakePlex) -> None:
    playlist_id, _key = synced(client, world, "Sortieren", [3001, 3003, 3013, 3020, 3026])
    before = len(writes(world))
    move(client, playlist_id, 3026, None)
    move(client, playlist_id, 3001, 3020)
    sync(world)
    assert both_equal(world, playlist_id) == [3026, 3003, 3013, 3020, 3001]
    moves = [(path, params) for method, path, params in writes(world, before) if path.endswith("/move")]
    assert moves, "sorted in Plex"
    for _path, params in moves:
        assert params.get("after") != "0"
    assert any("after" not in params for _path, params in moves)


def test_a_rename_in_nexmix_renames_in_plex(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Alt", [3001])
    assert client.put(f"/api/v1/playlists/{playlist_id}", json={"name": "Neu"}).status_code == 200
    sync(world)
    assert world.lists.lists[key].title == "Neu"


def test_deleted_in_nexmix_goes_from_plex_at_once_and_comes_back_anew(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Weg", [3001, 3003])
    assert client.delete(f"/api/v1/playlists/{playlist_id}").status_code == 200
    # At once, not after two seconds.
    with SessionLocal() as db:
        plex_sync.tick(db, clock.now())
    jobs.run_pending(lane="playlists")
    assert key not in world.lists.lists
    assert mirror(playlist_id).rating_key is None
    # Plex's list later on does not bring it back.
    sync(world, look_at_plex=True)
    assert playlist_row(playlist_id).deleted_at is not None
    assert client.post(f"/api/v1/playlists/{playlist_id}/restore").status_code == 200
    sync(world)
    new_key = plex_key(playlist_id)
    assert new_key != key and both_equal(world, playlist_id) == [3001, 3003]


def test_entries_put_in_in_plex_before_the_delete_go_along_into_the_trash(client: TestClient,
                                                                          world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Rettung", [3001])
    world.lists.add(key, [3013])
    assert client.delete(f"/api/v1/playlists/{playlist_id}").status_code == 200
    sync(world)
    assert key not in world.lists.lists
    assert client.post(f"/api/v1/playlists/{playlist_id}/restore").status_code == 200
    sync(world)
    assert both_equal(world, playlist_id) == [3001, 3013]


def test_deleted_for_good_before_the_sync_still_goes_from_plex(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Endgültig", [3001])
    assert client.delete(f"/api/v1/playlists/{playlist_id}").status_code == 200
    assert client.delete(f"/api/v1/playlists/{playlist_id}", params={"purge": True}).status_code == 200
    # The look at Plex comes first: the playlist is still there, but it is never taken in as a new one.
    sync(world, look_at_plex=True)
    assert key not in world.lists.lists
    with SessionLocal() as db:
        assert db.scalars(select(Playlist).where(Playlist.name == "Endgültig")).all() == []
        assert db.scalars(select(PlexPlaylist)).all() == []


# --- Plex to nexmix ----------------------------------------------------------------------------------------------------


def test_entries_added_in_plex_come_to_nexmix(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Party", [3001, 3003])
    world.lists.add(key, [3013, 3020])
    world.tick()
    seen = clock.now()
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3001, 3003, 3013, 3020]
    from_plex = [item for item in local_items(playlist_id) if item.from_plex]
    assert [item.track_id for item in from_plex] == [3013, 3020]
    assert all(item.added_by_name == "Plex" and item.added_by_user is None for item in from_plex)
    # "When" is when nexmix saw it, not Plex's date of the track.
    assert all(item.added_at >= seen - timedelta(seconds=1) for item in from_plex)
    entries = client.get(f"/api/v1/playlists/{playlist_id}").json()["items"]
    assert [entry["from_plex"] for entry in entries] == [False, False, True, True]


def test_entries_taken_out_and_sorted_in_plex_follow(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Plexamp", [3001, 3003, 3013, 3020])
    world.lists.remove_track(key, 3003)
    world.lists.move_track(key, 3020, None)
    world.tick()
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3020, 3001, 3013]
    with SessionLocal() as db:
        gone = db.scalars(select(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id,
                                                     PlaylistItem.track_id == 3003)).one()
        # Taken out softly, as in nexmix: the undo of the next day still finds it.
        assert gone.removed_at is not None


def test_a_move_in_plex_alone_comes_with_the_regular_read(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Nur verschoben", [3001, 3003, 3013])
    quiet(world)
    # A move in Plex changes nothing in the list of playlists.
    world.lists.move_track(key, 3013, None)
    world.tick(5)
    sync(world, look_at_plex=True)
    assert local_tracks(playlist_id) == [3001, 3003, 3013]
    # Two minutes after its last read the playlist is read anyway.
    with SessionLocal() as db:
        row = db.scalars(select(PlexPlaylist).where(PlexPlaylist.playlist_id == playlist_id)).one()
        row.synced_at = clock.now() - plex_sync.CHECK_EVERY
        db.commit()
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3013, 3001, 3003]


def test_a_regular_read_takes_a_few_at_a_look_the_longest_unread_first(client: TestClient, world: FakePlex,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plex_sync, "CHECK_PER_LOOK", 2)
    made = [synced(client, world, f"Liste {number}", [3001])[0] for number in range(3)]
    quiet(world)
    with SessionLocal() as db:
        for age, playlist_id in zip((5, 7, 6), made, strict=True):
            row = db.scalars(select(PlexPlaylist).where(PlexPlaylist.playlist_id == playlist_id)).one()
            row.synced_at = clock.now() - timedelta(minutes=age)
            db.commit()
    world.calls.clear()
    # One look, and the syncs it made due.
    sync(world, look_at_plex=True, rounds=2)
    read = {int(path.split("/")[2]) for _method, path, _params in world.library_calls() if path.endswith("/items")}
    assert read == {plex_key(made[1]), plex_key(made[2])}


def test_a_playlist_open_in_nexmix_is_read_at_every_look(client: TestClient, world: FakePlex,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    playlist_id, key = synced(client, world, "Offen", [3001, 3003])
    quiet(world)
    monkeypatch.setattr(plex_sync.hub, "topics", lambda: {f"playlist:{playlist_id}"})
    world.lists.move_track(key, 3003, None)
    world.tick(5)
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3003, 3001]


def test_a_rename_in_plex_is_taken(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Vorher", [3001])
    quiet(world)
    # A rename changes the title in Plex's list, not its updatedAt.
    world.lists.rename(key, "Nachher")
    world.tick()
    sync(world, look_at_plex=True)
    assert playlist_row(playlist_id).name == "Nachher"


def test_deleted_in_plex_goes_to_the_trash_and_comes_back_anew(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "In Plex gelöscht", [3001, 3013])
    world.lists.delete(key)
    sync(world, look_at_plex=True)
    row = playlist_row(playlist_id)
    assert row.deleted_at is not None
    trash = client.get("/api/v1/playlists", params={"trash": True}).json()["playlists"]
    assert [card["plex"]["gone_in_plex"] for card in trash if card["id"] == playlist_id] == [True]
    assert client.post(f"/api/v1/playlists/{playlist_id}/restore").status_code == 200
    sync(world)
    assert plex_key(playlist_id) != key
    assert both_equal(world, playlist_id) == [3001, 3013]


def test_playlists_made_in_plex_come_to_the_operator(client: TestClient, world: FakePlex) -> None:
    member = make_account("mona")
    world.lists.make("Aus Plexamp", [3026, 3001])
    world.lists.make("Alle Titel", [3001, 3003, 3013, 3020, 3026], smart=True)
    sync(world, look_at_plex=True)
    cards = {card["name"]: card for card in client.get("/api/v1/playlists").json()["playlists"]}
    assert cards["Aus Plexamp"]["count"] == 2 and cards["Aus Plexamp"]["owner"]["name"] == "tester"
    assert cards["Aus Plexamp"]["smart"] is False
    smart = cards["Alle Titel"]
    # The number from totalSize, not the stale leafCount of the list (two less).
    assert smart["smart"] is True and smart["count"] == 5 and smart["plex"]["total"] == 5
    with new_client(member) as browser:
        assert browser.get("/api/v1/playlists").json()["playlists"] == []
    detail = client.get(f"/api/v1/playlists/{cards['Aus Plexamp']['id']}").json()
    assert [entry["track"] for entry in detail["items"]] == [3026, 3001]
    assert all(entry["added_by_name"] == "Plex" for entry in detail["items"])


def test_at_the_first_connect_every_playlist_of_plex_comes_in(client: TestClient, operator: Account,
                                                              fake_plex: FakePlex) -> None:
    fake_plex.lists.make("Schon da", [3001])
    fake_plex.lists.make("Auch da", [3003, 3013])
    connect(client, fake_plex)
    plex_sync.forget()
    sync(fake_plex)
    names = sorted(card["name"] for card in client.get("/api/v1/playlists").json()["playlists"])
    assert names == ["Auch da", "Schon da"]
    # And nothing was written to Plex for it.
    assert [one for one in fake_plex.lists.writes if one[0] != "GET"] == []


def test_a_second_add_of_the_same_track_in_plex_changes_nothing(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Doppelt", [3001, 3003])
    assert world.lists.add(key, [3001, 3003]) == (0, 2)
    world.tick()
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3001, 3003]


def test_a_change_in_the_same_second_as_the_last_one_is_read_at_the_next_look(client: TestClient,
                                                                              world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Gleiche Sekunde", [3001, 3003, 3013])
    world.tick(5)
    # Plexamp adds one at second S; nexmix looks in that same second and takes it in.
    world.lists.add(key, [3020])
    world.lists.settle()
    sync(world, look_at_plex=True)
    assert local_tracks(playlist_id) == [3001, 3003, 3013, 3020]
    # Plexamp moves one, still in second S: neither updatedAt nor leafCount changes.
    world.lists.move_track(key, 3020, None)
    world.lists.settle()
    world.tick(5)
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3020, 3001, 3003, 3013]


def test_an_entry_added_in_plex_shows_by_its_count_before_plex_sets_its_time(client: TestClient,
                                                                             world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Zähler", [3001])
    quiet(world)
    world.lists.lag = 10_000
    world.tick(10)
    world.lists.add(key, [3013])
    # No settle: Plex's updatedAt is not there yet, only the count.
    sync(look_at_plex=True)
    assert local_tracks(playlist_id) == [3001, 3013]


def test_a_quiet_look_reads_nothing_but_the_list(client: TestClient, world: FakePlex) -> None:
    synced(client, world, "Ruhig", [3001])
    world.tick(10)
    sync(world, look_at_plex=True)
    world.calls.clear()
    world.tick(10)
    sync(world, look_at_plex=True)
    asked = [path for _method, path, _params in world.library_calls()]
    assert asked == ["/playlists"]
    # The playlist itself is never asked for: that alone would set its updatedAt (and make the next look read it).
    assert world.lists.meta_reads == 0


def test_a_smart_playlist_is_not_read_again_at_every_look(client: TestClient, world: FakePlex) -> None:
    world.lists.make("Zuletzt gespielt", [3001, 3003], smart=True)
    sync(world, look_at_plex=True)
    for _look in range(3):
        world.tick(15)
        world.calls.clear()
        sync(world, look_at_plex=True)
        assert [path for _method, path, _params in world.library_calls()] == ["/playlists"]
    assert world.lists.meta_reads == 0


# --- Both sides at once ----------------------------------------------------------------------------------------------


def test_both_add_and_both_keep_it(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Beide", [3001, 3003])
    add(client, playlist_id, 3013, 3020)
    world.lists.add(key, [3020, 3026])
    world.tick()
    sync(world, look_at_plex=True)
    tracks = both_equal(world, playlist_id)
    assert sorted(tracks) == [3001, 3003, 3013, 3020, 3026] and tracks[:2] == [3001, 3003]


def test_both_take_out_the_same(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Raus", [3001, 3003, 3013])
    remove(client, playlist_id, 3003)
    world.lists.remove_track(key, 3003)
    world.tick()
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3001, 3013]
    assert mirror(playlist_id).state == "synced"


def test_put_in_again_on_one_side_wins_over_taken_out_on_the_other(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Wieder rein", [3001, 3003, 3013])
    # nexmix takes 3003 out; Plexamp takes it out and puts it in again (a new entry there).
    remove(client, playlist_id, 3003)
    world.lists.remove_track(key, 3003)
    world.lists.add(key, [3003])
    # Plexamp takes 3013 out; nexmix takes it out and puts it in again.
    world.lists.remove_track(key, 3013)
    remove(client, playlist_id, 3013)
    add(client, playlist_id, 3013)
    world.tick()
    sync(world, look_at_plex=True)
    assert sorted(both_equal(world, playlist_id)) == [3001, 3003, 3013]


def test_only_plex_sorted_while_nexmix_added(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Plex sortiert", [3001, 3003, 3013])
    world.lists.move_track(key, 3013, None)
    add(client, playlist_id, 3026)
    world.tick()
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3013, 3001, 3003, 3026]
    assert mirror(playlist_id).note is None


def test_both_sorted_and_nexmix_wins_with_a_note_and_its_undo(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Beide sortiert", [3001, 3003, 3013, 3020])
    move(client, playlist_id, 3020, None)
    world.lists.move_track(key, 3003, None)
    world.tick()
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3020, 3001, 3003, 3013]
    view = client.get(f"/api/v1/playlists/{playlist_id}").json()["plex"]
    assert view["note"] is not None and view["note"]["count"] == 4
    # Undo: Plex's order of then.
    taken = client.post(f"/api/v1/playlists/{playlist_id}/plex-order")
    assert taken.status_code == 200 and taken.json()["op"] == "move"
    sync(world)
    assert both_equal(world, playlist_id) == [3003, 3001, 3013, 3020]
    assert client.get(f"/api/v1/playlists/{playlist_id}").json()["plex"]["note"] is None
    assert client.post(f"/api/v1/playlists/{playlist_id}/plex-order").status_code == 409


def test_the_note_can_be_dismissed(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Behalten", [3001, 3003, 3013])
    move(client, playlist_id, 3013, None)
    world.lists.move_track(key, 3003, None)
    world.tick()
    sync(world, look_at_plex=True)
    assert mirror(playlist_id).note is not None
    assert client.delete(f"/api/v1/playlists/{playlist_id}/plex-order").status_code == 200
    assert mirror(playlist_id).note is None
    assert both_equal(world, playlist_id) == [3013, 3001, 3003]


def test_renamed_on_both_sides_nexmix_wins(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Name", [3001])
    assert client.put(f"/api/v1/playlists/{playlist_id}", json={"name": "Hier"}).status_code == 200
    world.lists.rename(key, "Dort")
    world.tick()
    sync(world, look_at_plex=True)
    assert playlist_row(playlist_id).name == "Hier" and world.lists.lists[key].title == "Hier"


def test_deleted_in_plex_while_nexmix_changed_it_keeps_everything_in_the_trash(client: TestClient,
                                                                               world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Beides", [3001])
    add(client, playlist_id, 3013)
    world.lists.delete(key)
    sync(world, look_at_plex=True)
    assert playlist_row(playlist_id).deleted_at is not None
    assert client.post(f"/api/v1/playlists/{playlist_id}/restore").status_code == 200
    sync(world)
    assert both_equal(world, playlist_id) == [3001, 3013]


# --- Plex away, a stop in the middle -------------------------------------------------------------------------------------


def test_plex_away_in_the_middle_of_a_write_waits_and_writes_later(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Ausfall", [3001, 3003])
    add(client, playlist_id, 3013, 3020)
    move(client, playlist_id, 3020, None)
    # Plex answers the reading, then goes away while nexmix writes.
    world.fail_after = world.library_requests + 3
    world.fail_mode = "drop"
    sync(world)
    row = mirror(playlist_id)
    assert row.state == "waiting" and row.error == "plex_unreachable" and row.tries >= 1
    view = client.get(f"/api/v1/playlists/{playlist_id}").json()
    assert view["plex"]["state"] == "waiting" and view["plex"]["error"] == "plex_unreachable"
    # nexmix keeps everything, and more changes gather meanwhile.
    assert local_tracks(playlist_id) == [3020, 3001, 3003, 3013]
    add(client, playlist_id, 3026)
    world.fail_after = None
    # The next look reaches Plex again: the waiting playlist is tried at once.
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3020, 3001, 3003, 3013, 3026]
    assert mirror(playlist_id).state == "synced" and mirror(playlist_id).writing is None
    assert key == plex_key(playlist_id)


def test_a_look_that_reaches_plex_again_tries_the_waiting_ones_at_once(client: TestClient,
                                                                    world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Zurück", [3001])
    quiet(world)
    world.fail_after = world.library_requests
    add(client, playlist_id, 3013)
    sync(world)
    row = mirror(playlist_id)
    assert row.state == "waiting" and world.lists.tracks(key) == [3001]
    world.fail_after = None
    # The next try would come in five seconds; the look that reaches Plex brings it forward.
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3001, 3013]


def test_waiting_tries_again_later_and_later(client: TestClient, world: FakePlex) -> None:
    playlist_id, _key = synced(client, world, "Warten", [3001])
    world.fail_after = 0
    world.fail_mode = "503"
    waits = []
    for _try in range(4):
        with SessionLocal() as db:
            row = db.scalars(select(PlexPlaylist).where(PlexPlaylist.playlist_id == playlist_id)).one()
            row.due_at = clock.now()
            db.commit()
        tick(0)
        jobs.run_pending(lane="playlists")
        row = mirror(playlist_id)
        assert row.due_at is not None
        waits.append((row.due_at - clock.now()).total_seconds())
    assert waits == sorted(waits) and waits[0] > 3 and waits[-1] > 50
    assert mirror(playlist_id).state == "waiting" and mirror(playlist_id).error == "plex_busy"
    # "Try again" makes it due now.
    world.fail_after = None
    assert client.post(f"/api/v1/playlists/{playlist_id}/sync").json()["state"] == "pending"
    tick(0)
    jobs.run_pending(lane="playlists")
    assert mirror(playlist_id).state == "synced"


def test_while_plex_is_away_changes_gather_until_the_next_try(client: TestClient, world: FakePlex) -> None:
    playlist_id, _key = synced(client, world, "Sammeln ohne Plex", [3001])
    world.fail_after = 0
    for _try in range(3):
        with SessionLocal() as db:
            row = db.scalars(select(PlexPlaylist).where(PlexPlaylist.playlist_id == playlist_id)).one()
            row.due_at = clock.now()
            db.commit()
        tick(0)
        jobs.run_pending(lane="playlists")
    waiting = mirror(playlist_id)
    assert waiting.state == "waiting" and waiting.due_at is not None
    # A change while Plex is away does not make it due sooner: it goes along with the next try.
    add(client, playlist_id, 3013)
    after = mirror(playlist_id)
    assert after.due_at == waiting.due_at and after.state == "waiting"
    assert client.get(f"/api/v1/playlists/{playlist_id}").json()["plex"]["state"] == "waiting"
    world.fail_after = None
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3001, 3013]


def test_a_stop_in_the_middle_of_a_write_goes_on_at_the_next_start(client: TestClient, world: FakePlex) -> None:
    playlist_id, _key = synced(client, world, "Neustart", [3001, 3003, 3013])
    add(client, playlist_id, 3020, 3026)
    move(client, playlist_id, 3026, None)
    move(client, playlist_id, 3001, 3020)

    def stop_at_first_move(_count: int, path: str) -> None:
        if path.endswith("/move"):
            jobs.stopping.set()
            world.hook = None

    world.hook = stop_at_first_move
    sync(world, rounds=2)
    with SessionLocal() as db:
        waiting = db.scalars(select(Job).where(Job.kind == plex_sync.ROUND, Job.state == "queued")).all()
    assert len(waiting) == 1, "the job waits for the next start"
    assert mirror(playlist_id).writing is not None
    # The next start.
    jobs.stopping.clear()
    with SessionLocal() as db:
        jobs.recover(db, plex_sync.LANE)
        plex_sync.settle(db)
    sync(world)
    assert both_equal(world, playlist_id) == [3026, 3003, 3013, 3020, 3001]
    row = mirror(playlist_id)
    assert row.writing is None and row.note is None and row.state == "synced"


def test_a_lost_answer_after_plex_made_it_finds_that_one_and_makes_no_second(client: TestClient, world: FakePlex,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    real = plexlists.create

    def made_then_lost(*args: object) -> object:
        # Plex made it, but its answer never came.
        real(*args)  # type: ignore[arg-type]
        raise plex_sync.PlexError("plex_timeout", 504)

    monkeypatch.setattr(plexlists, "create", made_then_lost)
    playlist = make(client, "Kurz vor dem Halt", [3001, 3003])
    # One turn: the try that loses the answer, then the look at Plex (which finds the playlist made there).
    sync(world, rounds=1)
    row = mirror(playlist["id"])
    assert row.rating_key is None and row.creating_at is not None and row.state == "waiting"
    assert len([one for one in world.lists.lists.values() if one.title == "Kurz vor dem Halt"]) == 1
    monkeypatch.setattr(plexlists, "create", real)
    # The look at Plex comes before the next try: the playlist made there is not taken in as a new one.
    plex_sync.look_now()
    tick()
    jobs.run_pending(lane="playlists")
    names = [card["name"] for card in client.get("/api/v1/playlists").json()["playlists"]]
    assert names.count("Kurz vor dem Halt") == 1
    sync(world, look_at_plex=True)
    names = [card["name"] for card in client.get("/api/v1/playlists").json()["playlists"]]
    assert names.count("Kurz vor dem Halt") == 1
    assert len([one for one in world.lists.lists.values() if one.title == "Kurz vor dem Halt"]) == 1
    assert both_equal(world, playlist["id"]) == [3001, 3003]


def test_a_fault_in_the_middle_of_the_moves_is_finished_by_the_next_sync(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Fehler", [3001, 3003, 3013, 3020])
    move(client, playlist_id, 3020, None)
    move(client, playlist_id, 3003, 3013)

    def break_at_second_move(_count: int, path: str) -> None:
        if path.endswith("/move"):
            break_at_second_move.moves += 1  # type: ignore[attr-defined]
            if break_at_second_move.moves == 2:  # type: ignore[attr-defined]
                world.fail_after = 0

    break_at_second_move.moves = 0  # type: ignore[attr-defined]
    world.hook = break_at_second_move
    sync(world, rounds=1)
    world.hook = None
    world.fail_after = None
    # Meanwhile Plexamp adds one.
    world.lists.add(key, [3026])
    world.tick()
    with SessionLocal() as db:
        row = db.scalars(select(PlexPlaylist).where(PlexPlaylist.playlist_id == playlist_id)).one()
        row.due_at = clock.now()
        db.commit()
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3020, 3001, 3013, 3003, 3026]
    assert mirror(playlist_id).note is None


def test_a_change_in_nexmix_while_the_sync_merges_is_never_overwritten(client: TestClient, world: FakePlex,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    playlist_id, key = synced(client, world, "Mittendrin", [3001, 3003, 3013])
    world.lists.add(key, [3020])
    world.tick()
    original = plex_sync.Round._plan
    calls: list[int] = []

    def plan_with_a_change_meanwhile(self: plex_sync.Round, *args: object) -> plex_sync.Plan:
        found = original(self, *args)
        if not calls:
            calls.append(1)
            # Somebody sorts in nexmix right after the sync read nexmix's side.
            move(client, playlist_id, 3013, None)
        return found

    monkeypatch.setattr(plex_sync.Round, "_plan", plan_with_a_change_meanwhile)
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3013, 3001, 3003, 3020]


# --- The quirks of Plex --------------------------------------------------------------------------------------------------


def test_an_entry_gone_in_plex_before_nexmix_takes_it_out_is_no_failure(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Schon weg", [3001, 3003, 3013])
    remove(client, playlist_id, 3003)

    def gone_first(_count: int, path: str) -> None:
        if path.startswith(f"/playlists/{key}/items/") and not path.endswith("/move"):
            world.hook = None
            world.lists.remove_track(key, 3003)

    world.hook = gone_first
    sync(world)
    assert both_equal(world, playlist_id) == [3001, 3013]
    assert mirror(playlist_id).state == "synced"


def test_the_after_trap_is_caught_by_reading_again(client: TestClient, world: FakePlex) -> None:
    tracks = [3001, 3003, 3005, 3007, 3009, 3013, 3015, 3017, 3020, 3022]
    playlist_id, key = synced(client, world, "Falle", tracks)
    # nexmix puts the first two at the end: two moves, the second one after the first.
    move(client, playlist_id, 3001, 3022)
    move(client, playlist_id, 3003, 3001)
    moves: list[str] = []

    def plex_takes_out_the_place(_count: int, path: str) -> None:
        # Between nexmix's two moves Plexamp takes out the entry the second one names as its place: Plex answers 200
        # and puts the moved entry after its first one.
        if path.endswith("/move"):
            moves.append(path)
            if len(moves) == 2:
                world.lists.remove_track(key, 3001)

    world.hook = plex_takes_out_the_place
    sync(world)
    world.hook = None
    world.tick()
    sync(world, look_at_plex=True)
    assert both_equal(world, playlist_id) == [3005, 3007, 3009, 3013, 3015, 3017, 3020, 3022, 3003]


def test_plex_refusing_a_track_never_takes_it_from_nexmix(client: TestClient, world: FakePlex) -> None:
    playlist_id, _key = synced(client, world, "Verweigert", [3001])
    # Gone from Plex's library, but the catalogue does not know it yet.
    world.remove(3026)
    add(client, playlist_id, 3026)
    sync(world)
    world.tick()
    sync(world, look_at_plex=True)
    assert local_tracks(playlist_id) == [3001, 3026]
    with SessionLocal() as db:
        asked = db.scalars(select(Job).where(Job.kind == catalog.PART)).all()
    assert asked, "the catalogue is asked to look again"


def test_a_track_plex_will_not_take_stays_in_nexmix(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Nimmt nicht", [3001])
    world.lists.refuse.add(3026)
    add(client, playlist_id, 3026)
    sync(world)
    for _look in range(2):
        world.tick(20)
        sync(world, look_at_plex=True)
    assert local_tracks(playlist_id) == [3001, 3026]
    assert world.lists.tracks(key) == [3001]
    world.lists.refuse.clear()
    remove(client, playlist_id, 3001)
    sync(world)
    # Offered again only when its wait is over, or at once when somebody asks for a sync now.
    assert local_tracks(playlist_id) == [3026] and world.lists.tracks(key) == []
    assert client.post(f"/api/v1/playlists/{playlist_id}/sync").status_code == 200
    sync(world)
    assert both_equal(world, playlist_id) == [3026]


def test_a_track_gone_from_the_library_stays_in_nexmix(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Verschwunden", [3001, 3003, 3013])
    # Plex drops the track from its library, and with it from every playlist.
    world.remove(3003)
    world.lists.remove_track(key, 3003)
    world.tick()
    sync(world, look_at_plex=True)
    assert local_tracks(playlist_id) == [3001, 3003, 3013]
    assert world.lists.tracks(key) == [3001, 3013]
    assert mirror(playlist_id).state == "synced"
    # Changes still go both ways around it.
    world.lists.add(key, [3020])
    world.tick()
    sync(world, look_at_plex=True)
    assert local_tracks(playlist_id) == [3001, 3003, 3013, 3020]
    assert world.lists.tracks(key) == [3001, 3013, 3020]


def test_a_track_the_catalogue_does_not_know_yet_shows_what_plex_says(client: TestClient, world: FakePlex) -> None:
    playlist_id, key = synced(client, world, "Neu in der Mediathek", [3001])
    world.add(Item(3099, TRACK, "Lantern Song", 2001, duration=181_000))
    world.lists.add(key, [3099])
    world.tick()
    sync(world, look_at_plex=True)
    entries = client.get(f"/api/v1/playlists/{playlist_id}").json()["items"]
    found = entries[-1]
    assert found["track"] == 3099 and found["title"] == "Lantern Song" and found["artist"] == "The Velvet Harbour"
    assert found["album"] == "Paper Lanterns" and found["duration"] == 181 and found["from_plex"] is True
    # The catalogue is asked to look; once it did, the track is known.
    assert jobs.run_pending(lane="catalog") >= 1
    entries = client.get(f"/api/v1/playlists/{playlist_id}").json()["items"]
    assert entries[-1]["missing"] is False


# --- Smart playlists -----------------------------------------------------------------------------------------------------


def test_a_smart_playlist_is_only_read(client: TestClient, world: FakePlex) -> None:
    smart = world.lists.make("Frisch", [3001, 3003, 3013], smart=True)
    sync(world, look_at_plex=True)
    card = next(card for card in client.get("/api/v1/playlists").json()["playlists"] if card["name"] == "Frisch")
    base = f"/api/v1/playlists/{card['id']}"
    for method, path, body in (("POST", f"{base}/items", {"tracks": [3020]}), ("PUT", base, {"name": "Anders"}),
                               ("DELETE", base, None)):
        answer = client.request(method, path, json=body)
        assert answer.status_code == 409 and answer.json()["detail"]["code"] == "smart_playlist", (method, path)
    entry = client.get(base).json()["items"][0]["id"]
    for path, body in ((f"{base}/items/remove", {"entries": [entry]}),
                       (f"{base}/items/move", {"entry": entry, "after": None})):
        assert client.post(path, json=body).status_code == 409
    # Sharing changes nothing in Plex: it goes.
    member = make_account("mila")
    assert client.put(f"{base}/shares/{member.id}", json={"access": "read"}).status_code == 200
    # Plex changes what the rule finds: read again at the next look after a while.
    smart.rule = [3020, 3001]
    world.tick()
    sync(world, look_at_plex=True)
    assert [entry["track"] for entry in client.get(base).json()["items"]] == [3001, 3003, 3013]
    with SessionLocal() as db:
        row = db.scalars(select(PlexPlaylist).where(PlexPlaylist.playlist_id == card["id"])).one()
        row.synced_at = clock.now() - plex_sync.SMART_EVERY
        db.commit()
    sync(world, look_at_plex=True)
    assert [entry["track"] for entry in client.get(base).json()["items"]] == [3020, 3001]
    assert not [one for one in world.lists.writes if one[1].startswith(f"/playlists/{smart.key}")]


def test_a_smart_playlist_shows_its_first_entries_and_counts_all(client: TestClient, world: FakePlex,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plexlists, "SMART_LIMIT", 2)
    world.lists.make("Alles", [3001, 3003, 3013, 3020], smart=True)
    sync(world, look_at_plex=True)
    card = next(card for card in client.get("/api/v1/playlists").json()["playlists"] if card["name"] == "Alles")
    detail = client.get(f"/api/v1/playlists/{card['id']}").json()
    assert card["count"] == 4 and detail["count"] == 4 and len(detail["items"]) == 2


# --- Long and many -----------------------------------------------------------------------------------------------------


@pytest.fixture
def big_world(client: TestClient, operator: Account, fake_plex: FakePlex) -> Iterator[FakePlex]:
    """A library of 5,200 more tracks on one album."""
    for number in range(5200):
        fake_plex.items[100_000 + number] = Item(100_000 + number, TRACK, f"Song {number}", 2001,
                                                 index=number + 1, rating=10)
    connect(client, fake_plex)
    plex_sync.forget()
    yield fake_plex


def test_a_playlist_of_5000_goes_in_parts_and_both_sides_change_it(client: TestClient, big_world: FakePlex) -> None:
    tracks = [100_000 + number for number in range(5000)]
    with SessionLocal() as db:
        owner = db.scalars(select(Account).where(Account.role == OPERATOR)).first()
        assert owner is not None
        made = playlists.create(db, owner, "Lang")
        for start in range(0, 5000, 1000):
            playlists.add(db, owner, made.id, tracks[start:start + 1000])
        playlist_id = made.id
    started = time.perf_counter()
    sync(big_world)
    first = time.perf_counter() - started
    key = plex_key(playlist_id)
    assert big_world.lists.tracks(key) == tracks
    parts = [params for method, path, params in big_world.lists.writes if method == "PUT" and path.endswith("/items")]
    assert len(parts) == 17 and all(len(one["uri"].split(",")) <= 300 for one in parts)
    # Both sides change it.
    big_world.lists.move_track(key, 100_010, None)
    big_world.lists.remove_track(key, 100_020)
    big_world.lists.add(key, [105_100])
    with SessionLocal() as db:
        owner = db.scalars(select(Account).where(Account.role == OPERATOR)).first()
        assert owner is not None
        playlists.remove(db, owner, playlist_id, [next(item.id for item in local_items(playlist_id)
                                                        if item.track_id == 100_030)])
    big_world.tick()
    started = time.perf_counter()
    sync(big_world, look_at_plex=True)
    second = time.perf_counter() - started
    result = both_equal(big_world, playlist_id)
    assert result[0] == 100_010 and 100_020 not in result and 100_030 not in result and result[-1] == 105_100
    assert len(result) == 4999
    assert first < 30 and second < 30, (first, second)


def test_200_playlists_go_to_plex_and_a_quiet_look_costs_one_request(client: TestClient, world: FakePlex) -> None:
    with SessionLocal() as db:
        owner = db.scalars(select(Account).where(Account.role == OPERATOR)).first()
        assert owner is not None
        for number in range(200):
            made = playlists.create(db, owner, f"Liste {number}")
            playlists.add(db, owner, made.id, [3001 + number % 20, 3021 - number % 20])
    started = time.perf_counter()
    sync(world, rounds=40)
    took = time.perf_counter() - started
    assert len(world.lists.lists) == 200
    with SessionLocal() as db:
        states = set(db.scalars(select(PlexPlaylist.state)))
    assert states == {"synced"}
    world.tick(10)
    sync(world, look_at_plex=True, rounds=40)
    world.calls.clear()
    world.tick(10)
    sync(world, look_at_plex=True)
    assert [path for _method, path, _params in world.library_calls()] == ["/playlists"]
    # About a third of a second each against the stand-in (one connection per transaction, as in the app); a ceiling
    # against something gone quadratic, not a promise of speed.
    assert took < 150, took


# --- Who may do what ---------------------------------------------------------------------------------------------------


def test_the_sync_routes_check_the_right_of_the_one_who_asks(client: TestClient, world: FakePlex) -> None:
    playlist = make(client, "Rechte", [3001])
    reader_row, stranger_row = make_account("rita"), make_account("sven")
    assert client.put(f"/api/v1/playlists/{playlist['id']}/shares/{reader_row.id}",
                      json={"access": "read"}).status_code == 200
    base = f"/api/v1/playlists/{playlist['id']}"
    with new_client(reader_row) as reader, new_client(stranger_row) as stranger:
        assert stranger.post(f"{base}/sync").status_code == 404
        assert stranger.post(f"{base}/plex-order").status_code == 404
        assert reader.post(f"{base}/sync").status_code == 403
        assert reader.post(f"{base}/plex-order").status_code == 403
        assert reader.delete(f"{base}/plex-order").status_code == 403
        assert reader.get("/api/v1/plex/playlists").status_code == 403
        assert reader.post("/api/v1/plex/playlists/sync").status_code == 403
    overview = client.get("/api/v1/plex/playlists").json()
    assert overview["active"] is True and overview["playlists"] == 1
    assert client.post("/api/v1/plex/playlists/sync").status_code == 200


def test_without_plex_the_sync_is_off_and_waits(client: TestClient, operator: Account) -> None:
    playlist = make(client, "Ohne Plex", [])
    assert playlist["plex"]["state"] == "off"
    assert tick() == 0
    assert jobs.run_pending(lane="playlists") == 0
    assert client.get("/api/v1/plex/playlists").json()["active"] is False


def test_the_overview_counts_the_states(client: TestClient, world: FakePlex) -> None:
    synced(client, world, "Eins", [3001])
    world.lists.make("Smart", [3001], smart=True)
    sync(world, look_at_plex=True)
    found = client.get("/api/v1/plex/playlists").json()
    assert found["synced"] == 2 and found["smart"] == 1 and found["poll_error"] is None
    assert found["polled_at"] is not None
