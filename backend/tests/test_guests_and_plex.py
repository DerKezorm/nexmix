"""Guests and the sync with Plex, against the stand-in Plex: what a guest adds, takes out or moves goes to Plex like any
other change, the guest's name stays in nexmix; a playlist deleted in Plex goes to the trash and ends its invitation
(its guests are out at once and do not come back with it); a smart playlist of Plex takes no guests; syncing now needs
the right to change the playlist, and a guest's cookie never reaches it."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Account, CatalogTrack, GuestEntry, GuestInvite
from app.services import catalog, plex_sync

from .conftest import TAB, make_account, new_client
from .fakeplex import FakePlex
from .plex_world import both_equal, connect, local_items, make, playlist_row, plex_key, sync


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex) -> Iterator[FakePlex]:
    connect(client, fake_plex)
    plex_sync.forget()
    yield fake_plex


def invite(client: TestClient, playlist_id: int, **rights: Any) -> str:
    assert client.post(f"/api/v1/playlists/{playlist_id}/invite").status_code == 200
    answer = client.put(f"/api/v1/playlists/{playlist_id}/invite", json={**rights, "start": True})
    assert answer.status_code == 200, answer.text
    return str(answer.json()["invite"]["code"])


def phone(code: str, name: str = "Lena") -> TestClient:
    found = TestClient(app, base_url="http://testserver", headers=TAB, client=(f"phone-{name}", 50000))
    assert found.post(f"/api/v1/join/{code}", json={"name": name}).status_code == 200
    return found


def synced_playlist(client: TestClient, fake: FakePlex, name: str, tracks: list[int]) -> tuple[int, int]:
    playlist = make(client, name, tracks)
    sync(fake)
    assert both_equal(fake, playlist["id"]) == tracks
    return playlist["id"], plex_key(playlist["id"])


def test_what_a_guest_adds_takes_out_and_moves_goes_to_plex_and_its_name_stays_here(client: TestClient,
                                                                                  world: FakePlex) -> None:
    playlist_id, key = synced_playlist(client, world, "Gartenfest", [3001, 3003])
    lena = phone(invite(client, playlist_id, may_sort=True))
    assert lena.post("/api/v1/guest/items", json={"tracks": [3013, 3026]}).status_code == 200
    sync(world)
    assert world.lists.tracks(key) == [3001, 3003, 3013, 3026]
    # The name is nexmix's: Plex has the tracks only, and nothing it sends back takes the name away.
    world.tick(20)
    sync(world, look_at_plex=True)
    by = {item.track_id: (item.added_by_name, item.from_plex) for item in local_items(playlist_id)}
    assert by[3013] == ("Lena", False) and by[3026] == ("Lena", False)
    with SessionLocal() as db:
        assert len(list(db.scalars(select(GuestEntry)))) == 2, "still the guest's entries (its limit, its undo)"
        # Like any entry it keeps the track's guid: with it the entry finds its track on another Plex server.
        generation = catalog.pin(db)
        wanted = db.scalar(select(CatalogTrack.guid).where(CatalogTrack.generation == generation,
                                                            CatalogTrack.id == 3013))
    assert wanted and {item.track_id: item.guid for item in local_items(playlist_id)}[3013] == wanted
    sent = json.dumps([list(map(str, one)) for one in world.lists.writes])
    assert "Lena" not in sent and "Lena" not in world.lists.lists[key].title
    entries = {one["track"]: one["id"] for one in lena.get("/api/v1/guest").json()["playlist"]["items"]}
    assert lena.post("/api/v1/guest/items/move", json={"entry": entries[3026], "after": None}).status_code == 200
    assert lena.post("/api/v1/guest/items/remove", json={"entries": [entries[3001]]}).status_code == 200
    sync(world)
    assert world.lists.tracks(key) == [3026, 3003, 3013]
    assert both_equal(world, playlist_id) == [3026, 3003, 3013]


def test_a_playlist_deleted_in_plex_ends_its_invitation_and_its_guests_stay_out(client: TestClient,
                                                                              world: FakePlex) -> None:
    playlist_id, key = synced_playlist(client, world, "Kellerparty", [3001, 3013])
    code = invite(client, playlist_id)
    lena = phone(code)
    assert lena.get("/api/v1/guest").status_code == 200
    world.lists.delete(key)
    sync(world, look_at_plex=True)
    assert playlist_row(playlist_id).deleted_at is not None
    with SessionLocal() as db:
        ended = db.scalar(select(GuestInvite.ended_at).where(GuestInvite.playlist_id == playlist_id))
    assert ended is not None, "the invitation ended with the playlist"
    out = lena.get("/api/v1/guest")
    assert out.status_code == 403 and out.json()["detail"]["code"] == "invite_ended"
    assert lena.post("/api/v1/guest/items", json={"tracks": [3026]}).status_code == 403
    in_trash = client.get(f"/api/v1/playlists/{playlist_id}/invite")
    assert in_trash.status_code != 200 or in_trash.json()["invite"] is None, in_trash.text
    # Brought back from the trash: made in Plex anew, the old code and the old guest stay out.
    assert client.post(f"/api/v1/playlists/{playlist_id}/restore").status_code == 200
    sync(world)
    assert both_equal(world, playlist_id) == [3001, 3013]
    assert lena.get("/api/v1/guest").status_code == 403
    with TestClient(app, base_url="http://testserver", headers=TAB) as stranger:
        assert stranger.get(f"/api/v1/join/{code}").status_code == 404
    assert client.get(f"/api/v1/playlists/{playlist_id}/invite").json()["invite"] is None


def test_a_smart_playlist_of_plex_takes_no_guests(client: TestClient, world: FakePlex) -> None:
    world.lists.make("Alle Titel", [3001, 3003, 3013], smart=True)
    sync(world, look_at_plex=True)
    smart = next(card for card in client.get("/api/v1/playlists").json()["playlists"] if card["smart"])
    answer = client.post(f"/api/v1/playlists/{smart['id']}/invite")
    assert answer.status_code == 409 and answer.json()["detail"]["code"] == "smart_playlist"
    with SessionLocal() as db:
        assert db.scalar(select(GuestInvite).where(GuestInvite.playlist_id == smart["id"])) is None


def test_syncing_now_needs_the_right_to_change_and_never_opens_to_a_guest(client: TestClient,
                                                                         world: FakePlex) -> None:
    playlist_id, _key = synced_playlist(client, world, "Nur zum Hören", [3001])
    reader, editor = make_account("rafael"), make_account("edith")
    for row, access in ((reader, "read"), (editor, "edit")):
        assert client.put(f"/api/v1/playlists/{playlist_id}/shares/{row.id}", json={"access": access}).status_code == 200
    with new_client(reader) as listening, new_client(editor) as changing:
        refused = listening.post(f"/api/v1/playlists/{playlist_id}/sync")
        assert refused.status_code == 403 and refused.json()["detail"]["code"] == "read_only"
        assert changing.post(f"/api/v1/playlists/{playlist_id}/sync").status_code == 200
    lena = phone(invite(client, playlist_id, may_sort=True))
    for path in (f"/api/v1/playlists/{playlist_id}/sync", f"/api/v1/playlists/{playlist_id}/plex-order",
                 "/api/v1/plex/playlists/sync"):
        assert lena.post(path).status_code == 401, path
    assert "plex" not in lena.get("/api/v1/guest").json()["playlist"], "a guest sees nothing of the sync"
