"""Two mirrors of one playlist: the source on the stand-in Plex, mirrors on two stand-in Jellyfins (each its own
server, with its own tracks left out). A change in nexmix reaches all three copies; a change in one mirror reaches
nexmix, the source and the other mirror, and the copy that brought it is not synced again for it (``sync.mark`` with
``but``); quiet rounds write nothing anywhere; moving the playlist to one mirror keeps the other one."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Account
from app.services import jellyfin
from app.services import sync as server_sync

from .fakejellyfin import FakeJellyfin
from .fakeplex import FakePlex
from .jellyfin_world import connect as connect_jellyfin
from .mirror_world import (
    connect_both,
    copy_row,
    detail,
    local_titles,
    mapping,
    mirror_key,
    plex_numbers,
    twin,
)
from .plex_world import make, sync

PLAYLIST = [3001, 3003, 3013, 3017, 3026, 3021]
#: Left out of the first Jellyfin, and of the second.
FIRST_LACKS = frozenset({3021})
SECOND_LACKS = frozenset({3021, 3001})
SECOND = "http://jellyfin-two.example.com:8096"


@pytest.fixture
def three(client: TestClient, operator: Account, fake_plex: FakePlex, fake_jellyfin: FakeJellyfin,
          monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    other = FakeJellyfin(name="Zweit", server_id="0f" * 16)
    by_host = {"jellyfin.example.com": fake_jellyfin, "jellyfin-two.example.com": other}
    transport = httpx.MockTransport(lambda request: by_host[request.url.host].handle(request))
    monkeypatch.setattr(jellyfin, "TRANSPORT", transport)
    monkeypatch.setattr(jellyfin, "ASYNC_TRANSPORT", transport)
    _plex, first, twins = connect_both(client, fake_plex, fake_jellyfin, FIRST_LACKS)
    other_twins = twin(other, fake_plex, SECOND_LACKS)
    second = connect_jellyfin(client, other, url=SECOND)
    assert second != first
    made = make(client, "Dreifach", plex_numbers(PLAYLIST))
    sync(fake_plex)
    for server in (first, second):
        answer = client.post(f"/api/v1/playlists/{made['id']}/mirrors", json={"server": server})
        assert answer.status_code == 201, answer.text
    sync(fake_plex)
    return {"client": client, "plex": fake_plex, "a": fake_jellyfin, "b": other, "first": first, "second": second,
            "twins_a": twins, "twins_b": other_twins, "playlist": int(made["id"])}


def every_round() -> None:
    with SessionLocal() as db:
        server_sync.request_all(db)


def expected_on(playlist_id: int, server: int) -> list[str]:
    """What the copy on ``server`` should hold, by the server's keys, in nexmix's order: the entries' own tracks on
    the source, the tracks that stand for them on a mirror (entries without one left out)."""
    from app.models import Playlist
    from app.services import media
    from app.services import playlists as playlist_service

    with SessionLocal() as db:
        playlist = db.get(Playlist, playlist_id)
        assert playlist is not None
        items = playlist_service.live_items(db, playlist_id)
        if playlist.source_server_id == server:
            # An entry that kept another server's track (moved here, found nowhere here) is not on this server.
            tracks = [item.track_id for item in items if item.plex_hint != server_sync.HINT_ELSEWHERE]
        else:
            stands = mapping(playlist_id, server)
            tracks = [stands[item.id][0] for item in items if stands.get(item.id, (None,))[0] is not None]
        keys = media.keys(db, server, media.TRACK, [int(one) for one in tracks if one is not None])
    return [str(keys[int(one)]) for one in tracks if one is not None]


def alike(world: dict[str, Any]) -> list[str]:
    """nexmix, Plex and both Jellyfins hold the same, in the same order, each copy what stands for the entries there,
    nothing twice. Returns nexmix's titles."""
    playlist, fake_plex = world["playlist"], world["plex"]
    here = local_titles(playlist)
    with SessionLocal() as db:
        from app.services import playlists as playlist_service

        tracks = [item.track_id for item in playlist_service.live_items(db, playlist)]
    assert len(tracks) == len(set(tracks)), "an entry twice in nexmix"
    on_plex = [str(one) for one in fake_plex.lists.tracks(int(mirror_key(playlist, 1)))]
    assert on_plex == expected_on(playlist, 1)
    for fake, server in ((world["a"], world["first"]), (world["b"], world["second"])):
        raw = fake.tracks_of(mirror_key(playlist, server))
        assert len(raw) == len(set(raw)), "twice in a Jellyfin"
        assert raw == expected_on(playlist, server)
    return here


def reads_of(fake: FakeJellyfin, key: str) -> int:
    """How often the stand-in was asked for the entries of the playlist ``key``."""
    return sum(1 for method, path, params in fake.calls
               if method == "GET" and (f"/Playlists/{key}/Items" in path or params.get("ParentId") == key))


def test_both_mirrors_are_made_each_with_its_own_tracks_left_out(three: dict[str, Any]) -> None:
    assert len(alike(three)) == 6
    by_server = {one["server"]: one for one in detail(three["client"], three["playlist"])["mirrors"]}
    assert set(by_server) == {three["first"], three["second"]}
    assert by_server[three["first"]]["absent"] == 1 and by_server[three["second"]]["absent"] == 1
    assert {one["state"] for one in by_server.values()} == {"synced"}
    entries = detail(three["client"], three["playlist"])["items"]
    absent = {item["title"]: item["absent"] for item in entries if item["absent"]}
    assert absent == {three["plex"].items[3021].title: [three["first"], three["second"]]}
    # The second one lacks "Northern Lights" of this album: another version stands for it there (unsure, chosen when
    # the mirror was made), so it is in all three.
    assert len(three["b"].tracks_of(mirror_key(three["playlist"], three["second"]))) == 5


def test_a_change_in_nexmix_reaches_all_three(three: dict[str, Any]) -> None:
    client, playlist = three["client"], three["playlist"]
    assert client.post(f"/api/v1/playlists/{playlist}/items", json={"tracks": plex_numbers([3008])}).status_code == 200
    sync(three["plex"])
    here = alike(three)
    assert here[-1] == three["plex"].items[3008].title and len(here) == 7


def test_a_change_in_one_mirror_reaches_nexmix_the_source_and_the_other_mirror(three: dict[str, Any]) -> None:
    fake_a, playlist, first = three["a"], three["playlist"], three["first"]
    key = mirror_key(playlist, first)
    fake_a.list_add(key, [three["twins_a"][3008]])
    fake_a.list_remove(key, [three["twins_a"][3001]])
    fake_a.list_move(key, three["twins_a"][3026], 0)
    sync(three["plex"], look_at_plex=True)
    here = alike(three)
    plex = three["plex"]
    assert here[0] == plex.items[3026].title and here.count(plex.items[3008].title) == 2 and len(here) == 6
    assert plex.items[3001].title not in here


def test_the_mirror_that_brought_a_change_is_not_synced_again_for_it(three: dict[str, Any]) -> None:
    fake_a, fake_b, playlist, first = three["a"], three["b"], three["playlist"], three["first"]
    key_a, key_b = mirror_key(playlist, first), mirror_key(playlist, three["second"])
    fake_a.list_add(key_a, [three["twins_a"][3008]])
    with SessionLocal() as db:
        # Only the first mirror is due (as after a look at its list).
        server_sync.request_all(db, first)
    before_a, before_b = reads_of(fake_a, key_a), reads_of(fake_b, key_b)
    writes_a = len(fake_a.list_writes)
    sync(three["plex"])
    alike(three)
    assert len(fake_a.list_writes) == writes_a, "the change came from there: nothing to write back"
    # The first mirror is read once for the change it brought, never again for it; the second one takes it.
    assert reads_of(fake_a, key_a) - before_a == 1
    assert reads_of(fake_b, key_b) - before_b >= 1
    assert copy_row(playlist, first).due_at is None


def test_quiet_rounds_write_nothing_on_any_of_the_three(three: dict[str, Any]) -> None:
    plex, fake_a, fake_b = three["plex"], three["a"], three["b"]
    own = next(ident for ident, one in fake_a.tracks.items() if one.name == "Eisblume")
    fake_a.list_add(mirror_key(three["playlist"], three["first"]), [own])
    sync(plex, look_at_plex=True)
    sync(plex, look_at_plex=True)
    start = (len(plex.lists.writes), len(fake_a.list_writes), len(fake_b.list_writes))
    for _turn in range(20):
        fake_a.tick(20)
        fake_b.tick(20)
        plex.tick(20)
        every_round()
        sync(plex, look_at_plex=True)
    assert (len(plex.lists.writes), len(fake_a.list_writes), len(fake_b.list_writes)) == start
    # The track only in the first Jellyfin stays there, and only there.
    assert "Eisblume" not in local_titles(three["playlist"])
    assert own not in fake_b.tracks_of(mirror_key(three["playlist"], three["second"]))


def test_moving_to_one_mirror_keeps_the_other_and_plex_as_mirrors(three: dict[str, Any]) -> None:
    client, playlist, first, second = three["client"], three["playlist"], three["first"], three["second"]
    plex = three["plex"]
    moved = client.post(f"/api/v1/playlists/{playlist}/move", json={"server": first})
    assert moved.status_code == 200, moved.text
    sync(plex, look_at_plex=True)
    view = detail(client, playlist)
    assert view["server"] == first
    assert sorted(one["server"] for one in view["mirrors"]) == sorted([1, second])
    # The entry Jellyfin lacked stays with its Plex track (not on the new source), the rest takes Jellyfin's.
    assert len(local_titles(playlist)) == 6
    # A change in Plex, now a mirror, reaches the source and the other mirror.
    plex.lists.add(int(mirror_key(playlist, 1)), [3008])
    sync(plex, look_at_plex=True)
    every_round()
    sync(plex, look_at_plex=True)
    here = alike(three)
    assert here[-1] == plex.items[3008].title


def test_the_source_that_brought_a_change_is_not_synced_again_for_it(three: dict[str, Any]) -> None:
    plex, playlist = three["plex"], three["playlist"]
    key = int(mirror_key(playlist, 1))
    plex.lists.add(key, [3008])
    with SessionLocal() as db:
        # Only Plex is due (as after a look at its list).
        server_sync.request_all(db, 1)

    def reads() -> int:
        return sum(1 for method, path, _params in plex.library_calls() if method == "GET"
                   and path.startswith(f"/playlists/{key}/items"))

    before, writes = reads(), len(plex.lists.writes)
    sync(plex)
    here = alike(three)
    assert here[-1] == plex.items[3008].title
    assert len(plex.lists.writes) == writes, "the change came from Plex: nothing to write back"
    # Read for the change, and once more after the merge (as every sync of Plex does); not a third time for it.
    assert reads() - before == 2, "Plex read again for the change it brought"
    row = copy_row(playlist, 1)
    assert row.due_at is None and row.state == "synced"
