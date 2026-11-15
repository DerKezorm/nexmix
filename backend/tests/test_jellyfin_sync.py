"""The sync of the playlists with the stand-in Jellyfin, both ways, with the quirks a real Jellyfin 12.4 has (an entry
named by its track, ids in the address only, silent 204 for ids it does not know, ``Move`` to the place afterwards, a
new ``Etag`` with every change, copies of a track, other users' public playlists in the list, 503 while it starts,
401 once the token is displaced), and what happens to entries whose file was renamed or whose library was made anew."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Account, PlaylistItem, ServerPlaylist
from app.services import catalog, jobs
from app.services import sync as server_sync

from .fakejellyfin import FakeJellyfin
from .jellyfin_world import both_equal, connect, keys, list_key, make, numbers, sync
from .plex_world import local_items, local_tracks, mirror, playlist_row, tick


@pytest.fixture
def jelly(client: TestClient, operator: Account, fake_jellyfin: FakeJellyfin) -> Iterator[tuple[FakeJellyfin, int]]:
    server = connect(client, fake_jellyfin)
    server_sync.forget()
    yield fake_jellyfin, server


def ids(fake: FakeJellyfin, *names: str) -> list[str]:
    return [next(ident for ident, one in fake.tracks.items() if one.name == name
                 and fake.albums[one.album].name != "Wolkenfabrik (Remastered)") for name in names]


def synced(client: TestClient, fake: FakeJellyfin, server: int, name: str, *tracks: str) -> int:
    playlist = make(client, server, name, numbers(server, list(tracks)))
    sync()
    assert both_equal(fake, server, playlist["id"]) == list(tracks)
    sync(look=True)
    return int(playlist["id"])


def partial(server: int) -> None:
    with SessionLocal() as db:
        catalog.request_partial(db, server_id=server)
    jobs.run_pending(lane="catalog")


def list_reads(fake: FakeJellyfin, key: str) -> int:
    return sum(1 for method, path, _params in fake.calls if method == "GET" and path == f"/Playlists/{key}/Items")


# --- nexmix to Jellyfin ---------------------------------------------------------------------------------------------


def test_a_playlist_made_in_nexmix_is_made_in_jellyfin_public_and_the_chosen_users(
        client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    tracks = ids(fake, "Glasfluss", "Möwenruf", "Ampelglühen")
    playlist = make(client, server, "Grillabend", numbers(server, tracks))
    assert playlist["plex"]["state"] == "pending" and playlist["plex"]["kind"] == "jellyfin"
    sync()
    key = list_key(playlist["id"])
    made = fake.lists[key]
    assert made.name == "Grillabend" and made.public and made.owner == fake.users[0].id
    assert both_equal(fake, server, playlist["id"]) == tracks
    assert client.get(f"/api/v1/playlists/{playlist['id']}").json()["plex"]["state"] == "synced"
    # The ids go in the address: Jellyfin passes over a body.
    adds = [params for method, path, params in fake.list_writes if method == "POST" and path.endswith("/Items")]
    assert adds and all("ids" in params for params in adds)
    # The entries came through nexmix: who added them is the account.
    assert {item.added_by_name for item in local_items(playlist["id"])} != {"Jellyfin"}


def test_changes_in_nexmix_go_to_jellyfin_as_the_difference(client: TestClient,
                                                            jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c, d = ids(fake, "Glasfluss", "Möwenruf", "Ampelglühen", "Teerseile")
    playlist_id = synced(client, fake, server, "Diff", a, b, c)
    before = len(fake.list_writes)
    entry = {one["track"]: one["id"] for one in client.get(f"/api/v1/playlists/{playlist_id}").json()["items"]}
    [na, nb, nc, nd] = numbers(server, [a, b, c, d])
    assert client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": [nd]}).status_code == 200
    assert client.post(f"/api/v1/playlists/{playlist_id}/items/remove", json={"entries": [entry[nb]]}).status_code == 200
    assert client.post(f"/api/v1/playlists/{playlist_id}/items/move",
                       json={"entry": entry[nc], "after": None}).status_code == 200
    assert client.put(f"/api/v1/playlists/{playlist_id}", json={"name": "Neu"}).status_code == 200
    sync()
    assert both_equal(fake, server, playlist_id) == [c, a, d]
    assert fake.lists[list_key(playlist_id)].name == "Neu"
    done = fake.list_writes[before:]
    assert sum(1 for method, path, _ in done if method == "POST" and path.endswith("/Items")) == 1
    assert sum(1 for method, _path, _ in done if method == "DELETE") == 1
    # A move names the place afterwards, never past the end.
    for _method, path, _ in done:
        if "/Move/" in path:
            assert int(path.rsplit("/", 1)[1]) <= 2
    assert na


def test_the_own_etag_is_taken_after_a_write(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b = ids(fake, "Glasfluss", "Möwenruf")
    playlist_id = synced(client, fake, server, "Etag", a)
    key = list_key(playlist_id)
    assert client.post(f"/api/v1/playlists/{playlist_id}/items",
                       json={"tracks": numbers(server, [b])}).status_code == 200
    sync()
    assert mirror(playlist_id).seen["etag"] == fake.lists[key].etag
    reads = list_reads(fake, key)
    sync(look=True)
    # The look finds its own change taken already: nothing to read.
    assert list_reads(fake, key) == reads


def test_deleted_in_nexmix_goes_from_jellyfin_and_comes_back_anew(client: TestClient,
                                                                  jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b = ids(fake, "Glasfluss", "Möwenruf")
    playlist_id = synced(client, fake, server, "Weg", a, b)
    key = list_key(playlist_id)
    assert client.delete(f"/api/v1/playlists/{playlist_id}").status_code == 200
    sync()
    assert key not in fake.lists
    sync(look=True)
    assert playlist_row(playlist_id).deleted_at is not None
    assert client.post(f"/api/v1/playlists/{playlist_id}/restore").status_code == 200
    sync()
    assert list_key(playlist_id) != key and both_equal(fake, server, playlist_id) == [a, b]


# --- Jellyfin to nexmix -------------------------------------------------------------------------------------------------


def test_changes_made_in_jellyfin_reach_nexmix(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c, d = ids(fake, "Glasfluss", "Möwenruf", "Ampelglühen", "Teerseile")
    playlist_id = synced(client, fake, server, "Von dort", a, b, c)
    key = list_key(playlist_id)
    fake.list_add(key, [d])
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == [a, b, c, d]
    added = next(item for item in local_items(playlist_id) if item.track_id == numbers(server, [d])[0])
    assert added.added_by_name == "Jellyfin" and added.from_plex
    fake.list_remove(key, [b])
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == [a, c, d]
    # A move shows only in the Etag.
    fake.list_move(key, d, 0)
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == [d, a, c]
    fake.list_rename(key, "Umbenannt")
    sync(look=True)
    assert playlist_row(playlist_id).name == "Umbenannt"


def test_deleted_in_jellyfin_goes_to_the_trash(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    (a,) = ids(fake, "Glasfluss")
    playlist_id = synced(client, fake, server, "Papierkorb", a)
    fake.list_delete(list_key(playlist_id))
    sync(look=True)
    assert playlist_row(playlist_id).deleted_at is not None
    assert client.get(f"/api/v1/playlists/{playlist_id}").json()["plex"]["gone_in_plex"] is True


def test_the_users_own_playlists_are_taken_in_and_nobody_elses(client: TestClient,
                                                                jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b = ids(fake, "Glasfluss", "Möwenruf")
    mine = fake.make_list("Meine", [a, b, a])
    fake.make_list("Von Jonas", [b], user="Jonas")
    fake.make_list("Privat von Jonas", [a], user="Jonas", public=False)
    shared = fake.make_list("Mit Erika geteilt", [a], user="Jonas")
    fake.lists[shared].shares[fake.users[0].id] = True
    sync(look=True)
    names = sorted(one["name"] for one in client.get("/api/v1/playlists").json()["playlists"])
    assert names == ["Meine"]
    with SessionLocal() as db:
        row = db.scalars(select(ServerPlaylist).where(ServerPlaylist.ext_key == mine)).one()
        playlist_id = row.playlist_id
    assert playlist_id is not None
    # A track twice in Jellyfin is one entry in nexmix; the copy stays where it is.
    assert keys(server, local_tracks(playlist_id)) == [a, b]
    assert fake.tracks_of(mine) == [a, b, a]


def test_signed_in_as_another_user_than_the_chosen_one_nothing_is_taken_in(
        client: TestClient, operator: Account, fake_jellyfin: FakeJellyfin) -> None:
    fake = fake_jellyfin
    # Quick Connect confirmed by Erika, the playlists to belong to Jonas: Erika's own are no playlists of Jonas.
    server = connect(client, fake, user="Jonas")
    server_sync.forget()
    (a,) = ids(fake, "Glasfluss")
    fake.make_list("Von Erika", [a])
    sync(look=True)
    assert client.get("/api/v1/playlists").json()["playlists"] == []
    assert server


def test_with_an_api_key_no_playlist_of_jellyfin_is_taken_in(client: TestClient, operator: Account,
                                                              fake_jellyfin: FakeJellyfin) -> None:
    fake = fake_jellyfin
    answer = client.post("/api/v1/jellyfin/connect", json={"url": "http://jellyfin.example.com:8096",
                                                          "key": fake.api_key})
    assert answer.status_code == 200, answer.text
    server = int(answer.json()["server"]["id"])
    found = answer.json()
    if found["server"]["library"] is None:
        client.put(f"/api/v1/jellyfin/{server}/library", json={"handle": found["libraries"][0]["handle"]})
    handle = next(one["handle"] for one in found["users"] if one["name"] == "Erika")
    client.put(f"/api/v1/jellyfin/{server}/user", json={"handle": handle})
    jobs.run_pending(lane="catalog")
    server_sync.forget()
    (a,) = ids(fake, "Glasfluss")
    fake.make_list("Unbekannt wessen", [a])
    sync(look=True)
    assert client.get("/api/v1/playlists").json()["playlists"] == []
    # nexmix's own playlists go there all the same, as Erika's.
    playlist = make(client, server, "Trotzdem", numbers(server, [a]))
    sync()
    assert fake.lists[list_key(playlist["id"])].owner == fake.users[0].id


def test_copies_of_a_track_in_jellyfin_count_once_and_sorting_still_ends_right(
        client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c = ids(fake, "Glasfluss", "Möwenruf", "Ampelglühen")
    playlist_id = synced(client, fake, server, "Doppelt", a, b, c)
    key = list_key(playlist_id)
    fake.list_add(key, [a])
    sync(look=True)
    assert keys(server, local_tracks(playlist_id)) == [a, b, c]
    entry = {one["track"]: one["id"] for one in client.get(f"/api/v1/playlists/{playlist_id}").json()["items"]}
    na = numbers(server, [a])[0]
    assert client.post(f"/api/v1/playlists/{playlist_id}/items/move",
                       json={"entry": entry[na], "after": entry[numbers(server, [c])[0]]}).status_code == 200
    for _round in range(3):
        sync(look=True)
    assert both_equal(fake, server, playlist_id)[0] in (b, a)
    assert set(keys(server, local_tracks(playlist_id))) == {a, b, c}
    # Taking it out in nexmix takes every copy out there.
    entry = {one["track"]: one["id"] for one in client.get(f"/api/v1/playlists/{playlist_id}").json()["items"]}
    assert client.post(f"/api/v1/playlists/{playlist_id}/items/remove", json={"entries": [entry[na]]}).status_code == 200
    sync()
    assert a not in fake.tracks_of(key)


# --- When Jellyfin is away ------------------------------------------------------------------------------------------------


def test_jellyfin_away_in_the_middle_of_a_write_waits_and_writes_later(client: TestClient,
                                                                       jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c = ids(fake, "Glasfluss", "Möwenruf", "Ampelglühen")
    playlist_id = synced(client, fake, server, "Ausfall", a)
    assert client.post(f"/api/v1/playlists/{playlist_id}/items",
                       json={"tracks": numbers(server, [b, c])}).status_code == 200
    fake.fail_writes, fake.fail_after = 1, True
    # One round only: a look that reaches Jellyfin afterwards tries the waiting ones at once.
    tick()
    jobs.run_pending(lane="playlists", limit=1)
    view = client.get(f"/api/v1/playlists/{playlist_id}").json()["plex"]
    assert view["state"] == "waiting" and view["error"] == "jellyfin_unreachable"
    # Jellyfin did add them before its answer was lost, and somebody took one out there meanwhile: Jellyfin's ids
    # cannot show that the request arrived, so the write stays open and adding wins.
    assert fake.tracks_of(list_key(playlist_id)) == [a, b, c]
    fake.list_remove(list_key(playlist_id), [c])
    with SessionLocal() as db:
        server_sync.request_all(db)
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == [a, b, c]


def test_tracks_added_in_jellyfin_meanwhile_never_show_that_a_write_arrived(
        client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    (a,) = ids(fake, "Glasfluss")
    playlist_id = synced(client, fake, server, "Offen", a)
    # Three tracks whose numbers follow each other, as Plex's ids of one request would.
    known = dict(zip(numbers(server, sorted(fake.tracks)), sorted(fake.tracks), strict=True))
    run = next(number for number in sorted(known) if number + 1 in known and number + 2 in known
               and known[number] != a and known[number + 1] != a and known[number + 2] != a)
    trio = [known[run], known[run + 1], known[run + 2]]
    assert client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": [run, run + 1, run + 2]}
                       ).status_code == 200
    # The write never reaches Jellyfin; somebody adds two of them there meanwhile.
    fake.fail_writes, fake.fail_after = 1, False
    tick()
    jobs.run_pending(lane="playlists", limit=1)
    assert mirror(playlist_id).writing is not None
    fake.list_add(list_key(playlist_id), trio[:2])
    with SessionLocal() as db:
        server_sync.request_all(db)
    sync(look=True)
    # The third is still to be added, not taken out there.
    assert both_equal(fake, server, playlist_id) == [a, *trio]


def test_jellyfin_starting_makes_the_playlist_wait(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    (a,) = ids(fake, "Glasfluss")
    playlist = make(client, server, "Startet", numbers(server, [a]))
    fake.starting = 3
    sync(rounds=1)
    view = client.get(f"/api/v1/playlists/{playlist['id']}").json()["plex"]
    assert view["state"] == "waiting" and view["error"] == "jellyfin_starting"
    with SessionLocal() as db:
        server_sync.request_all(db)
    sync()
    assert both_equal(fake, server, playlist["id"]) == [a]


def test_a_displaced_token_is_an_error_that_asks_to_connect_again(client: TestClient,
                                                                  jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    (a,) = ids(fake, "Glasfluss")
    playlist = make(client, server, "Verdrängt", numbers(server, [a]))
    # Another sign-in from nexmix's device id elsewhere displaces the token.
    device = next(made_for for _token, (_user, made_for) in fake.tokens.items() if made_for.startswith("nexmix"))
    fake._grant(fake.users[0].id, device)
    sync(rounds=2)
    view = client.get(f"/api/v1/playlists/{playlist['id']}").json()["plex"]
    assert view["state"] == "error" and view["error"] == "jellyfin_refused"
    assert playlist["id"] in [one["id"] for one in client.get("/api/v1/playlists").json()["playlists"]]


# --- Tracks gone from the library ---------------------------------------------------------------------------------


def test_a_renamed_file_is_no_entry_taken_out_and_goes_back_after_the_catalogue(
        client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c = ids(fake, "Glasfluss", "Möwenruf", "Ampelglühen")
    playlist_id = synced(client, fake, server, "Umbenannt", a, b, c)
    key = list_key(playlist_id)
    renamed = fake.rename(b, "02 Möwenruf (neu)")
    assert fake.tracks_of(key) == [a, c]
    sync(look=True)
    items = local_items(playlist_id)
    assert len(items) == 3
    assert [item.plex_hint for item in items] == ["", "missing", ""]
    view = client.get(f"/api/v1/playlists/{playlist_id}").json()
    assert [one["plex_hint"] for one in view["items"]] == [None, "missing", None]
    # Nothing goes to Jellyfin before the catalogue knows the file again.
    sync(look=True)
    assert fake.tracks_of(key) == [a, c]
    partial(server)
    sync()
    assert both_equal(fake, server, playlist_id) == [a, renamed, c]
    assert all(not item.plex_hint for item in local_items(playlist_id))


def test_a_library_made_anew_empties_nothing_in_nexmix(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    tracks = ids(fake, "Glasfluss", "Möwenruf", "Ampelglühen", "Teerseile", "Gleis Neun", "Abfahrt")
    playlist_id = synced(client, fake, server, "Neu angelegt", *tracks)
    key = list_key(playlist_id)
    fake.library_made_anew()
    sync(look=True)
    assert keys(server, local_tracks(playlist_id)) == tracks
    view = client.get(f"/api/v1/playlists/{playlist_id}").json()
    assert view["plex"]["loss"]["count"] == 6
    assert fake.tracks_of(key) == []
    partial(server)
    sync()
    assert both_equal(fake, server, playlist_id) == tracks
    assert client.get(f"/api/v1/playlists/{playlist_id}").json()["plex"]["loss"] is None


def test_many_lost_while_tracks_left_the_library_are_kept(client: TestClient,
                                                          jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    tracks = ids(fake, "Glasfluss", "Möwenruf", "Ampelglühen", "Teerseile", "Gleis Neun", "Abfahrt", "Eisblume")
    playlist_id = synced(client, fake, server, "Massen", *tracks)
    key = list_key(playlist_id)
    # Half the entries go at once while one file leaves the library: none of it is taken.
    fake.list_remove(key, tracks[:4])
    fake.delete(tracks[6])
    sync(look=True)
    assert keys(server, local_tracks(playlist_id)) == tracks
    assert client.get(f"/api/v1/playlists/{playlist_id}").json()["plex"]["loss"]["count"] == 5


def test_a_few_taken_out_in_jellyfin_are_taken_out(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    tracks = ids(fake, "Glasfluss", "Möwenruf", "Ampelglühen", "Teerseile", "Gleis Neun", "Abfahrt", "Eisblume")
    playlist_id = synced(client, fake, server, "Wenige", *tracks)
    fake.list_remove(list_key(playlist_id), tracks[:4])
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == tracks[4:]


def test_the_loss_can_be_taken_over_in_nexmix(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    tracks = ids(fake, "Glasfluss", "Möwenruf", "Ampelglühen", "Teerseile", "Gleis Neun", "Abfahrt")
    playlist_id = synced(client, fake, server, "Übernehmen", *tracks)
    fake.library_made_anew()
    sync(look=True)
    answer = client.post(f"/api/v1/playlists/{playlist_id}/missing/remove")
    assert answer.status_code == 200, answer.text
    assert local_tracks(playlist_id) == []
    assert client.get(f"/api/v1/playlists/{playlist_id}").json()["plex"]["loss"] is None
    partial(server)
    sync()
    assert fake.tracks_of(list_key(playlist_id)) == []
    with SessionLocal() as db:
        assert not list(db.scalars(select(PlaylistItem).where(PlaylistItem.plex_hint == "missing",
                                                              PlaylistItem.removed_at.is_(None))))
