"""A guest listens in through the preview route (``guests.preview_listener`` in ``preview.may_listen``), against the
stand-in Plex: with "listening allowed" it hears, without it not; a guest of another invitation hears by its own
invitation only; a guest taken out or of an ended invitation not at all; the limits count per guest; the operator's
switch holds for guests as well; a browser signed in as an account listens as that account."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.main import app
from app.models import Account
from app.security import SESSION_COOKIE
from app.services import guests, preview

from .conftest import TAB, make_account, new_client
from .fakeplex import FakePlex
from .test_preview import TRACK, synced  # noqa: F401 - the fixture

OTHER = 3003


class Party:
    """``olivia`` and her playlist with a running invitation; phones join it."""

    def __init__(self, name: str = "Gartenfest", **rights: Any) -> None:
        self.owner_row = make_account(f"olivia{name.lower()}")
        self.owner = new_client(self.owner_row)
        made = self.owner.post("/api/v1/playlists", json={"name": name}).json()
        self.base = f"/api/v1/playlists/{made['id']}"
        assert self.owner.post(f"{self.base}/items", json={"tracks": [TRACK]}).status_code == 200
        assert self.owner.post(f"{self.base}/invite").status_code == 200
        answer = self.owner.put(f"{self.base}/invite", json={"preview": True, **rights, "start": True})
        assert answer.status_code == 200, answer.text
        self.code = answer.json()["invite"]["code"]
        self.phones: list[TestClient] = []

    def join(self, name: str = "Lena") -> TestClient:
        phone = TestClient(app, base_url="http://testserver", headers=TAB, client=(f"phone-{name}", 50000))
        assert phone.post(f"/api/v1/join/{self.code}", json={"name": name}).status_code == 200
        self.phones.append(phone)
        return phone

    def close(self) -> None:
        for one in (self.owner, *self.phones):
            one.close()


@pytest.fixture
def party(synced: FakePlex) -> Iterator[Party]:  # noqa: F811
    synced.sound_bytes = 2_000
    made = Party()
    yield made
    made.close()


def code_of(answer: Any) -> str:
    return str(answer.json()["detail"]["code"])


def test_a_guest_with_listening_allowed_hears_any_track_of_the_catalogue(party: Party, synced: FakePlex) -> None:  # noqa: F811
    phone = party.join()
    answer = phone.get(f"/api/v1/preview/{TRACK}")
    assert answer.status_code == 200 and len(answer.content) == 2_000
    assert phone.get(f"/api/v1/preview/{OTHER}").status_code == 200, "a track of the catalogue, not in the list"
    assert phone.get("/api/v1/preview/999999").status_code == 404, "not in the catalogue"
    assert len(synced.starts) == 2 and len(synced.stops) == 2
    status = phone.get("/api/v1/preview").json()
    assert status["allowed"] is True and status["left"] == preview.PER_HOUR - 2


def test_without_listening_allowed_a_guest_hears_nothing(party: Party, synced: FakePlex) -> None:  # noqa: F811
    phone = party.join()
    assert party.owner.put(f"{party.base}/invite", json={"preview": False}).status_code == 200
    answer = phone.get(f"/api/v1/preview/{TRACK}")
    assert answer.status_code == 403 and code_of(answer) == "preview_off"
    assert phone.get("/api/v1/preview").status_code == 403
    assert synced.starts == []
    assert party.owner.put(f"{party.base}/invite", json={"preview": True}).status_code == 200
    assert phone.get(f"/api/v1/preview/{TRACK}").status_code == 200, "on again, at once"


def test_a_guest_hears_by_its_own_invitation_only(party: Party, synced: FakePlex) -> None:  # noqa: F811
    quiet = Party("Kaffeetafel", preview=False)
    try:
        theirs = quiet.join("Tim")
        mine = party.join("Lena")
        refused = theirs.get(f"/api/v1/preview/{TRACK}")
        assert refused.status_code == 403 and code_of(refused) == "preview_off"
        assert mine.get(f"/api/v1/preview/{TRACK}").status_code == 200
        # The cookie of the guest of the quiet invitation does not open the other one's listening.
        with TestClient(app, base_url="http://testserver", headers=TAB) as borrowed:
            borrowed.cookies.set(guests.GUEST_COOKIE, theirs.cookies[guests.GUEST_COOKIE])
            assert borrowed.get(f"/api/v1/preview/{TRACK}").status_code == 403
        assert len(synced.starts) == 1
    finally:
        quiet.close()


def test_a_guest_taken_out_or_of_an_ended_invitation_hears_nothing(party: Party, synced: FakePlex) -> None:  # noqa: F811
    lena = party.join("Lena")
    tim = party.join("Tim")
    guest_id = next(one["id"] for one in party.owner.get(f"{party.base}/invite").json()["guests"]
                    if one["name"] == "Lena")
    assert party.owner.delete(f"{party.base}/invite/guests/{guest_id}").status_code == 204
    out = lena.get(f"/api/v1/preview/{TRACK}")
    assert out.status_code == 403 and code_of(out) == "guest_removed"
    assert tim.get(f"/api/v1/preview/{TRACK}").status_code == 200
    assert party.owner.delete(f"{party.base}/invite").status_code == 204
    ended = tim.get(f"/api/v1/preview/{TRACK}")
    assert ended.status_code == 403 and code_of(ended) == "invite_ended"
    with TestClient(app, base_url="http://testserver", headers=TAB) as stranger:
        stranger.cookies.set(guests.GUEST_COOKIE, "made-up")
        unknown = stranger.get(f"/api/v1/preview/{TRACK}")
        assert unknown.status_code == 401 and code_of(unknown) == "guest_required"
    assert len(synced.starts) == 1


def test_the_limits_count_per_guest(party: Party, synced: FakePlex, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    moment = [1000.0]
    monkeypatch.setattr(clock, "monotonic", lambda: moment[0])
    monkeypatch.setattr(preview, "PER_HOUR", 3)
    lena = party.join("Lena")
    tim = party.join("Tim")
    for _ in range(3):
        assert lena.get(f"/api/v1/preview/{TRACK}").status_code == 200
        moment[0] += 1
    refused = lena.get(f"/api/v1/preview/{TRACK}")
    assert refused.status_code == 429 and code_of(refused) == "preview_too_often"
    assert lena.get("/api/v1/preview").json()["left"] == 0
    assert tim.get("/api/v1/preview").json()["left"] == 3, "the other guest's hour is its own"
    assert tim.get(f"/api/v1/preview/{TRACK}").status_code == 200
    assert party.owner.get("/api/v1/preview").json()["left"] == 3, "and the host's"


def test_the_operators_switch_holds_for_guests(party: Party, synced: FakePlex, client: TestClient) -> None:  # noqa: F811
    phone = party.join()
    assert phone.get("/api/v1/guest").json()["invite"]["preview"] is True
    assert client.put("/api/v1/settings", json={"preview_allowed": False}).status_code == 200
    answer = phone.get(f"/api/v1/preview/{TRACK}")
    assert answer.status_code == 403 and code_of(answer) == "preview_off"
    assert phone.get("/api/v1/guest").json()["invite"]["preview"] is False, "the guest's page shows no ▶"
    assert synced.starts == []


def test_a_guest_with_a_stale_session_cookie_still_listens_as_the_guest(party: Party) -> None:
    phone = party.join()
    phone.cookies.set(SESSION_COOKIE, "ended-long-ago")
    assert phone.get(f"/api/v1/preview/{TRACK}").status_code == 200
    assert phone.get("/api/v1/preview").json()["left"] == preview.PER_HOUR - 1


def test_a_browser_signed_in_listens_as_its_account_even_with_a_guest_cookie(party: Party) -> None:
    phone = party.join()
    member: Account = make_account("mia")
    with new_client(member) as browser:
        browser.cookies.set(guests.GUEST_COOKIE, phone.cookies[guests.GUEST_COOKIE])
        assert browser.get(f"/api/v1/preview/{TRACK}").status_code == 200
        assert browser.get("/api/v1/preview").json()["left"] == preview.PER_HOUR - 1
    assert phone.get("/api/v1/preview").json()["left"] == preview.PER_HOUR, "the guest's hour is untouched"
    # And without listening in its invitation, the account still hears: it is no guest here.
    party.owner.put(f"{party.base}/invite", json={"preview": False})
    with new_client(member) as browser:
        browser.cookies.set(guests.GUEST_COOKIE, phone.cookies[guests.GUEST_COOKIE])
        assert browser.get(f"/api/v1/preview/{TRACK}").status_code == 200
