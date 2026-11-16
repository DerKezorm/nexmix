"""Music Assistant beside Plex and Jellyfin: a mirror of a Plex playlist and of a Jellyfin playlist there, a playlist
of Music Assistant mirrored to Jellyfin, moving, "take all over", and the MusicBrainz ids that meet (Music Assistant's
recording id meets Jellyfin's, never Plex's track id)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import Account
from app.services import match

from .fakejellyfin import FakeJellyfin
from .fakema import FakeMA
from .fakeplex import FakePlex
from .ma_world import connect as connect_ma
from .ma_world import events, numbers
from .mirror_world import connect_both, detail, jelly_titles, mirror_key, plex_numbers, plex_titles
from .plex_world import connect as connect_plex
from .plex_world import make, sync

#: Plex's tracks: "Northern Lights", "Glasshouse", "Copper Sky (feat. Dax Orlo)", "Fever Glass".
PLAYLIST = [3001, 3003, 3013, 3017]


def _ma_titles(fake: FakeMA, playlist: int, server: int) -> list[str]:
    return fake.titles(int(mirror_key(playlist, server)))


def test_a_plex_playlist_mirrored_to_music_assistant_both_ways(client: TestClient, operator: Account,
                                                               fake_plex: FakePlex, fake_ma: FakeMA) -> None:
    connect_plex(client, fake_plex)
    ma = connect_ma(client, fake_ma)
    made = make(client, "Sommerfest", plex_numbers(PLAYLIST))
    sync(fake_plex)
    preview = client.post(f"/api/v1/playlists/{made['id']}/mirrors/preview", json={"server": ma}).json()
    assert (preview["entries"], preview["found"], preview["unsure"]) == (4, 4, 0)
    assert client.post(f"/api/v1/playlists/{made['id']}/mirrors", json={"server": ma}).status_code == 201
    sync(fake_plex)
    assert _ma_titles(fake_ma, made["id"], ma) == ["Northern Lights", "Glasshouse", "Copper Sky", "Fever Glass"]
    # Added in Music Assistant: found on Plex, an entry in nexmix, there in Plex too.
    key = int(mirror_key(made["id"], ma))
    fake_ma.ui_add(key, fake_ma.track_by_name("Slow Tide (Acoustic)").id)
    events(ma, fake_ma)
    sync(fake_plex)
    assert plex_titles(fake_plex, made["id"])[-1] == "Slow Tide (Acoustic)"
    # Taken out in nexmix: out in Music Assistant (by place) and in Plex.
    items = detail(client, made["id"])["items"]
    assert client.post(f"/api/v1/playlists/{made['id']}/items/remove",
                       json={"entries": [items[1]["id"]]}).status_code == 200
    sync(fake_plex)
    assert _ma_titles(fake_ma, made["id"], ma) == ["Northern Lights", "Copper Sky", "Fever Glass",
                                                    "Slow Tide (Acoustic)"]
    assert "Glasshouse" not in plex_titles(fake_plex, made["id"])
    # The badge names Music Assistant.
    mirrors = detail(client, made["id"])["mirrors"]
    assert any(one["kind"] == "music_assistant" and one["state"] == "synced" for one in mirrors)


def test_a_jellyfin_playlist_mirrored_to_music_assistant(client: TestClient, operator: Account, fake_plex: FakePlex,
                                                         fake_jellyfin: FakeJellyfin, fake_ma: FakeMA) -> None:
    _plex, jelly, _twins = connect_both(client, fake_plex, fake_jellyfin)
    ma = connect_ma(client, fake_ma)
    from .jellyfin_world import make as make_jelly
    from .mirror_world import jelly_numbers

    ids = [next(key for key, one in fake_jellyfin.tracks.items() if one.name == name)
           for name in ("Northern Lights", "Fever Glass", "Glasshouse")]
    made = make_jelly(client, jelly, "Winterabend", jelly_numbers(jelly, ids[:3]))
    sync()
    assert client.post(f"/api/v1/playlists/{made['id']}/mirrors", json={"server": ma}).status_code == 201
    sync()
    assert sorted(_ma_titles(fake_ma, made["id"], ma)) == sorted(jelly_titles(fake_jellyfin, made["id"], jelly))


def test_a_playlist_of_music_assistant_mirrored_to_jellyfin(client: TestClient, operator: Account,
                                                            fake_plex: FakePlex, fake_jellyfin: FakeJellyfin,
                                                            fake_ma: FakeMA) -> None:
    _plex, jelly, _twins = connect_both(client, fake_plex, fake_jellyfin)
    ma = connect_ma(client, fake_ma)
    from .ma_world import make as make_ma

    tracks = numbers(ma, [fake_ma.track_by_name(name).id for name in ("Northern Lights", "Fever Glass")])
    made = make_ma(client, ma, "Quelle MA", tracks)
    sync()
    answer = client.post(f"/api/v1/playlists/{made['id']}/mirrors", json={"server": jelly})
    assert answer.status_code == 201, answer.text
    sync()
    assert jelly_titles(fake_jellyfin, made["id"], jelly) == ["Northern Lights", "Fever Glass"]
    # Added in Jellyfin: found in Music Assistant, there too.
    key = mirror_key(made["id"], jelly)
    glass = next(ident for ident, one in fake_jellyfin.tracks.items() if one.name == "Glasshouse")
    fake_jellyfin.list_add(key, [glass])
    sync(look_at_plex=True)
    sync(look_at_plex=True)
    assert fake_ma.titles(int(mirror_key(made["id"], ma)))[-1] == "Glasshouse"


def test_moved_from_plex_to_music_assistant(client: TestClient, operator: Account, fake_plex: FakePlex,
                                            fake_ma: FakeMA) -> None:
    connect_plex(client, fake_plex)
    ma = connect_ma(client, fake_ma)
    made = make(client, "Umzug", plex_numbers(PLAYLIST))
    sync(fake_plex)
    moved = client.post(f"/api/v1/playlists/{made['id']}/move", json={"server": ma, "old": "keep"})
    assert moved.status_code in (200, 201, 202), moved.text
    sync(fake_plex)
    from .plex_world import playlist_row

    assert playlist_row(made["id"]).source_server_id == ma
    assert _ma_titles(fake_ma, made["id"], ma) == ["Northern Lights", "Glasshouse", "Copper Sky", "Fever Glass"]


def test_a_recording_id_meets_a_recording_id_never_a_track_id() -> None:
    wanted = match.Wanted(title="Anders", artist="Niemand", album="Nirgends", duration=100_000,
                          recording="0f1e2d3c-4b5a-4968-8776-655443322110")
    same = match.Candidate(id=1, title="Ganz anders", album="Woanders", album_artist="Wer", duration=300_000,
                           recording="0f1e2d3c-4b5a-4968-8776-655443322110")
    track_id = match.Candidate(id=2, title="Ganz anders", album="Woanders", album_artist="Wer",
                               mbid="0f1e2d3c-4b5a-4968-8776-655443322110")
    found = match.Finder([track_id, same]).find(wanted)
    assert found is not None and found.track.id == 1 and found.how == match.MBID
    assert match.Finder([track_id]).find(wanted) is None
    plex_side = match.Wanted(title="Anders", artist="Niemand", album="Nirgends",
                             mbid="0f1e2d3c-4b5a-4968-8776-655443322110")
    assert match.Finder([same]).find(plex_side) is None


def test_every_playlist_of_plex_taken_over_by_music_assistant(client: TestClient, operator: Account,
                                                              fake_plex: FakePlex, fake_ma: FakeMA) -> None:
    connect_plex(client, fake_plex)
    ma = connect_ma(client, fake_ma)
    first = make(client, "Eins", plex_numbers(PLAYLIST[:2]))
    second = make(client, "Zwei", plex_numbers(PLAYLIST[2:]))
    sync(fake_plex)
    from .mirror_world import look

    found = look(client, ma, {"mode": "move"})
    assert found
    started = client.post(f"/api/v1/servers/{ma}/transfer", json={"mode": "move", "old": "keep"})
    assert started.status_code == 202, started.text
    for _round in range(6):
        sync(fake_plex)
    from .plex_world import playlist_row

    assert playlist_row(first["id"]).source_server_id == ma and playlist_row(second["id"]).source_server_id == ma
    assert _ma_titles(fake_ma, first["id"], ma) == ["Northern Lights", "Glasshouse"]
    assert _ma_titles(fake_ma, second["id"], ma) == ["Copper Sky", "Fever Glass"]
    # Plex stays as a mirror of each, in step.
    assert plex_titles(fake_plex, first["id"]) == ["Northern Lights", "Glasshouse"]
