"""Before an entry taken out of a playlist on the server is taken out in nexmix, the server is asked whether its track
is still in its library: one question per sync, in parts. A track gone there keeps its entry with the note "missing"
whatever the size of the playlist and whatever nexmix's catalogue says, and goes back after the catalogue's run. A
question that fails takes nothing. A playlist emptied on purpose (its tracks all still there) is taken."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Account
from app.services import catalog, jobs
from app.services import sync as server_sync

from .fakejellyfin import FakeJellyfin
from .fakeplex import FakePlex
from .jellyfin_world import both_equal, connect, keys, list_key, make, numbers, sync
from .plex_world import local_items, local_tracks, mirror

NAMES = ("Glasfluss", "Möwenruf", "Ampelglühen", "Teerseile", "Gleis Neun", "Abfahrt", "Eisblume")


@pytest.fixture
def jelly(client: TestClient, operator: Account, fake_jellyfin: FakeJellyfin) -> Iterator[tuple[FakeJellyfin, int]]:
    server = connect(client, fake_jellyfin)
    server_sync.forget()
    yield fake_jellyfin, server


def ids(fake: FakeJellyfin, *names: str) -> list[str]:
    return [next(ident for ident, one in fake.tracks.items() if one.name == name
                 and fake.albums[one.album].name != "Wolkenfabrik (Remastered)") for name in names]


def partial(server: int | None = None) -> None:
    with SessionLocal() as db:
        catalog.request_partial(db, server_id=server)
    jobs.run_pending(lane="catalog")


def restart() -> None:
    """nexmix stopped and started again: what it kept in memory is gone, the jobs of the lane taken up."""
    server_sync.forget()
    jobs.stopping.clear()
    with SessionLocal() as db:
        jobs.recover(db, server_sync.LANE)
        server_sync.settle(db)


def tracks_for(fake: FakeJellyfin, server: int, size: int) -> list[str]:
    if size <= len(NAMES):
        return ids(fake, *NAMES[:size])
    many = fake.bulk(size)
    partial(server)
    return many


def synced(client: TestClient, fake: FakeJellyfin, server: int, name: str, tracks: list[str]) -> int:
    playlist = make(client, server, name, numbers(server, tracks))
    sync()
    assert both_equal(fake, server, playlist["id"]) == tracks
    sync(look=True)
    return int(playlist["id"])


def existence_asks(fake: FakeJellyfin) -> list[dict[str, str]]:
    """The questions whether tracks are still there: ``/Items?Ids=`` without a user."""
    return [params for method, path, params in fake.calls
            if method == "GET" and path == "/Items" and "Ids" in params and "userId" not in params]


# --- The library removed while nexmix did not look ------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 4, 5, 50])
def test_a_library_removed_while_nexmix_was_away_loses_nothing(client: TestClient, jelly: tuple[FakeJellyfin, int],
                                                               size: int) -> None:
    fake, server = jelly
    tracks = tracks_for(fake, server, size)
    playlist_id = synced(client, fake, server, f"Weg {size}", tracks)
    key = list_key(playlist_id)
    # nexmix away: the library removed, every playlist of Jellyfin empty. nexmix comes back and looks.
    fake.library_removed()
    restart()
    sync(look=True)
    assert keys(server, local_tracks(playlist_id)) == tracks
    assert {item.plex_hint for item in local_items(playlist_id)} == {"missing"}
    assert fake.tracks_of(key) == []
    # Stopped again before the catalogue's run: nothing changes.
    restart()
    sync(look=True)
    assert keys(server, local_tracks(playlist_id)) == tracks
    # The library back under the same paths, the catalogue's run: everything written back.
    fake.library_back()
    restart()
    partial(server)
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == tracks
    assert all(not item.plex_hint for item in local_items(playlist_id))


@pytest.mark.parametrize("size", [1, 4, 5, 50])
def test_the_catalogue_first_then_the_look_still_keeps_the_entries(client: TestClient,
                                                                   jelly: tuple[FakeJellyfin, int], size: int) -> None:
    """nexmix's own catalogue already lacks the tracks when the playlist is looked at: the server is asked all the
    same, and the entries say they are missing."""
    fake, server = jelly
    tracks = tracks_for(fake, server, size)
    playlist_id = synced(client, fake, server, f"Katalog zuerst {size}", tracks)
    fake.library_removed()
    partial(server)
    restart()
    sync(look=True)
    assert keys(server, local_tracks(playlist_id)) == tracks
    assert {item.plex_hint for item in local_items(playlist_id)} == {"missing"}
    fake.library_back()
    partial(server)
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == tracks


@pytest.mark.parametrize("size", [1, 4, 5])
def test_a_small_playlist_emptied_on_purpose_is_taken(client: TestClient, jelly: tuple[FakeJellyfin, int],
                                                      size: int) -> None:
    fake, server = jelly
    tracks = tracks_for(fake, server, size)
    playlist_id = synced(client, fake, server, f"Leer {size}", tracks)
    fake.list_remove(list_key(playlist_id), tracks)
    sync(look=True)
    assert local_tracks(playlist_id) == []
    partial(server)
    sync(look=True)
    assert fake.tracks_of(list_key(playlist_id)) == []


def test_one_entry_taken_out_of_a_small_playlist_is_taken(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    tracks = ids(fake, *NAMES[:2])
    playlist_id = synced(client, fake, server, "Zwei", tracks)
    asked = len(existence_asks(fake))
    fake.list_remove(list_key(playlist_id), tracks[:1])
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == tracks[1:]
    # Asked once, for that one track.
    questions = existence_asks(fake)[asked:]
    assert len(questions) == 1 and questions[0]["Ids"] == tracks[0]


def test_the_question_failing_takes_nothing_until_it_is_answered(client: TestClient,
                                                                 jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    tracks = ids(fake, *NAMES[:3])
    playlist_id = synced(client, fake, server, "Frage", tracks)
    fake.list_remove(list_key(playlist_id), tracks[:2])
    original = fake._handle

    def handle(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if request.url.path == "/Items" and "Ids" in params and "userId" not in params:
            return httpx.Response(503, text="Jellyfin server is loading. Please try again shortly.")
        return original(request)

    fake._handle = handle  # type: ignore[method-assign]
    sync(look=True)
    assert keys(server, local_tracks(playlist_id)) == tracks
    assert all(not item.plex_hint for item in local_items(playlist_id))
    assert mirror(playlist_id).state == server_sync.WAITING
    fake._handle = original  # type: ignore[method-assign]
    sync(look=True)
    assert both_equal(fake, server, playlist_id) == tracks[2:]


def test_the_question_goes_in_parts_of_two_hundred(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    tracks = tracks_for(fake, server, 450)
    playlist_id = synced(client, fake, server, "Viele", tracks)
    asked = len(existence_asks(fake))
    fake.library_removed()
    sync(look=True)
    questions = existence_asks(fake)[asked:]
    assert [len(one["Ids"].split(",")) for one in questions] == [200, 200, 50]
    assert len(local_tracks(playlist_id)) == 450


# --- Plex ------------------------------------------------------------------------------------------------------------


@pytest.fixture
def plex(client: TestClient, operator: Account, fake_plex: FakePlex) -> Iterator[FakePlex]:
    from .plex_world import connect as plex_connect

    plex_connect(client, fake_plex)
    server_sync.forget()
    yield fake_plex


def test_plex_a_track_gone_while_nexmix_was_away_keeps_its_entry_in_a_small_playlist(client: TestClient,
                                                                                     plex: FakePlex) -> None:
    from .fakeplex.library import TRACK, Item
    from .plex_world import both_equal as plex_equal
    from .plex_world import sync as plex_sync
    from .test_plex_sync import synced as plex_synced

    playlist_id, key = plex_synced(client, plex, "Klein Plex", [3001])
    kept = plex.items[3001]
    plex.remove(3001)
    plex.lists.remove_track(key, 3001)
    plex.tick()
    restart()
    plex_sync(plex, look_at_plex=True)
    assert local_tracks(playlist_id) == [3001]
    assert [item.plex_hint for item in local_items(playlist_id)] == ["missing"]
    plex.add(Item(3001, TRACK, kept.title, kept.parent, duration=kept.duration))
    partial()
    plex_sync(plex, look_at_plex=True)
    assert plex_equal(plex, playlist_id) == [3001]


def test_plex_the_question_failing_takes_nothing(client: TestClient, plex: FakePlex) -> None:
    from .plex_world import both_equal as plex_equal
    from .plex_world import sync as plex_sync
    from .test_plex_sync import synced as plex_synced

    playlist_id, key = plex_synced(client, plex, "Frage Plex", [3001, 3003, 3013])
    plex.lists.remove_track(key, 3001)
    plex.tick()
    original = plex._media

    def media(request: httpx.Request, server: object, path: str, params: dict[str, str]) -> httpx.Response:
        if request.method == "GET" and path == "/library/metadata/3001":
            return httpx.Response(503, text="busy")
        return original(request, server, path, params)  # type: ignore[arg-type]

    plex._media = media  # type: ignore[method-assign]
    plex_sync(plex, look_at_plex=True)
    assert local_tracks(playlist_id) == [3001, 3003, 3013]
    assert all(not item.plex_hint for item in local_items(playlist_id))
    plex._media = original  # type: ignore[method-assign]
    with SessionLocal() as db:
        server_sync.request_all(db)
    plex_sync(plex, look_at_plex=True)
    assert plex_equal(plex, playlist_id) == [3003, 3013]


def test_plex_asks_in_small_parts(client: TestClient, plex: FakePlex) -> None:
    from .plex_world import sync as plex_sync
    from .test_plex_sync import synced as plex_synced

    tracks = [3001, 3003, 3013, 3020, 3026, 3027]
    playlist_id, key = plex_synced(client, plex, "Teile", tracks)
    for track in tracks[:4]:
        plex.lists.remove_track(key, track)
    plex.tick()
    before = len(plex.library_calls())
    plex_sync(plex, look_at_plex=True)
    asked = [path for method, path, params in plex.library_calls()[before:]
             if method == "GET" and path.startswith("/library/metadata/") and not path.endswith("/items")]
    assert asked and all(len(path.rsplit("/", 1)[1].split(",")) <= 50 for path in asked)
    # Only the path: no parameter beyond what every request carries.
    assert all(not params.get("format") for method, path, params in plex.library_calls()[before:])
    assert local_tracks(playlist_id) == tracks[4:]
