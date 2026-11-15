"""Every mix of changes on both sides with the stand-in Jellyfin, as for Plex in 0.1.0: both add, both take out the
same, one takes out what the other added, both sort, only Jellyfin sorts, both rename; a long playlist and many
playlists; a Plex and a Jellyfin server side by side without one hearing of the other's playlists."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Account, ServerPlaylist
from app.services import catalog, jobs
from app.services import sync as server_sync

from .fakejellyfin import FakeJellyfin
from .fakeplex import FakePlex
from .jellyfin_world import both_equal, connect, keys, list_key, make, numbers, sync
from .plex_world import connect as connect_plex
from .plex_world import entry_of, local_tracks, playlist_row
from .plex_world import plex_key as plex_list_key


@pytest.fixture
def jelly(client: TestClient, operator: Account, fake_jellyfin: FakeJellyfin) -> Iterator[tuple[FakeJellyfin, int]]:
    server = connect(client, fake_jellyfin)
    server_sync.forget()
    yield fake_jellyfin, server


def pick(fake: FakeJellyfin, count: int) -> list[str]:
    return sorted(fake.tracks)[:count]


def synced(client: TestClient, fake: FakeJellyfin, server: int, tracks: list[str], name: str = "Mix") -> int:
    playlist_id = int(make(client, server, name, numbers(server, tracks))["id"])
    sync()
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == tracks
    return playlist_id


def nx_add(client: TestClient, server: int, playlist_id: int, *tracks: str) -> None:
    answer = client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": numbers(server, list(tracks))})
    assert answer.status_code == 200, answer.text


def nx_remove(client: TestClient, server: int, playlist_id: int, track: str) -> None:
    entry = entry_of(client, playlist_id, numbers(server, [track])[0])
    answer = client.post(f"/api/v1/playlists/{playlist_id}/items/remove", json={"entries": [entry]})
    assert answer.status_code == 200, answer.text


def nx_move(client: TestClient, server: int, playlist_id: int, track: str, after: str | None) -> None:
    answer = client.post(f"/api/v1/playlists/{playlist_id}/items/move", json={
        "entry": entry_of(client, playlist_id, numbers(server, [track])[0]),
        "after": entry_of(client, playlist_id, numbers(server, [after])[0]) if after else None})
    assert answer.status_code == 200, answer.text


def test_both_add(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c, d, e = pick(fake, 5)
    playlist_id = synced(client, fake, server, [a, b])
    nx_add(client, server, playlist_id, c, d)
    fake.list_add(list_key(playlist_id), [e, d])
    sync(look=True)
    result = both_equal(fake, server, playlist_id)
    assert sorted(result) == sorted([a, b, c, d, e]) and result[:2] == [a, b]


def test_both_take_out_the_same(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c = pick(fake, 3)
    playlist_id = synced(client, fake, server, [a, b, c])
    nx_remove(client, server, playlist_id, b)
    fake.list_remove(list_key(playlist_id), [b])
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == [a, c]


def test_one_takes_out_what_the_other_added(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c, d = pick(fake, 4)
    playlist_id = synced(client, fake, server, [a, b])
    # nexmix adds c and Jellyfin takes out b; Jellyfin adds d and nexmix takes out a: adding wins only for what was
    # added, taking out holds for what was there.
    nx_add(client, server, playlist_id, c)
    nx_remove(client, server, playlist_id, a)
    fake.list_remove(list_key(playlist_id), [b])
    fake.list_add(list_key(playlist_id), [d])
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == [c, d] or both_equal(fake, server, playlist_id) == [d, c]


def test_only_jellyfin_sorts(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c, d = pick(fake, 4)
    playlist_id = synced(client, fake, server, [a, b, c, d])
    key = list_key(playlist_id)
    fake.list_move(key, d, 0)
    fake.list_move(key, a, 3)
    nx_add(client, server, playlist_id, pick(fake, 5)[4])
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == [d, b, c, a, pick(fake, 5)[4]]


def test_both_sort_and_nexmix_wins_with_a_note(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c, d = pick(fake, 4)
    playlist_id = synced(client, fake, server, [a, b, c, d])
    fake.list_move(list_key(playlist_id), a, 3)
    nx_move(client, server, playlist_id, d, None)
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == [d, a, b, c]
    view = client.get(f"/api/v1/playlists/{playlist_id}").json()["plex"]
    assert view["note"]["count"] == 4
    # The undo takes Jellyfin's order.
    assert client.post(f"/api/v1/playlists/{playlist_id}/plex-order").status_code == 200
    sync()
    assert both_equal(fake, server, playlist_id) == [b, c, d, a]


def test_both_rename_and_nexmix_wins(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    playlist_id = synced(client, fake, server, pick(fake, 1))
    assert client.put(f"/api/v1/playlists/{playlist_id}", json={"name": "Hier"}).status_code == 200
    fake.list_rename(list_key(playlist_id), "Dort")
    sync(look=True)
    assert playlist_row(playlist_id).name == "Hier" and fake.lists[list_key(playlist_id)].name == "Hier"


def test_five_thousand_entries_go_in_parts_that_fit_the_address(client: TestClient,
                                                                jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    first = pick(fake, 10)
    made = fake.bulk(4990)
    with SessionLocal() as db:
        catalog.request_full(db, "test", server)
    jobs.run_pending(lane="catalog")
    tracks = [*first, *made]
    playlist_id = int(make(client, server, "Lang")["id"])
    for start in range(0, len(tracks), 500):
        nx_add(client, server, playlist_id, *tracks[start:start + 500])
    sync()
    assert both_equal(fake, server, playlist_id) == tracks
    adds = [params for method, path, params in fake.list_writes if method == "POST" and path.endswith("/Items")]
    assert len(adds) == 25 and all(len(params["ids"].split(",")) <= 200 for params in adds)
    # Turned around in nexmix: the moves go in parts, Jellyfin ends in the same order.
    answer = client.post(f"/api/v1/playlists/{playlist_id}/items/move", json={
        "entry": entry_of(client, playlist_id, numbers(server, [tracks[-1]])[0]), "after": None})
    assert answer.status_code == 200
    fake.list_remove(list_key(playlist_id), tracks[100:103])
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == [tracks[-1], *tracks[:100], *tracks[103:-1]]


def test_two_hundred_playlists(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    pool = pick(fake, 20)
    for number in range(100):
        fake.make_list(f"Aus Jellyfin {number:03d}", pool[number % 20:number % 20 + 3])
    made = [int(make(client, server, f"Aus nexmix {number:03d}", numbers(server, pool[:number % 7]))["id"])
            for number in range(100)]
    sync(rounds=40, look=True)
    with SessionLocal() as db:
        rows = list(db.scalars(ServerPlaylist.__table__.select()))
    assert len(rows) == 200
    for playlist_id in made:
        both_equal(fake, server, playlist_id)
    assert len(fake.lists) == 200


def test_plex_and_jellyfin_side_by_side_keep_their_playlists_apart(
        client: TestClient, operator: Account, fake_plex: FakePlex, fake_jellyfin: FakeJellyfin) -> None:
    connect_plex(client, fake_plex)
    found = client.post("/api/v1/jellyfin/quick", json={"url": "http://jellyfin.example.com:8096"}).json()
    assert fake_jellyfin.confirm(found["code"])
    connected = client.post("/api/v1/jellyfin/connect", json={"quick": found["id"]}).json()
    jelly = int(connected["server"]["id"])
    if connected["server"]["library"] is None:
        client.put(f"/api/v1/jellyfin/{jelly}/library", json={"handle": connected["libraries"][0]["handle"]})
    if connected["server"]["user"] is None:
        handle = next(one["handle"] for one in connected["users"] if one["name"] == "Erika")
        client.put(f"/api/v1/jellyfin/{jelly}/user", json={"handle": handle})
    jobs.run_pending(lane="catalog")
    server_sync.forget()
    on_plex = client.post("/api/v1/playlists", json={"name": "Auf Plex"}).json()["id"]
    client.post(f"/api/v1/playlists/{on_plex}/items", json={"tracks": [3001, 3003]})
    a, b = sorted(fake_jellyfin.tracks)[:2]
    on_jelly = int(make(client, jelly, "Auf Jellyfin", numbers(jelly, [a, b]))["id"])
    fake_jellyfin.make_list("Neu in Jellyfin", [b])
    fake_plex.lists.settle()
    sync(look=True)
    plex_lists = {one.title for one in fake_plex.lists.lists.values()}
    jelly_lists = {one.name for one in fake_jellyfin.lists.values()}
    assert "Auf Plex" in plex_lists and "Auf Jellyfin" not in plex_lists and "Neu in Jellyfin" not in plex_lists
    assert jelly_lists == {"Auf Jellyfin", "Neu in Jellyfin"}
    assert both_equal(fake_jellyfin, jelly, on_jelly) == [a, b]
    assert fake_plex.lists.tracks(plex_list_key(on_plex)) == [3001, 3003]
    names = {one["name"]: one for one in client.get("/api/v1/playlists").json()["playlists"]}
    assert set(names) >= {"Auf Plex", "Auf Jellyfin", "Neu in Jellyfin"}
    # A change on one server stays there.
    fake_jellyfin.list_add(list_key(on_jelly), [sorted(fake_jellyfin.tracks)[2]])
    fake_plex.lists.add(plex_list_key(on_plex), [3013])
    fake_plex.lists.settle()
    sync(look=True)
    assert keys(jelly, local_tracks(on_jelly)) == [a, b, sorted(fake_jellyfin.tracks)[2]]
    assert fake_plex.lists.tracks(plex_list_key(on_plex)) == [3001, 3003, 3013]
    assert local_tracks(on_plex) == [3001, 3003, 3013]
