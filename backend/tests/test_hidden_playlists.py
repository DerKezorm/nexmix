"""Hiding a playlist from the own overview, and the smart playlists of Plex shown or not: a view of one account only.

Nothing goes to Plex, no right changes, nobody else sees a difference; the playlist stays where it was for sharing and
changing. Whoever may see a playlist may hide it (also one shared to look at, also a smart one); a stranger gets the
404 a playlist he may not see always gets. Twice is once, also at the same moment. The smart playlists of Plex are left
out of the overview unless the account chose to see them (``show_smart_playlists``, off from the start)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Account, HiddenPlaylist, ServerPlaylist
from app.services import sync as server_sync

from .conftest import make_account, new_client
from .fakeplex import FakePlex
from .plex_world import connect, sync
from .plex_world import make as make_synced
from .test_playlists import People, make


@pytest.fixture
def people() -> Iterator[People]:
    found = People()
    yield found
    found.close()


def card_of(browser: TestClient, playlist_id: int) -> dict[str, Any]:
    found = [card for card in browser.get("/api/v1/playlists").json()["playlists"] if card["id"] == playlist_id]
    assert len(found) == 1
    return found[0]


def rows() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.count()).select_from(HiddenPlaylist)) or 0)


def test_hiding_is_for_the_own_overview_only(people: People) -> None:
    from . import made_up_catalog

    made_up_catalog.install()
    playlist = make(people.owner, tracks=[301, 311])
    assert people.owner.put(f"/api/v1/playlists/{playlist['id']}/shares/{people.reader_row.id}",
                            json={"access": "read"}).status_code == 200
    playlist = people.owner.get(f"/api/v1/playlists/{playlist['id']}").json()
    assert card_of(people.owner, playlist["id"])["hidden"] is False
    answer = people.reader.put(f"/api/v1/playlists/{playlist['id']}/hidden")
    assert answer.status_code == 200 and answer.json() == {"id": playlist["id"], "hidden": True}
    # The reader's list and the playlist itself say so; it is still in the list, for a program and for "show again".
    assert card_of(people.reader, playlist["id"])["hidden"] is True
    assert people.reader.get(f"/api/v1/playlists/{playlist['id']}").json()["hidden"] is True
    # Nobody else sees a difference, and the playlist did not change: no new version, the same entries and shares.
    assert card_of(people.owner, playlist["id"])["hidden"] is False
    assert card_of(people.operator, playlist["id"])["hidden"] is False
    after = people.owner.get(f"/api/v1/playlists/{playlist['id']}").json()
    assert after["version"] == playlist["version"] and [item["track"] for item in after["items"]] == [301, 311]
    assert people.owner.get(f"/api/v1/playlists/{playlist['id']}/shares").json()["shares"][0]["access"] == "read"
    # Still shared, still to be read; the owner still changes it.
    assert people.reader.get(f"/api/v1/playlists/{playlist['id']}").status_code == 200
    assert people.owner.post(f"/api/v1/playlists/{playlist['id']}/items", json={"tracks": [317]}).status_code == 200
    # Shown again.
    shown = people.reader.delete(f"/api/v1/playlists/{playlist['id']}/hidden")
    assert shown.status_code == 200 and shown.json() == {"id": playlist["id"], "hidden": False}
    assert card_of(people.reader, playlist["id"])["hidden"] is False and rows() == 0


def test_a_stranger_cannot_hide_what_he_may_not_see(people: People) -> None:
    playlist = make(people.owner)
    for method in ("PUT", "DELETE"):
        assert people.stranger.request(method, f"/api/v1/playlists/{playlist['id']}/hidden").status_code == 404
        assert people.stranger.request(method, "/api/v1/playlists/999999/hidden").status_code == 404
    assert rows() == 0
    with new_client() as nobody:
        assert nobody.put(f"/api/v1/playlists/{playlist['id']}/hidden").status_code == 401
    assert rows() == 0


def test_twice_is_once_also_at_the_same_moment(people: People) -> None:
    playlist = make(people.owner)
    start = threading.Barrier(6)
    answers: list[int] = []

    def press() -> None:
        with new_client(people.owner_row) as browser:
            start.wait()
            answers.append(browser.put(f"/api/v1/playlists/{playlist['id']}/hidden").status_code)

    threads = [threading.Thread(target=press) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert answers == [200] * 6 and rows() == 1
    assert people.owner.delete(f"/api/v1/playlists/{playlist['id']}/hidden").status_code == 200
    assert people.owner.delete(f"/api/v1/playlists/{playlist['id']}/hidden").status_code == 200
    assert rows() == 0


def test_a_device_may_hide_and_a_read_key_may_not(client: TestClient, operator: Account) -> None:
    playlist = make(client)
    token = client.post("/api/v1/devices", json={"name": "phone"}).json()["token"]
    assert client.put("/api/v1/settings", json={"api_keys_allowed": True}).status_code == 200
    key = client.post("/api/v1/api-keys", json={"name": "script"}).json()["secret"]
    with TestClient(client.app, base_url="http://testserver") as program:
        refused = program.put(f"/api/v1/playlists/{playlist['id']}/hidden", headers={"Authorization": f"Bearer {key}"})
        assert refused.status_code in (401, 403) and rows() == 0
        # A key reads the list with the flag, as the account sees it.
        listed = program.get("/api/v1/playlists", headers={"Authorization": f"Bearer {key}"}).json()
        assert listed["playlists"][0]["hidden"] is False and listed["show_smart_playlists"] is False
        hidden = program.put(f"/api/v1/playlists/{playlist['id']}/hidden", headers={"Authorization": f"Bearer {token}"})
        assert hidden.status_code == 200 and rows() == 1


def test_the_rows_go_with_the_playlist_and_with_the_account(people: People) -> None:
    playlist = make(people.owner)
    assert people.owner.put(f"/api/v1/playlists/{playlist['id']}/shares/{people.reader_row.id}",
                            json={"access": "read"}).status_code == 200
    assert people.reader.put(f"/api/v1/playlists/{playlist['id']}/hidden").status_code == 200
    other = make(people.owner, "Zweite")
    assert people.owner.put(f"/api/v1/playlists/{other['id']}/hidden").status_code == 200
    assert rows() == 2
    # In the trash and gone for good: the row goes with it.
    assert people.owner.delete(f"/api/v1/playlists/{other['id']}").status_code == 200
    assert people.owner.delete(f"/api/v1/playlists/{other['id']}", params={"purge": True}).status_code == 200
    assert rows() == 1
    with SessionLocal() as db:
        row = db.get(Account, people.reader_row.id)
        assert row is not None
        db.delete(row)
        db.commit()
    assert rows() == 0


def test_the_smart_playlists_of_plex_are_left_out_unless_the_account_wants_them(
        client: TestClient, operator: Account, fake_plex: FakePlex) -> None:
    connect(client, fake_plex)
    server_sync.forget()
    smart = fake_plex.lists.make("Alles Neue", [3001, 3003], smart=True)
    fake_plex.tick()
    sync(fake_plex, look_at_plex=True)
    own = make_synced(client, "Gartenfest", [3005])
    sync(fake_plex)
    with SessionLocal() as db:
        smart_id = db.scalar(select(ServerPlaylist.playlist_id).where(ServerPlaylist.ext_key == str(smart.key)))
    assert smart_id is not None
    listed = client.get("/api/v1/playlists").json()
    assert listed["show_smart_playlists"] is False, "off from the start"
    assert {card["id"]: card["smart"] for card in listed["playlists"]} == {smart_id: True, own["id"]: False}
    assert client.put("/api/v1/me/preferences", json={"show_smart_playlists": True}).status_code == 200
    assert client.get("/api/v1/playlists").json()["show_smart_playlists"] is True
    assert client.put("/api/v1/me/preferences", json={"show_smart_playlists": "yes"}).status_code == 422
    with make_account_client("mia") as mia:
        # Another account keeps its own choice.
        assert mia.get("/api/v1/playlists").json()["show_smart_playlists"] is False
    # A smart playlist can be hidden one by one as well, and nothing of it goes to Plex.
    writes = len(fake_plex.lists.writes)
    assert client.put(f"/api/v1/playlists/{smart_id}/hidden").status_code == 200
    assert client.put(f"/api/v1/playlists/{own['id']}/hidden").status_code == 200
    sync(fake_plex, look_at_plex=True)
    assert len(fake_plex.lists.writes) == writes
    assert fake_plex.lists.lists[smart.key].smart and fake_plex.lists.tracks(smart.key) == [3001, 3003]
    assert card_of(client, smart_id)["hidden"] is True and card_of(client, own["id"])["hidden"] is True


def make_account_client(name: str) -> TestClient:
    return new_client(make_account(name))
