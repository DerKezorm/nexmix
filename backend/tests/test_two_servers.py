"""Two Plex servers at once, against two stand-in Plex servers with libraries of their own whose keys overlap: each has
its catalogue, numbered apart; a playlist searches, suggests, adds and is synced with its own server only, and its
guests see nothing of the other one."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.db import SessionLocal
from app.models import Account, CatalogTrack, Playlist
from app.services import jobs, media, plex, plex_link, plextv
from app.services import sync as server_sync

from .conftest import fake_resolver
from .fakeplex import FakePlex, ServerSpec
from .fakeplex.library import ALBUM, ARTIST, TRACK, Item
from .plex_world import connect, sync
from .test_guests_and_plex import invite, phone

ATTIC_HOST = "attic.example.com"


def attic_library() -> dict[int, Item]:
    """A small library of its own, made up; its keys are the studio's keys for other things, and one is new."""
    items = [
        Item(1001, ARTIST, "Lumen Crane", genres=["Folk"]),
        Item(2001, ALBUM, "Attic Tapes", 1001, 1977, genres=["Folk"]),
        Item(3001, TRACK, "Dust and Light", 2001, duration=181_000, rating=900),
        Item(3002, TRACK, "Rafters", 2001, duration=203_000, rating=400),
        # The title of a song of the other server: the search of each still finds only its own.
        Item(3003, TRACK, "Northern Lights", 2001, duration=222_000, rating=100),
        Item(3004, TRACK, "Floorboards", 2001, duration=187_000, rating=70),
        Item(7001, TRACK, "Skylight", 2001, duration=199_000, rating=50),
    ]
    return {item.key: item for item in items}


@pytest.fixture
def two(client: TestClient, operator: Account, fake_plex: FakePlex,
        monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FakePlex, FakePlex, int, int]]:
    """The studio (server 1, the one of the Plex card) and the attic beside it (server 2)."""
    attic = FakePlex(items=attic_library())
    attic.servers = [ServerSpec("fake-machine-attic", "Attic", attic.account_token, (ATTIC_HOST,))]

    def route(request: httpx.Request) -> httpx.Response:
        return attic.handle(request) if request.url.host == ATTIC_HOST else fake_plex.handle(request)

    transport = httpx.MockTransport(route)
    monkeypatch.setattr(plex, "TRANSPORT", transport)
    monkeypatch.setattr(plex, "ASYNC_TRANSPORT", transport)
    monkeypatch.setattr(plextv, "TRANSPORT", transport)
    monkeypatch.setattr(plex, "resolver", fake_resolver)
    connect(client, fake_plex)
    with SessionLocal() as db:
        found = plex_link.by_hand(db, f"http://{ATTIC_HOST}:32400", attic.account_token)
        second = plex_link.store(db, found, "manual", alongside=True).id
        db.commit()
        from app.services import catalog

        assert catalog.request_full(db, "first", second)
    assert jobs.run_pending(lane="catalog") == 1
    server_sync.forget()
    yield fake_plex, attic, 1, second


def _numbers(server: int) -> set[int]:
    with SessionLocal() as db:
        return set(db.scalars(select(CatalogTrack.id).where(CatalogTrack.server_id == server)))


def test_each_server_has_its_own_catalogue_numbered_apart(client: TestClient,
                                                         two: tuple[FakePlex, FakePlex, int, int]) -> None:
    studio, attic, first, second = two
    mine, theirs = _numbers(first), _numbers(second)
    assert mine and theirs and not mine & theirs, "a number belongs to one server"
    assert len(theirs) == len([item for item in attic.items.values() if item.kind == TRACK])
    with SessionLocal() as db:
        keys = media.keys(db, second, media.TRACK, theirs)
        # The attic's keys that the studio uses too got numbers of their own; a key nobody used kept itself.
        assert sorted(int(key) for key in keys.values()) == sorted(key for key, item in attic.items.items()
                                                                   if item.kind == TRACK)
        assert keys.get(7001) == "7001"
        assert all(int(key) != number for number, key in keys.items() if key != "7001")
        indexed = dict(db.execute(text("SELECT server_id, count(*) FROM track_search GROUP BY server_id")).all())
    assert indexed == {first: len(mine), second: len(theirs)}
    # The catalogue for the browser, the status and the search: by server; without one, the first server's.
    default = client.get("/api/v1/catalog").json()
    attic_catalogue = client.get("/api/v1/catalog", params={"server": second}).json()
    assert {row[0] for row in default["tracks"]} == mine
    assert {row[0] for row in attic_catalogue["tracks"]} == theirs
    assert client.get("/api/v1/catalog", params={"server": second}).headers["etag"] != client.get(
        "/api/v1/catalog").headers["etag"]
    assert client.get("/api/v1/catalog/status", params={"server": second}).json()["counts"]["tracks"] == len(theirs)
    found = client.get("/api/v1/catalog/search", params={"q": "northern", "server": second}).json()
    assert {hit["id"] for hit in found["tracks"]} <= theirs and found["tracks"]
    found = client.get("/api/v1/catalog/search", params={"q": "northern"}).json()
    assert {hit["id"] for hit in found["tracks"]} <= mine and found["tracks"]
    assert client.get("/api/v1/catalog", params={"server": 99}).status_code == 409
    servers = client.get("/api/v1/servers").json()["servers"]
    assert [(one["id"], one["name"], one["active"], one["ready"], one["primary"]) for one in servers] == [
        (first, "Studio", True, True, True), (second, "Attic", True, True, False)]
    assert "url" not in servers[0] and studio.account_token not in str(servers)


def _attic_playlist(client: TestClient, attic: FakePlex, second: int) -> dict[str, Any]:
    """A playlist made in the attic's Plex comes to nexmix with the attic as its server."""
    attic.lists.make("Dachboden", [3001, 3002])
    attic.tick(5)
    sync(attic, look_at_plex=True)
    card = next(card for card in client.get("/api/v1/playlists").json()["playlists"] if card["name"] == "Dachboden")
    assert card["server"] == second
    return card


def test_a_playlist_searches_and_adds_on_its_own_server_and_syncs_there(
        client: TestClient, two: tuple[FakePlex, FakePlex, int, int]) -> None:
    studio, attic, first, second = two
    card = _attic_playlist(client, attic, second)
    with SessionLocal() as db:
        theirs = media.known_numbers(db, second, media.TRACK, ["3001", "3002", "3003", "3004", "7001"])
        mine = media.known_numbers(db, first, media.TRACK, ["3001", "3003"])
    detail = client.get(f"/api/v1/playlists/{card['id']}").json()
    assert [entry["track"] for entry in detail["items"]] == [theirs["3001"], theirs["3002"]]
    assert [entry["title"] for entry in detail["items"]] == ["Dust and Light", "Rafters"]
    # A track of the other server is no track of this playlist: left out, nothing goes anywhere.
    added = client.post(f"/api/v1/playlists/{card['id']}/items",
                        json={"tracks": [mine["3003"], theirs["3003"], theirs["7001"]]})
    assert added.status_code == 200 and added.json()["skipped"] == [mine["3003"]]
    # Added by the attic's numbers, sent by the attic's keys.
    assert theirs["3003"] != 3003
    studio_writes = len(studio.lists.writes)
    sync(attic)
    sync(studio)
    key = next(one.key for one in attic.lists.lists.values() if one.title == "Dachboden")
    assert attic.lists.tracks(key) == [3001, 3002, 3003, 7001]
    assert len(studio.lists.writes) == studio_writes, "the other server heard nothing of it"
    assert [one.title for one in studio.lists.lists.values()] == []
    # Changed in the attic's Plex: it comes back by the attic's numbers.
    attic.lists.add(key, [3004])
    attic.tick(5)
    sync(attic, look_at_plex=True)
    detail = client.get(f"/api/v1/playlists/{card['id']}").json()
    assert [entry["track"] for entry in detail["items"]][-1] == theirs["3004"] != 3004
    # A playlist made in nexmix belongs to the first server.
    made = client.post("/api/v1/playlists", json={"name": "Im Studio"}).json()
    assert made["server"] == first
    # The suggestions of a playlist come from its server's catalogue.
    suggested = client.get("/api/v1/suggestions", params={"playlist": card["id"]}).json()
    assert {track["id"] for track in suggested["tracks"]} <= _numbers(second)


def test_a_guest_of_a_playlist_on_the_second_server_sees_that_server_only(
        client: TestClient, two: tuple[FakePlex, FakePlex, int, int]) -> None:
    _studio, attic, first, second = two
    card = _attic_playlist(client, attic, second)
    with SessionLocal() as db:
        theirs = media.known_numbers(db, second, media.TRACK, ["7001", "3003"])
        mine = media.known_numbers(db, first, media.TRACK, ["3001"])
        source = db.scalar(select(Playlist.source_server_id).where(Playlist.id == card["id"]))
    assert source == second
    lena = phone(invite(client, card["id"]))
    catalogue = lena.get("/api/v1/guest/catalog").json()
    assert {row[0] for row in catalogue["tracks"]} == _numbers(second)
    assert lena.get("/api/v1/guest").json()["catalog"]["ready"] is True
    added = lena.post("/api/v1/guest/items", json={"tracks": [mine["3001"], theirs["7001"]]})
    assert added.status_code == 200 and added.json()["skipped"] == [mine["3001"]]
    # The covers of the other server's albums are not the guest's to see.
    with SessionLocal() as db:
        studio_album = db.scalar(select(CatalogTrack.album_id).where(CatalogTrack.server_id == first))
        attic_album = db.scalar(select(CatalogTrack.album_id).where(CatalogTrack.server_id == second))
    assert lena.get(f"/api/v1/guest/cover/{studio_album}").status_code == 404
    assert lena.get(f"/api/v1/guest/cover/{attic_album}").status_code == 200
    # Listening in: a track of the playlist's server, never one of the other.
    assert lena.get(f"/api/v1/preview/{mine['3001']}").status_code == 404
    heard = lena.get(f"/api/v1/preview/{theirs['3003']}")
    assert heard.status_code == 200 and heard.content
    assert theirs["3003"] != 3003 and attic.starts[-1]["path"] == "/library/metadata/3003"
