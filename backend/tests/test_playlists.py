"""Playlists: every right for every kind of person at every route, the entries (add, take out, bring back, move), one
track once, versions and no-ops, two people at once, the trash with the clock set, shares, and the summary the cards
show."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock
from app.db import SessionLocal
from app.models import OPERATOR, Account, Playlist, PlaylistItem
from app.services import playlists

from . import made_up_catalog
from .conftest import make_account, new_client


@pytest.fixture(autouse=True)
def catalog_in() -> None:
    made_up_catalog.install()


class People:
    def __init__(self) -> None:
        self.owner_row = make_account("olivia")
        self.reader_row = make_account("rafael")
        self.editor_row = make_account("edith")
        self.stranger_row = make_account("stella")
        self.operator_row = make_account("otto", OPERATOR)
        self.owner = new_client(self.owner_row)
        self.reader = new_client(self.reader_row)
        self.editor = new_client(self.editor_row)
        self.stranger = new_client(self.stranger_row)
        self.operator = new_client(self.operator_row)

    def close(self) -> None:
        for one in (self.owner, self.reader, self.editor, self.stranger, self.operator):
            one.close()


@pytest.fixture
def people() -> Iterator[People]:
    found = People()
    yield found
    found.close()


def make(browser: TestClient, name: str = "Grillabend", tracks: list[int] | None = None) -> dict[str, Any]:
    made = browser.post("/api/v1/playlists", json={"name": name})
    assert made.status_code == 201, made.text
    body = made.json()
    if tracks:
        added = browser.post(f"/api/v1/playlists/{body['id']}/items", json={"tracks": tracks})
        assert added.status_code == 200, added.text
    return browser.get(f"/api/v1/playlists/{body['id']}").json()


@pytest.fixture
def shared(people: People) -> dict[str, Any]:
    """A playlist of ``olivia`` with three tracks, shared with ``rafael`` to see and with ``edith`` to change."""
    playlist = make(people.owner, tracks=[301, 311, 317])
    assert people.owner.put(f"/api/v1/playlists/{playlist['id']}/shares/{people.reader_row.id}",
                            json={"access": "read"}).status_code == 200
    assert people.owner.put(f"/api/v1/playlists/{playlist['id']}/shares/{people.editor_row.id}",
                            json={"access": "edit"}).status_code == 200
    return people.owner.get(f"/api/v1/playlists/{playlist['id']}").json()


def entries(browser: TestClient, playlist_id: int) -> list[int]:
    return [item["track"] for item in browser.get(f"/api/v1/playlists/{playlist_id}").json()["items"]]


def version(playlist_id: int) -> int:
    with SessionLocal() as db:
        row = db.get(Playlist, playlist_id)
        assert row is not None
        return row.version


# --- Rights: who reaches what ----------------------------------------------------------------------------------------

#: (who, what) -> expected status. ``view``: read it; ``edit``: change entries or name; ``manage``: delete, share.
EXPECT = {
    "owner": {"view": 200, "edit": 200, "manage": 200},
    "operator": {"view": 200, "edit": 200, "manage": 200},
    "editor": {"view": 200, "edit": 200, "manage": 403},
    "reader": {"view": 200, "edit": 403, "manage": 403},
    "stranger": {"view": 404, "edit": 404, "manage": 404},
}


def routes(playlist: dict[str, Any], people: People) -> list[tuple[str, str, str, dict[str, Any] | None]]:
    """Every route about one playlist: (right needed, method, path, body)."""
    base = f"/api/v1/playlists/{playlist['id']}"
    first = playlist["items"][0]["id"]
    return [
        ("view", "GET", base, None),
        ("view", "GET", f"{base}/events?probe=1", None),
        ("edit", "PUT", base, {"name": "Neuer Name"}),
        ("edit", "POST", f"{base}/items", {"tracks": [302]}),
        ("edit", "POST", f"{base}/items/move", {"entry": first, "after": None}),
        ("edit", "POST", f"{base}/items/remove", {"entries": [first]}),
        ("edit", "POST", f"{base}/items/restore", {"entries": [first], "after": None}),
        ("manage", "GET", f"{base}/shares", None),
        ("manage", "PUT", f"{base}/shares/{people.stranger_row.id}", {"access": "read"}),
        ("manage", "DELETE", f"{base}/shares/{people.stranger_row.id}", None),
        ("manage", "DELETE", base, None),
        ("manage", "POST", f"{base}/restore", None),
    ]


@pytest.mark.parametrize("who", list(EXPECT))
def test_every_route_checks_the_right_of_the_one_who_asks(people: People, shared: dict[str, Any], who: str,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import playlists as router

    # The event stream ends at once here; the right is checked before it begins.
    monkeypatch.setattr(router, "STREAM_SECONDS", 0.05)
    browser = getattr(people, who)
    odd = []
    for need, method, path, body in routes(shared, people):
        answer = browser.request(method, path, json=body)
        expected = EXPECT[who][need]
        if answer.status_code != expected:
            odd.append(f"{method} {path} -> {answer.status_code}, wanted {expected}")
    assert odd == []


def test_purge_only_for_owner_and_operator_and_only_from_the_trash(people: People, shared: dict[str, Any]) -> None:
    path = f"/api/v1/playlists/{shared['id']}"
    answer = people.owner.delete(path, params={"purge": "true"})
    assert answer.status_code == 409 and answer.json()["detail"]["code"] == "not_in_trash"
    assert people.owner.delete(path).status_code == 200
    for browser in (people.editor, people.reader, people.stranger):
        assert browser.delete(path, params={"purge": "true"}).status_code == 404
    assert people.operator.delete(path, params={"purge": "true"}).json() == {"deleted": True}
    assert people.owner.get(path).status_code == 404


def test_members_see_their_own_and_what_is_shared_the_operator_all(people: People, shared: dict[str, Any]) -> None:
    own_of_stella = make(people.stranger, "Stellas")

    def names(browser: TestClient) -> set[str]:
        return {card["name"] for card in browser.get("/api/v1/playlists").json()["playlists"]}

    assert names(people.owner) == {"Grillabend"}
    assert names(people.reader) == {"Grillabend"}
    assert names(people.stranger) == {"Stellas"}
    assert names(people.operator) == {"Grillabend", "Stellas"}
    card = next(one for one in people.reader.get("/api/v1/playlists").json()["playlists"])
    assert card["access"] == "read" and card["owner"]["name"] == "olivia" and card["shares"] == 2
    assert people.owner.get(f"/api/v1/playlists/{own_of_stella['id']}").status_code == 404


def test_a_key_reads_playlists_but_changes_nothing(people: People, shared: dict[str, Any]) -> None:
    assert people.operator.put("/api/v1/settings", json={"api_keys_allowed": True}).status_code == 200
    secret = people.owner.post("/api/v1/api-keys", json={"name": "script"}).json()["secret"]
    with new_client() as program:
        program.headers.pop("X-Nexmix-Client", None)
        bearer = {"Authorization": f"Bearer {secret}"}
        assert program.get("/api/v1/playlists", headers=bearer).status_code == 200
        assert program.get(f"/api/v1/playlists/{shared['id']}", headers=bearer).json()["items"]
        refused = program.post(f"/api/v1/playlists/{shared['id']}/items", headers=bearer, json={"tracks": [302]})
        assert refused.status_code == 401
    assert entries(people.owner, shared["id"]) == [301, 311, 317]


def test_a_device_token_changes_like_its_account(people: People, shared: dict[str, Any]) -> None:
    token = people.editor.post("/api/v1/devices", json={"name": "phone"}).json()["token"]
    with new_client() as phone:
        phone.headers.pop("X-Nexmix-Client", None)
        answer = phone.post(f"/api/v1/playlists/{shared['id']}/items", json={"tracks": [302]},
                            headers={"Authorization": f"Bearer {token}"})
        assert answer.status_code == 200
    assert entries(people.owner, shared["id"])[-1] == 302


def test_a_change_from_a_browser_needs_the_header_of_the_tab(people: People, shared: dict[str, Any]) -> None:
    people.owner.headers.pop("X-Nexmix-Client")
    answer = people.owner.post(f"/api/v1/playlists/{shared['id']}/items", json={"tracks": [302]})
    assert answer.status_code == 400 and answer.json()["detail"]["code"] == "client_required"


# --- Entries ---------------------------------------------------------------------------------------------------------


def test_adding_keeps_one_track_once_and_skips_what_the_catalogue_does_not_know(people: People) -> None:
    playlist = make(people.owner)
    answer = people.owner.post(f"/api/v1/playlists/{playlist['id']}/items",
                               json={"tracks": [301, 311, 301, 999_999]}).json()
    assert answer["op"] == "add" and [entry["track"] for entry in answer["entries"]] == [301, 311]
    assert answer["skipped"] == [999_999]
    again = people.owner.post(f"/api/v1/playlists/{playlist['id']}/items", json={"tracks": [311]}).json()
    assert again["op"] == "none" and again["skipped"] == [311]
    # Another version of the same song is another track: explicitly chosen, it goes in.
    other = people.owner.post(f"/api/v1/playlists/{playlist['id']}/items", json={"tracks": [305]}).json()
    assert [entry["track"] for entry in other["entries"]] == [305]
    assert entries(people.owner, playlist["id"]) == [301, 311, 305]


def test_an_entry_says_what_it_is_and_who_added_it(people: People, shared: dict[str, Any]) -> None:
    people.editor.post(f"/api/v1/playlists/{shared['id']}/items", json={"tracks": [313]})
    item = people.reader.get(f"/api/v1/playlists/{shared['id']}").json()["items"][-1]
    assert item == {**item, "track": 313, "title": "Abendlied", "artist": "Zoë Brandt & Dax Orlo",
                    "album": "Straße der Träume", "album_id": 205, "cover": True, "year": 1979, "duration": 233,
                    "song": 313, "missing": False, "added_by_name": "edith"}
    assert item["added_by"]["name"] == "edith"


def test_versions_go_up_by_one_per_change_and_not_at_all_for_nothing(people: People) -> None:
    playlist = make(people.owner, tracks=[301, 302])
    start = version(playlist["id"])
    base = f"/api/v1/playlists/{playlist['id']}"
    first, second = (item["id"] for item in playlist["items"])
    assert people.owner.post(f"{base}/items", json={"tracks": [303]}).json()["version"] == start + 1
    assert people.owner.post(f"{base}/items", json={"tracks": [303]}).json()["version"] == start + 1
    assert people.owner.post(f"{base}/items/move", json={"entry": second, "after": None}).json()["version"] == start + 2
    # Already at the start: nothing to do.
    nothing = people.owner.post(f"{base}/items/move", json={"entry": second, "after": None}).json()
    assert nothing == {**nothing, "version": start + 2, "moved": False, "op": "none"}
    assert people.owner.put(base, json={"name": "Grillabend"}).json()["version"] == start + 2
    assert people.owner.put(base, json={"name": "  Neuer   Name "}).json()["name"] == "Neuer Name"
    assert version(playlist["id"]) == start + 3
    assert people.owner.post(f"{base}/items/remove", json={"entries": [first]}).json()["version"] == start + 4
    assert people.owner.post(f"{base}/items/remove", json={"entries": [first]}).json()["version"] == start + 4


def test_moving_by_entry_and_a_move_of_something_gone_does_nothing(people: People) -> None:
    playlist = make(people.owner, tracks=[301, 302, 303, 304])
    base = f"/api/v1/playlists/{playlist['id']}"
    a, b, c, d = (item["id"] for item in playlist["items"])
    moved = people.owner.post(f"{base}/items/move", json={"entry": a, "after": c}).json()
    assert moved["order"] == [b, c, a, d] and moved["moved"] is True
    assert people.owner.post(f"{base}/items/move", json={"entry": d, "after": None}).json()["order"] == [d, b, c, a]
    people.owner.post(f"{base}/items/remove", json={"entries": [c]})
    # Moved after an entry another person took out meanwhile, or the entry itself gone: no error, no change.
    assert people.owner.post(f"{base}/items/move", json={"entry": a, "after": c}).json()["moved"] is False
    assert people.owner.post(f"{base}/items/move", json={"entry": c, "after": None}).json()["moved"] is False
    assert people.owner.post(f"{base}/items/move", json={"entry": a, "after": a}).json()["moved"] is False
    assert entries(people.owner, playlist["id"]) == [304, 302, 301]


def test_the_undo_brings_an_entry_back_where_it_was_with_who_added_it(people: People, shared: dict[str, Any]) -> None:
    base = f"/api/v1/playlists/{shared['id']}"
    people.editor.post(f"{base}/items", json={"tracks": [313]})
    items = people.owner.get(base).json()["items"]
    middle = items[1]
    people.owner.post(f"{base}/items/remove", json={"entries": [middle["id"]]})
    back = people.owner.post(f"{base}/items/restore", json={"entries": [middle["id"]], "after": items[0]["id"]}).json()
    assert back["op"] == "restore" and back["order"] == [item["id"] for item in items]
    edith_s = items[3]
    people.owner.post(f"{base}/items/remove", json={"entries": [edith_s["id"]]})
    people.owner.post(f"{base}/items/restore", json={"entries": [edith_s["id"]], "after": items[2]["id"]})
    restored = people.reader.get(base).json()["items"][3]
    assert restored["id"] == edith_s["id"] and restored["added_by_name"] == "edith"


def test_the_undo_after_an_entry_gone_meanwhile_uses_the_old_place(people: People) -> None:
    playlist = make(people.owner, tracks=[301, 302, 303, 304])
    base = f"/api/v1/playlists/{playlist['id']}"
    _a, b, c, d = (item["id"] for item in playlist["items"])
    people.owner.post(f"{base}/items/remove", json={"entries": [c]})
    people.owner.post(f"{base}/items/remove", json={"entries": [b]})
    # After an entry that is gone: at the number it had (the third), as far as the list reaches now.
    people.owner.post(f"{base}/items/restore", json={"entries": [c], "after": b})
    assert entries(people.owner, playlist["id"]) == [301, 304, 303]
    people.owner.post(f"{base}/items/restore", json={"entries": [b], "after": None})
    assert entries(people.owner, playlist["id"]) == [302, 301, 304, 303]
    assert d


def test_the_undo_leaves_out_a_track_that_is_in_again(people: People) -> None:
    playlist = make(people.owner, tracks=[301, 302])
    base = f"/api/v1/playlists/{playlist['id']}"
    first = playlist["items"][0]["id"]
    people.owner.post(f"{base}/items/remove", json={"entries": [first]})
    people.owner.post(f"{base}/items", json={"tracks": [301]})
    nothing = people.owner.post(f"{base}/items/restore", json={"entries": [first], "after": None}).json()
    assert nothing["op"] == "none"
    assert entries(people.owner, playlist["id"]) == [302, 301]


def test_an_entry_whose_track_went_from_plex_still_says_what_it_was(people: People) -> None:
    playlist = make(people.owner, tracks=[301, 324])
    with SessionLocal() as db:
        db.execute(PlaylistItem.__table__.update().values(track_id=777).where(PlaylistItem.track_id == 324))
        db.commit()
    item = people.owner.get(f"/api/v1/playlists/{playlist['id']}").json()["items"][1]
    assert item["missing"] is True and item["title"] == "Paper Planes" and item["artist"] == "Juno Reyes"
    assert item["duration"] == 180 and item["song"] is None


def test_a_playlist_is_full_at_its_limit(people: People, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playlists, "MAX_ITEMS", 2)
    playlist = make(people.owner, tracks=[301, 302])
    answer = people.owner.post(f"/api/v1/playlists/{playlist['id']}/items", json={"tracks": [303]})
    assert answer.status_code == 409 and answer.json()["detail"]["code"] == "playlist_full"
    assert version(playlist["id"]) == playlist["version"]


def test_names_and_bodies_are_checked(people: People) -> None:
    assert people.owner.post("/api/v1/playlists", json={"name": "   "}).status_code == 422
    assert people.owner.post("/api/v1/playlists", json={"name": "x" * 201}).status_code == 422
    playlist = make(people.owner)
    base = f"/api/v1/playlists/{playlist['id']}"
    assert people.owner.post(f"{base}/items", json={"tracks": []}).status_code == 422
    assert people.owner.post(f"{base}/items", json={"tracks": [0]}).status_code == 422
    assert people.owner.put(f"{base}/shares/{people.reader_row.id}", json={"access": "all"}).status_code == 422


# --- Two at once -----------------------------------------------------------------------------------------------------


def test_two_people_adding_at_the_same_moment_both_get_theirs_in_a_clean_order(people: People,
                                                                               shared: dict[str, Any]) -> None:
    base = f"/api/v1/playlists/{shared['id']}"
    tracks = [track[0] for track in made_up_catalog.TRACKS if track[0] not in (301, 311, 317)]
    halves = [tracks[0::2], tracks[1::2]]
    errors: list[str] = []
    start = threading.Barrier(2)

    def work(browser: TestClient, mine: list[int]) -> None:
        start.wait()
        for track in mine:
            answer = browser.post(f"{base}/items", json={"tracks": [track]})
            if answer.status_code != 200:
                errors.append(answer.text)

    threads = [threading.Thread(target=work, args=(browser, half))
               for browser, half in zip((people.owner, people.editor), halves, strict=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with SessionLocal() as db:
        rows = db.scalars(select(PlaylistItem).where(PlaylistItem.playlist_id == shared["id"],
                                                     PlaylistItem.removed_at.is_(None))).all()
    assert sorted(row.track_id for row in rows) == sorted([301, 311, 317, *tracks])
    assert sorted(row.position for row in rows) == list(range(len(rows)))
    assert version(shared["id"]) == shared["version"] + len(tracks)


def test_the_same_track_added_twice_at_once_is_in_once(people: People, shared: dict[str, Any]) -> None:
    base = f"/api/v1/playlists/{shared['id']}"
    start = threading.Barrier(2)
    answers: list[dict[str, Any]] = []

    def work(browser: TestClient) -> None:
        start.wait()
        answers.append(browser.post(f"{base}/items", json={"tracks": [302]}).json())

    threads = [threading.Thread(target=work, args=(browser,)) for browser in (people.owner, people.editor)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(answer["op"] for answer in answers) == ["add", "none"]
    assert entries(people.owner, shared["id"]).count(302) == 1


def test_a_move_and_a_removal_of_the_same_entry_at_once_leave_a_clean_list(people: People,
                                                                           shared: dict[str, Any]) -> None:
    base = f"/api/v1/playlists/{shared['id']}"
    first, _second, third = (item["id"] for item in shared["items"])
    start = threading.Barrier(2)
    results: list[int] = []

    def mover() -> None:
        start.wait()
        results.append(people.owner.post(f"{base}/items/move", json={"entry": first, "after": third}).status_code)

    def remover() -> None:
        start.wait()
        results.append(people.editor.post(f"{base}/items/remove", json={"entries": [first]}).status_code)

    threads = [threading.Thread(target=mover), threading.Thread(target=remover)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [200, 200]
    assert entries(people.owner, shared["id"]) == [311, 317]


# --- The trash -------------------------------------------------------------------------------------------------------


def test_the_trash_keeps_it_thirty_days_for_the_owner_and_then_it_is_gone(people: People, shared: dict[str, Any],
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    now = clock.now()
    monkeypatch.setattr(clock, "now", lambda: now)
    path = f"/api/v1/playlists/{shared['id']}"
    card = people.owner.delete(path).json()
    assert card["deleted_at"] is not None and card["purge_at"] == (now + timedelta(days=30)).isoformat()
    assert card["days_left"] == 30
    assert people.owner.get("/api/v1/playlists").json()["playlists"] == []
    assert [one["id"] for one in people.owner.get("/api/v1/playlists", params={"trash": "true"}).json()["playlists"]] \
        == [shared["id"]]
    # Shared ones in the trash are gone for the others; the operator sees every trash.
    assert people.editor.get(path).status_code == 404
    assert people.editor.get("/api/v1/playlists", params={"trash": "true"}).json()["playlists"] == []
    assert len(people.operator.get("/api/v1/playlists", params={"trash": "true"}).json()["playlists"]) == 1
    # In the trash nothing changes.
    answer = people.owner.post(f"{path}/items", json={"tracks": [302]})
    assert answer.status_code == 409 and answer.json()["detail"]["code"] == "in_trash"
    monkeypatch.setattr(clock, "now", lambda: now + timedelta(days=29, hours=23))
    assert [one["days_left"] for one in
            people.owner.get("/api/v1/playlists", params={"trash": "true"}).json()["playlists"]] == [1]
    monkeypatch.setattr(clock, "now", lambda: now + timedelta(days=30))
    assert people.owner.get("/api/v1/playlists", params={"trash": "true"}).json()["playlists"] == []
    assert people.owner.get(path).status_code == 404


def test_brought_back_from_the_trash_it_is_as_it_was(people: People, shared: dict[str, Any]) -> None:
    path = f"/api/v1/playlists/{shared['id']}"
    people.owner.delete(path)
    back = people.owner.post(f"{path}/restore").json()
    assert back["deleted_at"] is None and [item["track"] for item in back["items"]] == [301, 311, 317]
    assert people.editor.get(path).status_code == 200


def test_entries_taken_out_are_kept_a_day_for_the_undo(people: People, monkeypatch: pytest.MonkeyPatch) -> None:
    playlist = make(people.owner, tracks=[301, 302])
    first = playlist["items"][0]["id"]
    people.owner.post(f"/api/v1/playlists/{playlist['id']}/items/remove", json={"entries": [first]})
    later = clock.now() + timedelta(days=1, seconds=1)
    with SessionLocal() as db:
        playlists.purge(db, later - timedelta(seconds=2))
        assert db.get(PlaylistItem, first) is not None
        playlists.purge(db, later)
        assert db.get(PlaylistItem, first) is None


# --- Shares ----------------------------------------------------------------------------------------------------------


def test_sharing_lists_members_only_and_never_the_owner_or_the_operator(people: People,
                                                                        shared: dict[str, Any]) -> None:
    view = people.owner.get(f"/api/v1/playlists/{shared['id']}/shares").json()
    assert {(one["person"]["name"], one["access"]) for one in view["shares"]} == {("rafael", "read"),
                                                                                    ("edith", "edit")}
    assert {one["name"] for one in view["people"]} == {"rafael", "edith", "stella"}
    for other in (people.owner_row, people.operator_row):
        answer = people.owner.put(f"/api/v1/playlists/{shared['id']}/shares/{other.id}", json={"access": "read"})
        assert answer.status_code == 422 and answer.json()["detail"]["code"] == "share_needless"
    assert people.owner.put(f"/api/v1/playlists/{shared['id']}/shares/99999", json={"access": "read"}).status_code \
        == 404


def test_a_share_changed_or_taken_back_counts_at_once(people: People, shared: dict[str, Any]) -> None:
    base = f"/api/v1/playlists/{shared['id']}"
    assert people.reader.post(f"{base}/items", json={"tracks": [302]}).status_code == 403
    people.owner.put(f"{base}/shares/{people.reader_row.id}", json={"access": "edit"})
    assert people.reader.post(f"{base}/items", json={"tracks": [302]}).status_code == 200
    view = people.owner.delete(f"{base}/shares/{people.reader_row.id}").json()
    assert "rafael" not in {one["person"]["name"] for one in view["shares"]}
    assert people.reader.get(base).status_code == 404


def test_an_owner_whose_account_is_gone_leaves_the_playlist_to_the_operator(people: People,
                                                                            shared: dict[str, Any]) -> None:
    with SessionLocal() as db:
        db.delete(db.get(Account, people.owner_row.id))
        db.commit()
    card = people.operator.get(f"/api/v1/playlists/{shared['id']}").json()
    assert card["owner"] is None and card["access"] == "operator"
    assert people.editor.get(f"/api/v1/playlists/{shared['id']}").json()["items"][0]["added_by_name"] == "olivia"


# --- What the cards show ---------------------------------------------------------------------------------------------


def test_the_summary_has_count_length_covers_people_and_moods(people: People, shared: dict[str, Any]) -> None:
    base = f"/api/v1/playlists/{shared['id']}"
    people.editor.post(f"{base}/items", json={"tracks": [302, 313, 324, 326]})
    view = people.owner.get(base).json()
    assert view["count"] == 7
    assert view["duration"] == 245 + 214 + 205 + 201 + 233 + 180 + 400
    # The first four albums with a cover (the compilation has none), in the order of the list.
    assert view["covers"] == [201, 205, 207, 210]
    assert [one["name"] for one in view["people"]] == ["olivia", "edith"] and view["contributors"] == 2
    # Summed over the entries, each track's own mood 1, the album's 0.7, the artist's 0.4.
    # Summery: 1 + 1 (own) = 2; Romantic: 0.7 (album) twice = 1.4; Brooding only 0.4 (artist) twice.
    assert view["moods"][0] == "Summery" and "Romantic" in view["moods"] and "Brooding" not in view["moods"]
    assert len(view["moods"]) == 6
    card = next(one for one in people.owner.get("/api/v1/playlists").json()["playlists"])
    assert card["count"] == 7 and card["covers"] == view["covers"] and "items" not in card
