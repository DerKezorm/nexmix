"""Guests, the harder cases: a party behind one address and a stranger guessing there too, a proxy nexmix does not
trust, an inviter who loses the right to change the playlist, an invitation filled and emptied again, the cookie when
the invitation lasts longer, the names guests see of the household, names that look alike or show nothing, bringing
back without the right to add, the code in the log and its hash."""

from __future__ import annotations

import hashlib
import hmac
import threading
from collections.abc import Callable, Iterator

import pytest
from fastapi import Response
from sqlalchemy import delete, select

from app import clock
from app.config import get_settings
from app.db import SessionLocal
from app.models import OPERATOR, Account, Guest, GuestInvite, PlaylistShare
from app.routers import catalog as catalog_routes
from app.security import server_key
from app.services import guests, logs

from . import made_up_catalog
from .test_guests import Host, browser, code_of, joined, walk


@pytest.fixture(autouse=True)
def catalog_in() -> None:
    made_up_catalog.install()


@pytest.fixture
def host() -> Iterator[Host]:
    found = Host()
    yield found
    found.close()


def wrong_code(number: int) -> str:
    """A well-formed code that names no invitation (the invitation's own code is random; these count up)."""
    digits = []
    for _ in range(guests.CODE_LENGTH):
        number, rest = divmod(number, len(guests.ALPHABET))
        digits.append(guests.ALPHABET[rest])
    return "".join(digits)


# --- Brakes: a party behind one address, a stranger among them ------------------------------------------------------


@pytest.fixture
def no_covers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_cover(*_args: object) -> Response:
        return Response(status_code=204)

    monkeypatch.setattr(catalog_routes, "serve_cover", no_cover)


def test_fifty_guests_behind_one_address_join_one_after_another(host: Host, no_covers: None) -> None:
    code = code_of(host.invite())
    for number in range(50):
        with browser(peer="198.51.100.50") as phone:
            door = phone.get(f"/api/v1/join/{code}")
            assert door.status_code == 200, number
            for album in door.json()["playlist"]["covers"][:4]:
                assert phone.get(f"/api/v1/join/{code}/cover/{album}").status_code == 204, number
            assert phone.post(f"/api/v1/join/{code}", json={"name": f"Gast {number}"}).status_code == 200, number
            assert phone.get(f"/api/v1/join/{code}").json()["joined"] is True
    assert len(host.owner.get(f"{host.base}/invite").json()["guests"]) == 50


def test_a_stranger_with_a_thousand_wrong_codes_behind_the_same_address_locks_no_guest_out(host: Host) -> None:
    code = code_of(host.invite())
    with browser(peer="198.51.100.50") as stranger:
        answers = [stranger.get(f"/api/v1/join/{wrong_code(number)}").status_code for number in range(1000)]
    assert answers[:guests.MISS_LIMIT] == [404] * guests.MISS_LIMIT
    assert set(answers[guests.MISS_LIMIT:]) == {429}, "guessing stays braked"
    with browser(peer="198.51.100.50") as guest:
        assert guest.get(f"/api/v1/join/{code}").status_code == 200
        assert guest.post(f"/api/v1/join/{code}", json={"name": "Lena"}).status_code == 200
        assert guest.get("/api/v1/guest").status_code == 200


def test_guessing_is_braked_per_address_and_the_wait_is_told(host: Host) -> None:
    with browser(peer="198.51.100.7") as guesser:
        for number in range(guests.MISS_LIMIT):
            assert guesser.get(f"/api/v1/join/{wrong_code(number)}").status_code == 404
        braked = guesser.get(f"/api/v1/join/{wrong_code(999)}")
        assert braked.status_code == 429 and 0 < int(braked.headers["retry-after"]) <= 600
        assert guesser.post(f"/api/v1/join/{wrong_code(998)}", json={"name": "Mia"}).status_code == 429
        assert guesser.get("/api/v1/join/AAAAAAAAAAAA/cover/201").status_code == 429
    with browser(peer="198.51.100.8") as neighbour:
        assert neighbour.get(f"/api/v1/join/{wrong_code(997)}").status_code == 404, "another address is not braked"


def test_behind_a_proxy_nexmix_does_not_trust_wrong_codes_count_per_sender_and_per_proxy(
        host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    code = code_of(host.invite())
    monkeypatch.setattr(get_settings(), "trusted_proxies", "")
    monkeypatch.setattr(guests, "MISS_PROXY_LIMIT", 2 * guests.MISS_LIMIT + 5)
    with browser(peer="10.0.0.2", **{"X-Forwarded-For": "203.0.113.66"}) as stranger:
        for number in range(guests.MISS_LIMIT):
            assert stranger.get(f"/api/v1/join/{wrong_code(number)}").status_code == 404
        assert stranger.get(f"/api/v1/join/{wrong_code(500)}").status_code == 429
    with browser(peer="10.0.0.2", **{"X-Forwarded-For": "198.51.100.10"}) as other:
        assert other.get(f"/api/v1/join/{wrong_code(501)}").status_code == 404, "another sender behind the proxy"
        assert other.get(f"/api/v1/join/{code}").status_code == 200
        assert other.post(f"/api/v1/join/{code}", json={"name": "Lena"}).status_code == 200
    # Made-up senders buy no more than the proxy's own room.
    for number in range(guests.MISS_LIMIT + 4):
        with browser(peer="10.0.0.2", **{"X-Forwarded-For": f"192.0.2.{number}"}) as made_up:
            made_up.get(f"/api/v1/join/{wrong_code(600 + number)}")
    with browser(peer="10.0.0.2", **{"X-Forwarded-For": "198.51.100.11"}) as late:
        assert late.get(f"/api/v1/join/{wrong_code(700)}").status_code == 429, "the proxy as a whole rests"
        assert late.get(f"/api/v1/join/{code}").status_code == 200, "the right code still leads in"


def test_the_covers_of_the_page_of_the_link_do_not_count_against_joining(host: Host, no_covers: None,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    code = code_of(host.invite())
    monkeypatch.setattr(guests.JOIN_CODE, "limit", 2)
    monkeypatch.setattr(guests.DOOR, "limit", 3)
    with browser() as phone:
        covers = phone.get(f"/api/v1/join/{code}").json()["playlist"]["covers"]
        for _ in range(20):
            assert phone.get(f"/api/v1/join/{code}/cover/{covers[0]}").status_code == 204
    joined(code, "Lena", peer="198.51.100.1")
    joined(code, "Tim", peer="198.51.100.2")
    with browser(peer="198.51.100.3") as third:
        assert third.post(f"/api/v1/join/{code}", json={"name": "Mia"}).status_code == 429, "joins per code"


# --- The inviter loses the right ------------------------------------------------------------------------------------


def _share_gone(host: Host) -> None:
    with SessionLocal() as db:
        db.execute(delete(PlaylistShare).where(PlaylistShare.account_id == host.editor_row.id))
        db.commit()


def _share_read(host: Host) -> None:
    with SessionLocal() as db:
        share = db.get(PlaylistShare, (host.id, host.editor_row.id))
        assert share is not None
        share.access = "read"
        db.commit()


def _blocked(host: Host) -> None:
    with SessionLocal() as db:
        row = db.get(Account, host.editor_row.id)
        assert row is not None
        row.blocked_at = clock.now()
        db.commit()


def _deleted(host: Host) -> None:
    with SessionLocal() as db:
        row = db.get(Account, host.editor_row.id)
        db.delete(row)
        db.commit()


LOSSES: dict[str, Callable[[Host], None]] = {"share_taken_back": _share_gone, "share_set_to_read": _share_read,
                                              "blocked": _blocked, "deleted": _deleted}


@pytest.mark.parametrize("loss", list(LOSSES))
def test_the_guests_are_out_at_once_when_the_inviter_loses_the_right(host: Host, loss: str) -> None:
    """Checked at every request of a guest: here the database changes behind the server's back."""
    assert host.editor.post(f"{host.base}/invite").status_code == 200
    code = code_of(host.editor.put(f"{host.base}/invite", json={"start": True}).json())
    phone = joined(code)
    assert phone.get("/api/v1/guest").status_code == 200
    LOSSES[loss](host)
    for method, path, body in (("GET", "/api/v1/guest", None), ("POST", "/api/v1/guest/items", {"tracks": [302]}),
                               ("GET", "/api/v1/guest/catalog", None)):
        answer = phone.request(method, path, json=body)
        assert answer.status_code == 403 and answer.json()["detail"]["code"] == "invite_ended", (loss, path)
    with browser() as late:
        assert late.get(f"/api/v1/join/{code}").status_code == 404
    # The owner sees no invitation any more and may make a new one.
    assert host.owner.get(f"{host.base}/invite").json()["invite"] is None
    assert code_of(host.owner.post(f"{host.base}/invite").json()) != code


def test_through_the_interface_taking_the_share_back_ends_the_invitations_of_that_person_only(host: Host) -> None:
    assert host.editor.post(f"{host.base}/invite").status_code == 200
    phone = joined(code_of(host.editor.put(f"{host.base}/invite", json={"start": True}).json()))
    other = host.owner.post("/api/v1/playlists", json={"name": "Andere Party"}).json()
    other_base = f"/api/v1/playlists/{other['id']}"
    host.owner.post(f"{other_base}/invite")
    owners_guest = joined(code_of(host.owner.put(f"{other_base}/invite", json={"start": True}).json()), "Tim")
    assert host.owner.delete(f"{host.base}/shares/{host.editor_row.id}").status_code == 200
    assert phone.get("/api/v1/guest").json()["detail"]["code"] == "invite_ended"
    with SessionLocal() as db:
        ended = db.scalars(select(GuestInvite).where(GuestInvite.playlist_id == host.id)).one()
        assert ended.ended_at is not None, "ended in the database too, not only refused"
    assert owners_guest.get("/api/v1/guest").status_code == 200, "the owner's own invitation goes on"


def test_an_operator_made_member_takes_the_invitations_it_had_only_as_operator(host: Host) -> None:
    assert host.operator.post(f"{host.base}/invite").status_code == 200
    phone = joined(code_of(host.operator.put(f"{host.base}/invite", json={"start": True}).json()))
    with SessionLocal() as db:
        row = db.get(Account, host.operator_row.id)
        assert row is not None and row.role == OPERATOR
        row.role = "member"
        db.commit()
    assert phone.get("/api/v1/guest").json()["detail"]["code"] == "invite_ended"


# --- Full, emptied, full again --------------------------------------------------------------------------------------


def test_guests_taken_out_do_not_count_against_the_most_an_invitation_takes(host: Host,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    code = code_of(host.invite())
    monkeypatch.setattr(guests, "MAX_GUESTS", 5)
    for number in range(5):
        joined(code, f"Bot {number}", peer=f"198.51.100.{number + 1}")
    with browser(peer="198.51.100.100") as early:
        assert early.post(f"/api/v1/join/{code}", json={"name": "Zu früh"}).json()["detail"]["code"] == "invite_full"
    for guest in host.owner.get(f"{host.base}/invite").json()["guests"]:
        assert host.owner.delete(f"{host.base}/invite/guests/{guest['id']}").status_code == 204
    with browser(peer="198.51.100.200") as real:
        answer = real.post(f"/api/v1/join/{code}", json={"name": "Echter Gast"})
        assert answer.status_code == 200, answer.text
        assert real.get("/api/v1/guest").status_code == 200


def test_ten_joins_at_once_never_pass_the_most_an_invitation_takes(host: Host,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    code = code_of(host.invite())
    monkeypatch.setattr(guests, "MAX_GUESTS", 3)
    start = threading.Barrier(10)
    answers: list[int] = []

    def work(number: int) -> None:
        with browser(peer=f"198.51.100.{number + 1}") as phone:
            start.wait()
            answers.append(phone.post(f"/api/v1/join/{code}", json={"name": f"Gast {number}"}).status_code)

    threads = [threading.Thread(target=work, args=(number,)) for number in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(answers) == [200] * 3 + [409] * 7
    with SessionLocal() as db:
        assert len(db.scalars(select(Guest)).all()) == 3


# --- The cookie lasts as long as the invitation ---------------------------------------------------------------------


def _max_age(line: str) -> int:
    return int(line.lower().split("max-age=")[1].split(";")[0])


def test_more_hours_for_the_invitation_make_the_cookies_of_its_guests_last_longer(host: Host) -> None:
    code = code_of(host.invite(hours=1))
    with browser() as phone:
        first = phone.post(f"/api/v1/join/{code}", json={"name": "Lena"})
        assert _max_age(first.headers["set-cookie"]) <= 3600 + 120
        token = phone.cookies[guests.GUEST_COOKIE]
        assert host.owner.put(f"{host.base}/invite", json={"hours": 48}).status_code == 200
        again = phone.get("/api/v1/guest")
        assert again.status_code == 200
        line = again.headers["set-cookie"]
        assert line.lower().startswith(f"{guests.GUEST_COOKIE}={token}".lower())
        assert 47 * 3600 < _max_age(line) <= 48 * 3600 + 120
        assert "httponly" in line.lower() and "samesite=lax" in line.lower() and "path=/api/v1" in line.lower()


# --- Names of the household -----------------------------------------------------------------------------------------


def test_a_guest_sees_people_of_the_household_only_by_their_display_name(host: Host) -> None:
    """The name to sign in with stays with the household; without a display name the page says "someone"."""
    host.editor.post(f"{host.base}/items", json={"tracks": [320]})
    code = code_of(host.invite())
    with browser() as phone:
        door = phone.get(f"/api/v1/join/{code}").json()
        assert door["inviter"] == ""
        phone.post(f"/api/v1/join/{code}", json={"name": "Lena"})
        phone.post("/api/v1/guest/items", json={"tracks": [302]})
        seen = phone.get("/api/v1/guest").json()
    assert seen["playlist"]["inviter"] == ""
    assert [entry["by"] for entry in seen["playlist"]["items"]] == ["", "", "", "", "Lena"]
    words = " ".join(walk(seen)) + " " + " ".join(walk(door))
    for secret in ("olivia", "edith"):
        assert secret not in words, secret
    with SessionLocal() as db:
        for row_id, shown in ((host.owner_row.id, "Olivia K."), (host.editor_row.id, "Edith")):
            row = db.get(Account, row_id)
            assert row is not None
            row.display_name = shown
        db.commit()
    with browser() as phone:
        assert phone.get(f"/api/v1/join/{code}").json()["inviter"] == "Olivia K."
        phone.post(f"/api/v1/join/{code}", json={"name": "Tim"})
        seen = phone.get("/api/v1/guest").json()
    assert [entry["by"] for entry in seen["playlist"]["items"]] == ["Olivia K."] * 3 + ["Edith", "Lena"]


def test_the_entries_of_a_deleted_account_do_not_show_its_name_to_sign_in_with(host: Host) -> None:
    host.editor.post(f"{host.base}/items", json={"tracks": [320]})
    phone = joined(code_of(host.invite()))
    with SessionLocal() as db:
        db.delete(db.get(Account, host.editor_row.id))
        db.commit()
    seen = phone.get("/api/v1/guest").json()
    assert seen["playlist"]["items"][-1]["by"] == ""
    assert "edith" not in " ".join(walk(seen))
    # The household still sees the name kept on the entry.
    assert host.owner.get(host.base).json()["items"][-1]["added_by_name"] == "edith"


# --- Names that look alike or show nothing ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "Аnna",  # a Cyrillic A in a Latin name
    "Annα",  # a Greek alpha
    "ㅤ",  # Hangul filler, looks like nothing
    "⠀",  # Braille blank
    "ᅟᅠ",
    "ﾠ",
    "́́",  # only marks
    "a" + "́" * 23,  # marks stacked high
    "Lena",  # private use
    "Le⁠na",  # word joiner
    "Lena︎",  # a variation selector after a letter
    "‍Lena",
])
def test_names_that_look_like_others_or_show_nothing_are_refused(host: Host, name: str) -> None:
    code = code_of(host.invite())
    with browser() as phone:
        answer = phone.post(f"/api/v1/join/{code}", json={"name": name})
    assert answer.status_code == 422 and answer.json()["detail"]["code"] == "bad_name", ascii(name)


@pytest.mark.parametrize("name, kept", [
    ("\U0001F468‍\U0001F469‍\U0001F467", "\U0001F468‍\U0001F469‍\U0001F467"),
    ("\U0001F3F3️‍\U0001F308", "\U0001F3F3️‍\U0001F308"),
    ("\U0001F44D\U0001F3FD Tim", "\U0001F44D\U0001F3FD Tim"),
    ("José", "José"),
    ("Ａnna", "Anna"),
    ("Ζωή", "Ζωή"),
    ("Мария", "Мария"),
    ("Nguyễn", "Nguyễn"),
    ("李 Lena", "李 Lena"),
])
def test_names_are_kept_composed_and_emoji_with_joiners_are_welcome(name: str, kept: str) -> None:
    assert guests.clean_name(name) == kept, ascii(name)


def test_the_same_name_written_otherwise_is_the_same_name(host: Host) -> None:
    code = code_of(host.invite())
    names = [joined(code, name, peer=f"198.51.100.{number + 1}").get("/api/v1/guest").json()["me"]["name"]
             for number, name in enumerate(("José", "José", "Ａnna", "Anna", "ANNA"))]
    assert names == ["José", "José 2", "Anna", "Anna 2", "ANNA 3"]


def test_ten_guests_of_the_same_name_at_once_get_ten_names(host: Host) -> None:
    code = code_of(host.invite())
    start = threading.Barrier(10)
    names: list[str] = []
    lock = threading.Lock()

    def work(number: int) -> None:
        with browser(peer=f"198.51.100.{number + 1}") as phone:
            start.wait()
            answer = phone.post(f"/api/v1/join/{code}", json={"name": "Lena"})
            with lock:
                names.append(answer.json().get("name", str(answer.status_code)))

    threads = [threading.Thread(target=work, args=(number,)) for number in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(names) == sorted(["Lena"] + [f"Lena {number}" for number in range(2, 11)])


# --- Bringing back needs the right to add ---------------------------------------------------------------------------


def test_bringing_back_a_track_needs_the_right_to_add(host: Host) -> None:
    phone = joined(code_of(host.invite(may_add=False, may_sort=True)))
    entry = host.owner.get(host.base).json()["items"][0]["id"]
    host.owner.post(f"{host.base}/items/remove", json={"entries": [entry]})
    refused = phone.post("/api/v1/guest/items/restore", json={"entries": [entry]})
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "guest_no_adding"
    assert entry not in [item["id"] for item in host.owner.get(host.base).json()["items"]]
    host.owner.put(f"{host.base}/invite", json={"may_add": True})
    assert phone.post("/api/v1/guest/items/restore", json={"entries": [entry]}).json()["op"] == "restore"


# --- The code in the log, the code in the database ------------------------------------------------------------------


@pytest.mark.parametrize("line, masked", [
    ("GET /api/v1/join/ABCDE%20FGHJK -> 500", "GET /api/v1/join/… -> 500"),
    ("GET /api/v1/join/ABCDE+FGHJK/cover/201 -> 500", "GET /api/v1/join/…/cover/201 -> 500"),
    ("POST /api/v1/join/abcde%2Dfghjk -> 429", "POST /api/v1/join/… -> 429"),
    ("GET /j/ABC%20DE%20FGH%20JK?x=1", "GET /j/…?x=1"),
    ("GET /api/v1/join/ABCDE%20FGHJK", "GET /api/v1/join/…"),
])
def test_the_code_in_any_spelling_never_stands_in_the_log(line: str, masked: str) -> None:
    assert logs.redact(line) == masked


def test_the_code_is_kept_as_a_keyed_hash(host: Host) -> None:
    code = code_of(host.invite()).replace("-", "")
    with SessionLocal() as db:
        stored = db.scalars(select(GuestInvite)).one().code_hash
    assert stored != hashlib.sha256(code.encode()).hexdigest(), "a plain hash is open to anybody with a copy"
    assert stored == hmac.new(server_key(), b"guest-code:" + code.encode(), hashlib.sha256).hexdigest()
    assert guests.digest(code.lower()) == stored
