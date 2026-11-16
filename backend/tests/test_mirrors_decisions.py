"""How mirrors decide where a person should, against the stand-in Plex and Jellyfin (``mirror_world``):

* **A mirror emptied on its server** never empties the playlist by itself: not in nexmix, not on the source, not on
  the other mirrors, also when several are emptied at once (the server's card says so). The playlist asks: "empty it
  here too" empties it everywhere, "keep" fills the mirror again. A source emptied on purpose is still taken over.
* **Unsure findings** are taken when copying a mirror, only offered when moving and taking all over: an entry found by
  an unsure step keeps its track (not on the new source) unless chosen.
* A track dropped from a mirror and moved there before it was taken out never becomes an entry."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import Account
from app.services import mirrors

from .fakejellyfin import FakeJellyfin
from .fakeplex import FakePlex
from .mirror_world import (
    connect_both,
    detail,
    extras,
    jelly_titles,
    local_titles,
    look,
    mirror_key,
    plex_numbers,
    plex_titles,
)
from .plex_world import make, plex_key, sync
from .test_mirrors_only_there import every_round, own_track

PLAYLIST = [3001, 3003, 3013, 3017, 3026, 3021]
LEFT_OUT = frozenset({3021})


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex,
          fake_jellyfin: FakeJellyfin) -> tuple[FakePlex, FakeJellyfin, int, dict[int, str]]:
    _plex, jelly, twins = connect_both(client, fake_plex, fake_jellyfin, LEFT_OUT)
    return fake_plex, fake_jellyfin, jelly, twins


def mirrored(client: TestClient, world: tuple, tracks: list[int] | None = None, name: str = "Sommerfest") -> int:
    fake_plex, _fake, jelly, _twins = world
    made = make(client, name, plex_numbers(tracks or PLAYLIST))
    sync(fake_plex)
    assert client.post(f"/api/v1/playlists/{made['id']}/mirrors", json={"server": jelly}).status_code == 201
    sync(fake_plex)
    return int(made["id"])


def emptied_of(client: TestClient, playlist_id: int) -> dict | None:
    return detail(client, playlist_id)["mirrors"][0]["emptied"]


# --- A mirror emptied on its server --------------------------------------------------------------------------------


def test_a_jellyfin_library_made_anew_empties_neither_nexmix_nor_plex(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    before = local_titles(playlist_id)
    writes = len(fake.list_writes)
    fake.library_made_anew()
    for _turn in range(3):
        every_round()
        sync(fake_plex, look_at_plex=True)
    assert local_titles(playlist_id) == before, "a mirror emptied in Jellyfin emptied the source"
    assert plex_titles(fake_plex, playlist_id) == before
    note = emptied_of(client, playlist_id)
    assert note is not None and note["count"] == 5
    # Nothing written to the emptied mirror while it waits for an answer.
    assert len(fake.list_writes) == writes and jelly_titles(fake, playlist_id, jelly) == []


def test_several_mirrors_emptied_at_once_empty_nothing_and_the_server_says_so(client: TestClient,
                                                                              world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    ids = [mirrored(client, world, PLAYLIST, f"P{number}") for number in range(3)]
    for one in ids:
        fake.list_remove(mirror_key(one, jelly), list(fake.tracks_of(mirror_key(one, jelly))))
    sync(fake_plex, look_at_plex=True)
    assert [len(local_titles(one)) for one in ids] == [6, 6, 6]
    assert [len(plex_titles(fake_plex, one)) for one in ids] == [6, 6, 6]
    card = client.get(f"/api/v1/jellyfin/{jelly}/playlists").json()
    assert card["emptied"] == 3


def test_empty_it_here_too_empties_it_everywhere(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    fake.list_remove(key, list(fake.tracks_of(key)))
    sync(fake_plex, look_at_plex=True)
    assert emptied_of(client, playlist_id) is not None
    answer = client.post(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}/emptied", json={"empty": True})
    assert answer.status_code == 200, answer.text
    assert answer.json()["mirrors"][0]["emptied"] is None
    sync(fake_plex, look_at_plex=True)
    assert local_titles(playlist_id) == [] and plex_titles(fake_plex, playlist_id) == []
    assert fake.tracks_of(key) == []
    # Asked twice: nothing to answer any more.
    again = client.post(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}/emptied", json={"empty": True})
    assert again.status_code == 404


def test_keep_fills_the_mirror_again(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    before = local_titles(playlist_id)
    fake.library_made_anew()
    sync(fake_plex, look_at_plex=True)
    assert client.post(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}/emptied",
                       json={"empty": False}).status_code == 200
    sync(fake_plex, look_at_plex=True)
    every_round()
    sync(fake_plex, look_at_plex=True)
    assert local_titles(playlist_id) == before and plex_titles(fake_plex, playlist_id) == before
    assert jelly_titles(fake, playlist_id, jelly) == [one for one in before if one != "Op. 3 (No. 2)"]
    assert emptied_of(client, playlist_id) is None
    raw = fake.tracks_of(key)
    assert len(raw) == len(set(raw))


def test_a_mirror_filled_again_there_goes_on_without_the_note(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    fake.list_remove(key, list(fake.tracks_of(key)))
    sync(fake_plex, look_at_plex=True)
    assert emptied_of(client, playlist_id) is not None
    fake.list_add(key, [twins[3001]])
    every_round()
    sync(fake_plex, look_at_plex=True)
    assert emptied_of(client, playlist_id) is None


def test_an_answer_needs_the_right_to_change_the_playlist(client: TestClient, world: tuple) -> None:
    from .conftest import make_account, new_client

    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    fake.list_remove(key, list(fake.tracks_of(key)))
    sync(fake_plex, look_at_plex=True)
    leser = make_account("leser")
    reader = new_client(leser)
    try:
        assert client.put(f"/api/v1/playlists/{playlist_id}/shares/{leser.id}",
                          json={"access": "read"}).status_code == 200
        refused = reader.post(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}/emptied", json={"empty": True})
        assert refused.status_code == 403
    finally:
        reader.close()
    assert len(local_titles(playlist_id)) == 6


def test_a_source_emptied_on_purpose_is_still_taken_over(client: TestClient, world: tuple) -> None:
    fake_plex, _fake, _jelly, _twins = world
    playlist_id = mirrored(client, world)
    for track in list(fake_plex.lists.tracks(plex_key(playlist_id))):
        fake_plex.lists.remove_track(plex_key(playlist_id), track)
    sync(fake_plex, look_at_plex=True)
    every_round()
    sync(fake_plex, look_at_plex=True)
    assert local_titles(playlist_id) == []


# --- Unsure findings ------------------------------------------------------------------------------------------------


def unsure_world(client: TestClient, world: tuple) -> tuple[int, int, str]:
    """A playlist whose Plex track 3021 (left out of Jellyfin) has a namesake by another artist in Jellyfin: found
    only by its title, unsure."""
    fake_plex, fake, jelly, _twins = world
    plex_item = fake_plex.items[3021]
    album = next(ident for ident, one in fake.albums.items() if one.name == "Hafenlieder")
    other = fake.add(album, plex_item.title, round(plex_item.duration / 1000) + 1)
    from app.db import SessionLocal
    from app.services import catalog, jobs

    with SessionLocal() as db:
        catalog.request_partial(db, server_id=jelly)
    jobs.run_pending(lane="catalog")
    made = make(client, "Unsicher", plex_numbers(PLAYLIST))
    sync(fake_plex)
    return int(made["id"]), jelly, other


def test_moving_offers_an_unsure_track_and_never_takes_it_unasked(client: TestClient, world: tuple) -> None:
    fake_plex, fake, _jelly, _twins = world
    playlist_id, jelly, other = unsure_world(client, world)
    preview = client.post(f"/api/v1/playlists/{playlist_id}/mirrors/preview",
                          json={"server": jelly, "move": True}).json()
    assert preview["preselect"] is False
    assert (preview["entries"], preview["found"], preview["unsure"]) == (6, 5, 1)
    row = next(one for one in preview["rows"] if one["how"] in ("song", "title"))
    assert row["found"] is not None
    before = {(item["title"], item["artist"]) for item in detail(client, playlist_id)["items"]}
    moved = client.post(f"/api/v1/playlists/{playlist_id}/move", json={"server": jelly, "old": "delete"})
    assert moved.status_code == 200 and moved.json()["found"] == 5
    sync(fake_plex, look_at_plex=True)
    view = detail(client, playlist_id)
    assert {(item["title"], item["artist"]) for item in view["items"]} == before, "an entry became another song"
    assert other not in fake.tracks_of(mirror_key(playlist_id, jelly))
    assert any(item["plex_hint"] == "elsewhere" for item in view["items"])


def test_moving_takes_an_unsure_track_chosen(client: TestClient, world: tuple) -> None:
    fake_plex, fake, _jelly, _twins = world
    playlist_id, jelly, other = unsure_world(client, world)
    preview = client.post(f"/api/v1/playlists/{playlist_id}/mirrors/preview",
                          json={"server": jelly, "move": True}).json()
    row = next(one for one in preview["rows"] if one["how"] in ("song", "title"))
    choices = {str(row["track"]): row["found"]["track"]}
    chosen = client.post(f"/api/v1/playlists/{playlist_id}/mirrors/preview",
                         json={"server": jelly, "move": True, "choices": choices}).json()
    assert chosen["found"] == 6
    moved = client.post(f"/api/v1/playlists/{playlist_id}/move", json={"server": jelly, "choices": choices})
    assert moved.status_code == 200 and moved.json()["found"] == 6
    sync(fake_plex, look_at_plex=True)
    assert other in fake.tracks_of(mirror_key(playlist_id, jelly))


def test_copying_a_mirror_still_takes_an_unsure_track(client: TestClient, world: tuple) -> None:
    fake_plex, fake, _jelly, _twins = world
    playlist_id, jelly, other = unsure_world(client, world)
    preview = client.post(f"/api/v1/playlists/{playlist_id}/mirrors/preview", json={"server": jelly}).json()
    assert preview["preselect"] is True and preview["found"] == 6 and preview["unsure"] == 1
    assert client.post(f"/api/v1/playlists/{playlist_id}/mirrors", json={"server": jelly}).status_code == 201
    sync(fake_plex)
    assert other in fake.tracks_of(mirror_key(playlist_id, jelly))


def test_taking_all_over_offers_unsure_tracks_only(client: TestClient, world: tuple,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    fake_plex, fake, _jelly, _twins = world
    playlist_id, jelly, other = unsure_world(client, world)
    found = look(client, jelly, {"mode": "mirror"})
    assert found["preselect"] is False and found["found"] == 5 and found["unsure"] == 1
    from app.services import jobs

    monkeypatch.setattr(mirrors, "TRANSFER_SLICE", 60.0)
    assert client.post(f"/api/v1/servers/{jelly}/transfer", json={"mode": "mirror"}).status_code == 202
    jobs.run_pending(lane="playlists")
    sync(fake_plex)
    assert other not in fake.tracks_of(mirror_key(playlist_id, jelly))
    assert detail(client, playlist_id)["mirrors"][0]["absent"] == 1


# --- A track dropped, then moved there -----------------------------------------------------------------------------


def test_a_track_only_there_dropped_and_moved_there_before_it_went_never_becomes_an_entry(
        client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = mirrored(client, world)
    key = mirror_key(playlist_id, jelly)
    own = own_track(fake, "Eisblume")
    fake.list_add(key, [own])
    sync(fake_plex, look_at_plex=True)
    extra = extras(playlist_id, jelly)[0]
    assert client.delete(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}/extras/{extra.id}").status_code == 204
    # Moved before the mirror's sync took it out.
    assert client.post(f"/api/v1/playlists/{playlist_id}/move", json={"server": jelly}).status_code == 200
    sync(fake_plex, look_at_plex=True)
    every_round()
    sync(fake_plex, look_at_plex=True)
    assert "Eisblume" not in local_titles(playlist_id)
    assert own not in fake.tracks_of(key)
    assert extras(playlist_id, jelly) == []
