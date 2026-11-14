"""Guests: the invitation of a playlist (who may make, start, change and end it), joining by the code with a name, the
guest cookie and its flags, what a guest sees and may do (its limit, also with ten requests at once), taking a guest
out, the end of an invitation, guessing codes, flooding an invitation, the live events and the tidy-up (listening in:
``test_guest_listening.py``)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import OPERATOR, Guest, GuestEntry, GuestInvite, PlaylistItem
from app.routers import guest as guest_router
from app.services import guests, settings_service

from . import made_up_catalog
from .conftest import TAB, make_account, new_client


@pytest.fixture(autouse=True)
def catalog_in() -> None:
    made_up_catalog.install()


class Host:
    """The owner ``olivia`` of a playlist with three tracks, shared with ``rafael`` to see and ``edith`` to change, and
    the operator ``otto``."""

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
        made = self.owner.post("/api/v1/playlists", json={"name": "Grillabend"}).json()
        self.id = made["id"]
        assert self.owner.post(f"{self.base}/items", json={"tracks": [301, 311, 317]}).status_code == 200
        for row, access in ((self.reader_row, "read"), (self.editor_row, "edit")):
            assert self.owner.put(f"{self.base}/shares/{row.id}", json={"access": access}).status_code == 200

    @property
    def base(self) -> str:
        return f"/api/v1/playlists/{self.id}"

    def invite(self, **values: Any) -> dict[str, Any]:
        """A running invitation, with ``values`` for its rights."""
        assert self.owner.post(f"{self.base}/invite").status_code == 200
        answer = self.owner.put(f"{self.base}/invite", json={**values, "start": True})
        assert answer.status_code == 200, answer.text
        return answer.json()

    def close(self) -> None:
        for one in (self.owner, self.reader, self.editor, self.stranger, self.operator):
            one.close()


@pytest.fixture
def host() -> Iterator[Host]:
    found = Host()
    yield found
    found.close()


def browser(base_url: str = "http://testserver", peer: str = "testclient", **headers: str) -> TestClient:
    """A phone: no account, no cookie."""
    return TestClient(app, base_url=base_url, headers={**TAB, **headers}, client=(peer, 50000))


def code_of(view: dict[str, Any]) -> str:
    return str(view["invite"]["code"])


def joined(code: str, name: str = "Lena", **kwargs: Any) -> TestClient:
    phone = browser(**kwargs)
    answer = phone.post(f"/api/v1/join/{code}", json={"name": name})
    assert answer.status_code == 200, answer.text
    return phone


def items_of(host: Host) -> list[dict[str, Any]]:
    return host.owner.get(host.base).json()["items"]


# --- The invitation, for the people who invite --------------------------------------------------------------------


def test_an_invitation_waits_until_it_is_started_and_then_lasts_its_hours(host: Host) -> None:
    empty = host.owner.get(f"{host.base}/invite").json()
    assert empty["invite"] is None and empty["guests"] == []
    assert empty["defaults"] == {"may_add": True, "may_sort": False, "max": 10, "hours": 24, "preview": True}
    waiting = host.owner.post(f"{host.base}/invite").json()["invite"]
    assert waiting["state"] == "waiting" and waiting["expires_at"] is None
    code = waiting["code"]
    assert len(code) == 14 and code[4] == code[9] == "-"
    assert len(code.replace("-", "")) == 12 and all(ch in guests.ALPHABET for ch in code.replace("-", ""))
    assert waiting["link"] == f"http://testserver/j/{code}"
    assert waiting["qr"].startswith("<svg") and 'xmlns="http://www.w3.org/2000/svg"' in waiting["qr"]
    # Made a second time: the same one.
    assert host.owner.post(f"{host.base}/invite").json()["invite"]["code"] == code
    with browser() as phone:
        assert phone.get(f"/api/v1/join/{code}").status_code == 404, "a waiting invitation lets nobody in"
    started = host.owner.put(f"{host.base}/invite", json={"start": True, "hours": 3}).json()["invite"]
    assert started["state"] == "running" and started["hours"] == 3
    begun = clock.now()
    ends = started["expires_at"]
    from datetime import datetime

    assert abs((datetime.fromisoformat(ends) - begun) - timedelta(hours=3)) < timedelta(seconds=30)
    with browser() as phone:
        door = phone.get(f"/api/v1/join/{code.lower().replace('-', ' ')}").json()
    assert door["playlist"]["name"] == "Grillabend" and door["playlist"]["count"] == 3
    assert door["inviter"] == "", "olivia has no display name; her name to sign in with stays home"
    assert door["joined"] is False


def test_a_code_has_twelve_characters_without_ones_to_mix_up_and_reads_in_groups_of_four() -> None:
    assert guests.CODE_LENGTH == 12 and len(guests.ALPHABET) == 31
    assert not set("01ILO") & set(guests.ALPHABET)
    made = {guests.new_code() for _ in range(200)}
    assert len(made) == 200 and all(len(code) == 12 and guests.well_formed(code) for code in made)
    code = "K7MXQR2HPA4N"
    assert guests.shown(code) == "K7MX-QR2H-PA4N"
    assert guests.normalise(" k7mx-qr2h pa4n ") == code and guests.well_formed(guests.normalise(guests.shown(code)))
    assert not guests.well_formed("K7MXQR2HPA"), "ten characters are too few"
    assert not guests.well_formed("K7MXQR2HPA4N7"), "thirteen too many"
    assert not guests.well_formed("K7MXQR2HPA4O"), "an O is never part of one"


def test_the_defaults_of_the_settings_card_go_into_a_new_invitation(host: Host) -> None:
    host.operator.put("/api/v1/settings", json={"guest_preview": False, "guest_max": 4, "guest_hours": 6})
    settings = host.operator.get("/api/v1/settings").json()
    assert (settings["guest_preview"], settings["guest_max"], settings["guest_hours"]) == (False, 4, 6)
    made = host.owner.post(f"{host.base}/invite").json()["invite"]
    assert (made["preview"], made["max"], made["hours"]) == (False, 4, 6)
    assert host.operator.put("/api/v1/settings", json={"guest_max": 0}).status_code == 422
    assert host.operator.put("/api/v1/settings", json={"guest_hours": 721}).status_code == 422
    assert host.owner.put("/api/v1/settings", json={"guest_max": 5}).status_code == 403


#: Who may do what with the invitation: owner, edit and operator yes; read 403; somebody without the playlist 404.
RIGHTS = {"owner": 200, "editor": 200, "operator": 200, "reader": 403, "stranger": 404}


@pytest.mark.parametrize("who", list(RIGHTS))
def test_only_who_may_change_the_playlist_invites(host: Host, who: str) -> None:
    host.owner.post(f"{host.base}/invite")
    client = getattr(host, who)
    want = RIGHTS[who]
    ok = {200: 200, 403: 403, 404: 404}
    assert client.get(f"{host.base}/invite").status_code == ok[want]
    assert client.post(f"{host.base}/invite").status_code == ok[want]
    assert client.put(f"{host.base}/invite", json={"max": 5}).status_code == ok[want]
    assert client.delete(f"{host.base}/invite/guests/1").status_code == (404 if want == 200 else want)
    assert client.delete(f"{host.base}/invite").status_code == (204 if want == 200 else want)


def test_a_playlist_in_the_trash_takes_no_invitation_and_lets_its_guests_go(host: Host) -> None:
    view = host.invite()
    phone = joined(code_of(view))
    assert host.owner.delete(host.base).status_code == 200
    assert host.owner.post(f"{host.base}/invite").status_code == 409
    assert phone.get("/api/v1/guest").json()["detail"]["code"] == "invite_ended"
    assert phone.get(f"/api/v1/join/{code_of(view)}").status_code == 404
    # Brought back, the old invitation stays over.
    assert host.owner.post(f"{host.base}/restore").status_code == 200
    assert phone.get("/api/v1/guest").json()["detail"]["code"] == "invite_ended"
    assert host.owner.get(f"{host.base}/invite").json()["invite"] is None


def test_the_link_warns_when_phones_outside_the_home_network_cannot_reach_it(host: Host) -> None:
    address = host.owner.get(f"{host.base}/invite").json()["address"]
    assert address == {"url": "http://testserver", "configured": False, "reach": "home"}
    with SessionLocal() as db:
        settings_service.save(db, {"public_url": "https://mix.example.com"})
    view = host.owner.post(f"{host.base}/invite").json()
    assert view["address"] == {"url": "https://mix.example.com", "configured": True, "reach": "internet"}
    assert view["invite"]["link"].startswith("https://mix.example.com/j/")
    for url, where in (("http://192.168.1.20:8620", "home"), ("http://mix.example.home", "home"),
                       ("http://nas.local", "home"), ("http://localhost:8620", "machine"),
                       ("http://127.0.0.1:8620", "machine"), ("http://[::1]:8620", "machine"),
                       ("http://10.1.2.3", "home"), ("https://mix.example.org", "internet"), ("http://nas", "home")):
        assert guests.reach(url) == where, url


# --- Joining -------------------------------------------------------------------------------------------------------


def test_joining_gives_a_guest_cookie_that_is_http_only_lax_and_only_for_the_interface(host: Host) -> None:
    view = host.invite()
    with browser() as phone:
        answer = phone.post(f"/api/v1/join/{code_of(view)}", json={"name": "  Lena   Maier "})
        assert answer.status_code == 200 and answer.json() == {"name": "Lena Maier"}
        line = answer.headers["set-cookie"].lower()
        assert line.startswith(guests.GUEST_COOKIE + "=")
        assert "httponly" in line and "samesite=lax" in line and "path=/api/v1" in line
        assert "secure" not in line.replace("samesite", ""), "plain http: no Secure"
        assert "max-age=" in line
        age = int(line.split("max-age=")[1].split(";")[0])
        assert 23 * 3600 < age <= 24 * 3600 + 120, "the cookie ends with the invitation"
        token = answer.cookies[guests.GUEST_COOKIE]
        assert len(token) >= 40
    with SessionLocal() as db:
        stored = db.scalars(select(Guest)).one()
    assert token not in stored.token_hash and len(stored.token_hash) == 64


def secure_join(code: str, base_url: str, headers: dict[str, str], peer: str) -> bool:
    with browser(base_url, peer=peer, **headers) as phone:
        answer = phone.post(f"/api/v1/join/{code}", json={"name": "Tim"})
    assert answer.status_code == 200, answer.text
    return "; secure" in answer.headers["set-cookie"].lower()


def test_the_guest_cookie_is_secure_over_https_and_behind_a_trusted_proxy_only(host: Host,
                                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    code = code_of(host.invite())
    assert secure_join(code, "https://testserver", {}, "testclient")
    monkeypatch.setattr(get_settings(), "trusted_proxies", "")
    assert not secure_join(code, "http://testserver", {"X-Forwarded-Proto": "https"}, "10.0.0.2"), \
        "no proxy is trusted from the start"
    monkeypatch.setattr(get_settings(), "trusted_proxies", "10.0.0.2")
    assert not secure_join(code, "http://testserver", {"X-Forwarded-Proto": "https"}, "203.0.113.5"), \
        "a stranger's header is not believed"
    assert secure_join(code, "http://testserver", {"X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.4"},
                       "10.0.0.2"), "a trusted proxy that speaks https"


@pytest.mark.parametrize("name", ["", "   ", "x" * 25, "Lena\x00", "Le\u200bna", "Lena\u202e", "Tab\tthere\x07",
                                  "\U000e0041gent"])
def test_a_name_with_control_characters_or_too_long_is_refused(host: Host, name: str) -> None:
    code = code_of(host.invite())
    with browser() as phone:
        answer = phone.post(f"/api/v1/join/{code}", json={"name": name})
    assert answer.status_code == 422, name
    with SessionLocal() as db:
        assert db.scalars(select(Guest)).all() == []


def test_names_are_folded_and_two_guests_of_the_same_name_are_told_apart(host: Host) -> None:
    assert guests.clean_name("  Ana \t María  ") == "Ana María"
    assert guests.clean_name("x" * 24) == "x" * 24
    assert guests.clean_name("Zeile\u2028zwei") == "Zeile zwei", "a line separator is a blank"
    assert guests.clean_name("Jörg 🎉") == "Jörg 🎉"
    code = code_of(host.invite())
    names = [joined(code, name).get("/api/v1/guest").json()["me"]["name"] for name in ("Lena", "lena", "LENA")]
    assert names == ["Lena", "lena 2", "LENA 3"]


def test_the_same_browser_joining_again_stays_the_same_guest(host: Host) -> None:
    code = code_of(host.invite())
    phone = joined(code, "Lena")
    again = phone.post(f"/api/v1/join/{code}", json={"name": "Somebody else"})
    assert again.status_code == 200 and again.json() == {"name": "Lena"} and "set-cookie" not in again.headers
    assert phone.get(f"/api/v1/join/{code}").json()["joined"] is True
    with SessionLocal() as db:
        assert len(db.scalars(select(Guest)).all()) == 1


# --- What a guest sees ------------------------------------------------------------------------------------------------


def walk(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from walk(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from walk(inner)
    else:
        yield str(value)


def test_a_guest_sees_the_playlist_with_the_names_on_its_entries_and_nothing_of_other_accounts(host: Host) -> None:
    from app.models import Account

    with SessionLocal() as db:
        row = db.get(Account, host.owner_row.id)
        assert row is not None
        row.display_name = "Olivia K."
        db.commit()
    # Edith may change the playlist but added nothing; another playlist of Olivia's is not the guest's business.
    other = host.owner.post("/api/v1/playlists", json={"name": "Geheime Liste"}).json()
    host.owner.post(f"/api/v1/playlists/{other['id']}/items", json={"tracks": [302]})
    phone = joined(code_of(host.invite()))
    seen = phone.get("/api/v1/guest").json()
    assert seen["playlist"]["name"] == "Grillabend" and seen["playlist"]["inviter"] == "Olivia K."
    assert [entry["track"] for entry in seen["playlist"]["items"]] == [301, 311, 317]
    assert {entry["by"] for entry in seen["playlist"]["items"]} == {"Olivia K."}
    assert seen["me"] == {"id": seen["me"]["id"], "name": "Lena", "count": 0}
    assert seen["invite"]["may_add"] is True and seen["invite"]["max"] == 10
    words = " ".join(walk(seen))
    for secret in ("olivia", "edith", "rafael", "otto", "stella", "Geheime", "owner", "email", "role", "avatar"):
        assert secret not in words, secret
    assert set(seen["playlist"]["items"][0]) == {"id", "track", "title", "artist", "album", "album_id", "cover",
                                                 "year", "duration", "song", "missing", "by", "mine"}


def test_the_guest_reaches_the_catalogue_with_its_etag_and_the_covers(host: Host,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import Response

    from app.routers import catalog as catalog_routes

    phone = joined(code_of(host.invite()))
    first = phone.get("/api/v1/guest/catalog")
    assert first.status_code == 200 and first.headers["etag"]
    assert phone.get("/api/v1/guest/catalog", headers={"If-None-Match": first.headers["etag"]}).status_code == 304
    found = phone.get("/api/v1/guest/catalog/search", params={"q": "northern"}).json()
    assert found["tracks"] and found["tracks"][0]["title"] == "Northern Lights"
    seen: list[int] = []

    async def fake_cover(_request: Any, album_id: int, _db: Any, _size: int) -> Response:
        seen.append(album_id)
        return Response(status_code=204)

    monkeypatch.setattr(catalog_routes, "serve_cover", fake_cover)
    assert phone.get("/api/v1/guest/cover/201?s=80").status_code == 204 and seen == [201]
    with browser() as stranger:
        assert stranger.get("/api/v1/guest/catalog").status_code == 401
        assert stranger.get("/api/v1/guest/cover/201").status_code == 401


def test_the_guest_gets_suggestions_from_its_playlist_only(host: Host) -> None:
    """The server's suggestions for a guest (a catalogue too large for the browser): the editor's rules seeded from the
    last tracks of the playlist, none of its songs, and no "often together" from the household's other playlists."""
    from app.services import suggest, suggestions

    # Another playlist of the household with the same songs and one more: "often together" for an account.
    other = host.owner.post("/api/v1/playlists", json={"name": "Nebenan"}).json()["id"]
    assert host.owner.post(f"/api/v1/playlists/{other}/items", json={"tracks": [301, 311, 317, 302]}).status_code == 200
    account = host.owner.get("/api/v1/suggestions", params={"playlist": host.id, "limit": 50}).json()["tracks"]
    assert any(one["reason"]["together"] for one in account), "the account hears the other playlist"
    phone = joined(code_of(host.invite()))
    answer = phone.get("/api/v1/guest/suggestions")
    assert answer.status_code == 200, answer.text
    found = answer.json()
    assert found["seed"] == [301, 311, 317]
    assert found["tracks"] and len(found["tracks"]) <= suggestions.GUEST_LIMIT
    for one in found["tracks"]:
        assert one["reason"]["together"] == 0 and one["reason"]["sound"] is False
        assert one["id"] not in (301, 311, 317)
    with SessionLocal() as db:
        ix = suggestions.view_of(db).index
        positions = suggest.seed_positions(ix, [301, 311, 317])
        wanted = suggest.suggest(ix, positions, mode="fit", exclude={ix.song[p] for p in positions},
                                 limit=suggestions.GUEST_LIMIT)
    assert [one["id"] for one in found["tracks"]] == [one["id"] for one in wanted]
    with browser() as stranger:
        assert stranger.get("/api/v1/guest/suggestions").status_code == 401
    host.owner.delete(f"{host.base}/invite")
    assert phone.get("/api/v1/guest/suggestions").status_code == 403


def test_the_guest_is_never_suggested_a_song_of_its_list_beyond_the_seed(host: Host) -> None:
    """301 fits the last five tracks of this list best of all, and a live version of it (308) stands first in the list,
    outside the seed: the song is in the list, so it is not suggested."""
    made = host.owner.post("/api/v1/playlists", json={"name": "Tanzfläche"}).json()["id"]
    base = f"/api/v1/playlists/{made}"
    tracks = [308, 302, 303, 304, 307, 311]
    assert host.owner.post(f"{base}/items", json={"tracks": tracks}).json()["entries"]
    host.owner.post(f"{base}/invite")
    code = host.owner.put(f"{base}/invite", json={"start": True}).json()["invite"]["code"]
    found = joined(code).get("/api/v1/guest/suggestions").json()
    assert found["seed"] == tracks[1:]
    assert found["tracks"], "the seed has fitting songs"
    assert 301 not in [one["id"] for one in found["tracks"]]


def test_the_page_of_the_link_shows_only_the_covers_of_the_playlist(host: Host,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import Response

    from app.routers import catalog as catalog_routes

    code = code_of(host.invite())

    async def no_cover(*_args: object) -> Response:
        return Response(status_code=204)

    monkeypatch.setattr(catalog_routes, "serve_cover", no_cover)
    with browser() as phone:
        covers = phone.get(f"/api/v1/join/{code}").json()["playlist"]["covers"]
        assert covers, "the made-up playlist has covers"
        assert phone.get(f"/api/v1/join/{code}/cover/{covers[0]}").status_code == 204
        assert phone.get(f"/api/v1/join/{code}/cover/210").status_code == 404, "not in the playlist"
        assert phone.get("/api/v1/join/AAAAAAAAAAAA/cover/201").status_code == 404


# --- What a guest does ------------------------------------------------------------------------------------------------


def test_a_guest_adds_within_its_limit_and_the_host_sees_its_name_at_the_entry(host: Host) -> None:
    phone = joined(code_of(host.invite(max=2)))
    added = phone.post("/api/v1/guest/items", json={"tracks": [302]}).json()
    assert added["op"] == "add" and added["state"]["me"]["count"] == 1
    assert added["state"]["playlist"]["items"][-1]["mine"] is True
    assert phone.post("/api/v1/guest/items", json={"tracks": [302]}).json()["op"] == "none", "already in"
    assert phone.post("/api/v1/guest/items", json={"tracks": [999999]}).json()["skipped"] == [999999]
    over = phone.post("/api/v1/guest/items", json={"tracks": [303, 304]})
    assert over.status_code == 409 and over.json()["detail"]["code"] == "guest_limit", "all or nothing"
    assert phone.post("/api/v1/guest/items", json={"tracks": [303]}).status_code == 200
    assert phone.post("/api/v1/guest/items", json={"tracks": [304]}).json()["detail"]["code"] == "guest_limit"
    last = items_of(host)[-1]
    assert last["added_by"] is None and last["added_by_name"] == "Lena"
    invite = host.owner.get(f"{host.base}/invite").json()
    assert invite["guests"][0]["name"] == "Lena" and invite["guests"][0]["count"] == 2
    # Taking one out frees a place again.
    mine = [entry["id"] for entry in phone.get("/api/v1/guest").json()["playlist"]["items"] if entry["mine"]]
    assert phone.post("/api/v1/guest/items/remove", json={"entries": [mine[0]]}).status_code == 200
    assert phone.post("/api/v1/guest/items", json={"tracks": [304]}).status_code == 200


def test_ten_requests_at_once_never_pass_the_limit(host: Host) -> None:
    phone = joined(code_of(host.invite(max=3)))
    token = phone.cookies[guests.GUEST_COOKIE]
    tracks = [302, 303, 304, 305, 306, 307, 308, 309, 310, 312]
    start = threading.Barrier(len(tracks))
    answers: list[int] = []

    def work(track: int) -> None:
        with browser() as same:
            same.cookies.set(guests.GUEST_COOKIE, token, path="/api/v1")
            start.wait()
            answers.append(same.post("/api/v1/guest/items", json={"tracks": [track]}).status_code)

    threads = [threading.Thread(target=work, args=(track,)) for track in tracks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(answers) == [200] * 3 + [409] * 7
    with SessionLocal() as db:
        assert len(db.scalars(select(GuestEntry)).all()) == 3
        rows = db.scalars(select(PlaylistItem).where(PlaylistItem.removed_at.is_(None))).all()
    assert sorted(row.position for row in rows) == list(range(len(rows)))


def test_without_the_right_a_guest_takes_out_only_its_own_and_does_not_sort(host: Host) -> None:
    code = code_of(host.invite(may_sort=False))
    lena, tim = joined(code, "Lena"), joined(code, "Tim")
    tim.post("/api/v1/guest/items", json={"tracks": [302]})
    entries = items_of(host)
    host_entry, tims = entries[0]["id"], entries[-1]["id"]
    for target in (host_entry, tims):
        refused = lena.post("/api/v1/guest/items/remove", json={"entries": [target]})
        assert refused.status_code == 403 and refused.json()["detail"]["code"] == "guest_not_yours"
    moved = lena.post("/api/v1/guest/items/move", json={"entry": tims, "after": None})
    assert moved.status_code == 403 and moved.json()["detail"]["code"] == "guest_no_sorting"
    assert [entry["id"] for entry in items_of(host)] == [entry["id"] for entry in entries]
    taken = tim.post("/api/v1/guest/items/remove", json={"entries": [tims]})
    assert taken.status_code == 200 and taken.json()["removed"] == [tims]
    back = tim.post("/api/v1/guest/items/restore", json={"entries": [tims], "after": host_entry})
    assert back.status_code == 200 and [entry["id"] for entry in items_of(host)][1] == tims
    # Bringing back somebody else's needs the right too.
    host.owner.post(f"{host.base}/items/remove", json={"entries": [host_entry]})
    assert lena.post("/api/v1/guest/items/restore", json={"entries": [host_entry]}).status_code == 403


def test_with_the_right_a_guest_sorts_and_takes_out_the_tracks_of_others(host: Host) -> None:
    phone = joined(code_of(host.invite(may_sort=True)))
    entries = [entry["id"] for entry in items_of(host)]
    moved = phone.post("/api/v1/guest/items/move", json={"entry": entries[2], "after": None}).json()
    assert moved["moved"] is True
    assert [entry["id"] for entry in items_of(host)] == [entries[2], entries[0], entries[1]]
    assert phone.post("/api/v1/guest/items/remove", json={"entries": [entries[0]]}).status_code == 200
    assert [entry["id"] for entry in items_of(host)] == [entries[2], entries[1]]


def test_an_invitation_without_adding_lets_the_guest_look_only(host: Host) -> None:
    phone = joined(code_of(host.invite(may_add=False)))
    refused = phone.post("/api/v1/guest/items", json={"tracks": [302]})
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "guest_no_adding"
    assert phone.get("/api/v1/guest").json()["invite"]["may_add"] is False


def test_a_change_of_the_rights_counts_for_the_guests_at_once(host: Host) -> None:
    phone = joined(code_of(host.invite(may_add=True)))
    host.owner.put(f"{host.base}/invite", json={"may_add": False})
    assert phone.post("/api/v1/guest/items", json={"tracks": [302]}).status_code == 403
    host.owner.put(f"{host.base}/invite", json={"may_add": True, "max": 1})
    assert phone.post("/api/v1/guest/items", json={"tracks": [302]}).status_code == 200
    assert phone.post("/api/v1/guest/items", json={"tracks": [303]}).status_code == 409


# --- Out: taken out, ended, run out ----------------------------------------------------------------------------------


def test_a_guest_taken_out_is_out_at_once_and_stays_out_its_tracks_stay(host: Host) -> None:
    code = code_of(host.invite())
    phone = joined(code)
    phone.post("/api/v1/guest/items", json={"tracks": [302]})
    guest_id = host.owner.get(f"{host.base}/invite").json()["guests"][0]["id"]
    assert host.editor.delete(f"{host.base}/invite/guests/{guest_id}").status_code == 204
    for method, path in (("GET", "/api/v1/guest"), ("POST", "/api/v1/guest/items"), ("GET", "/api/v1/guest/catalog")):
        answer = phone.request(method, path, json={"tracks": [303]} if method == "POST" else None)
        assert answer.status_code == 403 and answer.json()["detail"]["code"] == "guest_removed", path
    again = phone.post(f"/api/v1/join/{code}", json={"name": "Lena"})
    assert again.status_code == 403 and again.json()["detail"]["code"] == "guest_removed"
    assert items_of(host)[-1]["added_by_name"] == "Lena"
    assert host.owner.get(f"{host.base}/invite").json()["guests"] == []
    # Another phone comes in as somebody new: without accounts nothing else tells people apart.
    assert joined(code, "Lena").get("/api/v1/guest").status_code == 200


def test_ending_the_invitation_puts_every_guest_out_and_the_code_leads_nowhere(host: Host) -> None:
    code = code_of(host.invite())
    phones = [joined(code, name) for name in ("Lena", "Tim")]
    assert host.owner.delete(f"{host.base}/invite").status_code == 204
    for phone in phones:
        answer = phone.get("/api/v1/guest")
        assert answer.status_code == 403 and answer.json()["detail"]["code"] == "invite_ended"
        assert phone.post("/api/v1/guest/items", json={"tracks": [302]}).status_code == 403
    with browser() as late:
        assert late.get(f"/api/v1/join/{code}").status_code == 404
        assert late.post(f"/api/v1/join/{code}", json={"name": "Mia"}).status_code == 404
    # The next invitation has another code.
    assert code_of(host.invite()) != code


def test_an_invitation_that_ran_out_is_over_and_the_tidy_up_takes_its_guests(host: Host) -> None:
    code = code_of(host.invite())
    phone = joined(code)
    phone.post("/api/v1/guest/items", json={"tracks": [302]})
    with SessionLocal() as db:
        invite = db.scalars(select(GuestInvite)).one()
        invite.expires_at = clock.now() - timedelta(seconds=1)
        db.commit()
    assert phone.get("/api/v1/guest").json()["detail"]["code"] == "invite_ended"
    assert host.owner.get(f"{host.base}/invite").json()["invite"] is None
    with browser() as late:
        assert late.get(f"/api/v1/join/{code}").status_code == 404
    with SessionLocal() as db:
        assert guests.purge(db) == 1
        assert db.scalars(select(Guest)).all() == [] and db.scalars(select(GuestEntry)).all() == []
    assert items_of(host)[-1]["added_by_name"] == "Lena", "the name stays on the entry"
    assert phone.get("/api/v1/guest").json()["detail"]["code"] == "guest_required", "the cookie is worth nothing"


def test_the_tidy_up_ends_running_out_invitations_and_drops_old_waiting_ones(host: Host) -> None:
    host.owner.post(f"{host.base}/invite")
    with SessionLocal() as db:
        assert guests.purge(db) == 0
        assert guests.purge(db, clock.now() + timedelta(days=guests.WAITING_DAYS, minutes=1)) == 1
        assert db.scalars(select(GuestInvite)).all() == []
    host.invite(hours=1)
    with SessionLocal() as db:
        assert guests.purge(db, clock.now() + timedelta(minutes=30)) == 0
        assert guests.purge(db, clock.now() + timedelta(hours=1, minutes=1)) == 1


def test_fewer_hours_than_have_passed_end_the_invitation(host: Host) -> None:
    phone = joined(code_of(host.invite(hours=2)))
    with SessionLocal() as db:
        invite = db.scalars(select(GuestInvite)).one()
        assert invite.started_at is not None
        invite.started_at = invite.started_at - timedelta(hours=1, minutes=30)
        db.commit()
    host.owner.put(f"{host.base}/invite", json={"hours": 1})
    assert phone.get("/api/v1/guest").json()["detail"]["code"] == "invite_ended"


# --- Guest A and invitation B ----------------------------------------------------------------------------------------


def test_a_guest_of_one_invitation_can_do_nothing_with_another(host: Host) -> None:
    phone = joined(code_of(host.invite(may_sort=True)))
    other = host.owner.post("/api/v1/playlists", json={"name": "Andere Party"}).json()
    other_base = f"/api/v1/playlists/{other['id']}"
    host.owner.post(f"{other_base}/items", json={"tracks": [320, 321]})
    host.owner.post(f"{other_base}/invite")
    host.owner.put(f"{other_base}/invite", json={"start": True})
    theirs = [entry["id"] for entry in host.owner.get(other_base).json()["items"]]
    assert phone.post("/api/v1/guest/items/remove", json={"entries": theirs}).json()["op"] == "none"
    assert phone.post("/api/v1/guest/items/move", json={"entry": theirs[1], "after": None}).json()["op"] == "none"
    assert [entry["id"] for entry in host.owner.get(other_base).json()["items"]] == theirs
    # Host routes of either playlist: the guest cookie is no account.
    for path in (other_base, f"{other_base}/invite", host.base, f"{host.base}/invite"):
        assert phone.get(path).status_code == 401, path
    other_guest = joined(host.owner.get(f"{other_base}/invite").json()["invite"]["code"], "Tim")
    tims = other_guest.get("/api/v1/guest").json()["me"]["id"]
    assert phone.delete(f"{host.base}/invite/guests/{tims}").status_code == 401
    assert host.owner.delete(f"{host.base}/invite/guests/{tims}").status_code == 404, "not a guest of this one"


def test_a_guest_of_one_invitation_joining_another_becomes_a_guest_of_that_one(host: Host) -> None:
    phone = joined(code_of(host.invite()), "Lena")
    other = host.owner.post("/api/v1/playlists", json={"name": "Andere Party"}).json()
    other_base = f"/api/v1/playlists/{other['id']}"
    host.owner.post(f"{other_base}/invite")
    second = host.owner.put(f"{other_base}/invite", json={"start": True}).json()
    door = phone.get(f"/api/v1/join/{code_of(second)}").json()
    assert door["joined"] is False and door["name"] is None, "the cookie of the first one is no guest of this one"
    answer = phone.post(f"/api/v1/join/{code_of(second)}", json={"name": "Lena"})
    assert answer.status_code == 200 and guests.GUEST_COOKIE in answer.headers.get("set-cookie", "")
    assert phone.get("/api/v1/guest").json()["playlist"]["name"] == "Andere Party"
    assert [guest["name"] for guest in host.owner.get(f"{other_base}/invite").json()["guests"]] == ["Lena"]


# --- Every route ----------------------------------------------------------------------------------------------------


def _routes() -> list[tuple[str, str]]:
    from .test_routes import api_routes

    return api_routes()


def test_the_guest_cookie_opens_no_route_but_the_guest_routes(host: Host) -> None:
    from .test_routes import OPEN, concrete

    phone = joined(code_of(host.invite()))
    wrong: list[str] = []
    for method, path in _routes():
        # Listening in is the one route of its own a guest reaches (``test_guest_listening.py``).
        if (method, path) in OPEN or path.startswith(("/api/v1/guest", "/api/v1/join", "/api/v1/preview")):
            continue
        answer = phone.request(method, concrete(path), json={} if method in ("POST", "PUT", "PATCH") else None)
        if answer.status_code not in (401, 403):
            wrong.append(f"{method} {path} -> {answer.status_code}")
    assert wrong == []


def test_the_guest_routes_answer_401_without_a_guest_cookie_also_to_an_account(host: Host) -> None:
    guest_routes = [(method, path) for method, path in _routes() if path.startswith("/api/v1/guest")]
    assert len(guest_routes) >= 9
    with browser() as nobody:
        for client in (nobody, host.owner, host.operator):
            for method, path in guest_routes:
                answer = client.request(method, path.replace("{album_id}", "201"),
                                        json={"tracks": [302]} if method == "POST" else None)
                assert answer.status_code == 401, f"{method} {path}"
                assert answer.json()["detail"]["code"] == "guest_required"


def test_a_made_up_guest_cookie_is_nobody(host: Host) -> None:
    host.invite()
    with browser() as phone:
        phone.cookies.set(guests.GUEST_COOKIE, "x" * 43, path="/api/v1")
        assert phone.get("/api/v1/guest").status_code == 401
        phone.cookies.set(guests.GUEST_COOKIE, "y" * 500, path="/api/v1")
        assert phone.get("/api/v1/guest").status_code == 401


def test_changes_of_a_guest_need_the_tab_header_like_any_other(host: Host) -> None:
    phone = joined(code_of(host.invite()))
    token = phone.cookies[guests.GUEST_COOKIE]
    with TestClient(app, base_url="http://testserver") as bare:
        bare.cookies.set(guests.GUEST_COOKIE, token, path="/api/v1")
        assert bare.post("/api/v1/guest/items", json={"tracks": [302]}).status_code == 400
        assert bare.post(f"/api/v1/join/{code_of(host.owner.get(f'{host.base}/invite').json())}",
                         json={"name": "Mia"}).status_code == 400


def test_the_code_of_an_invitation_never_stands_in_the_log(host: Host) -> None:
    from app.services import logs

    assert logs.redact("GET /api/v1/join/K7MX-QR2H-PA4N/cover/201 -> 200") == "GET /api/v1/join/…/cover/201 -> 200"
    assert logs.redact("POST /api/v1/join/k7mxqr2hpa -> 200") == "POST /api/v1/join/… -> 200"
    assert logs.redact("GET /j/K7MX-QR2H-PA4N") == "GET /j/…"
    # The request line of a slow answer goes through the same filter as every record.
    code = code_of(host.invite())
    import logging

    seen: list[str] = []

    class Catch(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    catch = Catch(level=logging.DEBUG)
    # As the log's own handlers do: every record through the filter that writes it as one masked line.
    catch.addFilter(logs._ContextFilter())
    api = logging.getLogger("nexmix.api")
    before = api.level
    api.addHandler(catch)
    api.setLevel(logging.DEBUG)
    try:
        with browser() as phone:
            phone.get(f"/api/v1/join/{code}")
    finally:
        api.removeHandler(catch)
        api.setLevel(before)
    assert any("/api/v1/join/" in line for line in seen)
    assert not any(code in line or code.replace("-", "") in line for line in seen)


# --- Brakes ----------------------------------------------------------------------------------------------------------


def test_guessing_codes_is_braked_per_address_but_the_right_code_still_leads_in(host: Host) -> None:
    code = code_of(host.invite())
    with browser(peer="198.51.100.7") as guesser:
        for number in range(guests.MISS.limit):
            assert guesser.get(f"/api/v1/join/{'A' * 9}{guests.ALPHABET[number % 31]}").status_code == 404
        braked = guesser.get(f"/api/v1/join/{'B' * 10}")
        assert braked.status_code == 429 and int(braked.headers["retry-after"]) > 0
        # Whoever shares the address with a guesser (a party, a carrier) is not kept out.
        assert guesser.get(f"/api/v1/join/{code}").status_code == 200
        assert guesser.post(f"/api/v1/join/{code}", json={"name": "Mia"}).status_code == 200
    with browser(peer="198.51.100.8") as neighbour:
        assert neighbour.get(f"/api/v1/join/{'B' * 10}").status_code == 404, "another address is not braked"


def test_a_flood_of_names_at_one_invitation_is_braked(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    code = code_of(host.invite())
    monkeypatch.setattr(guests.JOIN_CODE, "limit", 3)
    for number in range(3):
        joined(code, f"Gast {number}", peer=f"198.51.100.{number + 1}")
    with browser(peer="198.51.100.99") as fourth:
        assert fourth.post(f"/api/v1/join/{code}", json={"name": "Noch einer"}).status_code == 429


def test_an_invitation_takes_at_most_so_many_guests(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    code = code_of(host.invite())
    monkeypatch.setattr(guests, "MAX_GUESTS", 2)
    joined(code, "A")
    joined(code, "B")
    with browser() as third:
        answer = third.post(f"/api/v1/join/{code}", json={"name": "C"})
    assert answer.status_code == 409 and answer.json()["detail"]["code"] == "invite_full"


def test_a_guest_changing_too_often_is_braked(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    phone = joined(code_of(host.invite()))
    monkeypatch.setattr(guests.ACT, "limit", 2)
    assert phone.post("/api/v1/guest/items", json={"tracks": [302]}).status_code == 200
    assert phone.post("/api/v1/guest/items", json={"tracks": [303]}).status_code == 200
    assert phone.post("/api/v1/guest/items", json={"tracks": [304]}).status_code == 429


def test_the_brake_counts_on_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(clock, "monotonic", lambda: now[0])
    brake = guests.Brake(2, 60)
    brake.take("k")
    brake.take("k")
    with pytest.raises(Exception) as caught:
        brake.take("k")
    assert caught.value.status_code == 429  # type: ignore[attr-defined]
    assert brake.wait("k") == 60
    now[0] += 59.5
    assert brake.wait("k") == 1
    now[0] += 0.5
    assert brake.wait("k") == 0
    brake.take("k")


# --- Live -----------------------------------------------------------------------------------------------------------


class Stream:
    def __init__(self, phone: TestClient, path: str = "/api/v1/guest/events") -> None:
        self.events: list[tuple[str, Any]] = []
        self.ready = threading.Event()
        self.status = 0
        self.thread = threading.Thread(target=self._read, args=(phone, path), daemon=True)
        self.thread.start()
        assert self.ready.wait(5)

    def _read(self, phone: TestClient, path: str) -> None:
        name = ""
        with phone.stream("GET", path) as answer:
            self.status = answer.status_code
            for line in answer.iter_lines():
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    self.events.append((name, json.loads(line[6:])))
                    if name == "hello":
                        self.ready.set()
        self.ready.set()

    def wait_for(self, name: str, seconds: float = 3.0) -> Any:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for event, data in self.events:
                if event == name:
                    return data
            time.sleep(0.02)
        raise AssertionError(f"no {name}; got {[event for event, _data in self.events]}")


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import playlists as playlist_router

    for module in (guest_router, playlist_router):
        monkeypatch.setattr(module, "STREAM_SECONDS", 3.0)
        monkeypatch.setattr(module, "PING_SECONDS", 0.3)


def test_a_guest_hears_of_changes_with_the_version_only_and_of_the_end(host: Host, quick: None) -> None:
    phone = joined(code_of(host.invite()))
    stream = Stream(phone)
    hello = stream.wait_for("hello")
    host.editor.post(f"{host.base}/items", json={"tracks": [302]})
    event = stream.wait_for("items")
    assert event == {"version": hello["version"] + 1}, "no names, no accounts"
    host.owner.put(host.base, json={"name": "Grillabend 2"})
    assert stream.wait_for("playlist") == {"version": hello["version"] + 2, "name": "Grillabend 2"}
    host.owner.delete(f"{host.base}/invite")
    stream.wait_for("gone")
    stream.thread.join(5)
    assert not stream.thread.is_alive()


def test_the_host_sees_a_guest_join_and_its_track_live(host: Host, quick: None) -> None:
    from .test_playlist_events import Listener

    code = code_of(host.invite())
    listener = Listener(host.owner, host.id)
    phone = joined(code)
    assert listener.wait_for("guests") == {"guests": 1}
    phone.post("/api/v1/guest/items", json={"tracks": [302]})
    event = listener.wait_for("items")
    assert event["op"] == "add" and event["by"] == {"id": None, "name": "Lena", "display_name": "Lena",
                                                    "avatar": None, "guest": True}
    assert event["entries"][0]["added_by_name"] == "Lena"


def test_a_guest_taken_out_hears_it_at_once(host: Host, quick: None) -> None:
    phone = joined(code_of(host.invite()))
    stream = Stream(phone)
    guest_id = host.owner.get(f"{host.base}/invite").json()["guests"][0]["id"]
    host.owner.delete(f"{host.base}/invite/guests/{guest_id}")
    stream.wait_for("gone")


def test_a_guest_holds_only_a_few_streams(host: Host, quick: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guests, "STREAMS", 1)
    phone = joined(code_of(host.invite()))
    first = Stream(phone)
    first.wait_for("hello")
    second = phone.get("/api/v1/guest/events")
    assert second.status_code == 429


def test_the_cards_count_the_guests_of_a_running_invitation(host: Host) -> None:
    card = next(one for one in host.owner.get("/api/v1/playlists").json()["playlists"] if one["id"] == host.id)
    assert card["guests"] is None
    code = code_of(host.invite())
    joined(code, "Lena")
    joined(code, "Tim")
    card = next(one for one in host.owner.get("/api/v1/playlists").json()["playlists"] if one["id"] == host.id)
    assert card["guests"] == 2
    host.owner.delete(f"{host.base}/invite")
    assert host.owner.get(host.base).json()["guests"] is None


def test_a_guest_leaves_by_itself(host: Host) -> None:
    code = code_of(host.invite())
    phone = joined(code)
    answer = phone.post("/api/v1/guest/leave")
    assert answer.status_code == 204 and guests.GUEST_COOKIE in answer.headers["set-cookie"]
    assert phone.get("/api/v1/guest").status_code == 401
    with SessionLocal() as db:
        assert db.scalars(select(Guest)).one().removed_at is not None


def test_the_host_sees_whether_listening_in_is_on_on_the_server(host: Host) -> None:
    """The dialog shows the switch of an invitation as off and fixed while the operator has listening in off."""
    host.invite()
    assert host.owner.get(f"{host.base}/invite").json()["listening"] is True
    assert host.operator.put("/api/v1/settings", json={"preview_allowed": False}).status_code == 200
    assert host.owner.get(f"{host.base}/invite").json()["listening"] is False
