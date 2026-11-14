"""Edges of the guests' way in that the other tests leave open.

* A guest of one invitation brings back no entry of another playlist (the undo names entries by number).
* One person with the code cannot keep the others out of the party from one address: joins are counted per invitation
  and sender too (``guests.JOIN_SENDER``), so a flood from one browser is braked before it uses up the invitation's
  room (``guests.JOIN_CODE``, ``MAX_GUESTS``). Many addresses with the code can still fill it; the host's way out is to
  end the invitation and make a new one."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.services import guests

from . import made_up_catalog
from .test_guests import Host, browser, code_of, joined


@pytest.fixture(autouse=True)
def catalog_in() -> None:
    made_up_catalog.install()


@pytest.fixture
def host() -> Iterator[Host]:
    found = Host()
    yield found
    found.close()


def test_a_guest_brings_back_no_entry_of_another_playlist(host: Host) -> None:
    phone = joined(code_of(host.invite(may_sort=True)))
    other = host.owner.post("/api/v1/playlists", json={"name": "Andere"}).json()
    other_base = f"/api/v1/playlists/{other['id']}"
    host.owner.post(f"{other_base}/items", json={"tracks": [320, 321]})
    theirs = [entry["id"] for entry in host.owner.get(other_base).json()["items"]]
    assert host.owner.post(f"{other_base}/items/remove", json={"entries": theirs}).status_code == 200
    answer = phone.post("/api/v1/guest/items/restore", json={"entries": theirs})
    assert answer.status_code == 200 and answer.json()["op"] == "none"
    assert host.owner.get(other_base).json()["items"] == []
    assert len(phone.get("/api/v1/guest").json()["playlist"]["items"]) == 3


def test_one_browser_with_the_code_does_not_keep_the_others_out(host: Host) -> None:
    code = code_of(host.invite())
    with browser(peer="198.51.100.7") as flood:
        for number in range(110):
            flood.cookies.clear()
            answer = flood.post(f"/api/v1/join/{code}", json={"name": f"Bot {number}"})
            if answer.status_code != 200:
                break
    assert answer.status_code == 429 and number == guests.JOIN_SENDER_LIMIT, "the flood from one address is braked"
    with browser(peer="203.0.113.20") as friend:
        answer = friend.post(f"/api/v1/join/{code}", json={"name": "Lena"})
        assert answer.status_code == 200, f"a friend at the party cannot join: {answer.status_code} {answer.text}"
