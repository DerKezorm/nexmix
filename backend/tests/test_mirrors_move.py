"""Moving playlists between servers, against the stand-in Plex and Jellyfin (``mirror_world``): one playlist with the
copy before kept as a mirror or deleted, copied once, moved in the middle of a sync, every playlist taken over in the
background (stopped, and gone on with after a restart), and a server let go of only once no playlist has it as its
source, its mirrors forgotten and not deleted."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import Account, Job, MediaServer, Playlist, ServerPlaylist, Transfer
from app.services import jobs, mirrors

from .fakejellyfin import FakeJellyfin
from .fakeplex import FakePlex
from .mirror_world import (
    connect_both,
    copy_row,
    detail,
    jelly_titles,
    local_titles,
    mapping,
    mirror_key,
    plex_numbers,
    plex_titles,
)
from .plex_world import make, plex_key, sync
from .test_guests_and_plex import invite, phone

PLAYLIST = [3001, 3003, 3013, 3017, 3026, 3021]
LEFT_OUT = frozenset({3021})


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex,
          fake_jellyfin: FakeJellyfin) -> tuple[FakePlex, FakeJellyfin, int, dict[int, str]]:
    _plex, jelly, twins = connect_both(client, fake_plex, fake_jellyfin, LEFT_OUT)
    return fake_plex, fake_jellyfin, jelly, twins


def on_plex(client: TestClient, fake_plex: FakePlex, name: str = "Sommerfest",
            tracks: list[int] | None = None) -> int:
    made = make(client, name, plex_numbers(tracks or PLAYLIST))
    sync(fake_plex)
    return int(made["id"])


def test_moved_with_the_old_copy_kept_as_a_mirror(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, twins = world
    playlist_id = on_plex(client, fake_plex)
    code = invite(client, playlist_id)
    answer = client.post(f"/api/v1/playlists/{playlist_id}/move", json={"server": jelly})
    assert answer.status_code == 200, answer.text
    assert (answer.json()["found"], answer.json()["entries"]) == (5, 6)
    sync(fake_plex, look_at_plex=True)
    view = detail(client, playlist_id)
    assert view["server"] == jelly and view["plex"]["kind"] == "jellyfin" and view["plex"]["state"] == "synced"
    assert [(one["server"], one["kind"], one["state"]) for one in view["mirrors"]] == [(1, "plex", "synced")]
    # Every entry is still there; the one Jellyfin lacks says so and stays on Plex.
    assert local_titles(playlist_id) == ["Northern Lights", "Glasshouse", "Copper Sky (feat. Dax Orlo)", "Fever Glass",
                                         "Paper Planes", "Op. 3 (No. 2)"]
    hints = {item["title"]: item["plex_hint"] for item in view["items"]}
    assert hints["Op. 3 (No. 2)"] == "elsewhere" and hints["Northern Lights"] is None
    assert jelly_titles(fake, playlist_id, jelly) == local_titles(playlist_id)[:5]
    assert plex_titles(fake_plex, playlist_id) == local_titles(playlist_id)
    # The source searches and suggests on Jellyfin now; the guests too.
    guest = phone(code)
    assert "Glasfluss" in guest.get("/api/v1/guest/catalog").text
    # Changes keep going both ways, now from Jellyfin as the source.
    key = mirror_key(playlist_id, jelly)
    fake.list_add(key, [twins[3008]])
    sync(fake_plex, look_at_plex=True)
    # An entry Jellyfin has no track for stays where it was, at the end.
    assert local_titles(playlist_id)[-2:] == ["Glasshouse", "Op. 3 (No. 2)"]
    assert plex_titles(fake_plex, playlist_id) == local_titles(playlist_id)
    fake_plex.lists.remove_track(int(mirror_key(playlist_id, 1)), 3001)
    sync(fake_plex, look_at_plex=True)
    assert "Northern Lights" not in local_titles(playlist_id)
    assert "Northern Lights" not in jelly_titles(fake, playlist_id, jelly)
    assert "Op. 3 (No. 2)" in plex_titles(fake_plex, playlist_id)


def test_moved_with_the_old_copy_deleted_there(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = on_plex(client, fake_plex)
    old_key = plex_key(playlist_id)
    answer = client.post(f"/api/v1/playlists/{playlist_id}/move", json={"server": jelly, "old": "delete"})
    assert answer.status_code == 200, answer.text
    sync(fake_plex, look_at_plex=True)
    assert old_key not in fake_plex.lists.lists
    view = detail(client, playlist_id)
    assert view["mirrors"] == [] and view["server"] == jelly
    assert "Op. 3 (No. 2)" in local_titles(playlist_id)
    assert jelly_titles(fake, playlist_id, jelly) == local_titles(playlist_id)[:5]
    # The old copy is never taken in again as a new playlist.
    sync(fake_plex, look_at_plex=True)
    with SessionLocal() as db:
        assert db.scalar(select(Playlist.id).where(Playlist.id != playlist_id)) is None


def test_a_mirror_moved_to_keeps_its_choices_and_its_state(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = on_plex(client, fake_plex)
    other = next(key for key, one in fake.tracks.items() if one.name == "Eisblume")
    from .mirror_world import jelly_numbers

    eis = jelly_numbers(jelly, [other])[0]
    op3 = plex_numbers([3021])[0]
    made = client.post(f"/api/v1/playlists/{playlist_id}/mirrors", json={"server": jelly, "choices": {str(op3): eis}})
    assert made.status_code == 201, made.text
    sync(fake_plex)
    key = mirror_key(playlist_id, jelly)
    writes = len(fake.list_writes)
    assert client.post(f"/api/v1/playlists/{playlist_id}/move", json={"server": jelly}).status_code == 200
    sync(fake_plex, look_at_plex=True)
    # The same Jellyfin playlist, nothing written there: it held everything already.
    assert mirror_key(playlist_id, jelly) == key and len(fake.list_writes) == writes
    assert local_titles(playlist_id)[-1] == "Eisblume"
    # Plex, now the mirror, keeps its own track for that entry.
    assert plex_titles(fake_plex, playlist_id)[-1] == "Op. 3 (No. 2)"


def test_copied_once_is_a_playlist_of_its_own(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = on_plex(client, fake_plex)
    answer = client.post(f"/api/v1/playlists/{playlist_id}/mirrors", json={"server": jelly, "once": True})
    assert answer.status_code == 201, answer.text
    copy_id = answer.json()["playlist"]
    assert copy_id != playlist_id and answer.json()["mirrors"] == []
    sync(fake_plex)
    copy = detail(client, copy_id)
    assert copy["server"] == jelly and copy["name"] == "Sommerfest" and copy["count"] == 5
    assert jelly_titles(fake, copy_id, jelly) == local_titles(copy_id)
    # No tie: a change in one is not in the other.
    assert client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": plex_numbers([3008])}).status_code \
        == 200
    sync(fake_plex)
    assert "Glasshouse" not in local_titles(copy_id)[2:]
    assert local_titles(copy_id) == ["Northern Lights", "Glasshouse", "Copper Sky (feat. Dax Orlo)", "Fever Glass",
                                     "Paper Planes"]


def test_moved_in_the_middle_of_a_sync_loses_nothing_and_writes_nothing_twice(
        client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, twins = world
    playlist_id = on_plex(client, fake_plex)
    assert client.post(f"/api/v1/playlists/{playlist_id}/mirrors", json={"server": jelly}).status_code == 201
    sync(fake_plex)
    key = mirror_key(playlist_id, jelly)
    # Somebody adds in Jellyfin; while the mirror's sync reads it, the playlist moves to Jellyfin.
    fake.list_add(key, [twins[3008]])
    moved: list[bool] = []

    def hook(_count: int, path: str) -> None:
        if not moved and path.startswith(f"/Playlists/{key}/Items"):
            moved.append(True)
            with SessionLocal() as db:
                mirrors.move(db, playlist_id, jelly)

    writes = len(fake.list_writes)
    fake.hook = hook
    sync(fake_plex, look_at_plex=True)
    fake.hook = None
    sync(fake_plex, look_at_plex=True)
    assert moved
    # Nothing was ever taken out of Jellyfin on the way (no sync wrote with the part the copy played before).
    assert [path for method, path, _params in fake.list_writes[writes:] if method == "DELETE"] == []
    assert copy_row(playlist_id, jelly).mirror is False and copy_row(playlist_id, 1).mirror is True
    titles = local_titles(playlist_id)
    assert len(titles) == 7 and "Op. 3 (No. 2)" in titles
    from .plex_world import local_tracks

    assert len(set(local_tracks(playlist_id))) == 7
    assert jelly_titles(fake, playlist_id, jelly) == [one for one in titles if one != "Op. 3 (No. 2)"]
    assert plex_titles(fake_plex, playlist_id) == titles
    assert len(fake.tracks_of(key)) == len(set(fake.tracks_of(key)))


def test_take_all_over_in_the_background_stops_and_goes_on_after_a_restart(
        client: TestClient, world: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_plex, fake, jelly, _twins = world
    ids = [on_plex(client, fake_plex, f"Liste {number}", PLAYLIST[number:number + 3]) for number in range(4)]
    found = client.post(f"/api/v1/servers/{jelly}/transfer/preview", json={"mode": "move"})
    assert found.status_code == 200, found.text
    assert (found.json()["playlists"], found.json()["entries"], found.json()["missing"]) == (4, 12, 1)
    # One playlist per slice.
    monkeypatch.setattr(mirrors, "TRANSFER_SLICE", 0.0)
    started = client.post(f"/api/v1/servers/{jelly}/transfer", json={"mode": "move"})
    assert started.status_code == 202, started.text
    assert client.post(f"/api/v1/servers/{jelly}/transfer", json={"mode": "move"}).status_code == 409
    assert jobs.run_pending(lane="playlists", limit=1) == 1
    state = client.get(f"/api/v1/servers/{jelly}/transfer").json()["transfer"]
    assert (state["state"], state["done"], state["total"]) == ("running", 1, 4)
    # The server stops: the job that waited is gone with it; at the next start the lane goes on.
    with SessionLocal() as db:
        db.execute(delete(Job).where(Job.kind == mirrors.TRANSFER))
        db.commit()
        mirrors.settle_transfers(db)
    sync(fake_plex)
    jobs.run_pending(lane="playlists")
    state = client.get(f"/api/v1/servers/{jelly}/transfer").json()["transfer"]
    assert (state["state"], state["done"], state["skipped"]) == ("done", 4, 0)
    sync(fake_plex, look_at_plex=True)
    for playlist_id in ids:
        view = detail(client, playlist_id)
        assert view["server"] == jelly and [one["server"] for one in view["mirrors"]] == [1]
        assert jelly_titles(fake, playlist_id, jelly) == [one for one in local_titles(playlist_id)
                                                          if one != "Op. 3 (No. 2)"]


def test_take_all_over_stopped_ends_cleanly(client: TestClient, world: tuple,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    fake_plex, _fake, jelly, _twins = world
    for number in range(3):
        on_plex(client, fake_plex, f"Liste {number}", PLAYLIST[number:number + 2])
    monkeypatch.setattr(mirrors, "TRANSFER_SLICE", 0.0)
    assert client.post(f"/api/v1/servers/{jelly}/transfer", json={"mode": "mirror"}).status_code == 202
    assert jobs.run_pending(lane="playlists", limit=1) == 1
    stopped = client.delete(f"/api/v1/servers/{jelly}/transfer")
    assert stopped.status_code == 200 and stopped.json()["transfer"]["stopping"] is True
    jobs.run_pending(lane="playlists")
    state = client.get(f"/api/v1/servers/{jelly}/transfer").json()["transfer"]
    assert (state["state"], state["done"]) == ("stopped", 1)
    with SessionLocal() as db:
        assert db.scalar(select(Transfer.state)) == "stopped"
        assert len(list(db.scalars(select(ServerPlaylist).where(ServerPlaylist.mirror.is_(True))))) == 1
    # A new one may start; it takes the two left (one is mirrored already).
    again = client.post(f"/api/v1/servers/{jelly}/transfer/preview", json={"mode": "mirror"}).json()
    assert again["playlists"] == 2


def test_a_server_is_let_go_of_only_when_no_playlist_takes_its_tracks_from_it(
        client: TestClient, world: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = on_plex(client, fake_plex)
    refused = client.delete("/api/v1/servers/1")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "server_is_source"
    assert refused.json()["detail"]["count"] == 1
    # Everything moved to Jellyfin first, then Plex let go of: its mirror forgotten, its playlist left there.
    monkeypatch.setattr(mirrors, "TRANSFER_SLICE", 60.0)
    assert client.post(f"/api/v1/servers/{jelly}/transfer",
                       json={"mode": "move", "disconnect": 1}).status_code == 202
    jobs.run_pending(lane="playlists")
    sync(fake_plex)
    with SessionLocal() as db:
        plex_row = db.get(MediaServer, 1)
        assert plex_row is not None and plex_row.active is False
    old = copy_row(playlist_id, 1)
    assert old.mirror is False and old.resume is True and old.ext_key is not None
    assert int(old.ext_key) in fake_plex.lists.lists
    assert mapping(playlist_id, jelly) == {}
    view = detail(client, playlist_id)
    assert view["mirrors"] == [] and view["server"] == jelly
    assert jelly_titles(fake, playlist_id, jelly) == local_titles(playlist_id)[:5]


def test_letting_go_of_a_mirror_server_forgets_its_mirrors(client: TestClient, world: tuple) -> None:
    fake_plex, fake, jelly, _twins = world
    playlist_id = on_plex(client, fake_plex)
    assert client.post(f"/api/v1/playlists/{playlist_id}/mirrors", json={"server": jelly}).status_code == 201
    sync(fake_plex)
    key = mirror_key(playlist_id, jelly)
    assert client.delete(f"/api/v1/servers/{jelly}").status_code == 204
    assert key in fake.lists
    assert detail(client, playlist_id)["mirrors"] == []
    assert local_titles(playlist_id) == plex_titles(fake_plex, playlist_id)
