"""A playlist goes to nexmix's trash because it was deleted on its server only when the server's list, read again
with 200, lacks it, no other playlist of that server is missing from the list at once, and the server says of the
playlist itself that it is gone. A 404 of the whole server (a proxy, a changed base path), a Jellyfin user gone, or
several playlists missing at once move nothing to the trash, end no invitation, and never bring the server's old
playlists in again as new ones."""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.db import SessionLocal
from app.models import Account, GuestInvite, MediaServer, Playlist, ServerPlaylist
from app.services import jobs
from app.services import sync as server_sync

from .fakejellyfin import FakeJellyfin
from .fakeplex import FakePlex
from .jellyfin_world import both_equal, connect, list_key, make, numbers, sync
from .plex_world import mirror, playlist_row

NAMES = ("Glasfluss", "Möwenruf", "Ampelglühen", "Teerseile")


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


def invited(client: TestClient, playlist_id: int) -> None:
    answer = client.post(f"/api/v1/playlists/{playlist_id}/invite")
    assert answer.status_code in (200, 201), answer.text
    started = client.put(f"/api/v1/playlists/{playlist_id}/invite", json={"start": True})
    assert started.status_code == 200, started.text


def invitation_open(playlist_id: int) -> bool:
    with SessionLocal() as db:
        return bool(db.scalar(select(func.count()).select_from(GuestInvite).where(
            GuestInvite.playlist_id == playlist_id, GuestInvite.ended_at.is_(None))))


def change(client: TestClient, server: int, playlist_id: int, track: str) -> None:
    """A change in nexmix: the playlist is due, and its sync reads the server."""
    answer = client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": numbers(server, [track])})
    assert answer.status_code == 200, answer.text


def playlist_count() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.count()).select_from(Playlist)) or 0)


def overview(client: TestClient, server: int) -> dict:
    return client.get(f"/api/v1/jellyfin/{server}/playlists").json()


# --- Jellyfin -------------------------------------------------------------------------------------------------------


def test_jellyfin_answering_404_to_everything_trashes_nothing_and_ends_no_invitation(
        client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c, _d = ids(fake, *NAMES)
    playlist_id = synced(client, fake, server, "Hinter dem Proxy", a, b)
    invited(client, playlist_id)
    count = playlist_count()
    fake.moved_away = True
    server_sync.forget()
    change(client, server, playlist_id, c)
    for _round in range(3):
        with SessionLocal() as db:
            server_sync.request_all(db)
        sync(look=True)
    assert playlist_row(playlist_id).deleted_at is None
    assert invitation_open(playlist_id)
    row = mirror(playlist_id)
    assert row.state == server_sync.ERROR and row.error == "playlists_not_found"
    assert overview(client, server)["poll_error"] == "playlists_not_found"
    # Back at its address: written as if nothing had been, nothing taken in twice.
    fake.moved_away = False
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == [a, b, c]
    assert playlist_count() == count and len(fake.lists) == 1


def test_the_chosen_user_unknown_to_jellyfin_is_an_error_of_the_connection(
        client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    """Measured on 12.4: ``/Items`` as an unknown user answers 404, a playlist's entries as one 500."""
    fake, server = jelly
    a, b, c, _d = ids(fake, *NAMES)
    playlist_id = synced(client, fake, server, "Benutzer weg", a, b)
    invited(client, playlist_id)
    # A user id Jellyfin does not know, made now.
    ghost = secrets.token_hex(16)
    with SessionLocal() as db:
        db.execute(update(MediaServer).where(MediaServer.id == server).values(user_id=ghost))
        db.commit()
    server_sync.forget()
    change(client, server, playlist_id, c)
    sync(look=True)
    assert playlist_row(playlist_id).deleted_at is None
    assert invitation_open(playlist_id)
    row = mirror(playlist_id)
    assert row.state == server_sync.ERROR and row.error == "jellyfin_user_gone"
    assert overview(client, server)["poll_error"] == "jellyfin_user_gone"
    assert client.get(f"/api/v1/playlists/{playlist_id}").json()["plex"]["error"] == "jellyfin_user_gone"


def test_the_chosen_user_deleted_in_jellyfin_trashes_nothing(client: TestClient, operator: Account,
                                                             fake_jellyfin: FakeJellyfin) -> None:
    """Connected with an API key, the playlists Jonas's: Jonas deleted in Jellyfin."""
    fake = fake_jellyfin
    answer = client.post("/api/v1/jellyfin/connect", json={"url": "http://jellyfin.example.com:8096",
                                                          "key": fake.api_key})
    assert answer.status_code == 200, answer.text
    found = answer.json()
    server = int(found["server"]["id"])
    if found["server"]["library"] is None:
        client.put(f"/api/v1/jellyfin/{server}/library", json={"handle": found["libraries"][0]["handle"]})
    handle = next(one["handle"] for one in found["users"] if one["name"] == "Jonas")
    assert client.put(f"/api/v1/jellyfin/{server}/user", json={"handle": handle}).status_code == 200
    jobs.run_pending(lane="catalog")
    server_sync.forget()
    a, b, c, d = ids(fake, *NAMES)
    first = synced(client, fake, server, "Jonas eins", a, b)
    second = synced(client, fake, server, "Jonas zwei", c)
    invited(client, first)
    count = playlist_count()
    fake.remove_user("Jonas")
    server_sync.forget()
    change(client, server, first, d)
    sync(look=True)
    for playlist_id in (first, second):
        assert playlist_row(playlist_id).deleted_at is None
    assert invitation_open(first)
    assert mirror(first).error == "jellyfin_user_gone"
    assert playlist_count() == count


def test_a_playlist_listed_no_more_but_there_is_not_deleted(client: TestClient,
                                                            jelly: tuple[FakeJellyfin, int]) -> None:
    """The user's list lacks it and its entries as that user answer 404 (rights changed in Jellyfin), but asked
    without a user they still answer: it is there, nothing goes to the trash."""
    fake, server = jelly
    a, b, c, _d = ids(fake, *NAMES)
    playlist_id = synced(client, fake, server, "Unsichtbar", a, b)
    synced(client, fake, server, "Daneben", c)
    invited(client, playlist_id)
    hidden = list_key(playlist_id)
    original = fake._handle

    def handle(request: httpx.Request) -> httpx.Response:
        answer = original(request)
        params = request.url.params
        if request.url.path == "/Items" and params.get("IncludeItemTypes") == "Playlist":
            found = json.loads(answer.content)
            found["Items"] = [one for one in found["Items"] if one["Id"] != hidden]
            return httpx.Response(200, json=found, headers={"date": answer.headers["date"]})
        if request.url.path == "/Items" and params.get("Ids") == hidden:
            return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0}, headers={"date": answer.headers["date"]})
        if request.url.path == f"/Playlists/{hidden}/Items" and params.get("userId"):
            return httpx.Response(404, text="Error processing request.")
        return answer

    fake._handle = handle  # type: ignore[method-assign]
    server_sync.forget()
    change(client, server, playlist_id, c)
    sync(look=True)
    assert playlist_row(playlist_id).deleted_at is None
    assert invitation_open(playlist_id)
    assert mirror(playlist_id).error == server_sync.UNREADABLE


def test_two_playlists_gone_at_once_go_nowhere_and_the_server_says_so(client: TestClient,
                                                                      jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c, d = ids(fake, *NAMES)
    first = synced(client, fake, server, "Eins", a, b)
    second = synced(client, fake, server, "Zwei", c)
    third = synced(client, fake, server, "Drei", d)
    invited(client, first)
    fake.list_delete(list_key(first))
    fake.list_delete(list_key(second))
    server_sync.forget()
    sync(look=True)
    change(client, server, first, d)
    sync(look=True)
    for playlist_id in (first, second, third):
        assert playlist_row(playlist_id).deleted_at is None
    assert invitation_open(first)
    assert mirror(first).error == "playlists_not_found"
    assert overview(client, server)["missing"] == 2
    assert client.get(f"/api/v1/playlists/{third}").json()["plex"]["state"] == "synced"


def test_one_playlist_deleted_in_jellyfin_still_goes_to_the_trash(client: TestClient,
                                                                  jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c, _d = ids(fake, *NAMES)
    first = synced(client, fake, server, "Weg", a, b)
    second = synced(client, fake, server, "Bleibt", c)
    invited(client, first)
    fake.list_delete(list_key(first))
    sync(look=True)
    assert playlist_row(first).deleted_at is not None
    assert playlist_row(second).deleted_at is None
    # Deleted there for real: its invitation ends, as in the trash from nexmix.
    assert not invitation_open(first)
    assert overview(client, server)["missing"] == 0


def test_a_playlist_deleted_there_is_never_taken_in_again_as_a_new_one(client: TestClient,
                                                                       jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c, _d = ids(fake, *NAMES)
    first = synced(client, fake, server, "Zurück", a, b)
    synced(client, fake, server, "Daneben", c)
    key = list_key(first)
    kept = fake.lists[key]
    fake.list_delete(key)
    sync(look=True)
    assert playlist_row(first).deleted_at is not None
    count = playlist_count()
    # The same playlist there again under its key (a backup played back): no second playlist in nexmix.
    fake.lists[key] = kept
    sync(look=True)
    sync(look=True)
    assert playlist_count() == count
    # Brought back from the trash, it takes up the one there instead of making another.
    assert client.post(f"/api/v1/playlists/{first}/restore").status_code == 200
    sync(look=True)
    assert mirror(first).ext_key == key
    assert both_equal(fake, server, first) == [a, b]
    assert sum(1 for one in fake.lists.values() if one.name == "Zurück") == 1


def test_deleted_for_good_after_it_was_deleted_there_leaves_a_new_one_there_alone(
        client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, b, c, _d = ids(fake, *NAMES)
    first = synced(client, fake, server, "Endgültig", a, b)
    synced(client, fake, server, "Daneben", c)
    key = list_key(first)
    fake.list_delete(key)
    sync(look=True)
    assert playlist_row(first).deleted_at is not None
    # Somebody makes one there that gets the same id (Jellyfin's ids hang on paths); nexmix's is deleted for good.
    fake.lists[key] = type(fake.lists[list_key(first + 1)])(key, "Endgültig", fake.users[0].id, entries=[c])
    answer = client.delete(f"/api/v1/playlists/{first}?purge=true")
    assert answer.status_code == 200, answer.text
    sync(look=True)
    assert key in fake.lists and fake.tracks_of(key) == [c]


# --- Plex -----------------------------------------------------------------------------------------------------------


@pytest.fixture
def plex(client: TestClient, operator: Account, fake_plex: FakePlex) -> Iterator[FakePlex]:
    from .plex_world import connect as plex_connect

    plex_connect(client, fake_plex)
    server_sync.forget()
    yield fake_plex


def test_plex_answering_404_to_everything_trashes_nothing(client: TestClient, plex: FakePlex,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import plex as plex_client

    from .plex_world import sync as plex_sync
    from .test_plex_sync import synced as plex_synced

    playlist_id, _key = plex_synced(client, plex, "Proxy Plex", [3001, 3003])
    invited(client, playlist_id)
    real = plex_client.TRANSPORT
    monkeypatch.setattr(plex_client, "TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(404, text="x")))
    server_sync.forget()
    assert client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": [3013]}).status_code == 200
    plex_sync(plex, look_at_plex=True)
    assert playlist_row(playlist_id).deleted_at is None
    assert invitation_open(playlist_id)
    assert mirror(playlist_id).error == "playlists_not_found"
    monkeypatch.setattr(plex_client, "TRANSPORT", real)
    plex_sync(plex, look_at_plex=True)
    from .plex_world import both_equal as plex_equal

    assert plex_equal(plex, playlist_id) == [3001, 3003, 3013]


def test_two_plex_playlists_gone_at_once_go_nowhere(client: TestClient, plex: FakePlex) -> None:
    from .plex_world import sync as plex_sync
    from .test_plex_sync import synced as plex_synced

    first, first_key = plex_synced(client, plex, "Plex eins", [3001])
    second, second_key = plex_synced(client, plex, "Plex zwei", [3003])
    plex.lists.delete(first_key)
    plex.lists.delete(second_key)
    plex.tick()
    server_sync.forget()
    plex_sync(plex, look_at_plex=True)
    assert client.post(f"/api/v1/playlists/{first}/items", json={"tracks": [3013]}).status_code == 200
    plex_sync(plex, look_at_plex=True)
    assert playlist_row(first).deleted_at is None and playlist_row(second).deleted_at is None
    assert client.get("/api/v1/plex/playlists").json()["missing"] == 2


def test_rows_of_a_server_keep_their_key_once_deleted_there(client: TestClient,
                                                            jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    a, _b, c, _d = ids(fake, *NAMES)
    first = synced(client, fake, server, "Schlüssel", a)
    synced(client, fake, server, "Daneben", c)
    key = list_key(first)
    fake.list_delete(key)
    sync(look=True)
    with SessionLocal() as db:
        row = db.scalars(select(ServerPlaylist).where(ServerPlaylist.playlist_id == first)).one()
        assert (row.ext_key, row.resume, row.gone_there) == (key, True, True)


def test_with_an_api_key_a_playlist_deleted_in_jellyfin_still_goes_to_the_trash(
        client: TestClient, operator: Account, fake_jellyfin: FakeJellyfin) -> None:
    """An API key has no user: its entries asked without one answer 400 (measured), so they are asked as the chosen
    user, and 404 there is deleted."""
    fake = fake_jellyfin
    answer = client.post("/api/v1/jellyfin/connect", json={"url": "http://jellyfin.example.com:8096",
                                                          "key": fake.api_key})
    assert answer.status_code == 200, answer.text
    found = answer.json()
    server = int(found["server"]["id"])
    if found["server"]["library"] is None:
        client.put(f"/api/v1/jellyfin/{server}/library", json={"handle": found["libraries"][0]["handle"]})
    handle = next(one["handle"] for one in found["users"] if one["name"] == "Erika")
    assert client.put(f"/api/v1/jellyfin/{server}/user", json={"handle": handle}).status_code == 200
    jobs.run_pending(lane="catalog")
    server_sync.forget()
    a, b, c, _d = ids(fake, *NAMES)
    first = synced(client, fake, server, "Schlüssel weg", a, b)
    second = synced(client, fake, server, "Schlüssel bleibt", c)
    fake.list_delete(list_key(first))
    sync(look=True)
    assert playlist_row(first).deleted_at is not None
    assert playlist_row(second).deleted_at is None
    asked = [params for method, path, params in fake.calls if method == "GET" and path.endswith("/Items")
             and path.startswith("/Playlists/") and "userId" not in params]
    assert asked, "asked without a user first"
