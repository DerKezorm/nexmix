"""The rules of the sync of Plex and Jellyfin, with Music Assistant: an error is never a deletion, a track gone
from the library is "missing" (never taken out), a mirror emptied there is asked about, a source emptied there on
purpose is taken over, the trash and back, letting go of the server."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Account
from app.services import sync as server_sync

from .fakema import FILES, FakeMA
from .fakeplex import FakePlex
from .ma_world import both_equal, connect, events, list_key, make, numbers, sync
from .mirror_world import detail, mirror_key, plex_numbers, plex_titles
from .plex_world import connect as connect_plex
from .plex_world import local_tracks, mirror, playlist_row
from .plex_world import make as make_plex
from .plex_world import sync as plex_sync


def _setup(client: TestClient, fake: FakeMA, count: int = 6) -> tuple[int, int, list[int]]:
    server = connect(client, fake)
    chosen = sorted(one.id for one in fake.tracks.values() if one.mappings[0][0] == FILES)[:count]
    made = make(client, server, "Regeln", numbers(server, chosen))
    sync()
    return server, int(made["id"]), chosen


def _again(server: int, fake: FakeMA) -> None:
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    events(server, fake)
    sync(look=True)


def test_an_error_999_on_reading_takes_nothing_and_trashes_nothing(client: TestClient, operator: Account,
                                                                    fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    for command in ("music/playlists/playlist_tracks", "music/playlists/get", "music/playlists/library_items"):
        fake_ma.fail_once[command] = 999
        _again(server, fake_ma)
        assert playlist_row(playlist).deleted_at is None, command
        assert len(local_tracks(playlist)) == len(chosen), command
    _again(server, fake_ma)
    assert both_equal(fake_ma, server, playlist) == chosen


def test_a_track_gone_from_the_library_stays_in_nexmix_as_missing(client: TestClient, operator: Account,
                                                                  fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    gone = fake_ma.tracks.pop(chosen[2])
    _again(server, fake_ma)
    assert len(local_tracks(playlist)) == len(chosen), "a track gone from the library is no entry taken out"
    # Back in the library: the playlist there still holds it, nothing doubled.
    fake_ma.tracks[gone.id] = gone
    _again(server, fake_ma)
    assert both_equal(fake_ma, server, playlist) == chosen


def test_a_source_emptied_in_music_assistant_on_purpose_is_taken_over(client: TestClient, operator: Account,
                                                                     fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    for track in chosen:
        fake_ma.ui_remove(key, track)
    _again(server, fake_ma)
    assert local_tracks(playlist) == [] and fake_ma.entries(key) == []


def test_a_mirror_emptied_in_music_assistant_is_asked_about_and_plex_keeps_all(
        client: TestClient, operator: Account, fake_plex: FakePlex, fake_ma: FakeMA) -> None:
    connect_plex(client, fake_plex)
    ma = connect(client, fake_ma)
    made = make_plex(client, "Gespiegelt", plex_numbers([3001, 3003, 3013, 3017]))
    plex_sync(fake_plex)
    assert client.post(f"/api/v1/playlists/{made['id']}/mirrors", json={"server": ma}).status_code == 201
    plex_sync(fake_plex)
    key = int(mirror_key(made["id"], ma))
    for track in list(fake_ma.entries(key)):
        fake_ma.ui_remove(key, track)
    events(ma, fake_ma)
    plex_sync(fake_plex, look_at_plex=True)
    assert len(plex_titles(fake_plex, made["id"])) == 4
    assert len(local_tracks(made["id"])) == 4
    mirrors = detail(client, made["id"])["mirrors"]
    assert any(one.get("emptied") for one in mirrors), mirrors


def test_the_trash_and_back(client: TestClient, operator: Account, fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    assert client.delete(f"/api/v1/playlists/{playlist}").status_code in (200, 204)
    sync()
    # In the trash: the playlist rests in Music Assistant until it goes for good (or is gone there already).
    there_after_trash = key in fake_ma.playlists
    assert client.post(f"/api/v1/playlists/{playlist}/restore").status_code == 200
    _again(server, fake_ma)
    assert both_equal(fake_ma, server, playlist) == chosen
    mine = [one for one in fake_ma.playlists.values() if one.editable]
    assert len(mine) == 1, ([one.name for one in mine], there_after_trash)


def test_letting_go_while_it_is_a_source_is_refused_and_a_mirror_stays_there(
        client: TestClient, operator: Account, fake_plex: FakePlex, fake_ma: FakeMA) -> None:
    connect_plex(client, fake_plex)
    ma = connect(client, fake_ma)
    made = make(client, ma, "Quelle", numbers(ma, sorted(one.id for one in fake_ma.tracks.values()
                                                          if one.mappings[0][0] == FILES)[:2]))
    sync()
    assert client.delete(f"/api/v1/music-assistant/{ma}").status_code == 409
    assert client.delete(f"/api/v1/playlists/{made['id']}").status_code in (200, 204)
    client.delete(f"/api/v1/playlists/{made['id']}?for_good=true")
    mirrored = make_plex(client, "Spiegel", plex_numbers([3001, 3003]))
    plex_sync(fake_plex)
    assert client.post(f"/api/v1/playlists/{mirrored['id']}/mirrors", json={"server": ma}).status_code == 201
    plex_sync(fake_plex)
    key = int(mirror_key(mirrored["id"], ma))
    answer = client.delete(f"/api/v1/music-assistant/{ma}")
    assert answer.status_code in (204, 409), answer.text
    if answer.status_code == 204:
        plex_sync(fake_plex)
        assert key in fake_ma.playlists, "a mirror is forgotten, never deleted, when its server is let go of"
    assert mirror(mirrored["id"]).ext_key is not None
