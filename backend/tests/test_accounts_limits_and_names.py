"""What one account can reach of others and how much it can make:

* A stream of events ends soon after the credential that opened it is taken away (signed out, the device revoked),
  not only when the account loses the playlist.
* An account holds at most ``MAX_PLAYLISTS`` playlists (each one is a playlist in Plex too), with a code the interface
  explains; the trash does not count.
* Others see a person by the display name: the name to sign in with stays out of what a member reads of a playlist
  shared with them, as long as the person has a display name."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update

from app.db import SessionLocal
from app.models import Account, AuthSession
from app.routers import playlists as router
from app.services import playlists

from . import made_up_catalog
from .conftest import TAB, make_account, new_client


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    made_up_catalog.install()
    monkeypatch.setattr(router, "STREAM_SECONDS", 20.0)
    monkeypatch.setattr(router, "PING_SECONDS", 0.3)


def _listen(browser: TestClient, playlist_id: int) -> tuple[list[str], threading.Event, threading.Thread]:
    events: list[str] = []
    ready, ended = threading.Event(), threading.Event()

    def read() -> None:
        with browser.stream("GET", f"/api/v1/playlists/{playlist_id}/events") as answer:
            for line in answer.iter_lines():
                if line.startswith("event: "):
                    events.append(line[7:])
                    if line == "event: hello":
                        ready.set()
        ended.set()
        ready.set()

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    assert ready.wait(5), "the stream did not open"
    return events, ended, thread


def test_a_stream_ends_when_its_session_is_signed_out() -> None:
    owner_row = make_account("olivia")
    with new_client(owner_row) as owner:
        playlist = owner.post("/api/v1/playlists", json={"name": "Party"}).json()
        events, ended, _thread = _listen(owner, playlist["id"])
        with SessionLocal() as db:
            db.execute(delete(AuthSession).where(AuthSession.account_id == owner_row.id))
            db.commit()
        assert ended.wait(5), "the stream of a session that ended went on"
        assert events[-1] == "gone"


def test_a_stream_ends_when_its_device_is_revoked() -> None:
    owner_row = make_account("olivia")
    with new_client(owner_row) as owner:
        playlist = owner.post("/api/v1/playlists", json={"name": "Party"}).json()
        made = owner.post("/api/v1/devices", json={"name": "Phone"})
        assert made.status_code in (200, 201), made.text
        token = made.json()["token"]
        from app.main import app

        with TestClient(app, base_url="http://testserver", headers={**TAB, "Authorization": f"Bearer {token}"}) as phone:
            events, ended, _thread = _listen(phone, playlist["id"])
            device_id = next(row["id"] for row in owner.get("/api/v1/devices").json() if row["name"] == "Phone")
            assert owner.delete(f"/api/v1/devices/{device_id}").status_code == 204
            assert ended.wait(5), "the stream of a revoked device went on"
            assert events[-1] == "gone"


def test_a_stream_goes_on_while_its_credential_holds() -> None:
    owner_row = make_account("olivia")
    with new_client(owner_row) as owner:
        playlist = owner.post("/api/v1/playlists", json={"name": "Party"}).json()
        events, ended, _thread = _listen(owner, playlist["id"])
        time.sleep(1.2)
        assert not ended.is_set()
        assert "gone" not in events


def test_an_account_holds_at_most_so_many_playlists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playlists, "MAX_PLAYLISTS", 3)
    with new_client(make_account("olivia")) as owner, new_client(make_account("edith")) as other:
        made = [owner.post("/api/v1/playlists", json={"name": f"Liste {number}"}) for number in range(3)]
        assert [answer.status_code for answer in made] == [201, 201, 201]
        refused = owner.post("/api/v1/playlists", json={"name": "Eine zu viel"})
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "too_many_playlists"
        assert refused.json()["detail"]["max"] == 3
        assert len(owner.get("/api/v1/playlists").json()["playlists"]) == 3
        # Another account has its own room; the trash makes room again.
        assert other.post("/api/v1/playlists", json={"name": "Meine"}).status_code == 201
        assert owner.delete(f"/api/v1/playlists/{made[0].json()['id']}").status_code == 200
        assert owner.post("/api/v1/playlists", json={"name": "Wieder Platz"}).status_code == 201


def _words(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def test_a_member_sees_others_by_their_display_name_only() -> None:
    owner_row, member_row, third_row = make_account("olivia.k"), make_account("edith.w"), make_account("rafael.m")
    with SessionLocal() as db:
        db.execute(update(Account).where(Account.id == owner_row.id).values(display_name="Olivia"))
        db.execute(update(Account).where(Account.id == member_row.id).values(display_name="Edith"))
        db.execute(update(Account).where(Account.id == third_row.id).values(display_name="Rafa"))
        db.commit()
    with new_client(owner_row) as owner, new_client(member_row) as member:
        playlist = owner.post("/api/v1/playlists", json={"name": "Grillabend"}).json()
        base = f"/api/v1/playlists/{playlist['id']}"
        assert owner.put(f"{base}/shares/{member_row.id}", json={"access": "edit"}).status_code == 200
        assert owner.put(f"{base}/shares/{third_row.id}", json={"access": "read"}).status_code == 200
        assert member.post(f"{base}/items", json={"tracks": [301]}).status_code == 200
        seen = [member.get(base).json(), member.get("/api/v1/playlists").json()]
        shares = member.get(f"{base}/shares")
        if shares.status_code == 200:
            seen.append(shares.json())
        words = _words(seen)
        for login in ("olivia.k", "edith.w", "rafael.m"):
            assert login not in words, login
        assert "Olivia" in words


def test_a_stream_of_events_is_no_slow_request(monkeypatch: pytest.MonkeyPatch,
                                               caplog: pytest.LogCaptureFixture) -> None:
    """A stream stays open as long as its page: its end is no "slow request" in the log. Other routes still are."""
    import logging

    from app import middleware

    monkeypatch.setattr(middleware, "SLOW_MS", 0)
    monkeypatch.setattr(router, "STREAM_SECONDS", 0.5)
    caplog.set_level(logging.DEBUG, logger="nexmix.api")
    with new_client(make_account("olivia")) as owner:
        playlist = owner.post("/api/v1/playlists", json={"name": "Party"}).json()
        _events, ended, _thread = _listen(owner, playlist["id"])
        assert ended.wait(5)
        owner.get(f"/api/v1/playlists/{playlist['id']}")
    slow = [record.getMessage() for record in caplog.records if "Slow request" in record.getMessage()]
    assert not [line for line in slow if line.split(" -> ")[0].endswith("/events")], slow
    assert [line for line in slow if f"/api/v1/playlists/{playlist['id']} " in line + " "], slow
