"""No writes while nothing changes, with Music Assistant as source and as mirror beside Plex and Jellyfin,
three servers at one playlist, playlists made in Music Assistant taken in, many rounds."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Account
from app.services import sync as server_sync

from .fakejellyfin import FakeJellyfin
from .fakema import FILES, FakeMA
from .fakeplex import FakePlex
from .ma_world import connect as connect_ma
from .ma_world import events, numbers
from .ma_world import make as make_ma
from .mirror_world import connect_both, jelly_titles, mirror_key, plex_numbers, plex_titles
from .plex_world import make, sync

MA_WRITES = {"music/playlists/create_playlist", "music/playlists/add_playlist_tracks",
             "music/playlists/remove_playlist_tracks", "music/library/remove_item", "music/playlists/update",
             "music/playlists/remove", "tasks/cancel"}


def _ma_writes(fake: FakeMA) -> int:
    return sum(1 for command, _args in fake.calls if command in MA_WRITES)


def test_many_quiet_rounds_write_nothing_anywhere(client: TestClient, operator: Account, fake_plex: FakePlex,
                                                   fake_jellyfin: FakeJellyfin, fake_ma: FakeMA) -> None:
    _plex, jelly, _twins = connect_both(client, fake_plex, fake_jellyfin)
    ma = connect_ma(client, fake_ma)
    # A playlist of Plex mirrored to Music Assistant and Jellyfin (three servers at one playlist).
    three = make(client, "Drei", plex_numbers([3001, 3003, 3013, 3017]))
    sync(fake_plex)
    for server in (ma, jelly):
        assert client.post(f"/api/v1/playlists/{three['id']}/mirrors", json={"server": server}).status_code == 201
        sync(fake_plex)
    # A playlist of Music Assistant mirrored to Jellyfin.
    tracks = numbers(ma, [fake_ma.track_by_name(name).id for name in ("Northern Lights", "Fever Glass", "Glasshouse")])
    source = make_ma(client, ma, "Quelle MA", tracks)
    sync(fake_plex)
    assert client.post(f"/api/v1/playlists/{source['id']}/mirrors", json={"server": jelly}).status_code == 201
    sync(fake_plex)
    # A playlist made in Music Assistant, with an entry outside the chosen providers, taken in.
    own = sorted(one.id for one in fake_ma.tracks.values() if one.mappings[0][0] == FILES)[:3]
    other = next(one.id for one in fake_ma.tracks.values() if one.mappings[0][0] != FILES)
    fake_ma.ui_create("Aus MA", [*own, other])
    events(ma, fake_ma)
    sync(fake_plex, look_at_plex=True)
    sync(fake_plex, look_at_plex=True)
    events(ma, fake_ma)
    sync(fake_plex, look_at_plex=True)
    assert plex_titles(fake_plex, three["id"]) and jelly_titles(fake_jellyfin, source["id"], jelly)
    assert len(fake_ma.titles(int(mirror_key(three["id"], ma)))) == 4
    ma_before, plex_before = _ma_writes(fake_ma), len(fake_plex.lists.writes)
    jelly_before = len(fake_jellyfin.list_writes)
    for _round in range(30):
        with SessionLocal() as db:
            server_sync.request_all(db, ma)
            server_sync.request_all(db, jelly)
            server_sync.request_all(db, 1)
        events(ma, fake_ma)
        sync(fake_plex, look_at_plex=True)
    assert (_ma_writes(fake_ma) - ma_before, len(fake_plex.lists.writes) - plex_before,
            len(fake_jellyfin.list_writes) - jelly_before) == (0, 0, 0)
