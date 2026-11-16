"""Plex and Jellyfin at once, against both stand-ins: two catalogues numbered apart, a playlist per source that searches,
suggests, adds and listens on its own server only, a playlist on Jellyfin that lives in nexmix only (no sync), a guest
who sees only the catalogue of its playlist's server, and not one id of Jellyfin, not its address and no token in any
answer a browser gets along the whole way."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Account, CatalogAlbum, ServerPlaylist
from app.services import jobs

from .conftest import new_client
from .fakejellyfin import LIBRARY_ID, OTHER_LIBRARY_ID, SERVER_ID, FakeJellyfin, make_id
from .fakeplex import FakePlex
from .jellyfin_world import number_of, quick_connect
from .plex_world import connect as connect_plex
from .plex_world import sync
from .test_guests_and_plex import invite, phone


class Seen:
    """Every answer a browser got, headers and body, as text."""

    def __init__(self) -> None:
        self.answers: list[tuple[str, str]] = []

    def watch(self, client: TestClient) -> TestClient:
        original: Callable[..., Any] = client.request

        def request(method: str, url: Any, *args: Any, **kwargs: Any) -> Any:
            answer = original(method, url, *args, **kwargs)
            body = answer.content.decode("latin-1")
            self.answers.append((f"{method} {url}", f"{dict(answer.headers)}\n{body}"))
            return answer

        client.request = request  # type: ignore[method-assign]
        return client


def secrets_of(fake: FakeJellyfin) -> set[str]:
    """What must never reach a browser: Jellyfin's ids of everything, its address, the tokens, the secrets."""
    found = {SERVER_ID, LIBRARY_ID, OTHER_LIBRARY_ID, "jellyfin.example.com", fake.api_key}
    found |= set(fake.tracks) | set(fake.albums) | {one.id for one in fake.users}
    found |= {make_id("picture", album) for album in fake.albums}
    found |= {make_id("artist", one.artist) for one in fake.albums.values()}
    found |= set(fake.tokens) | set(fake.quick)
    return found


@pytest.fixture
def both(client: TestClient, operator: Account, fake_plex: FakePlex,
         fake_jellyfin: FakeJellyfin) -> tuple[FakePlex, FakeJellyfin, int, int]:
    connect_plex(client, fake_plex)
    return fake_plex, fake_jellyfin, 1, 0


def jellyfin_in(client: TestClient, fake: FakeJellyfin) -> int:
    found = quick_connect(client, fake)
    server = int(found["server"]["id"])
    handle = next(one["handle"] for one in found["users"] if one["name"] == "Erika")
    assert client.put(f"/api/v1/jellyfin/{server}/user", json={"handle": handle}).status_code == 200
    assert jobs.run_pending(lane="catalog") == 1
    return server


def test_two_kinds_of_server_keep_their_catalogues_and_their_playlists_apart(
        client: TestClient, both: tuple[FakePlex, FakeJellyfin, int, int]) -> None:
    fake_plex, fake, plex_server, _ = both
    seen = Seen()
    seen.watch(client)
    jelly = jellyfin_in(client, fake)
    listed = client.get("/api/v1/servers").json()["servers"]
    assert [(one["id"], one["kind"], one["primary"]) for one in listed] == [(plex_server, "plex", True),
                                                                            (jelly, "jellyfin", False)]
    caps = {one["kind"]: one["capabilities"] for one in listed}
    assert caps["jellyfin"] == {"moods": False, "sound": False, "smart_playlists": False, "duplicates": True,
                                "changes": "both", "playlists": True}
    assert caps["plex"]["moods"] and caps["plex"]["playlists"]
    # Each catalogue finds its own.
    assert client.get("/api/v1/catalog/search", params={"q": "Glasfluss", "server": jelly}).json()["tracks"]
    assert not client.get("/api/v1/catalog/search", params={"q": "Glasfluss", "server": plex_server}).json()["tracks"]
    assert not client.get("/api/v1/catalog/search", params={"q": "Northern Lights", "server": jelly}).json()["tracks"]
    assert client.get(f"/api/v1/catalog/facets?server={jelly}").json()["moods"] == []
    # The state of each catalogue names the kind of its server (the overview says where the playlists end up).
    assert client.get(f"/api/v1/catalog/status?server={jelly}").json()["kind"] == "jellyfin"
    assert client.get("/api/v1/catalog/status").json()["kind"] == "plex"
    assert client.get(f"/api/v1/catalog?server={jelly}").status_code == 200
    # A playlist of each; without a server named, the first connected (Plex).
    on_plex = client.post("/api/v1/playlists", json={"name": "Auf Plex"}).json()
    on_jelly = client.post("/api/v1/playlists", json={"name": "Auf Jellyfin", "server": jelly}).json()
    assert on_plex["server"] == plex_server and on_jelly["server"] == jelly
    assert client.post("/api/v1/playlists", json={"name": "x", "server": 99}).status_code == 422
    glass = number_of(jelly, next(key for key, one in fake.tracks.items() if one.name == "Glasfluss"
                                  and fake.albums[one.album].name == "Mondgarten"))
    more = [number_of(jelly, key) for key in list(fake.tracks)[:3]]
    added = client.post(f"/api/v1/playlists/{on_jelly['id']}/items", json={"tracks": [glass, *more]})
    assert added.status_code == 200, added.text
    # A track of the other server is no track of this playlist.
    wrong = client.post(f"/api/v1/playlists/{on_jelly['id']}/items", json={"tracks": [3001]})
    assert wrong.status_code == 200 and wrong.json()["skipped"] == [3001] and wrong.json()["entries"] == []
    assert client.post(f"/api/v1/playlists/{on_plex['id']}/items", json={"tracks": [3001]}).status_code == 200
    # Each server keeps its own playlist: the one on Jellyfin there, the one on Plex in Plex.
    sync(fake_plex)
    detail = client.get(f"/api/v1/playlists/{on_jelly['id']}").json()
    assert detail["plex"]["state"] == "synced" and detail["plex"]["kind"] == "jellyfin" and detail["count"] == 4
    assert client.get(f"/api/v1/playlists/{on_plex['id']}").json()["plex"]["kind"] == "plex"
    assert client.get(f"/api/v1/playlists/{on_plex['id']}").json()["plex"]["state"] == "synced"
    with SessionLocal() as db:
        rows = db.scalars(select(ServerPlaylist).where(ServerPlaylist.playlist_id == on_jelly["id"])).all()
        assert [row.server_id for row in rows] == [jelly]
        jelly_key = rows[0].ext_key
    assert jelly_key in fake.lists and len(fake.lists[jelly_key].entries) == 4
    assert not any(one.title == "Auf Jellyfin" for one in fake_plex.lists.lists.values())
    # Listening and covers from the playlist's own server.
    assert client.get(f"/api/v1/preview/{glass}").status_code == 200
    covers = detail["covers"]
    assert covers and all(client.get(f"/api/v1/catalog/cover/{album}?s=80").status_code == 200 for album in covers)
    with SessionLocal() as db:
        jelly_albums = set(db.scalars(select(CatalogAlbum.id).where(CatalogAlbum.server_id == jelly)))
    assert set(covers) <= jelly_albums
    # Suggestions from the playlist's server, without moods and without "sounds alike".
    suggested = client.get("/api/v1/suggestions", params={"playlist": on_jelly["id"]}).json()["tracks"]
    assert suggested and all(one["id"] not in (3001,) for one in suggested)
    assert client.get("/api/v1/suggestions/sonic", params={"track": glass, "server": jelly}).json()["songs"] == []
    assert not any("Similar" in path or "InstantMix" in path for _method, path, _params in fake.calls)
    # The operator's views of Jellyfin.
    for path in ("/api/v1/jellyfin", f"/api/v1/jellyfin/{jelly}", f"/api/v1/jellyfin/{jelly}/choices",
                 f"/api/v1/catalog/status?server={jelly}", "/api/v1/settings/readiness", "/api/v1/playlists",
                 f"/api/v1/catalog/albums/{covers[0]}?server={jelly}"):
        assert client.get(path).status_code == 200, path

    # A guest of the Jellyfin playlist: its catalogue, its covers, its sound, never Plex's.
    code = invite(client, on_jelly["id"])
    guest = seen.watch(phone(code))
    with new_client(None) as door:
        seen.watch(door)
        assert door.get(f"/api/v1/join/{code}").status_code == 200
    page = guest.get("/api/v1/guest")
    assert page.status_code == 200
    guest_catalogue = guest.get("/api/v1/guest/catalog")
    assert guest_catalogue.status_code == 200
    assert "Glasfluss" in guest_catalogue.text and "Northern Lights" not in guest_catalogue.text
    assert guest.get("/api/v1/guest/suggestions").status_code == 200
    assert guest.get(f"/api/v1/guest/cover/{covers[0]}?s=80").status_code == 200
    with SessionLocal() as db:
        plex_album = db.scalars(select(CatalogAlbum.id).where(CatalogAlbum.server_id == plex_server)).first()
    assert guest.get(f"/api/v1/guest/cover/{plex_album}?s=80").status_code == 404
    assert guest.get(f"/api/v1/preview/{glass}").status_code == 200
    assert guest.get("/api/v1/preview/3001").status_code == 404
    fresh = number_of(jelly, next(key for key, one in fake.tracks.items() if one.name == "Eisblume"))
    added = guest.post("/api/v1/guest/items", json={"tracks": [fresh]})
    assert added.status_code == 200, added.text
    # What a guest adds goes to Jellyfin like everything else.
    sync(fake_plex)
    assert len(fake.lists[jelly_key].entries) == 5

    # Not one of Jellyfin's ids, its address or a token in anything a browser got.
    forbidden = secrets_of(fake)
    assert len(seen.answers) > 30
    leaks = [(where, value) for where, text in seen.answers for value in forbidden if value in text]
    assert leaks == []
    # Nor anything shaped like one of Jellyfin's ids (32 hexadecimal characters), but the names nexmix gives its own
    # covers and catalogues (their ETags).
    tags = {value.strip('"') for _where, text in seen.answers for value in re.findall(r"'etag': '([^']+)'", text)}
    shaped = {value for _where, text in seen.answers for value in re.findall(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", text)}
    assert shaped - tags == set()
