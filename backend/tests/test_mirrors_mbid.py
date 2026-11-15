"""The MusicBrainz id of a Plex track, asked for only where it helps: the catalogue run never asks Plex for the agents'
ids (``includeGuids`` would make a full run about half as long again), but a track a copy cannot find by its name is
looked for once more with its MusicBrainz id, asked of Plex for those tracks only, and found where Jellyfin has the
same id under another title on another album."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import Account
from app.services import mirrors

from .fakejellyfin import FakeJellyfin, Track
from .fakeplex import FakePlex
from .jellyfin_world import connect as connect_jellyfin
from .jellyfin_world import number_of
from .mirror_world import mapping, plex_numbers, twin
from .plex_world import connect as connect_plex
from .plex_world import make, sync

MB = "1b5f2c3d-4e6f-4a7b-8c9d-0e1f2a3b4c5d"


def test_a_track_found_only_by_its_musicbrainz_id_asked_of_plex(client: TestClient, operator: Account,
                                                                fake_plex: FakePlex,
                                                                fake_jellyfin: FakeJellyfin) -> None:
    mirrors.forget_asked()
    fake_plex.items[3021].mbid = MB
    connect_plex(client, fake_plex)
    twins = twin(fake_jellyfin, fake_plex, frozenset({3021}))
    # Jellyfin has the same recording under another title, on another album, with the MusicBrainz id of the track.
    album = next(iter(fake_jellyfin.albums))
    other = Track("/music/x/y/09 Opus drei.flac", "Opus drei (Studio)", album, ["Dax Orlo"], 210, 2010, [],
                  number=9, mbid_track=MB)
    fake_jellyfin.tracks[other.id] = other
    jelly = connect_jellyfin(client, fake_jellyfin)
    assert not any(params.get("includeGuids") for _method, _host, _path, params in fake_plex.calls), \
        "the catalogue run asks for no agents' ids"
    made = make(client, "Sommerfest", plex_numbers([3001, 3021]))
    sync(fake_plex)
    found = client.post(f"/api/v1/playlists/{made['id']}/mirrors/preview", json={"server": jelly}).json()
    assert (found["entries"], found["found"], found["unsure"], found["rows"]) == (2, 2, 0, [])
    asked = [params for _method, _host, path, params in fake_plex.calls if params.get("includeGuids") == "1"]
    assert asked and all(path.startswith("/library/metadata/") for _m, _h, path, params in fake_plex.calls
                         if params.get("includeGuids") == "1")
    # Only the track its name did not find was asked for.
    assert [path for _m, _h, path, params in fake_plex.calls if params.get("includeGuids") == "1"] == [
        "/library/metadata/3021"]
    assert client.post(f"/api/v1/playlists/{made['id']}/mirrors", json={"server": jelly}).status_code == 201
    sync(fake_plex)
    hows = sorted(how for _track, how, _hint in mapping(made["id"], jelly).values())
    assert hows == ["album", "mbid"]
    assert {track for track, _how, _hint in mapping(made["id"], jelly).values()} == {
        number_of(jelly, twins[3001]), number_of(jelly, other.id)}
    # Asked once: the copy after the preview takes it from what was asked then.
    assert len([1 for _m, _h, _p, params in fake_plex.calls if params.get("includeGuids") == "1"]) == 1


def test_plex_away_leaves_the_steps_by_name(client: TestClient, operator: Account, fake_plex: FakePlex,
                                            fake_jellyfin: FakeJellyfin) -> None:
    mirrors.forget_asked()
    fake_plex.items[3021].mbid = MB
    connect_plex(client, fake_plex)
    twin(fake_jellyfin, fake_plex, frozenset({3021}))
    jelly = connect_jellyfin(client, fake_jellyfin)
    made = make(client, "Sommerfest", plex_numbers([3001, 3021]))
    sync(fake_plex)
    fake_plex.down.add("plex-local.example.com")
    fake_plex.down.add("127.0.0.1")
    found = client.post(f"/api/v1/playlists/{made['id']}/mirrors/preview", json={"server": jelly})
    assert found.status_code == 200, found.text
    assert (found.json()["found"], found.json()["missing"]) == (1, 1)
