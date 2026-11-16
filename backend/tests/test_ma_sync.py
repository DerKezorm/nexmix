"""Playlists kept in Music Assistant both ways, against the stand-in with every way of its that was measured: taking
out by place, no moving, a new name that does not take, tasks that end later, events at once."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Playlist, ServerPlaylist

from .fakema import FakeMA
from .ma_world import both_equal, connect, events, list_key, make, numbers, sync
from .plex_world import local_tracks, mirror, playlist_row


def arrange(client: TestClient, playlist: int, wanted: list[int]) -> None:
    """The entries in this order in nexmix, moved one after another as the interface does."""
    for place, entry in enumerate(wanted):
        moved = client.post(f"/api/v1/playlists/{playlist}/items/move",
                            json={"entry": entry, "after": wanted[place - 1] if place else None})
        assert moved.status_code == 200, moved.text


def _tracks(fake: FakeMA, *names: str) -> list[int]:
    return [fake.track_by_name(name).id for name in names]


def _setup(client: TestClient, fake: FakeMA, name: str = "Abendrunde", count: int = 5) -> tuple[int, int, list[int]]:
    server = connect(client, fake)
    chosen = sorted(one.id for one in fake.tracks.values() if one.mappings[0][0].startswith("filesystem_local--a"))
    tracks = numbers(server, chosen[:count])
    made = make(client, server, name, tracks)
    sync()
    return server, int(made["id"]), chosen[:count]


def test_a_playlist_of_nexmix_is_made_and_filled_in_music_assistant(client: TestClient, operator: object,
                                                                    fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    assert both_equal(fake_ma, server, playlist) == chosen
    there = fake_ma.playlists[list_key(playlist)]
    assert there.name == "Abendrunde" and there.editable
    assert mirror(playlist).state == "synced"
    # The answer said "running": nexmix asked until the task ended.
    assert any(command == "tasks/get" for command, _args in fake_ma.calls)


def test_a_name_music_assistant_has_already_is_made_unique(client: TestClient, operator: object,
                                                           fake_ma: FakeMA) -> None:
    fake_ma.ui_create("Abendrunde")
    _server, playlist, _chosen = _setup(client, fake_ma)
    there = fake_ma.playlists[list_key(playlist)]
    assert there.name == "Abendrunde (2)"
    # nexmix keeps its own name, and never takes the number for a new name there.
    sync(look=True)
    assert playlist_row(playlist).name == "Abendrunde"
    assert sum(command == "music/playlists/create_playlist" for command, _args in fake_ma.calls) == 1


def test_entries_taken_out_and_sorted_in_nexmix(client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma, count=6)
    items = client.get(f"/api/v1/playlists/{playlist}").json()["items"]
    going = [items[1]["id"], items[4]["id"]]
    assert client.post(f"/api/v1/playlists/{playlist}/items/remove", json={"entries": going}).status_code == 200
    sync()
    assert both_equal(fake_ma, server, playlist) == [chosen[0], chosen[2], chosen[3], chosen[5]]
    order = [one["id"] for one in client.get(f"/api/v1/playlists/{playlist}").json()["items"]]
    arrange(client, playlist, [order[3], order[0], order[2], order[1]])
    sync()
    assert both_equal(fake_ma, server, playlist) == [chosen[5], chosen[0], chosen[3], chosen[2]]
    # Places were never 0 or below, never past the end.
    for command, args in fake_ma.calls:
        if command == "music/playlists/remove_playlist_tracks":
            assert all(place >= 1 for place in args["positions_to_remove"])


def test_a_new_name_is_a_new_playlist_with_everything_and_the_old_one_goes(client: TestClient, operator: object,
                                                                           fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    before = list_key(playlist)
    assert client.put(f"/api/v1/playlists/{playlist}", json={"name": "Spätschicht"}).status_code == 200
    sync()
    after = list_key(playlist)
    assert after != before and before not in fake_ma.playlists
    assert fake_ma.playlists[after].name == "Spätschicht"
    assert both_equal(fake_ma, server, playlist) == chosen
    # Deleted for good (``remove_item``), never ``music/playlists/remove`` (that comes back at the next scan).
    assert ("music/library/remove_item", {"media_type": "playlist", "library_item_id": before}) in fake_ma.calls
    assert not any(command == "music/playlists/remove" for command, _args in fake_ma.calls)
    # Neither the old nor the new one was taken in as another playlist.
    sync(look=True)
    with SessionLocal() as db:
        assert db.scalar(select(Playlist.id).where(Playlist.id != playlist)) is None


def test_changes_in_music_assistant_come_by_their_event(client: TestClient, operator: object,
                                                        fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    extra = _tracks(fake_ma, "Small Hours")[0]
    fake_ma.ui_add(key, extra)
    fake_ma.ui_remove(key, chosen[1])
    assert "due" in events(server, fake_ma)
    sync()
    assert both_equal(fake_ma, server, playlist) == [chosen[0], *chosen[2:], extra]
    added = [one for one in client.get(f"/api/v1/playlists/{playlist}").json()["items"]
             if one["title"] == "Small Hours"]
    assert added and added[0]["added_by_name"] == "Music Assistant"
    fake_ma.ui_order(key, list(reversed(fake_ma.entries(key))))
    events(server, fake_ma)
    sync()
    assert both_equal(fake_ma, server, playlist) == list(reversed([chosen[0], *chosen[2:], extra]))


def test_existing_playlists_of_music_assistant_are_taken_in_dynamic_ones_never(client: TestClient, operator: object,
                                                                              fake_ma: FakeMA) -> None:
    mine = fake_ma.ui_create("Küche", _tracks(fake_ma, "Northern Lights", "Fever Glass"))
    server = connect(client, fake_ma)
    sync(look=True)
    with SessionLocal() as db:
        names = list(db.scalars(select(Playlist.name)))
    assert names == ["Küche"]
    with SessionLocal() as db:
        playlist = db.scalar(select(Playlist.id))
    assert playlist is not None
    assert [fake_ma.title(fake_ma.tracks[one]) for one in fake_ma.entries(mine)] == ["Northern Lights", "Fever Glass"]
    assert both_equal(fake_ma, server, playlist)
    # Taking it in wrote nothing there.
    assert not any(command.startswith("music/playlists/") and command.endswith("_tracks") and "remove" in command
                   for command, _args in fake_ma.calls)


def test_deleted_in_nexmix_goes_from_music_assistant_for_good(client: TestClient, operator: object,
                                                              fake_ma: FakeMA) -> None:
    _server, playlist, _chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    assert client.delete(f"/api/v1/playlists/{playlist}").status_code in (200, 204)
    sync()
    assert key not in fake_ma.playlists
    fake_ma.scan()
    assert not any(one.name == "Abendrunde" for one in fake_ma.playlists.values())


def test_deleted_in_music_assistant_goes_to_the_trash(client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    server, playlist, _chosen = _setup(client, fake_ma)
    fake_ma.ui_delete(list_key(playlist))
    assert "look" in events(server, fake_ma)
    sync(look=True)
    assert playlist_row(playlist).deleted_at is not None


def test_two_missing_at_once_are_no_deletion(client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    server, first, _chosen = _setup(client, fake_ma)
    second = int(make(client, server, "Zweite", numbers(server, _chosen[:2]))["id"])
    sync()
    fake_ma.ui_delete(list_key(first))
    fake_ma.ui_delete(list_key(second))
    events(server, fake_ma)
    sync(look=True)
    assert playlist_row(first).deleted_at is None and playlist_row(second).deleted_at is None
    overview = client.get(f"/api/v1/music-assistant/{server}/playlists").json()
    assert overview["missing"] == 2


def test_a_name_change_in_music_assistant_does_not_take_and_changes_nothing(client: TestClient, operator: object,
                                                                            fake_ma: FakeMA) -> None:
    _server, playlist, chosen = _setup(client, fake_ma)
    # Music Assistant's ``update`` answers with the new name and keeps the old one.
    from app.services import music_assistant

    connection = music_assistant.current(_server)
    assert connection is not None
    answered = connection.call("music/playlists/update", {"item_id": str(list_key(playlist)),
                                                          "update": {"name": "Anders"}, "overwrite": True})
    assert answered["name"] == "Anders"
    events(_server, fake_ma)
    sync(look=True)
    assert playlist_row(playlist).name == "Abendrunde"
    assert fake_ma.entries(list_key(playlist)) == chosen


def test_music_assistant_restarting_waits_and_writes_after(client: TestClient, operator: object,
                                                           fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    fake_ma.restart()
    events(server, fake_ma)
    items = client.get(f"/api/v1/playlists/{playlist}").json()["items"]
    assert client.post(f"/api/v1/playlists/{playlist}/items/remove",
                       json={"entries": [items[0]["id"]]}).status_code == 200
    sync(rounds=3)
    assert mirror(playlist).state == "waiting"
    assert fake_ma.entries(list_key(playlist)) == chosen
    fake_ma.up()
    from app.services import music_assistant

    music_assistant.forget()
    with SessionLocal() as db:
        db.execute(ServerPlaylist.__table__.update().values(due_at=None))
        db.commit()
    from app.services import sync as server_sync

    with SessionLocal() as db:
        server_sync.request_all(db, server)
    sync()
    assert both_equal(fake_ma, server, playlist) == chosen[1:]


def test_a_token_run_out_says_connect_again(client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    server, playlist, _chosen = _setup(client, fake_ma)
    fake_ma.revoke(fake_ma.token)
    fake_ma.restart()
    fake_ma.up()
    items = client.get(f"/api/v1/playlists/{playlist}").json()["items"]
    client.post(f"/api/v1/playlists/{playlist}/items/remove", json={"entries": [items[0]["id"]]})
    from app.services import music_assistant

    music_assistant.forget()
    sync(rounds=3)
    row = mirror(playlist)
    assert row.state == "error" and row.error == "ma_refused"
    assert local_tracks(playlist)
    assert server
