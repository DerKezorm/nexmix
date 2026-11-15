"""Edges of the Jellyfin sync: after a lost answer to "make a playlist" only the user's own, freshly made one is taken
(never another user's public one, never an older one of the same name); a playlist whose owner could not be told is
asked about again after an hour; an entry whose file was renamed and retagged finds its track again by its
MusicBrainz id."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import clock
from app.models import Account
from app.services import catalog, jobs
from app.services import sync as server_sync

from .fakejellyfin import FakeJellyfin
from .jellyfin_world import both_equal, connect, keys, list_key, make, numbers, sync
from .plex_world import local_items, local_tracks

NAMES = ("Glasfluss", "Möwenruf", "Ampelglühen", "Teerseile")


@pytest.fixture
def jelly(client: TestClient, operator: Account, fake_jellyfin: FakeJellyfin) -> Iterator[tuple[FakeJellyfin, int]]:
    server = connect(client, fake_jellyfin)
    server_sync.forget()
    yield fake_jellyfin, server


def ids(fake: FakeJellyfin, *names: str) -> list[str]:
    return [next(ident for ident, one in fake.tracks.items() if one.name == name
                 and fake.albums[one.album].name != "Wolkenfabrik (Remastered)") for name in names]


def lost_answer_to_make(client: TestClient, fake: FakeJellyfin, server: int, name: str, tracks: list[str]) -> int:
    """A playlist made in nexmix; Jellyfin makes it but the answer is lost. Then the next sync."""
    playlist = make(client, server, name, numbers(server, tracks))
    fake.fail_writes, fake.fail_after = 1, True
    sync()
    fake.fail_writes = 0
    sync(look=True)
    return int(playlist["id"])


def test_after_a_lost_answer_another_users_public_playlist_is_never_taken(client: TestClient,
                                                                          jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    (a,) = ids(fake, NAMES[0])
    foreign = fake.make_list("Gemeinsam", user="Jonas")
    playlist_id = lost_answer_to_make(client, fake, server, "Gemeinsam", [a])
    ours = list_key(playlist_id)
    assert ours != foreign and fake.lists[ours].owner == fake.users[0].id
    assert fake.tracks_of(foreign) == [] and fake.lists[foreign].owner != fake.users[0].id
    assert both_equal(fake, server, playlist_id) == [a]
    # Deleted in nexmix: only nexmix's own goes from Jellyfin.
    assert client.delete(f"/api/v1/playlists/{playlist_id}").status_code == 200
    sync()
    assert foreign in fake.lists and ours not in fake.lists


def test_after_a_lost_answer_an_older_one_of_the_same_name_is_not_taken(client: TestClient,
                                                                        jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    (a,) = ids(fake, NAMES[0])
    older = fake.make_list("Wieder")
    fake.tick(3 * 24 * 3600)
    playlist_id = lost_answer_to_make(client, fake, server, "Wieder", [a])
    made = [key for key, one in fake.lists.items() if one.name == "Wieder" and key != older]
    assert list_key(playlist_id) == made[0]
    assert fake.tracks_of(older) == []


def test_after_a_lost_answer_the_one_made_then_is_taken_and_nothing_twice(client: TestClient,
                                                                          jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    (a,) = ids(fake, NAMES[0])
    playlist_id = lost_answer_to_make(client, fake, server, "Einmal", [a])
    assert sum(1 for one in fake.lists.values() if one.name == "Einmal") == 1
    assert both_equal(fake, server, playlist_id) == [a]


def test_a_playlist_whose_owner_could_not_be_told_is_asked_about_again_after_an_hour(
        client: TestClient, jelly: tuple[FakeJellyfin, int], monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _server = jelly
    (a,) = ids(fake, NAMES[0])
    made = fake.make_list("Später", [a])
    original = fake._handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/Playlists/{made}/Users":
            return httpx.Response(403, text="Error processing request.")
        return original(request)

    fake._handle = handle  # type: ignore[method-assign]
    sync(look=True)
    assert client.get("/api/v1/playlists").json()["playlists"] == []
    fake._handle = original  # type: ignore[method-assign]
    sync(look=True)
    assert client.get("/api/v1/playlists").json()["playlists"] == []
    later = clock.now() + timedelta(minutes=61)
    monkeypatch.setattr(clock, "now", lambda: later)
    sync(look=True)
    assert [one["name"] for one in client.get("/api/v1/playlists").json()["playlists"]] == ["Später"]


def test_a_renamed_and_retagged_file_is_found_again_by_its_musicbrainz_id(client: TestClient,
                                                                          jelly: tuple[FakeJellyfin, int]) -> None:
    """The title changed with the file (no longer the same title on the same album): only the MusicBrainz id of the
    track says it is the same file."""
    fake, server = jelly
    a, b = ids(fake, "Glasfluss", "Ampelglühen")
    assert fake.tracks[b].mbid_track
    playlist = make(client, server, "Umbenannt", numbers(server, [a, b]))
    sync()
    sync(look=True)
    renamed = fake.rename(b, "01 Ampelgluehen (Neu abgemischt)")
    fake.touch(renamed, name="Ampelgluehen (Neu abgemischt)")
    with server_sync.SessionLocal() as db:
        catalog.request_partial(db, server_id=server)
    jobs.run_pending(lane="catalog")
    sync(look=True)
    assert keys(server, local_tracks(playlist["id"])) == [a, renamed]
    assert all(not item.plex_hint for item in local_items(playlist["id"]))
    assert both_equal(fake, server, playlist["id"]) == [a, renamed]
