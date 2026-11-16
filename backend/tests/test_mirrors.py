"""Mirrors against the stand-in Plex and the stand-in Jellyfin with the same made-up music (``mirror_world``): the
preview, a mirror made and kept in step both ways, what only one side has, a mirror deleted on its server, the source
deleted, a server away, and the entries that have no track on the mirror's server, which never count as taken out
there."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import Account
from app.services import sync as server_sync

from .fakejellyfin import FakeJellyfin
from .fakeplex import FakePlex
from .mirror_world import (
    connect_both,
    copy_row,
    detail,
    extras,
    jelly_numbers,
    jelly_titles,
    jelly_tracks_of,
    local_titles,
    mapping,
    mirror_key,
    plex_numbers,
    plex_titles,
)
from .plex_world import make, plex_key, sync

#: Plex's tracks of the playlist: "Northern Lights", "Glasshouse", "Copper Sky (feat. Dax Orlo)", "Fever Glass",
#: "Paper Planes" (Juno Reyes), and "Rafters"... no: "Op. 3 (No. 2)", which Jellyfin lacks.
PLAYLIST = [3001, 3003, 3013, 3017, 3026, 3021]
LEFT_OUT = frozenset({3021})


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex,
          fake_jellyfin: FakeJellyfin) -> tuple[FakePlex, FakeJellyfin, int, dict[int, str]]:
    _plex, jelly, twins = connect_both(client, fake_plex, fake_jellyfin, LEFT_OUT)
    return fake_plex, fake_jellyfin, jelly, twins


def mirrored(client: TestClient, world: tuple[FakePlex, FakeJellyfin, int, dict[int, str]],
             tracks: list[int] | None = None) -> int:
    fake_plex, _fake, jelly, _twins = world
    made = make(client, "Sommerfest", plex_numbers(tracks or PLAYLIST))
    sync(fake_plex)
    answer = client.post(f"/api/v1/playlists/{made['id']}/mirrors", json={"server": jelly})
    assert answer.status_code == 201, answer.text
    sync(fake_plex)
    return int(made["id"])


def test_the_preview_counts_and_names_what_is_not_found(client: TestClient, world: tuple) -> None:
    _plex, _fake, jelly, _twins = world
    made = make(client, "Sommerfest", plex_numbers(PLAYLIST))
    answer = client.post(f"/api/v1/playlists/{made['id']}/mirrors/preview", json={"server": jelly})
    assert answer.status_code == 200, answer.text
    found = answer.json()
    assert (found["entries"], found["found"], found["unsure"], found["missing"]) == (6, 5, 0, 1)
    assert [(row["title"], row["how"], row["found"]) for row in found["rows"]] == [("Op. 3 (No. 2)", "none", None)]
    # Nothing written by a preview: no copy on Jellyfin, nothing there.
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import ServerPlaylist

    with SessionLocal() as db:
        assert db.scalars(select(ServerPlaylist.server_id).where(ServerPlaylist.playlist_id == made["id"])).all() == [1]
    assert world[1].lists == {}
    # A choice by hand counts as found and drops the row.
    other = jelly_numbers(jelly, [next(key for key, one in world[1].tracks.items() if one.name == "Eisblume")])[0]
    chosen = client.post(f"/api/v1/playlists/{made['id']}/mirrors/preview",
                         json={"server": jelly, "choices": {str(plex_numbers([3021])[0]): other}}).json()
    assert (chosen["found"], chosen["rows"]) == (6, [])
    left = client.post(f"/api/v1/playlists/{made['id']}/mirrors/preview",
                       json={"server": jelly, "choices": {str(plex_numbers([3021])[0]): None}}).json()
    assert (left["found"], [row["how"] for row in left["rows"]]) == (5, ["skip"])
    # The source itself is no target.
    assert client.post(f"/api/v1/playlists/{made['id']}/mirrors/preview", json={"server": 1}).status_code == 409


def test_a_mirror_is_made_and_kept_in_step_both_ways(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    # The mirror holds what was found, in order; the entry Jellyfin lacks is in nexmix and Plex only.
    assert jelly_titles(fake, playlist_id, jelly) == ["Northern Lights", "Glasshouse", "Copper Sky (feat. Dax Orlo)",
                                                      "Fever Glass", "Paper Planes"]
    assert plex_titles(fake_plex, playlist_id)[-1] == "Op. 3 (No. 2)"
    view = detail(client, playlist_id)
    assert [(one["server"], one["state"], one["absent"]) for one in view["mirrors"]] == [(jelly, "synced", 1)]
    absent = [item["title"] for item in view["items"] if item["absent"] == [jelly]]
    assert absent == ["Op. 3 (No. 2)"]

    # nexmix → Plex and Jellyfin.
    add = plex_numbers([3008])
    assert client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": add}).status_code == 200
    sync(fake_plex)
    assert plex_titles(fake_plex, playlist_id)[-1] == "Glasshouse" and jelly_titles(fake, playlist_id, jelly)[-1] == \
        "Glasshouse"

    # Jellyfin → nexmix → Plex: a track put in there that Plex has comes in "via Jellyfin".
    fake.list_add(key, [twins[3014]])
    sync(fake_plex, look_at_plex=True)
    view = detail(client, playlist_id)
    assert view["items"][-1]["title"] == "Slow Tide feat. Dax Orlo"
    assert view["items"][-1]["added_by_name"] == "Jellyfin"
    assert plex_titles(fake_plex, playlist_id)[-1] == "Slow Tide feat. Dax Orlo"

    # Moved in Jellyfin: nexmix and Plex follow; the entry without a track there keeps its place among the others.
    fake.list_move(key, twins[3026], 0)
    sync(fake_plex, look_at_plex=True)
    assert local_titles(playlist_id)[0] == "Paper Planes"
    assert plex_titles(fake_plex, playlist_id)[0] == "Paper Planes"
    assert "Op. 3 (No. 2)" in local_titles(playlist_id)

    # Taken out in Jellyfin: out in nexmix and in Plex.
    fake.list_remove(key, [twins[3017]])
    sync(fake_plex, look_at_plex=True)
    assert "Fever Glass" not in local_titles(playlist_id) and "Fever Glass" not in plex_titles(fake_plex, playlist_id)

    # Plex → nexmix → Jellyfin.
    fake_plex.lists.add(plex_key(playlist_id), [3019])
    sync(fake_plex, look_at_plex=True)
    assert local_titles(playlist_id)[-1] == "Copper Sky" and jelly_titles(fake, playlist_id, jelly)[-1] == "Copper Sky"

    # In the end all three hold the same, but the entry Jellyfin has no track for.
    assert plex_titles(fake_plex, playlist_id) == local_titles(playlist_id)
    assert jelly_titles(fake, playlist_id, jelly) == [title for title in local_titles(playlist_id)
                                                      if title != "Op. 3 (No. 2)"]
    # Nothing left to do, and nothing written once more.
    writes = len(fake.list_writes)
    sync(fake_plex, look_at_plex=True)
    assert len(fake.list_writes) == writes


def test_a_track_only_in_the_mirror_is_shown_never_taken_out_and_taken_over_or_dropped(
        client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    own = next(ident for ident, one in fake.tracks.items() if one.name == "Glasfluss" and
               fake.albums[one.album].name == "Mondgarten")
    fake.list_add(key, [own])
    sync(fake_plex, look_at_plex=True)
    found = extras(playlist_id, jelly)
    assert [(one.title, one.discard) for one in found] == [("Glasfluss", False)]
    assert "Glasfluss" not in local_titles(playlist_id) and "Glasfluss" not in plex_titles(fake_plex, playlist_id)
    # It stays in Jellyfin, through further syncs and changes.
    assert client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": plex_numbers([3008])}).status_code \
        == 200
    sync(fake_plex, look_at_plex=True)
    assert own in fake.tracks_of(key)
    view = client.get(f"/api/v1/playlists/{playlist_id}/mirrors").json()["mirrors"][0]
    assert [one["title"] for one in view["extras"]] == ["Glasfluss"]
    # Taken over as a track of Plex: an entry, standing for the Jellyfin track, now everywhere.
    taken = client.post(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}/extras/{found[0].id}/take",
                        json={"track": plex_numbers([3020])[0]})
    assert taken.status_code == 200, taken.text
    sync(fake_plex, look_at_plex=True)
    assert local_titles(playlist_id)[-1] == "Northern Lights"
    assert plex_titles(fake_plex, playlist_id)[-1] == "Northern Lights"
    assert jelly_tracks_of(fake, playlist_id, jelly).count(own) == 1
    assert extras(playlist_id, jelly) == []
    # Another one only there, dropped: taken out of Jellyfin, nothing else changes.
    second = next(ident for ident, one in fake.tracks.items() if one.name == "Eisblume")
    fake.list_add(key, [second])
    sync(fake_plex, look_at_plex=True)
    extra = extras(playlist_id, jelly)[0]
    before = local_titles(playlist_id)
    assert client.delete(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}/extras/{extra.id}").status_code == 204
    sync(fake_plex, look_at_plex=True)
    assert second not in fake.tracks_of(key) and extras(playlist_id, jelly) == []
    assert local_titles(playlist_id) == before


def test_an_entry_without_a_track_on_the_mirror_never_counts_as_taken_out_there(
        client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    for _turn in range(3):
        fake.list_add(mirror_key(playlist_id, jelly), [])
        sync(fake_plex, look_at_plex=True)
    assert "Op. 3 (No. 2)" in local_titles(playlist_id) and "Op. 3 (No. 2)" in plex_titles(fake_plex, playlist_id)
    # Chosen by hand later: then it goes to the mirror.
    entry = next(item["id"] for item in detail(client, playlist_id)["items"] if item["title"] == "Op. 3 (No. 2)")
    other = jelly_numbers(jelly, [next(key for key, one in fake.tracks.items() if one.name == "Eisblume")])[0]
    answer = client.put(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}/entries/{entry}", json={"track": other})
    assert answer.status_code == 200, answer.text
    sync(fake_plex)
    assert jelly_titles(fake, playlist_id, jelly)[-1] == "Eisblume"
    assert mapping(playlist_id, jelly)[entry] == (other, "hand", "")
    # And left out by hand: out of the mirror, in nexmix and Plex still.
    assert client.put(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}/entries/{entry}",
                      json={"track": None}).status_code == 200
    sync(fake_plex)
    assert "Eisblume" not in jelly_titles(fake, playlist_id, jelly)
    assert "Op. 3 (No. 2)" in local_titles(playlist_id)


def test_a_mirror_deleted_on_its_server_is_gone_there_only(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    before = local_titles(playlist_id)
    fake.list_delete(mirror_key(playlist_id, jelly))
    sync(fake_plex, look_at_plex=True)
    view = detail(client, playlist_id)
    assert view["deleted_at"] is None and local_titles(playlist_id) == before
    assert plex_titles(fake_plex, playlist_id) == before
    assert [(one["state"], one["absent"]) for one in view["mirrors"]] == [("gone", 1)]
    # Changes go on to Plex; the mirror stays gone until it is made anew.
    assert client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": plex_numbers([3008])}).status_code \
        == 200
    sync(fake_plex, look_at_plex=True)
    assert plex_titles(fake_plex, playlist_id)[-1] == "Glasshouse"
    assert len(fake.lists) == 0
    assert client.post(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}/remake").status_code == 200
    sync(fake_plex)
    assert jelly_titles(fake, playlist_id, jelly) == [title for title in local_titles(playlist_id)
                                                      if title != "Op. 3 (No. 2)"]


def test_the_source_deleted_puts_the_playlist_in_the_trash_and_the_mirror_rests(
        client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    there = fake.tracks_of(key)
    fake_plex.lists.delete(plex_key(playlist_id))
    sync(fake_plex, look_at_plex=True)
    assert client.get(f"/api/v1/playlists/{playlist_id}").json()["deleted_at"] is not None
    # The mirror rests in Jellyfin with what it held; changes made there meanwhile wait.
    assert fake.tracks_of(key) == there
    restored = client.post(f"/api/v1/playlists/{playlist_id}/restore")
    assert restored.status_code == 200, restored.text
    sync(fake_plex, look_at_plex=True)
    assert plex_titles(fake_plex, playlist_id) == local_titles(playlist_id)
    assert jelly_titles(fake, playlist_id, jelly) == [title for title in local_titles(playlist_id)
                                                      if title != "Op. 3 (No. 2)"]
    assert copy_row(playlist_id, jelly).ext_key == key


def test_a_mirror_server_away_catches_up_and_the_source_does_not_wait(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    fake.down.add("jellyfin.example.com")
    assert client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": plex_numbers([3008])}).status_code \
        == 200
    sync(fake_plex)
    assert plex_titles(fake_plex, playlist_id)[-1] == "Glasshouse"
    mirror = detail(client, playlist_id)["mirrors"][0]
    assert mirror["state"] == "waiting" and mirror["error"]
    fake.down.clear()
    server_sync.request_all(*_db_and(jelly))
    sync(fake_plex, look_at_plex=True)
    assert jelly_titles(fake, playlist_id, jelly)[-1] == "Glasshouse"
    assert detail(client, playlist_id)["mirrors"][0]["state"] == "synced"


def _db_and(server: int) -> tuple:
    from app.db import SessionLocal

    return (SessionLocal(), server)


def test_a_track_put_into_the_mirror_found_only_unsurely_on_the_source_stays_only_there(
        client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    # Jellyfin has a song of the same title by somebody else: on Plex only its title would find it (unsure), and
    # that track of Plex is not in the playlist yet.
    album = next(ident for ident, one in fake.albums.items() if one.name == "Hafenlieder")
    other = fake.add(album, "Night Drive (Part 1)", 200)
    before = local_titles(playlist_id)
    fake.list_add(mirror_key(playlist_id, jelly), [other])
    sync(fake_plex, look_at_plex=True)
    assert local_titles(playlist_id) == before, "an unsure match never becomes an entry by itself"
    assert [(one.title, one.artist) for one in extras(playlist_id, jelly)] == [("Night Drive (Part 1)", "Orvenna")]
    assert other in fake.tracks_of(mirror_key(playlist_id, jelly))


def test_a_long_new_order_written_in_parts_keeps_the_tracks_only_in_the_mirror(
        client: TestClient, world: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    own = next(ident for ident, one in fake.tracks.items() if one.name == "Eisblume")
    fake.list_add(mirror_key(playlist_id, jelly), [own])
    sync(fake_plex, look_at_plex=True)
    assert [one.title for one in extras(playlist_id, jelly)] == ["Eisblume"]
    # Every new order goes in parts of one move: the write stays open between them.
    monkeypatch.setattr(server_sync, "MOVE_PART", 1)
    view = detail(client, playlist_id)
    entries = [item["id"] for item in view["items"]]
    for entry in entries[1:]:
        assert client.post(f"/api/v1/playlists/{playlist_id}/items/move",
                           json={"entry": entry, "after": None}).status_code == 200
    sync(fake_plex, look_at_plex=True)
    sync(fake_plex, look_at_plex=True)
    assert own in fake.tracks_of(mirror_key(playlist_id, jelly)), "the track only in the mirror stays there"
    assert jelly_titles(fake, playlist_id, jelly)[:5] == [title for title in local_titles(playlist_id)
                                                          if title != "Op. 3 (No. 2)"]
    assert [one.title for one in extras(playlist_id, jelly)] == ["Eisblume"]


def test_a_playlist_emptied_from_the_trash_takes_its_mirror_from_its_server(client: TestClient,
                                                                             world: tuple) -> None:
    from datetime import timedelta

    from app import clock
    from app.db import SessionLocal
    from app.services import playlists as playlist_service

    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    assert client.delete(f"/api/v1/playlists/{playlist_id}").status_code == 200
    sync(fake_plex)
    # In the trash: the mirror rests on its server.
    assert key in fake.lists
    with SessionLocal() as db:
        assert playlist_service.purge(db, clock.now() + timedelta(days=31)) == 1
    sync(fake_plex)
    assert key not in fake.lists


def test_the_card_of_each_server_counts_its_own_playlists_and_the_mirrors_there_apart(
        client: TestClient, world: tuple) -> None:
    """Plex holds two playlists of its own, Jellyfin one of its own and the mirror of one of Plex's: each card says so
    ("2 sources", "1 source, 1 mirror"), the states count both, and a playlist in the trash counts nowhere."""
    from .jellyfin_world import make as make_on_jellyfin

    fake_plex, _fake, jelly, twins = world
    playlist_id = mirrored(client, world)
    other = make(client, "Nur Plex", plex_numbers([3001]))
    make_on_jellyfin(client, jelly, "Nur Jellyfin", jelly_numbers(jelly, [next(iter(twins.values()))]))
    sync(fake_plex)

    def card(path: str) -> tuple[int, int, int, int]:
        found = client.get(path).json()
        return found["playlists"], found["sources"], found["mirrors"], found["synced"]

    assert card("/api/v1/plex/playlists") == (2, 2, 0, 2)
    assert card(f"/api/v1/jellyfin/{jelly}/playlists") == (2, 1, 1, 2)
    assert client.delete(f"/api/v1/playlists/{playlist_id}").status_code in (200, 204)
    assert client.delete(f"/api/v1/playlists/{other['id']}").status_code in (200, 204)
    assert card("/api/v1/plex/playlists")[:3] == (0, 0, 0)
    assert card(f"/api/v1/jellyfin/{jelly}/playlists")[:3] == (1, 1, 0)
